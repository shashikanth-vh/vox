import { Badge, Box, Tooltip } from '@mui/material';
import MicIcon from '@mui/icons-material/Mic';
import { useVocx } from './VocxProvider';
import { vx } from './vocxStyles';
import { tokens } from '../../theme';

/**
 * The way into VocX: one control, docked in the top bar on every device.
 *
 * It rides beside the notification bell — the toolbar slot a pointer looks for, and the
 * one a thumb already knows on a phone. It sits a size up from the bell there, because
 * capture is the thing an RM reaches for in the field and it should never be the small
 * target.
 *
 * The badge counts captures recorded but never filed. Those are the easiest thing in the
 * product to lose — a preview writes nothing — so the count is on the way IN.
 */

const MOBILE = '@media (max-width:760px)';

function MicButton({ pending, open, recording, d, mobileD }:
                  { pending: number; open: boolean; recording: boolean; d: number; mobileD?: number }) {
  const icon = Math.round(d * 0.55);
  const mobileIcon = mobileD ? Math.round(mobileD * 0.55) : undefined;
  const halo = Math.round(d * 0.28);
  return (
    <Badge
      badgeContent={recording ? 0 : pending}
      max={99}
      overlap="circular"
      sx={{ '& .MuiBadge-badge': {
        bgcolor: '#F0B43C', color: '#2A1B00', fontWeight: 800, fontSize: 10, pointerEvents: 'none' } }}
    >
      {/* Decorative only — the REAL button is the parent. A role="button" here made
          the drag guard read the mic face as an interactive control and yield to it,
          which froze the FAB: pressing a floating button always starts on its face. */}
      <Box
        component="span"
        aria-hidden
        sx={{
          width: d, height: d, borderRadius: '50%',
          display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
          bgcolor: recording ? vx.live : (open ? vx.grn2 : tokens.tealHi),
          color: '#04241B',
          transition: 'background-color 180ms',
          ...(mobileD ? { [MOBILE]: { width: mobileD, height: mobileD } } : {}),
          // A HALO, not a size change: the button keeps its hit area (and the phone's
          // draggable one keeps its geometry) while an expanding ring says, from
          // anywhere in the app, that the microphone is open. Respects
          // prefers-reduced-motion — a persistent pulse is exactly what that setting is
          // for, so the colour alone carries it there.
          '@keyframes vocxHalo': {
            '0%': { boxShadow: `0 0 0 0 ${vx.liveSoft}` },
            '70%': { boxShadow: `0 0 0 ${halo}px rgba(34,211,238,0)` },
            '100%': { boxShadow: '0 0 0 0 rgba(34,211,238,0)' },
          },
          ...(recording ? { animation: 'vocxHalo 1.5s ease-out infinite' } : {}),
          '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
        }}
      >
        <MicIcon sx={{ fontSize: icon, ...(mobileIcon ? { [MOBILE]: { fontSize: mobileIcon } } : {}) }} />
      </Box>
    </Badge>
  );
}

/** A plain toolbar control, docked in the navbar on desktop and on mobile alike. */
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
        {/* 34px on a phone against the bell's ~31px control: the same family, one step up. */}
        <MicButton pending={pending} open={open} d={40} mobileD={34} recording={recording} />
      </Box>
    </Tooltip>
  );
}
