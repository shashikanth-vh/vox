/**
 * The local take store — a recording must survive anything the browser does.
 *
 * The panel's close guard stops the polite exits; this stops the impolite ones.
 * While the mic is open, every captured chunk is persisted into IndexedDB under
 * the take's capture id. A refresh, a crashed tab or a dead battery then loses
 * at most the last second: when the Record screen next mounts it finds the
 * unsent take and offers to send it — under the SAME capture id, so a take that
 * actually made it to the server before the crash replays instead of duplicating.
 *
 * v2, for the 90-minute live mode: chunks are APPENDED, one row each, written
 * exactly once. v1 rewrote the whole accumulated array every second — fine for
 * a 3-minute note, quadratic disaster for a 90-minute meeting (by the end,
 * every second re-wrote the entire take). The meta row stays tiny and is the
 * only thing updated per tick. v1 records (with an inline chunks array) still
 * load, so an in-flight take survives the upgrade.
 *
 * Cleared only when the take is safely on the server or explicitly discarded.
 */

const DB_NAME = 'vox-takes';
const STORE = 'takes';
const CHUNKS = 'chunks';

export interface StoredTake {
  id: string;              // the capture id — the idempotency key
  startedAt: string;
  elapsed: number;
  mime: string;
  rm: string;
  chunks: Blob[];
}

interface TakeMeta {
  id: string;
  startedAt: string;
  elapsed: number;
  mime: string;
  rm: string;
  /** v1 records carried the blobs inline; v2 keeps them in the chunk store. */
  chunks?: Blob[];
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 2);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: 'id' });
      }
      if (!db.objectStoreNames.contains(CHUNKS)) {
        db.createObjectStore(CHUNKS, { keyPath: ['takeId', 'idx'] });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function withTx<T>(stores: string[], mode: IDBTransactionMode,
  fn: (tx: IDBTransaction) => Promise<T> | T): Promise<T> {
  const db = await openDb();
  try {
    const tx = db.transaction(stores, mode);
    const done = new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
    const out = await fn(tx);
    await done;
    return out;
  } finally {
    db.close();
  }
}

const reqDone = <T,>(r: IDBRequest<T>) => new Promise<T>((resolve, reject) => {
  r.onsuccess = () => resolve(r.result);
  r.onerror = () => reject(r.error);
});

/** Append ONE new chunk and refresh the tiny meta row. Fire-and-forget from the
 *  recorder's dataavailable — a storage hiccup must never disturb a live take. */
export function appendTakeChunk(meta: Omit<StoredTake, 'chunks'>, idx: number, chunk: Blob): void {
  void withTx([STORE, CHUNKS], 'readwrite', (tx) => {
    tx.objectStore(STORE).put({ ...meta });
    tx.objectStore(CHUNKS).put({ takeId: meta.id, idx, chunk });
  }).catch(() => {});
}

/** v1-compatible full save — used only by legacy callers; avoid for long takes. */
export function saveTake(take: StoredTake): void {
  void withTx([STORE], 'readwrite', (tx) => {
    tx.objectStore(STORE).put(take);
  }).catch(() => {});
}

/** The oldest unsent take BELONGING TO THIS USER, if any — offered for recovery
 *  on mount. A browser is shared between logins on field laptops: a take is only
 *  ever offered back to the person who recorded it (the rm stamped at capture),
 *  and a stray empty meta (an aborted start with no audio) is cleaned up
 *  silently instead of haunting the next login as a 00:00 recovery card.
 *  Reassembles v2 chunk rows; passes a v1 inline-chunks record through. */
export async function loadUnsentTake(rm?: string): Promise<StoredTake | null> {
  const owns = (takeRm: string | undefined) =>
    !rm || (takeRm || '').trim().toLowerCase() === rm.trim().toLowerCase();
  try {
    // One SHORT transaction per read: an IndexedDB transaction auto-commits the
    // moment the event loop spins past it, so a second await inside one tx dies
    // with TransactionInactiveError on some engines.
    const metas = (await withTx([STORE], 'readonly', (tx) =>
      reqDone(tx.objectStore(STORE).getAll()))) as TakeMeta[];
    metas.sort((a, b) => (a.startedAt || '').localeCompare(b.startedAt || ''));
    for (const m of metas) {
      if (Array.isArray(m.chunks) && m.chunks.length) {
        if (!owns(m.rm)) continue;               // another login's audio — never offered
        return { ...(m as StoredTake) };
      }
      const rows = (await withTx([CHUNKS], 'readonly', (tx) =>
        reqDone(tx.objectStore(CHUNKS).getAll(
          IDBKeyRange.bound([m.id, 0], [m.id, Infinity]))))) as { idx: number; chunk: Blob }[];
      if (!rows.length) { void deleteTake(m.id); continue; }   // aborted start — clean, move on
      if (!owns(m.rm)) continue;
      rows.sort((a, b) => a.idx - b.idx);
      return { id: m.id, startedAt: m.startedAt, elapsed: m.elapsed,
               mime: m.mime, rm: m.rm, chunks: rows.map((r) => r.chunk) };
    }
    return null;
  } catch {
    return null;
  }
}

/** Remove a take (meta + chunks) once it is safely on the server or discarded. */
export async function deleteTake(id: string): Promise<void> {
  try {
    await withTx([STORE, CHUNKS], 'readwrite', async (tx) => {
      tx.objectStore(STORE).delete(id);
      const keys = (await reqDone(tx.objectStore(CHUNKS).getAllKeys(
        IDBKeyRange.bound([id, 0], [id, Infinity])))) as IDBValidKey[];
      for (const k of keys) tx.objectStore(CHUNKS).delete(k);
    });
  } catch { /* best effort */ }
}
