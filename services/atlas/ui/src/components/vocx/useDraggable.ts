import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Drag-to-place, for the things the user is meant to put where they like: the floating
 * VocX panel, and the capture button on a phone.
 *
 * Pointer events rather than mouse+touch: one code path covers mouse, touch and pen, and
 * `setPointerCapture` means a fast drag that leaves the element still tracks instead of
 * sticking. Position is stored per key so a panel the user parked bottom-right is still
 * there tomorrow.
 *
 * Two rules that make the difference between "movable" and "usable":
 *   * the element is CLAMPED into the viewport on every move AND on resize/rotate —
 *     otherwise a phone turned to landscape leaves the button off-screen for good;
 *   * a press that never really moved is NOT a drag, so the same element can be both
 *     draggable and clickable without the click being eaten.
 */

export interface Point { x: number; y: number; }

const MOVED_ENOUGH = 4;          // px before a press counts as a drag, not a tap

function clamp(p: Point, w: number, h: number, margin: number): Point {
  const maxX = Math.max(margin, window.innerWidth - w - margin);
  const maxY = Math.max(margin, window.innerHeight - h - margin);
  return {
    x: Math.min(Math.max(p.x, margin), maxX),
    y: Math.min(Math.max(p.y, margin), maxY),
  };
}

function load(key: string): Point | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const p = JSON.parse(raw);
    return typeof p?.x === 'number' && typeof p?.y === 'number' ? p : null;
  } catch { return null; }
}

export interface Draggable {
  /** Current top-left, already clamped into the viewport. */
  pos: Point;
  /** Spread onto the element that starts a drag (the whole button, a panel's header). */
  handleProps: {
    onPointerDown: (e: React.PointerEvent) => void;
    style: { touchAction: 'none' };
  };
  dragging: boolean;
  /** True until the pointer has travelled far enough to count as a drag — so a click
   *  handler can ignore the release that ended a move. */
  wasTap: () => boolean;
  /** Put it back where it started. */
  reset: () => void;
}

export function useDraggable(opts: {
  /** localStorage key; the position is remembered under it. */
  storageKey: string;
  /** Where it sits before the user has ever moved it. */
  initial: (vw: number, vh: number) => Point;
  size: () => { w: number; h: number };
  margin?: number;
  enabled?: boolean;
}): Draggable {
  const { storageKey, initial, size, margin = 8, enabled = true } = opts;
  const [pos, setPos] = useState<Point>(() => {
    const saved = load(storageKey);
    const { w, h } = size();
    return clamp(saved ?? initial(window.innerWidth, window.innerHeight), w, h, margin);
  });
  const [dragging, setDragging] = useState(false);
  const originRef = useRef<{ px: number; py: number; x: number; y: number } | null>(null);
  const movedRef = useRef(false);

  // Persist lazily — writing on every pointermove would hammer storage for no benefit.
  const persist = useCallback((p: Point) => {
    try { localStorage.setItem(storageKey, JSON.stringify(p)); } catch { /* private mode */ }
  }, [storageKey]);

  // A rotated phone or a resized window must not strand the element off-screen.
  useEffect(() => {
    const onResize = () => {
      const { w, h } = size();
      setPos((p) => clamp(p, w, h, margin));
    };
    window.addEventListener('resize', onResize);
    window.addEventListener('orientationchange', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      window.removeEventListener('orientationchange', onResize);
    };
  }, [margin, size]);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    if (!enabled || e.button !== 0) return;
    // A press that starts on a CONTROL is that control's, not the drag's. The handle
    // calls setPointerCapture, which retargets every following pointer event — including
    // the pointerup that would have completed the click — to the handle. So the close and
    // roll-up buttons in the panel's header simply stopped working: the panel could not
    // be shut. Interactive descendants opt out of the drag entirely.
    if ((e.target as HTMLElement)?.closest?.(
      'button, a, input, textarea, select, [role="button"], [role="tab"]')) {
      return;
    }
    const el = e.currentTarget as HTMLElement;
    el.setPointerCapture?.(e.pointerId);
    originRef.current = { px: e.clientX, py: e.clientY, x: pos.x, y: pos.y };
    movedRef.current = false;

    const move = (ev: PointerEvent) => {
      const o = originRef.current;
      if (!o) return;
      const dx = ev.clientX - o.px;
      const dy = ev.clientY - o.py;
      if (!movedRef.current && Math.hypot(dx, dy) < MOVED_ENOUGH) return;
      movedRef.current = true;
      setDragging(true);
      const { w, h } = size();
      setPos(clamp({ x: o.x + dx, y: o.y + dy }, w, h, margin));
    };
    const up = () => {
      el.releasePointerCapture?.(e.pointerId);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
      originRef.current = null;
      setDragging(false);
      // Read the settled value through the setter — `pos` here is the stale closure.
      setPos((p) => { if (movedRef.current) persist(p); return p; });
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
  }, [enabled, margin, persist, pos.x, pos.y, size]);

  const reset = useCallback(() => {
    const { w, h } = size();
    const p = clamp(initial(window.innerWidth, window.innerHeight), w, h, margin);
    setPos(p); persist(p);
  }, [initial, margin, persist, size]);

  return {
    pos,
    dragging,
    wasTap: () => !movedRef.current,
    reset,
    handleProps: { onPointerDown, style: { touchAction: 'none' } },
  };
}
