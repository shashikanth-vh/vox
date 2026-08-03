import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Box, IconButton, Paper, Tab, Tabs, Tooltip, Typography } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import MinimizeIcon from '@mui/icons-material/Minimize';
import OpenInFullIcon from '@mui/icons-material/OpenInFull';
import DragIndicatorIcon from '@mui/icons-material/DragIndicator';
import { useDraggable } from './useDraggable';
import RecordTab from './RecordTab';
import ReportsTab from './ReportsTab';
import { tokens } from '../../theme';

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

const PANEL_W = 420;
const PANEL_H = 620;
const MOBILE_MAX = 760;

export default function VocxPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [tab, setTab] = useState(0);
  const [rolled, setRolled] = useState(false);
  const [mobile, setMobile] = useState(
    () => typeof window !== 'undefined' && window.innerWidth <= MOBILE_MAX);
  /** Bumped to make the reports list refetch after a capture is filed. */
  const [reportsEpoch, setReportsEpoch] = useState(0);
  const paperRef = useRef<HTMLDivElement | null>(null);

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
      if (e.key !== 'Escape') return;
      if (paperRef.current?.contains(document.activeElement)) { e.stopPropagation(); onClose(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const placement = useMemo(() => (mobile
    ? { left: 0, right: 0, bottom: 0, top: 'auto' as const,
        width: '100%', maxHeight: '85vh', borderRadius: '14px 14px 0 0' }
    : { left: drag.pos.x, top: drag.pos.y, width: PANEL_W, borderRadius: '12px' }
  ), [mobile, drag.pos.x, drag.pos.y]);

  if (!open) return null;

  return (
    <Paper
      ref={paperRef}
      elevation={12}
      role="region"
      aria-label="VocX field intel capture"
      sx={{
        position: 'fixed',
        ...placement,
        // Above the app chrome, below MUI's own modals (1300) so a dialog opened from
        // inside the panel still lands on top of it.
        zIndex: 1250,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        bgcolor: '#0E1B2C',
        color: '#E8EEF2',
        border: `1px solid ${tokens.line}`,
        // No transition on position: a dragged panel must track the pointer exactly.
        boxShadow: '0 18px 48px rgba(0,0,0,.45)',
      }}
    >
      {/* Header — the drag handle on desktop, a plain title bar on a phone. */}
      <Box
        {...(mobile ? {} : drag.handleProps)}
        sx={{
          display: 'flex', alignItems: 'center', gap: 0.5, px: 1, py: 0.75,
          bgcolor: '#132539', borderBottom: `1px solid ${tokens.line}`,
          cursor: mobile ? 'default' : (drag.dragging ? 'grabbing' : 'grab'),
          userSelect: 'none', flexShrink: 0,
        }}
      >
        {!mobile && <DragIndicatorIcon sx={{ fontSize: 18, color: 'rgba(232,238,242,.45)' }} />}
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography sx={{ fontSize: 13, fontWeight: 800, letterSpacing: '.5px', lineHeight: 1.1 }}>
            VOM
          </Typography>
          <Typography sx={{ fontSize: 9.5, color: 'rgba(232,238,242,.55)', letterSpacing: '1px' }}>
            EVAM · FIELD INTEL
          </Typography>
        </Box>
        {!mobile && (
          <Tooltip title={rolled ? 'Expand' : 'Roll up'}>
            <IconButton size="small" onClick={() => setRolled((r) => !r)}
              aria-label={rolled ? 'Expand VocX' : 'Roll up VocX'}
              sx={{ color: 'rgba(232,238,242,.7)' }}>
              {rolled ? <OpenInFullIcon sx={{ fontSize: 16 }} />
                      : <MinimizeIcon sx={{ fontSize: 16 }} />}
            </IconButton>
          </Tooltip>
        )}
        <Tooltip title="Close">
          <IconButton size="small" onClick={onClose} aria-label="Close VocX"
            sx={{ color: 'rgba(232,238,242,.7)' }}>
            <CloseIcon sx={{ fontSize: 18 }} />
          </IconButton>
        </Tooltip>
      </Box>

      {!rolled && (
        <>
          <Tabs
            value={tab}
            onChange={(_, v) => setTab(v)}
            variant="fullWidth"
            sx={{
              minHeight: 38, flexShrink: 0,
              borderBottom: `1px solid ${tokens.line}`,
              '& .MuiTab-root': {
                minHeight: 38, fontSize: 12.5, textTransform: 'none', fontWeight: 700,
                color: 'rgba(232,238,242,.6)',
              },
              '& .Mui-selected': { color: `${tokens.tealHi} !important` },
              '& .MuiTabs-indicator': { backgroundColor: tokens.tealHi },
            }}
          >
            <Tab label="Record" id="vocx-tab-record" aria-controls="vocx-panel-record" />
            <Tab label="Reports" id="vocx-tab-reports" aria-controls="vocx-panel-reports" />
          </Tabs>

          <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto',
                     maxHeight: mobile ? 'calc(85vh - 88px)' : PANEL_H - 88 }}>
            {tab === 0 ? (
              <Box role="tabpanel" id="vocx-panel-record" aria-labelledby="vocx-tab-record">
                <RecordTab onFiled={() => { setReportsEpoch((n) => n + 1); setTab(1); }} />
              </Box>
            ) : (
              <Box role="tabpanel" id="vocx-panel-reports" aria-labelledby="vocx-tab-reports">
                <ReportsTab epoch={reportsEpoch} />
              </Box>
            )}
          </Box>
        </>
      )}
    </Paper>
  );
}
