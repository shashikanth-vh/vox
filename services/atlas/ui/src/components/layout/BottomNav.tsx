import { useState } from 'react';
import { Box, Drawer } from '@mui/material';
import { useNavigate, useLocation } from 'react-router-dom';
import { NAV } from './navConfig';
import { useAuth } from '../../auth/AuthContext';
import { canSee } from '../../auth/rbac';
import { tokens } from '../../theme';

const MOBILE = '@media (max-width:760px)';

export default function BottomNav() {
  const { user } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const [moreOpen, setMoreOpen] = useState(false);

  const visible = NAV.filter((n) => canSee(user.roles, n.tab));
  const primary = visible.slice(0, 4);
  const inMore = !primary.some((n) => n.path === loc.pathname);

  const go = (path: string) => { setMoreOpen(false); nav(path); };

  const item = (icon: string, label: string, active: boolean, onClick: () => void, key: string) => (
    <Box component="button" key={key} onClick={onClick}
      sx={{ flex: 1, background: 'none', border: 'none', cursor: 'pointer', fontFamily: 'inherit',
        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '2px', p: '4px 2px',
        borderRadius: '10px', color: active ? '#fff' : '#9FB3BA' }}>
      <Box sx={{ fontSize: 19, lineHeight: 1, filter: active ? 'none' : 'grayscale(.5)', opacity: active ? 1 : 0.85,
        display: 'flex', alignItems: 'center', justifyContent: 'center', height: 22 }}>{icon}</Box>
      <Box sx={{ fontSize: 9.8, fontWeight: 700, letterSpacing: '.2px', color: active ? tokens.tealHi : 'inherit' }}>{label}</Box>
    </Box>
  );

  return (
    <>
      <Box component="nav" className="no-print" sx={{ display: 'none',
        [MOBILE]: { display: 'flex', position: 'fixed', left: 0, right: 0, bottom: 0, zIndex: 170,
          background: 'linear-gradient(180deg,#1E2F4F,#16233C)', borderTop: '1px solid rgba(255,255,255,.08)',
          px: '4px', pt: '6px', pb: 'calc(6px + env(safe-area-inset-bottom))',
          boxShadow: '0 -6px 24px rgba(6,14,26,.35)' } }}>
        {primary.map((n) => item(n.icon, n.label.split(' ')[0], loc.pathname === n.path && !moreOpen, () => go(n.path), n.path))}
        {item('⋯', 'More', moreOpen || inMore, () => setMoreOpen(true), 'more')}
      </Box>

      <Drawer anchor="bottom" open={moreOpen} onClose={() => setMoreOpen(false)}
        PaperProps={{ sx: { borderRadius: '18px 18px 0 0', bgcolor: tokens.card,
          pb: 'calc(12px + env(safe-area-inset-bottom))', px: '14px', pt: '8px', maxHeight: '60vh' } }}>
        <Box sx={{ width: 38, height: 4, borderRadius: '99px', bgcolor: '#D5DDE2', mx: 'auto', mt: '4px', mb: '10px' }} />
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '8px' }}>
          {visible.map((n) => {
            const active = loc.pathname === n.path;
            return (
              <Box component="button" key={n.path} onClick={() => go(n.path)}
                sx={{ border: `1px solid ${active ? tokens.tealHi : tokens.line}`,
                  background: active ? '#F0F8F6' : '#fff', borderRadius: '12px', p: '11px 6px',
                  fontSize: 11.6, fontWeight: 600, fontFamily: 'inherit',
                  color: active ? tokens.teal : tokens.ink, cursor: 'pointer',
                  display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '5px' }}>
                <Box sx={{ fontSize: 19 }}>{n.icon}</Box>{n.label}
              </Box>
            );
          })}
        </Box>
      </Drawer>
    </>
  );
}
