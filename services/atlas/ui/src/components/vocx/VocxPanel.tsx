import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Box, IconButton, Paper, Tab, Tabs, Tooltip, Typography } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import MinimizeIcon from '@mui/icons-material/Minimize';
import OpenInFullIcon from '@mui/icons-material/OpenInFull';
import DragIndicatorIcon from '@mui/icons-material/DragIndicator';
import FiberManualRecordIcon from '@mui/icons-material/FiberManualRecord';
import { useDraggable } from './useDraggable';
import VoxApp from './vox/VoxApp';
import { useVocx } from './VocxProvider';
import { vx } from './vocxStyles';

/**
 * The VocX capture panel — FLOATING, not modal.
 *
 * The distinction is the whole point. A modal drawer with a backdrop makes the rest of
 * ATLAS unusable while it is open, so an RM who wants to check the lending line they are
 * describing has to close their capture to do it. This is a plain positioned surface:
 * the grid behind it scrolls, filters, and opens rows exactly as it would with the panel
 * shut, and the panel can be dragged out of the way by its header or rolled up to its
 * title bar while a thought is checked.
 *
 * On a phone it docks to the bottom instead, full width — a 360px-wide window that can be
 * dragged off the edge of a 390px screen is a worse answer than a sheet.
 */

const PANEL_W = 468;
const PANEL_H = 680;
const MOBILE_MAX = 760;

