import { useCallback, useEffect, useState } from 'react';
import { Badge, Box, Tooltip } from '@mui/material';
import MicIcon from '@mui/icons-material/Mic';
import { useDraggable } from './useDraggable';
import { useVocx } from './VocxProvider';
import { tokens } from '../../theme';

/**
 * The way into VocX, in the two shapes the two devices want.
 *
 * `VocxNavButton` sits in the top bar beside the search box, where a pointer looks for a
 * toolbar. `VocxFab` is the phone's answer: a FLOATING button the user drags wherever
 * their thumb reaches. Which hand holds the phone is not a designer's decision, and a
 * control pinned bottom-right is a stretch for a left-handed user on a large screen — so
 * it goes where they put it, and stays there.
 *
 * They are separate components rather than one that switches, because the desktop one is
 * a child of the navbar (which hides itself on mobile) and the floating one must be a
 * child of the layout root — a `display:none` ancestor would take the FAB with it.
 *
 * The badge counts captures recorded but never filed. Those are the easiest thing in the
 * product to lose — a preview writes nothing — so the count is on the way IN.
 */

const FAB = 56;

function MicButton({ pending, open, big }: { pending: number; open: boolean; big: boolean }) {
  return (
    <Badge
      badgeContent={pending}
      max={99}
      overlap="circular"
      sx={{ '& .MuiBadge-badge': {
        bgcolor: '#F0B43C', color: '#2A1B00', fontWeight: 800, fontSize: 10, pointerEvents: 'none' } }}
    >
      <Box
        component="span"
        role="button"
        tabIndex={-1}
        aria-hidden
        sx={{
          width: big ? FAB : 40, height: big ? FAB : 40, borderRadius: '50%',
          display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
          bgcolor: open ? '#E0554E' : tokens.tealHi, color: open ? '#fff' : '#04241B',
          boxShadow: big ? '0 8px 22px rgba(0,0,0,.45)' : 'none',
          transition: 'background-color 180ms',
        }}
      >
        <MicIcon sx={{ fontSize: big ? 28 : 22 }} />
      </Box>
    </Badge>
  );
}

/** Desktop: a plain toolbar control. */
export function VocxNavButton() {
  const { open, toggle, pending } = useVocx();
  return (
    <Tooltip title={pending ? `VocX — ${pending} awaiting approval` : 'VocX field intel'}>
      <Box
        component="button"
        type="button"
        onClick={toggle}
        aria-label={pending
          ? `VocX field intel — ${pending} capture(s) awaiting approval`
          : 'VocX field intel'}
        aria-expanded={open}
        sx={{
          background: 'none', border: 0, p: 0, cursor: 'pointer', display: 'inline-flex',
          '&:focus-visible': { outline: `3px solid ${tokens.tealHi}`, outlineOffset: 3, borderRadius: '50%' },
        }}
      >
        <MicButton pending={pending} open={open} big={false} />
      </Box>
    </Tooltip>
  );
}

/** Mobile: draggable, and remembered where it was left. */
export function VocxFab() {
  const { open, setOpen, pending } = useVocx();
  const [show, setShow] = useState(
    () => typeof window !== 'undefined' && window.innerWidth <= 760);

  useEffect(() => {
    const onResize = () => setShow(window.innerWidth <= 760);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const size = useCallback(() => ({ w: FAB, h: FAB }), []);
  // Above the bottom nav by default, so it does not cover the tabs on first use.
  const initial = useCallback(
    (vw: number, vh: number) => ({ x: vw - FAB - 16, y: vh - FAB - 96 }), []);
  const drag = useDraggable({
    storageKey: 'atlas.vocx.launcher', initial, size, margin: 10, enabled: show,
  });

  if (!show) return null;

  return (
    <Box
      {...drag.handleProps}
      component="button"
      type="button"
      // Dragged by the same element that opens it: a press that never travelled counts
      // as a tap, so both gestures live on one control with no separate handle.
      onClick={() => { if (drag.wasTap()) setOpen(!open); }}
      onDoubleClick={drag.reset}
      title="Drag to move · double-tap to reset"
      aria-label={pending
        ? `VocX field intel — ${pending} capture(s) awaiting approval. Drag to move.`
        : 'VocX field intel. Drag to move.'}
      aria-expanded={open}
      sx={{
        position: 'fixed', left: drag.pos.x, top: drag.pos.y, zIndex: 1260,
        background: 'none', border: 0, p: 0, cursor: drag.dragging ? 'grabbing' : 'pointer',
        opacity: drag.dragging ? 0.85 : 1,
        '&:focus-visible': { outline: `3px solid ${tokens.tealHi}`, outlineOffset: 3, borderRadius: '50%' },
      }}
    >
      <MicButton pending={pending} open={open} big />
    </Box>
  );
}
