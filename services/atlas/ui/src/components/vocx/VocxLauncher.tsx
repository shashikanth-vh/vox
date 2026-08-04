import { useCallback, useEffect, useState } from 'react';
import { Badge, Box, Tooltip } from '@mui/material';
import MicIcon from '@mui/icons-material/Mic';
import { useDraggable } from './useDraggable';
import { useVocx } from './VocxProvider';
import { vx } from './vocxStyles';
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

function MicButton({ pending, open, big, recording }:
                  { pending: number; open: boolean; big: boolean; recording: boolean }) {
  const d = big ? FAB : 40;
  return (
    <Badge
      badgeContent={recording ? 0 : pending}
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
          width: d, height: d, borderRadius: '50%',
          display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
          bgcolor: recording ? vx.live : (open ? vx.grn2 : tokens.tealHi),
          color: '#04241B',
          boxShadow: big ? '0 8px 22px rgba(0,0,0,.45)' : 'none',
          transition: 'background-color 180ms',
          // A HALO, not a size change: the button keeps its hit area (and the phone's
          // draggable one keeps its geometry) while an expanding ring says, from
          // anywhere in the app, that the microphone is open. Respects
          // prefers-reduced-motion — a persistent pulse is exactly what that setting is
          // for, so the colour alone carries it there.
          '@keyframes vocxHalo': {
            '0%': { boxShadow: `0 0 0 0 ${vx.liveSoft}` },
            '70%': { boxShadow: `0 0 0 ${big ? 16 : 11}px rgba(34,211,238,0)` },
            '100%': { boxShadow: '0 0 0 0 rgba(34,211,238,0)' },
          },
          ...(recording ? { animation: 'vocxHalo 1.5s ease-out infinite' } : {}),
          '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
        }}
      >
        <MicIcon sx={{ fontSize: big ? 28 : 22 }} />
      </Box>
    </Badge>
  );
}

/** Desktop: a plain toolbar control. */
export function VocxNavButton() {
  const { open, toggle, pending, recording } = useVocx();
  return (
    <Tooltip title={recording
      ? 'VocX is recording — stop or minimise from the panel'
      : (pending ? `VocX — ${pending} awaiting approval` : 'VocX field intell')}>
      <Box
        component="button"
        type="button"
        onClick={toggle}
        aria-label={recording
          ? 'VocX field intell — recording in progress'
          : (pending
            ? `VocX field intell — ${pending} capture(s) awaiting approval`
            : 'VocX field intell')}
        aria-expanded={open}
        sx={{
          background: 'none', border: 0, p: 0, cursor: 'pointer', display: 'inline-flex',
          '&:focus-visible': { outline: `3px solid ${tokens.tealHi}`, outlineOffset: 3, borderRadius: '50%' },
        }}
      >
        <MicButton pending={pending} open={open} big={false} recording={recording} />
      </Box>
    </Tooltip>
  );
}

/** Mobile: draggable, and remembered where it was left. */
export function VocxFab() {
  const { open, setOpen, pending, recording } = useVocx();
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
      title={recording ? 'Recording — open VocX to stop' : 'Drag to move · double-tap to reset'}
      aria-label={recording
        ? 'VocX field intell — recording in progress. Drag to move.'
        : (pending
          ? `VocX field intell — ${pending} capture(s) awaiting approval. Drag to move.`
          : 'VocX field intell. Drag to move.')}
      aria-expanded={open}
      sx={{
        position: 'fixed', left: drag.pos.x, top: drag.pos.y, zIndex: 1260,
        background: 'none', border: 0, p: 0, cursor: drag.dragging ? 'grabbing' : 'pointer',
        opacity: drag.dragging ? 0.85 : 1,
        '&:focus-visible': { outline: `3px solid ${tokens.tealHi}`, outlineOffset: 3, borderRadius: '50%' },
      }}
    >
      <MicButton pending={pending} open={open} big recording={recording} />
    </Box>
  );
}
