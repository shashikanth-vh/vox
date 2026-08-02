import { Box, Typography, Card, CardActionArea } from '@mui/material';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../auth/AuthContext';
import { canSee } from '../../auth/rbac';
import { tokens } from '../../theme';

const TILES = [
  { tab: 'dash', path: '/dashboard', ic: '◫', bg: tokens.navy, title: 'Dashboard', sub: 'Book at a glance' },
  { tab: 'leads', path: '/leads', ic: '☎', bg: tokens.teal, title: 'Leads', sub: 'Top of the funnel' },
  { tab: 'deals', path: '/deals', ic: '⚑', bg: '#E58A2E', title: 'Deals', sub: 'Client relationships' },
  { tab: 'lend', path: '/lending', ic: '₹', bg: tokens.lend, title: 'Lending', sub: 'Own-book facilities' },
  { tab: 'syn', path: '/syndication', ic: '⇄', bg: tokens.synd, title: 'Platform Deals', sub: 'Lender register' },
  { tab: 'am', path: '/asset-monetisation', ic: '▦', bg: tokens.am, title: 'Asset Monetisation', sub: 'Assets on mandate' },
];

export default function HomePage() {
  const nav = useNavigate();
  const { user } = useAuth();
  const tiles = TILES.filter((t) => canSee(user.roles, t.tab));
  return (
    <Box sx={{ maxWidth: 960, mx: 'auto', py: 4 }}>
      <Typography sx={{ fontSize: 21, color: tokens.navy, fontWeight: 700 }}>Welcome to ATLAS</Typography>
      <Typography sx={{ color: tokens.muted, mb: 3, fontSize: 13 }}>Pick a register. Everything is keyed on Group Code.</Typography>
      <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(240px,1fr))', gap: 2 }}>
        {tiles.map((t) => (
          <Card key={t.tab} variant="outlined">
            <CardActionArea sx={{ p: 2.5 }} onClick={() => nav(t.path)}>
              <Box sx={{ width: 40, height: 40, borderRadius: 2, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 19, color: '#fff', bgcolor: t.bg, mb: 1.5 }}>{t.ic}</Box>
              <Typography sx={{ fontSize: 15.5, color: tokens.navy, fontWeight: 700 }}>{t.title}</Typography>
              <Typography sx={{ color: tokens.muted, fontSize: 12.3 }}>{t.sub}</Typography>
            </CardActionArea>
          </Card>
        ))}
      </Box>
    </Box>
  );
}
