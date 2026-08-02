// v11's `stamp()` — a transient "✓ saved" indicator in the header that fades in on any
// action and fades out after a couple of seconds. Services emit; the Navbar subscribes.
type Listener = (msg: string) => void;

const listeners = new Set<Listener>();

export function onSave(fn: Listener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

export function emitSave(msg: string): void {
  listeners.forEach((fn) => fn(msg));
}
