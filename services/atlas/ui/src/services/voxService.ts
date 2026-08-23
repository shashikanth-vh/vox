/**
 * VOX (the spec build) — the panel's transport to the conversation store and the
 * pipeline.
 *
 * Two backends, deliberately split: the REGISTER owns the conversation rows (list,
 * read, the atomic edit path, approve) and is reached through the same `/v1` client
 * as every grid; the VOCX SERVICE owns capture and processing (audio in, pipeline
 * kicked) and is reached through the `/vocx` client with its long capture timeout.
 * The registry (the schema contract's field blocks) comes from the service too, so
 * the renderer draws whatever the registry defines — zero hard-coded blocks.
 */

import { api } from '../api/http';
import vocxClient, { vocxError } from '../api/vocxClient';

export type Confidence = 'high' | 'medium' | 'low' | 'n/a';
export interface VoxCell { value: any; confidence: Confidence; user_override?: boolean }
export type VoxReport = Record<string, any> & {
  detected_use_cases?: string[];
  common?: Record<string, VoxCell>;
  subsector_details?: Record<string, VoxCell>;
  entity_candidates?: string[];
};

export interface VoxConversation {
  id: string;
  recorder_email: string;
  recorder_name?: string | null;
  entity_id?: string | null;
  lead_id?: string | null;
  deal_id?: string | null;
  recording_mode: 'post_meeting' | 'live';
  status: 'queued' | 'uploading' | 'processing' | 'ready' | 'submitted'
        | 'processing_failed' | 'failed_permanently';
  processing_stage?: string | null;
  processing_error?: string | null;
  retry_count?: number;
  duration_seconds?: number | null;
  latitude?: number | null;
  longitude?: number | null;
  /** Rows in the append-only audit trail — served on the single-GET only. */
  edits_count?: number;
  sector?: string | null;
  subsector?: string | null;
  meeting_date?: string | null;
  language_detected?: string | null;
  prompt_version?: string | null;
  registry_version?: string | null;
  use_cases?: string[];
  snippet?: string | null;
  entity_candidates?: string[] | null;
  /** "Create new lead" intent — held on the row, materialised by approve. */
  proposed_lead_company?: string | null;
  proposed_lead_rm?: string | null;
  raw_transcript?: string | null;
  /** The reviewer's corrected copy — what regeneration structures. The verbatim
   *  raw_transcript is evidence and never changes. */
  corrected_transcript?: string | null;
  structured_report?: VoxReport | null;
  erased_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface VoxRegistry {
  registry_version: string;
  use_cases: string[];
  common: any[];
  blocks: Record<string, { label: string; ui_note?: string; fields: any[] }>;
  taxonomy: Record<string, string[]>;
  subsector_canonicals: Record<string, any[]>;
}

let specCache: { registry: VoxRegistry; prompt_version: string } | null = null;

export const voxService = {
  /** The registry the renderer is driven by — fetched once per session. */
  async spec(): Promise<{ registry: VoxRegistry; prompt_version: string }> {
    if (specCache) return specCache;
    const r = await vocxClient.get('/v1/spec');
    specCache = { registry: r.data.registry, prompt_version: r.data.prompt_version };
    return specCache;
  },

  /** One POST from the panel: audio in, conversation created (or replayed by
   *  capture id), pipeline kicked. Everything after is server-side. */
  async capture(audio: Blob, opts: {
    captureId: string; mode?: 'post_meeting' | 'live'; rm?: string; email?: string;
    durationSeconds?: number; lat?: number; lng?: number; consentId?: string;
  }): Promise<{ conversation_id: string; replayed?: boolean; status?: string }> {
    try {
      const r = await vocxClient.post('/v1/vox/capture', await audio.arrayBuffer(), {
        headers: { 'Content-Type': 'application/octet-stream' },
        timeout: 300_000,
        params: {
          capture_id: opts.captureId,
          mode: opts.mode || 'post_meeting',
          rm: opts.rm || '',
          email: opts.email || '',
          duration: opts.durationSeconds ?? '',
          lat: opts.lat ?? '',
          lng: opts.lng ?? '',
          consent_id: opts.consentId ?? '',
          content_type: audio.type || '',
          ts: new Date().toISOString(),
        },
      });
      if (!r.data?.ok) throw new Error(r.data?.error || 'capture failed');
      return r.data;
    } catch (e: any) {
      throw new Error(vocxError(e, 'capture'));
    }
  },

  /** Kick (or retry) processing — the Queue's "Retry & open". */
  async process(conversationId: string): Promise<void> {
    const r = await vocxClient.post('/v1/vox/process', { conversation_id: conversationId });
    if (!r.data?.ok) throw new Error(r.data?.error || 'could not start processing');
  },

  list(params: {
    status?: string; mine?: boolean; use_case?: string; q?: string;
    entity_id?: string; date_from?: string; date_to?: string;
    limit?: number; offset?: number;
  } = {}): Promise<{ items: VoxConversation[]; total: number }> {
    const clean = Object.fromEntries(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== '' && v !== false));
    return api.get('/vox/conversations', clean);
  },

