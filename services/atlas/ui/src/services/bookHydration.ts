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
