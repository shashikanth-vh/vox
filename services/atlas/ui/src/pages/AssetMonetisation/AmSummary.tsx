import { useQuery } from '@tanstack/react-query';
import { Box, Paper, Typography, Chip } from '@mui/material';
import { assetMonService } from '../../services/assetMonService';
import { fmt } from '../../utils/format';
import { tokens } from '../../theme';

// v12 vAM overlay: three tiles (Live / Closed / Dropped) plus investor-mix and
// status pill rows, above the grid.
function Tile({ label, n, sub, subColor }: { label: string; n: number; sub: string; subColor?: string }) {
  return (
    <Paper variant="outlined" sx={{ borderColor: tokens.line, px: 1.6, py: 1, flex: '1 1 160px', minWidth: 150 }}>
      <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.5px', color: tokens.muted, fontWeight: 700 }}>{label}</Typography>
      <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1 }}>
        <Typography component="b" sx={{ fontSize: 20, fontWeight: 700 }}>{n}</Typography>
        <Typography sx={{ fontSize: 11.5, color: subColor || tokens.muted, fontWeight: 600 }}>{sub}</Typography>
      </Box>
    </Paper>
  );
}

export default function AmSummary() {
  const { data: s } = useQuery({ queryKey: ['am', 'summary'], queryFn: async () => assetMonService.summary() });
  if (!s) return null;

  return (
    <Box sx={{ mb: 1.4 }}>
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mb: 1 }}>
        <Tile label="Live mandates" n={s.live.n} subColor={tokens.warn}
          sub={`₹${fmt(s.live.val, 0)} Cr · ${fmt(s.live.mw, 0)} MW`} />
        <Tile label="Closed" n={s.closed.n} subColor={tokens.ok} sub={`₹${fmt(s.closed.val, 0)} Cr`} />
        <Tile label="Dropped" n={s.dropped.n} sub={`₹${fmt(s.dropped.val, 0)} Cr`} />
      </Box>
      {s.investors.length > 0 && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.6, mb: 0.5 }}>
          <Typography sx={{ fontSize: 11.5, fontWeight: 700, color: tokens.navy, mr: 0.3 }}>Investor mix:</Typography>
          {s.investors.map(([k, v]) => <Chip key={k} size="small" variant="outlined" label={`${k}: ${v}`} />)}
        </Box>
      )}
      {s.statuses.length > 0 && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.6 }}>
          <Typography sx={{ fontSize: 11.5, fontWeight: 700, color: tokens.navy, mr: 0.3 }}>Status:</Typography>
          {s.statuses.map(([k, v]) => <Chip key={k} size="small" variant="outlined" label={`${k}: ${v}`} />)}
        </Box>
      )}
    </Box>
  );
}
