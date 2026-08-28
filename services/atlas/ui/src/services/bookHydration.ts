import { USE_REAL_API } from '../api/http';
import { leadsService } from './leadsService';
import { dealsService } from './dealsService';
import { lendingService } from './lendingService';
import { syndicationService } from './syndicationService';
import { assetMonService } from './assetMonService';
import { clientsService } from './clientsService';
import { fiService } from './fiService';

// The WHOLE book into the local store, all resources, all pages — the dashboard (and
// Today) must aggregate over everything the register holds, never over whichever grid
// pages this session happened to visit. Each pull is a cursor loop (listAll) behind the
// register's own scoping, so a scoped role still sees exactly its own book — complete.
let inflight: Promise<void> | null = null;
let hydratedAt = 0;
const TTL_MS = 30_000;

// A tab that sat in the background for a while holds an AGING mirror — the grids
// are server-paged and refetch on focus (react-query), but the store-fed views
// (Today, the drawer, the matrix) kept their snapshot until a manual refresh.
// On refocus after this staleness window the whole book re-pulls, mounted queries
// refetch, and store-driven pages are nudged via the event below.
const STALE_FOCUS_MS = 5 * 60_000;
let focusArmed = false;
function armFocusRehydrate(): void {
  if (focusArmed || !USE_REAL_API || typeof window === 'undefined') return;
  focusArmed = true;
  const wake = () => {
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    if (!hydratedAt || Date.now() - hydratedAt < STALE_FOCUS_MS) return;
    void hydrateBook(true).then(async () => {
      const { queryClient } = await import('../utils/queryClient');
      void queryClient.invalidateQueries();
      window.dispatchEvent(new Event('prism:book-rehydrated'));
    });
  };
  window.addEventListener('focus', wake);
  document.addEventListener('visibilitychange', wake);
}
armFocusRehydrate();

export async function hydrateBook(force = false): Promise<void> {
  if (!USE_REAL_API) return;
  if (!force && Date.now() - hydratedAt < TTL_MS) return;
  if (inflight) return inflight;
  inflight = (async () => {
    await Promise.allSettled([
      clientsService.hydrateAll(),
      leadsService.hydrateAll(),
      dealsService.hydrateAll(),
      lendingService.hydrateAll(),
      syndicationService.hydrate(true),
      assetMonService.hydrateAll(),
      fiService.hydrate(),
    ]);
    hydratedAt = Date.now();
  })().finally(() => { inflight = null; });
  return inflight;
}
