import { Card, CardContent, Typography } from '@mui/material';
import { tokens } from '../../theme';

export default function StatCard({ label, value, sub, accent }: {
  label: string; value: React.ReactNode; sub?: string; accent?: boolean;
}) {
  return (
    <Card variant="outlined" sx={{ borderTop: accent ? `3px solid ${tokens.teal}` : undefined, height: '100%' }}>
      <CardContent sx={{ p: '12px 14px !important' }}>
        <Typography sx={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.5px', fontWeight: 600, color: tokens.muted }}>{label}</Typography>
        <Typography sx={{ fontSize: 21, fontWeight: 700, mt: 0.4, fontVariantNumeric: 'tabular-nums' }}>{value}</Typography>
        {sub && <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 0.2 }}>{sub}</Typography>}
      </CardContent>
    </Card>
  );
}
