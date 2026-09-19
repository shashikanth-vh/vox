import {
  createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode,
} from 'react';
import { useAuth } from '../../auth/AuthContext';
import { PRISM_BASE_URL } from '../../api/axiosClient';
import { ChatError, complete, discoverModel, type Message } from './chatClient';
import {
  chatOwner, loadChatMemory, saveChatMemory, type DisplayMessage,
} from '../../auth/chatMemory';

/**
 * ONE Chitti conversation for the whole app.
 *
 * Chitti now has two faces — the full /chat page and the floating bubble that follows the
 * user around every other view — and they are the same conversation, not two. That is not
 * a nicety: both surfaces persist to the same sessionStorage key, so two independent
 * copies of this state would overwrite each other's history the moment both existed.
 * Holding it once, above the router, also means an answer that is still streaming
 * survives the user navigating from Deals to Lending, and a question asked in the bubble
 * is already there when they open the full page to read the tables it returned.
 *
 * The send/stream/abort logic below is the page's original, moved up unchanged.
 */

export interface Editing { index: number; text: string }

interface Chitti {
  /** False when there is no work-account session Chitti can be asked anything with. */
  ready: boolean;
  messages: DisplayMessage[];
  input: string;
  setInput: (v: string) => void;
  busy: boolean;
  error: string;
  /** False when the tab cannot persist the conversation (private mode, full storage). */
  memoryAvailable: boolean;
  editing: Editing | null;
  setEditing: (e: Editing | null) => void;
  send: (text?: string, editIndex?: number) => Promise<void>;
  cancel: () => void;
  reset: () => void;
  /** Floating bubble panel — open state lives here so any surface can raise it. */
  open: boolean;
  setOpen: (v: boolean) => void;
  toggle: () => void;
  /** Answers that finished while the panel was shut; the badge on the bubble. */
  unread: number;
}

const Ctx = createContext<Chitti | null>(null);

export function useChitti(): Chitti {
  const value = useContext(Ctx);
  if (!value) throw new Error('useChitti must be used inside <ChittiProvider>');
  return value;
}

export function ChittiProvider({ children }: { children: ReactNode }) {
  const { session, signOut } = useAuth();
  const ready = Boolean(session?.idToken);
  // Identity is the provider's key in the layout, so the stored conversation read here
  // always belongs to the person who is signed in now.
  const [saved] = useState(() => (session
    ? loadChatMemory(session)
    : { messages: [] as DisplayMessage[], input: '', error: '' }));
  const [messages, setMessages] = useState<DisplayMessage[]>(saved.messages);
  const [input, setInput] = useState(saved.input);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(saved.error);
  const [memoryAvailable, setMemoryAvailable] = useState(true);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [open, setOpenState] = useState(false);
  const [unread, setUnread] = useState(0);
  const active = useRef<AbortController | null>(null);
  // Read inside the async send, which must see whether the panel is open NOW — not
  // whether it was open when the question was asked.
  const openRef = useRef(open);
  const base = PRISM_BASE_URL.replace(/\/$/, '');

  useEffect(() => () => { active.current?.abort(); active.current = null; }, []);
  useEffect(() => { openRef.current = open; }, [open]);
  useEffect(() => {
    if (!session) return;
    setMemoryAvailable(saveChatMemory(session, { messages, input, error }));
  }, [session, messages, input, error]);

  const setOpen = useCallback((v: boolean) => {
    openRef.current = v;          // the send in flight reads this before the effect runs
    setOpenState(v);
    if (v) setUnread(0);
  }, []);
  const toggle = useCallback(() => setOpen(!openRef.current), [setOpen]);

  const cancel = useCallback(() => {
    active.current?.abort(); active.current = null; setBusy(false);
  }, []);

  const reset = useCallback(() => {
    cancel(); setEditing(null); setMessages([]); setInput(''); setError(''); setUnread(0);
  }, [cancel]);

  const send = useCallback(async (text?: string, editIndex?: number) => {
    const question = (text ?? input).trim();
    if (!question || active.current) return;
    if (!session?.idToken) { setError('Sign in with your work account to use Chitti.'); return; }
    const preceding = editIndex === undefined ? messages : messages.slice(0, editIndex);
    const history: Message[] = [
      ...preceding.filter(m => m.complete).map(({ role, content }) => ({ role, content })),
      { role: 'user', content: question },
    ];
    const controller = new AbortController();
    active.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 330_000);
    if (editIndex === undefined) setInput('');
    setEditing(null); setError(''); setBusy(true);
    setMessages([...preceding, { role: 'user', content: question, complete: true },
      { role: 'assistant', content: '', complete: false }]);
    try {
      const model = await discoverModel(base, session, controller.signal);
      await complete(base, session, model, history, controller.signal, update => {
        if (active.current !== controller) return;
        setMessages(previous => previous.map((message, index) => index === previous.length - 1
          ? { ...message, content: message.content + (update.text || ''),
            requestId: update.requestId || message.requestId,
            evidence: update.evidence ?? message.evidence,
            tables: update.tables ?? message.tables,
            progress: update.progress ?? message.progress }
          : message));
      });
      if (active.current === controller) {
        setMessages(previous => previous.map((message, index) => index === previous.length - 1
          ? { ...message, complete: true } : message));
        // A finished answer behind a shut panel is otherwise completely silent.
        if (!openRef.current) setUnread(n => n + 1);
      }
    } catch (failure) {
      if (active.current !== controller) return;
      if (failure instanceof ChatError && failure.status === 401) { signOut(); return; }
      setError(controller.signal.aborted ? 'The answer took too long. Please try again.'
        : failure instanceof ChatError ? failure.message : 'Unable to connect. Please try again.');
      if (failure instanceof ChatError && failure.requestId) {
        setMessages(previous => previous.map((message, index) => index === previous.length - 1
          ? { ...message, requestId: failure.requestId } : message));
      }
    } finally {
      window.clearTimeout(timeout);
      if (active.current === controller) { active.current = null; setBusy(false); }
    }
  }, [base, input, messages, session, signOut]);

  // Not memoised: every field in here changes with the conversation, and every consumer
  // renders off the conversation, so a stable identity would buy nothing.
  const value: Chitti = {
    ready, messages, input, setInput, busy, error, memoryAvailable,
    editing, setEditing, send, cancel, reset, open, setOpen, toggle, unread,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** The provider's key — remount (and re-read storage) when the signed-in identity changes. */
export const chittiKey = (session: Parameters<typeof chatOwner>[0] | null) =>
  (session ? chatOwner(session) : 'signed-out');
