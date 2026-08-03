import { orchestrator } from '../api/orchestratorClient';

/**
 * Watching a run long enough to catch an immediate refusal.
 *
 * Accepting a workflow request is not the same as the work succeeding. Both planes answer
 * 202 the moment the run is STARTED or RESUMED; the activities that actually write to the
 * register run afterwards. When one of those is refused — a validation the register applies,
 * a permission the caller does not hold — the run dies seconds later and, without this, the
 * screen has already said "done" and moved on. That is exactly how an approved lead
 * conversion could leave no rows behind in Deals or Lending while the approver saw success.
 *
 * Returns the failure text when the run has already died, or null — which covers both
 * "still running" (the normal, healthy case: parked awaiting the next decision, or still
 * applying) and "completed". Deliberately short: this catches a step refused on its first
 * activity, it does not wait out a real approval.
 */
const DEAD = ['FAILED', 'TIMED_OUT', 'TERMINATED'];
const BACKOFF = [400, 900, 1800, 3000];

export async function settled(workflowId?: string, statusUrl?: string): Promise<string | null> {
  // Prefer the URL the plane handed back; fall back to the conventional route.
  const url = statusUrl || (workflowId ? `/v1/workflows/${workflowId}` : '');
  if (!url) return null;
  const label = workflowId || url;
  for (const wait of BACKOFF) {
    await new Promise((r) => setTimeout(r, wait));
    try {
      const run = await orchestrator.get<any>(url);
      const status = String(run?.status ?? '');
      if (DEAD.includes(status)) {
        return run?.failure || `The run ended as ${status} — see the workflow log for ${label}.`;
      }
      if (status === 'COMPLETED') return null;
    } catch {
      return null;                    // cannot watch it; do not invent a failure
    }
  }
  return null;                        // still running: parked awaiting its decision
}
