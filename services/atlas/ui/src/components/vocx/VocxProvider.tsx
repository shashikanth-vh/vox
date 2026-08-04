import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from 'react';
import { vocxService } from '../../services/vocxService';
import { currentRm } from './rm';

/**
 * One VocX session for the whole app.
 *
 * The launcher appears in two places — a toolbar button on a desktop, a draggable
 * floating button on a phone — and the panel is mounted once at the layout root so it
 * outlives page navigation: an RM can start a capture on Lending, walk to Deals to check
 * something, and come back to the same in-progress note. Shared state is what makes those
 * three components one feature instead of three.
 */

interface VocxState {
  open: boolean;
  setOpen: (v: boolean) => void;
  toggle: () => void;
  /** Captures recorded but never filed — the badge on the launcher. */
  pending: number;
  refreshPending: () => void;
  /** True from the moment the microphone opens until the clip is handed off. */
  recording: boolean;
  setRecording: (v: boolean) => void;
}

const Ctx = createContext<VocxState>({
  open: false, setOpen: () => {}, toggle: () => {}, pending: 0, refreshPending: () => {},
  recording: false, setRecording: () => {},
});

export const useVocx = () => useContext(Ctx);

export function VocxProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(0);
  const [epoch, setEpoch] = useState(0);
  const [recording, setRecording] = useState(false);

  const refreshPending = useCallback(() => setEpoch((n) => n + 1), []);

  // Counted on mount, whenever the panel closes, and on demand after a capture is filed.
  // Fail-quiet: VocX being unreachable must not put an error on a toolbar the user has
  // not asked to use.
  useEffect(() => {
    let alive = true;
    if (!vocxService.enabled()) { setPending(0); return; }
    void vocxService.reports(currentRm()).then((r) => {
      if (!alive || !r.ok) return;
      setPending(r.data.filter((x) => String(x.status || '').toLowerCase() !== 'committed').length);
    });
    return () => { alive = false; };
  }, [open, epoch]);

  // Closing the panel unmounts the recorder, and an unmounted recorder loses the take.
  // A live microphone therefore refuses a close from ANY of the three controls that can
  // ask for one — the panel's own button, the toolbar mic, the phone's floating mic —
  // by refusing it in the one place all three go through. Opening is never blocked, and
  // rolling the panel up is not a close (see VocxPanel): it keeps recording, minimised.
  const requestOpen = useCallback((v: boolean) => {
    setOpen((prev) => (prev && !v && recording ? prev : v));
  }, [recording]);

  const value = useMemo<VocxState>(() => ({
    open,
    setOpen: requestOpen,
    toggle: () => requestOpen(!open),
    pending,
    refreshPending,
    recording,
    setRecording,
  }), [open, requestOpen, pending, refreshPending, recording]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