export default function VocxPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [rolled, setRolled] = useState(false);
  const [mobile, setMobile] = useState(
    () => typeof window !== 'undefined' && window.innerWidth <= MOBILE_MAX);
  const paperRef = useRef<HTMLDivElement | null>(null);
  const { recording } = useVocx();

  useEffect(() => {
    const onResize = () => setMobile(window.innerWidth <= MOBILE_MAX);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const size = useCallback(() => ({ w: PANEL_W, h: rolled ? 48 : PANEL_H }), [rolled]);
  const initial = useCallback(
    (vw: number, vh: number) => ({ x: vw - PANEL_W - 24, y: Math.max(24, vh - PANEL_H - 24) }),
    []);
  const drag = useDraggable({
    storageKey: 'atlas.vocx.panel', initial, size, margin: 8, enabled: !mobile,
  });

  // Escape closes, as it would for a dialog — but only when the panel itself holds focus,
  // since the page behind is live and Escape belongs to whatever the user is working in.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || recording) return;      // a live mic is not dismissable
      if (paperRef.current?.contains(document.activeElement)) { e.stopPropagation(); onClose(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose, recording]);

  // On a phone the panel IS the screen (field feedback: the 85vh sheet left a sliver
  // of dashboard peeking through that was only good for mis-taps) — except when rolled
  // up, where it returns to a bottom-docked title bar so the page is usable behind it.
  // On a laptop it stays the floating, draggable surface it has always been.
  const placement = useMemo(() => (mobile
    ? (rolled
        // Docked ABOVE the bottom nav (its ~54px + the phone's safe-area inset), not
        // over it — a minimised panel that buries Today/Dashboard/Leads/Deals would
        // make the rest of the app unreachable, which is the opposite of minimising.
        ? { left: 0, right: 0, top: 'auto' as const,
            bottom: 'calc(56px + env(safe-area-inset-bottom))',
            width: '100%', borderRadius: '14px 14px 0 0' }
        : { left: 0, right: 0, top: 0, bottom: 0,
            width: '100%', height: '100%', borderRadius: 0 })
    : { left: drag.pos.x, top: drag.pos.y, width: PANEL_W, borderRadius: '12px' }
  ), [mobile, rolled, drag.pos.x, drag.pos.y]);

  if (!open) return null;

  return (
    <Paper
      ref={paperRef}
      elevation={12}
      role="region"
      aria-label="VocX field intelligence capture"
      sx={{
        position: 'fixed',
        ...placement,
        // Above the app chrome, below MUI's own modals (1300) so a dialog opened from
        // inside the panel still lands on top of it.
        zIndex: 1250,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        bgcolor: vx.bg,
        color: vx.ink,
        border: `1px solid ${vx.line}`,
        // No transition on position: a dragged panel must track the pointer exactly.
        boxShadow: '0 18px 48px rgba(0,0,0,.45)',
      }}
    >
      {/* Header — the drag handle on desktop, a plain title bar on a phone. */}
      <Box
        {...(mobile ? {} : drag.handleProps)}
        sx={{
          display: 'flex', alignItems: 'center', gap: 0.5, px: 1, py: 0.75,
          bgcolor: vx.card, borderBottom: `1px solid ${vx.line}`,
          cursor: mobile ? 'default' : (drag.dragging ? 'grabbing' : 'grab'),
          userSelect: 'none', flexShrink: 0,
        }}
      >
        {!mobile && <DragIndicatorIcon sx={{ fontSize: 18, color: vx.mut }} />}
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography sx={{ fontSize: 17, fontWeight: 700, letterSpacing: '.02em', lineHeight: 1.05 }}>
            VOCX
          </Typography>
          {/* Spelled out: "FIELD INTELL" read as a word cut in half. nowrap keeps the
              full phrase off the title line. */}
          <Typography sx={{ fontSize: 9.5, color: vx.mut, letterSpacing: '.18em',
            whiteSpace: 'nowrap' }}>
            EVAM · FIELD INTELLIGENCE
          </Typography>
        </Box>
        {recording && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.4, mr: 0.5,
                     '@keyframes vocxBlink': { '0%,100%': { opacity: 1 }, '50%': { opacity: 0.25 } } }}>
            <FiberManualRecordIcon sx={{ fontSize: 12, color: vx.live,
                                         animation: 'vocxBlink 1.1s ease-in-out infinite' }} />
            <Typography sx={{ fontSize: 10.5, fontWeight: 700, color: vx.live,
                              letterSpacing: '.08em' }}>REC</Typography>
          </Box>
        )}
        {/* Roll up is always available — including mid-take, which is the point: the
            panel gets out of the way without the recorder being unmounted. On a phone
            the sheet is the whole surface, so it rolls up there too. */}
        <Tooltip title={rolled ? 'Expand' : 'Minimise'}>
          <IconButton size="small" onClick={() => setRolled((r) => !r)}
            aria-label={rolled ? 'Expand VocX' : 'Minimise VocX'}
            sx={{ color: vx.mut, '&:hover': { color: vx.ink } }}>
            {rolled ? <OpenInFullIcon sx={{ fontSize: 16 }} />
                    : <MinimizeIcon sx={{ fontSize: 16 }} />}
          </IconButton>
        </Tooltip>
        {/* Closing unmounts the recorder, and an unmounted recorder loses the take —
            so while the microphone is live the only ways out are Stop and Minimise. */}
        <Tooltip title={recording ? 'Recording — stop first, or minimise' : 'Close'}>
          <Box component="span">
            <IconButton size="small" onClick={onClose} disabled={recording}
              aria-label={recording ? 'Cannot close while recording' : 'Close VocX'}
              sx={{ color: vx.mut, '&:hover': { color: vx.ink },
                    '&.Mui-disabled': { color: 'rgba(232,238,242,.25)' } }}>
              <CloseIcon sx={{ fontSize: 18 }} />
            </IconButton>
          </Box>
        </Tooltip>
      </Box>

      {/* The body is the VOX blueprint app itself — screens, bottom tabs and all.
          Hidden (not unmounted) while rolled up, so a live recorder survives. */}
      <Box sx={{ display: rolled ? 'none' : 'flex', flexDirection: 'column',
                 flex: 1, minHeight: 0, minWidth: 0,
                 maxHeight: mobile ? 'none' : PANEL_H - 48 }}>
        <VoxApp />
      </Box>
    </Paper>
  );
}
