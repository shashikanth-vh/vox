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
 * Cleared only when the take is safely on the server or explicitly discarded.
 */

const DB_NAME = 'vox-takes';
const STORE = 'takes';

export interface StoredTake {
  id: string;              // the capture id — the idempotency key
  startedAt: string;
  elapsed: number;
  mime: string;
  rm: string;
  chunks: Blob[];
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) {
        req.result.createObjectStore(STORE, { keyPath: 'id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function withStore<T>(mode: IDBTransactionMode,
  fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await openDb();
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const req = fn(tx.objectStore(STORE));
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  } finally {
    db.close();
  }
}

/** Persist the take's current state. Fire-and-forget from the recorder's
 *  dataavailable — a storage hiccup must never disturb a live recording. */
export function saveTake(take: StoredTake): void {
  void withStore('readwrite', (s) => s.put(take)).catch(() => {});
}

/** The oldest unsent take, if any — offered for recovery on mount. */
export async function loadUnsentTake(): Promise<StoredTake | null> {
  try {
    const all = await withStore<StoredTake[]>('readonly', (s) => s.getAll() as any);
    if (!all?.length) return null;
    return all.sort((a, b) => a.startedAt.localeCompare(b.startedAt))[0];
  } catch {
    return null;
  }
}

export async function deleteTake(id: string): Promise<void> {
  try { await withStore('readwrite', (s) => s.delete(id)); } catch { /* gone is gone */ }
}
