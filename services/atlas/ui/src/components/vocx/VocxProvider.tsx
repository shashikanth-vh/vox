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
}

const Ctx = createContext<VocxState>({
  open: false, setOpen: () => {}, toggle: () => {}, pending: 0, refreshPending: () => {},
});

export const useVocx = () => useContext(Ctx);

export function VocxProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(0);
  const [epoch, setEpoch] = useState(0);

  const refreshPending = useCallback(() => setEpoch((n) => n + 1), []);

  // Counted on mount, whenever the panel closes, and on demand after a capture is filed.
  // Fail-quiet: VocX being unreachable must not put an error on a toolbar the user has
  // not asked to use.
  useEffect(() => {
    let alive = true;
    const rm = currentRm();
    if (!rm || !vocxService.enabled()) { setPending(0); return; }
    void vocxService.reports(rm).then((r) => {
      if (!alive || !r.ok) return;
      setPending(r.data.filter((x) => String(x.status || '').toLowerCase() !== 'committed').length);
    });
    return () => { alive = false; };
  }, [open, epoch]);

  const value = useMemo<VocxState>(() => ({
    open, setOpen, toggle: () => setOpen((o) => !o), pending, refreshPending,
  }), [open, pending, refreshPending]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
