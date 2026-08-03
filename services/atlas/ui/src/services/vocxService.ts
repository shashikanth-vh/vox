import { vocx, vocxError } from '../api/vocxClient';
import { USE_REAL_API } from '../api/http';

/**
 * VocX — field intel capture, as ATLAS speaks to it.
 *
 * Every shape here is the one the service actually answers with; they were taken from
 * the VocX route table (`services/vocx/app/vocx/core/server.py`) and from the console
 * that has been driving those endpoints since before this screen existed, rather than
 * being invented to fit the UI. A capture is a two-step act on purpose:
 *
 *   record / type  ->  POST /v1/capture_audio | /v1/capture   (PREVIEW — writes nothing)
 *   review + edit  ->  POST /v1/commit                        (files it, for real)
 *
 * Nothing reaches the register until the second step, which is why an unapproved
 * recording can sit in the report list for as long as it needs to.
 */

export interface VocxCapabilities {
  [k: string]: any;
}

/** One row of the RM's report list — the service's own list projection. */
export interface VocxReportRow {
  capture_id: string;
  rm?: string;
  /** draft | ready | committed — the vocabulary the store writes. */
  status?: string;
  updated_at?: string;
  company?: string;
  entity_code?: string;
  needs_approval?: boolean;
  summary?: string;
}

/** A preview (or a stored report) — the extraction plus what the gate decided. */
export interface VocxPreview {
  extraction?: any;
  decision?: any;
  summary?: string;
  writes?: any;
  [k: string]: any;
}

export type Result<T> = { ok: true; data: T } | { ok: false; error: string };

const ok = <T,>(data: T): Result<T> => ({ ok: true, data });
const fail = (error: string): Result<never> => ({ ok: false, error });

/** Capture id of a preview, wherever the service put it. */
export function captureIdOf(p: VocxPreview | null | undefined): string {
  return String(p?.extraction?._meta?.capture_id || p?.capture_id || '');
}

/** Best-effort GPS. Never blocks a capture — a refusal just means no location. */
export function currentPosition(timeoutMs = 8000): Promise<Record<string, string>> {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve({});
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({
        gps_lat: String(p.coords.latitude.toFixed(6)),
        gps_lng: String(p.coords.longitude.toFixed(6)),
      }),
      () => resolve({}),
      { timeout: timeoutMs, maximumAge: 60_000 },
    );
  });
}

export const vocxService = {
  enabled: () => USE_REAL_API,

  /** What this deployment can do right now (STT backend, calendar, report store). */
  async capabilities(): Promise<Result<VocxCapabilities>> {
    try { return ok(await vocx.get<VocxCapabilities>('/v1/capabilities')); }
    catch (e) { return fail(vocxError(e, 'read what VocX can do')); }
  },

  /** A recorded clip -> archive + transcription + a structured PREVIEW. Writes nothing. */
  async captureAudio(blob: Blob, rm: string, extra: Record<string, string> = {},
                     captureId?: string): Promise<Result<VocxPreview>> {
    try {
      const params: Record<string, string> = { rm, ts: new Date().toISOString(), ...extra };
      if (captureId) params.capture_id = captureId;
      return ok(await vocx.postAudio<VocxPreview>('/v1/capture_audio', blob, params));
    } catch (e) { return fail(vocxError(e, 'transcribe that recording')); }
  },

  /**
   * A typed transcript -> the same structured preview. Also the RE-ANALYSE path: pass
   * the existing capture_id with an edited transcript and VocX rebuilds the report
   * against the same record rather than starting a second one.
   */
  async captureTyped(transcript: string, rm: string, extra: Record<string, string> = {},
                     captureId?: string): Promise<Result<VocxPreview>> {
    try {
      return ok(await vocx.post<VocxPreview>('/v1/capture', {
        rm, transcript, ...extra, ...(captureId ? { capture_id: captureId } : {}),
      }));
    } catch (e) { return fail(vocxError(e, 'analyse that note')); }
  },

  /**
   * APPROVE — the only call that writes. `edits` carries what the reviewer changed,
   * `chosen_code` / `new_lead` settle which company it belongs to, and `log_to` pins
   * the interaction to a specific product line when the RM picked one.
   */
  async commit(payload: {
    rm: string; extraction: any; summary?: string; edits?: any;
    chosen_code?: string; new_lead?: boolean; company?: string;
    capture_id?: string; log_to?: { subject_type: string; subject_id: string };
  }): Promise<Result<VocxPreview>> {
    try { return ok(await vocx.post<VocxPreview>('/v1/commit', payload)); }
    catch (e) { return fail(vocxError(e, 'file that capture')); }
  },

  /** The RM's reports, newest first: drafts, ready-for-approval, and committed. */
  async reports(rm: string): Promise<Result<VocxReportRow[]>> {
    try {
      const r = await vocx.get<any>('/v1/reports', { rm });
      return ok((r?.reports || []) as VocxReportRow[]);
    } catch (e) { return fail(vocxError(e, 'list your reports')); }
  },

  /** One stored report, in full. */
  async report(rm: string, captureId: string): Promise<Result<VocxPreview>> {
    try {
      const r = await vocx.get<any>('/v1/reports/get', { rm, id: captureId });
      return ok((r?.report || r) as VocxPreview);
    } catch (e) { return fail(vocxError(e, 'open that report')); }
  },

  /** Keep an edited draft without filing it. */
  async saveDraft(rm: string, captureId: string, report: VocxPreview,
                  status = 'ready'): Promise<Result<true>> {
    try {
      await vocx.post('/v1/reports/save', { rm, capture_id: captureId, report, status });
      return ok(true as const);
    } catch (e) { return fail(vocxError(e, 'save that draft')); }
  },

  async remove(rm: string, captureId: string): Promise<Result<true>> {
    try {
      await vocx.post('/v1/reports/delete', { rm, capture_id: captureId });
      return ok(true as const);
    } catch (e) { return fail(vocxError(e, 'delete that report')); }
  },

  /** Company typeahead — the same scorer a commit resolves with, so what you pick is
   *  what gets linked. */
  async suggest(q: string, rm: string): Promise<Result<any>> {
    try { return ok(await vocx.get<any>('/v1/suggest', { q, rm, limit: 8 })); }
    catch (e) { return fail(vocxError(e, 'look that company up')); }
  },

  /** Playback for an archived recording. The object URL is the caller's to revoke. */
  async audioUrl(ref: string): Promise<Result<string>> {
    try {
      const blob = await vocx.blob('/v1/audio', { ref });
      return ok(URL.createObjectURL(blob));
    } catch (e) { return fail(vocxError(e, 'play that recording')); }
  },
};
