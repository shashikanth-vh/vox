import type { PrismSession } from './session';
import type { Evidence, Message, ResultTable } from '../components/chat/chatClient';

export interface DisplayMessage extends Message {
  progress?: string; requestId?: string; evidence?: Evidence[];
  tables?: ResultTable[]; complete?: boolean;
}
export interface ConversationMemory { messages: DisplayMessage[]; input: string; error: string; }
const KEY = 'atlas.chitti.conversation';
export const chatOwner = (session: PrismSession) => JSON.stringify([
  session.tenant, session.email, session.userId,
]);

export function clearChatMemory(): void {
  try { sessionStorage.removeItem(KEY); } catch { /* Storage may be disabled. */ }
}

export function loadChatMemory(session: PrismSession): ConversationMemory {
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY) || 'null');
    if (saved?.owner === chatOwner(session) && Array.isArray(saved.messages)
      && typeof saved.input === 'string' && typeof saved.error === 'string') return saved;
  } catch { /* Start empty if storage is unavailable or invalid. */ }
  clearChatMemory();
  return { messages: [], input: '', error: '' };
}

export function saveChatMemory(session: PrismSession, memory: ConversationMemory): boolean {
  try {
    if (!memory.messages.length && !memory.input && !memory.error) clearChatMemory();
    else sessionStorage.setItem(KEY, JSON.stringify({ owner: chatOwner(session), ...memory,
      messages: memory.messages.map(({ progress: _progress, ...message }) => message) }));
    return true;
  } catch {
    clearChatMemory();
    return false;
  }
}