  get(id: string): Promise<VoxConversation> {
    return api.get(`/vox/conversations/${id}`);
  },

  /** The atomic edit path: field cells, use-case retags and link pins travel in
   *  one transaction and land in the append-only audit. */
  edits(id: string, payload: {
    edits?: { field_path: string; new_value: VoxCell }[];
    use_cases?: string[];
  snippet?: string | null;
    entity_id?: string; lead_id?: string; deal_id?: string; interaction_id?: string;
    proposed_lead_company?: string; proposed_lead_rm?: string;
    corrected_transcript?: string;
  }): Promise<VoxConversation & { changed: number }> {
    return api.post(`/vox/conversations/${id}/edits`, payload);
  },

  approve(id: string): Promise<VoxConversation> {
    return api.post(`/vox/conversations/${id}/approve`, {});
  },

  /** Delete a draft (recorder/Management before approval) or erase an approved
   *  record (Admin). Content is removed; the consent record and audit survive. */
  erase(id: string): Promise<VoxConversation> {
    return api.post(`/vox/conversations/${id}/erase`, {});
  },

  consent(certificationText: string, deviceMeta?: Record<string, any>): Promise<{ id: string }> {
    return api.post('/vox/consents', {
      certification_text: certificationText, device_meta: deviceMeta });
  },
};

// ------------------------------------------------------------------ pure helpers

const JUDGEMENT_OR_SYSTEM = new Set([
  'opportunity_assessment', 'opportunity_score_override_reason',
  'competitive_intelligence', 'data_quality_flags',
]);

export interface NeedsYouItem {
  fieldPath: string;
  label: string;
  confidence: Confidence;
  /** Which block the field belongs to — the strip's right-hand tag. */
  blockLabel: string;
  /** The current value, shortened — the row reads "Quantum — ₹25 Cr". */
  valueShort: string;
}

function shortValue(v: any): string {
  if (v === null || v === undefined || v === '') return 'not heard';
  const s = Array.isArray(v)
    ? v.map((x) => (typeof x === 'string' ? x : (x?.action ?? ''))).filter(Boolean).join(', ')
    : String(v);
  return s.length > 28 ? `${s.slice(0, 27)}…` : s;
}

/** The needs-you strip: only low/medium-confidence fields, never judgement fields,
 *  never the opportunity score (always optional, per the spec). */
export function needsYou(registry: VoxRegistry, report: VoxReport): NeedsYouItem[] {
  const out: NeedsYouItem[] = [];
  const walk = (blockKey: string, blockLabel: string, defs: any[],
                cells: Record<string, VoxCell> | undefined) => {
    if (!cells) return;
    for (const def of defs) {
      if (JUDGEMENT_OR_SYSTEM.has(def.key) || def.key === 'opportunity_score') continue;
      const cell = cells[def.key];
      if (!cell) continue;
      if (cell.confidence === 'low' || cell.confidence === 'medium') {
        out.push({ fieldPath: `${blockKey}.${def.key}`, label: def.label,
                   confidence: cell.confidence, blockLabel,
                   valueShort: shortValue(cell.value) });
      }
    }
  };
  walk('common', 'Common', registry.common, report.common as any);
  for (const uc of report.detected_use_cases || []) {
    const block = registry.blocks[uc];
    if (block?.fields?.length) walk(uc, block.label || uc, block.fields, (report as any)[uc]);
  }
  return out;
}

/** Which AM fields show for the chosen party role ('both' shows all, in order). */
export function amFieldVisible(def: any, partyRole: string | null | undefined): boolean {
  const applies = def.applies_to || 'always';
  if (applies === 'always') return true;
  if (!partyRole || partyRole === 'not_specified') return applies === 'always';
  if (partyRole === 'both') return true;
  return applies === partyRole;
}
