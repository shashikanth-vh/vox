import type { PrismSession } from '../../auth/session';

export interface Message { role: 'user' | 'assistant'; content: string; }
export interface Evidence { reference: string; label: string; }
export interface ResultTable {
  title: string; columns: string[]; rows: { values: string[]; deal?: string }[];
  total: number; partial: boolean;
}
export interface Update { text?: string; progress?: string; requestId?: string; evidence?: Evidence[]; tables?: ResultTable[]; }

function resultTables(value: unknown): ResultTable[] {
  if (!Array.isArray(value)) return [];
  return value.filter(t => t && Array.isArray(t.columns) && t.columns.length <= 30
    && t.columns.every((c: unknown) => typeof c === 'string' && c.length <= 80)
    && Array.isArray(t.rows) && t.rows.length <= 2000
    && Number.isInteger(t.total) && t.total >= t.rows.length)
    .map(t => ({ title: 'Matching records', columns: t.columns, total: t.total,
      partial: t.partial === true,
      rows: t.rows.filter((r: any) => r && Array.isArray(r.values) && r.values.length === t.columns.length
        && r.values.every((v: unknown) => typeof v === 'string' && v.length <= 301))
        .map((r: any) => ({ values: r.values,
          ...(typeof r.deal === 'string' && /^[\w .-]{1,120}$/.test(r.deal) ? { deal: r.deal } : {}) })) }));
}

export class ChatError extends Error {
  constructor(message: string, public requestId = '', public status = 0) { super(message); }
}

function headers(session: PrismSession): Record<string, string> {
  if (!session.idToken) throw new ChatError('Sign in with your work account to use Chitti.', '', 401);
  return { Authorization: `Bearer ${session.idToken}`, 'X-Tenant': session.tenant,
    'Content-Type': 'application/json' };
}

async function checked(response: Response): Promise<Response> {
  if (response.ok) return response;
  // Never display arbitrary upstream bodies (which can contain implementation details).
  const message = response.status === 401 ? 'Your session has expired. Please sign in again.'
    : response.status === 403 ? "You don't have access to this information."
    : response.status === 429 ? 'Chitti is busy. Please try again shortly.'
    : response.status === 413 ? 'This conversation is too long. Please start a new conversation.'
    : 'Chitti is unavailable. Please try again shortly.';
  await response.body?.cancel();
  throw new ChatError(message, response.headers.get('X-Request-ID') || '', response.status);
}

export async function discoverModel(base: string, session: PrismSession, signal: AbortSignal): Promise<string> {
  const response = await checked(await fetch(`${base}/chitti/v1/models`, {
    headers: headers(session), signal, cache: 'no-store', credentials: 'omit',
  }));
  const data = await response.json();
  const model = data?.data?.[0]?.id;
  if (typeof model !== 'string' || !model) throw new ChatError('Chitti is unavailable. Please try again shortly.');
  return model;
}

/** Parse SSE incrementally, including split UTF-8, CRLF, comments and multiline data. */
export async function readCompletion(response: Response, onUpdate: (update: Update) => void): Promise<void> {
  const requestId = response.headers.get('X-Request-ID') || '';
  onUpdate({ requestId });
  if (!response.headers.get('Content-Type')?.startsWith('text/event-stream') || !response.body) {
    await response.body?.cancel();
    throw new ChatError('Chitti returned an unexpected response. Please try again.', requestId);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let lines: string[] = [];
  let eventBytes = 0;
  let answerLength = 0;
  let done = false;
  const event = () => {
    const data = lines.join('\n');
    lines = [];
    eventBytes = 0;
    if (!data) return;
    if (data === '[DONE]') { done = true; return; }
    const value = JSON.parse(data);
    const progress = value.event?.type === 'status' ? value.event.data?.description : undefined;
    if (typeof progress === 'string' && progress.trim()) {
      onUpdate({ progress });
    }
    const text = value.choices?.[0]?.delta?.content;
    if (typeof text === 'string') {
      answerLength += text.length;
      if (answerLength > 1_000_000) throw new ChatError('The answer is too large. Try a narrower question.', requestId);
      onUpdate({ text });
    }
    if (value.chitti) {
      const evidence = Array.isArray(value.chitti.evidence)
        ? value.chitti.evidence.filter((e: any) => typeof e.reference === 'string' && typeof e.label === 'string')
          .map((e: any) => ({ reference: e.reference, label: e.label })) : [];
      onUpdate({ evidence, tables: resultTables(value.chitti.tables) });
    }
    // Deliberately ignore all other metadata and model/provider progress details.
  };
  try {
    while (!done) {
      const next = await reader.read();
      buffer += decoder.decode(next.value, { stream: !next.done });
      if (buffer.length > 1_000_000) throw new ChatError('The answer is too large. Try a narrower question.', requestId);
      let newline: number;
      while (!done && (newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline).replace(/\r$/, '');
        buffer = buffer.slice(newline + 1);
        if (!line) event();
        else if (line.startsWith('data:')) {
          eventBytes += line.length;
          if (eventBytes > 1_000_000) throw new ChatError('The answer is too large. Try a narrower question.', requestId);
          lines.push(line.slice(5).replace(/^ /, ''));
        }
      }
      if (next.done && !done) throw new ChatError('The answer was interrupted. Please try again.', requestId);
    }
  } catch (error) {
    if (error instanceof ChatError || (error instanceof Error && error.name === 'AbortError')) throw error;
    throw new ChatError('The answer was interrupted. Please try again.', requestId);
  } finally {
    try { await reader.cancel(); } finally { reader.releaseLock(); }
  }
}

export async function complete(base: string, session: PrismSession, model: string, messages: Message[],
  signal: AbortSignal, onUpdate: (update: Update) => void): Promise<void> {
  const response = await checked(await fetch(`${base}/chitti/v1/chat/completions`, {
    method: 'POST', headers: headers(session), body: JSON.stringify({ model, messages, stream: true }),
    signal, cache: 'no-store', credentials: 'omit',
  }));
  await readCompletion(response, onUpdate);
}
