import { QueryClient } from '@tanstack/react-query';

// Focus refetch ON (field feedback: "I have to refresh to see the change"): the demo
// and the real desk both switch between windows — approve in one, look in the other —
// and a page that keeps yesterday's answer until F5 reads as broken. staleTime still
// throttles it: a focus flip within 10s serves the cache untouched, so at 1000 users
// this costs at most one list read per user per window-switch, not a polling storm.
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: true,
      staleTime: 10_000,
      // Gentle convergence for a user PARKED on a page: every mounted query re-asks
      // the server once a minute, so another desk's write shows up without F5. Hidden
      // tabs do not poll (refetchIntervalInBackground defaults false), and only the
      // queries actually on screen tick — one visible grid ≈ one list read per minute
      // per user. True push (SSE) can replace this later without touching pages.
      refetchInterval: 60_000,
    },
  },
});
