import axios from 'axios';
import { authHeaders } from '../auth/session';
import { PRISM_BASE_URL } from './axiosClient';

/**
 * The VocX transport.
 *
 * VocX sits behind the same edge as everything else — the gateway routes the `/vocx`
 * prefix to the service and injects the service's own front-door key, so the browser
 * never holds it. What the browser must carry is the SIGNED-IN IDENTITY, and it carries
 * it exactly the way every other client here does: the bearer for the prod posture, the
 * X-* headers for the dev one, read per request so a sign-in takes effect at once.
 *
 * Rooted at the ORIGIN and not at the register's /v1 base — `/vocx/v1/...` must not
 * inherit the register prefix.
 *
 * The capture timeout is deliberately long. A clip is transcribed SYNCHRONOUSLY by the
 * STT service on CPU, and two minutes of speech legitimately takes minutes to decode;
 * the 15s the other clients use would abandon a capture the user had already recorded.
 * It matches the window the edge and the gateway carry for the same path — the shortest
 * hop in the chain is the one that decides, so all three are set together.
 */
export const VOCX_URL: string =
  import.meta.env.VITE_VOCX_URL || (PRISM_BASE_URL ? `${PRISM_BASE_URL}/vocx` : '/vocx');

const CAPTURE_TIMEOUT_MS = 300_000;

const vocxClient = axios.create({
  baseURL: VOCX_URL,
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
});

vocxClient.interceptors.request.use((cfg) => {
  Object.entries(authHeaders()).forEach(([k, v]) => cfg.headers.set(k, v));
  return cfg;
});

/** VocX answers `{ok: false, error}` with a 200 as often as it uses a status code. */
export function vocxError(e: any, step: string): string {
  const body = e?.response?.data;
  const detail =
    (typeof body === 'string' && body) ||
    body?.error?.detail || body?.error?.title ||
    (typeof body?.error === 'string' ? body.error : '') ||
    body?.detail || '';
  const status = e?.response?.status;
  if (status === 401 || status === 403) return detail || 'You are not signed in to VocX.';
  if (status === 413) return 'That recording is too long for one capture.';
  if (status === 404) return detail || 'VocX has no such record.';
  if (status) return detail || `VocX returned ${status} while trying to ${step}.`;
  if (e?.code === 'ECONNABORTED') {
    return 'VocX did not answer in time. The recording is safe — try again in a moment.';
  }
  return detail || `Cannot reach VocX at ${VOCX_URL || '(unset)'}.`;
}

/** A body that already carries `{ok:false}` is a failure however it was delivered. */
function unwrap<T>(data: any, step: string): T {
  if (data && typeof data === 'object' && data.ok === false) {
    throw new Error(String(data.error || `VocX refused to ${step}.`));
  }
  return data as T;
}

export const vocx = {
  async get<T>(path: string, params?: Record<string, any>): Promise<T> {
    const r = await vocxClient.get(path, { params });
    return unwrap<T>(r.data, 'read');
  },
  async post<T>(path: string, body?: any, params?: Record<string, any>): Promise<T> {
    const r = await vocxClient.post(path, body, { params });
    return unwrap<T>(r.data, 'save');
  },
  /**
   * Raw audio, posted as the blob's own type. Not multipart: VocX reads the request
   * body as the clip itself, so the Content-Type must be the recording's real codec
   * (`audio/webm;codecs=opus` and friends) — not JSON, and not a multipart wrapper.
   */
  async postAudio<T>(path: string, blob: Blob, params: Record<string, any>,
                     onUpload?: (sentPct: number) => void): Promise<T> {
    const r = await vocxClient.post(path, blob, {
      params,
      timeout: CAPTURE_TIMEOUT_MS,
      headers: { 'Content-Type': blob.type || 'audio/webm' },
      // The BROWSER knows when the clip has finished going up; the server cannot say so
      // until it has the whole body. Reporting it lets the progress strip stay honest
      // during a long upload instead of guessing on a timer.
      onUploadProgress: onUpload
        ? (e) => {
          const total = e.total || blob.size || 0;
          onUpload(total ? Math.min(100, Math.round((e.loaded / total) * 100)) : 0);
        }
        : undefined,
    });
    return unwrap<T>(r.data, 'transcribe that recording');
  },
  /** Bytes back (audio playback), not JSON. */
  async blob(path: string, params?: Record<string, any>): Promise<Blob> {
    const r = await vocxClient.get(path, { params, responseType: 'blob' });
    return r.data as Blob;
  },
};

export default vocxClient;
