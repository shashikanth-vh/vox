import { useEffect, useRef, useState } from 'react';
import { InputBase, MenuItem, Menu, Divider, ListItemIcon, IconButton, Box, Typography, Stack, Badge, Button } from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import AccountCircleIcon from '@mui/icons-material/AccountCircle';
import NotificationsNoneIcon from '@mui/icons-material/NotificationsNone';
import LogoutIcon from '@mui/icons-material/Logout';
import FileDownloadIcon from '@mui/icons-material/FileDownload';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import { useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { useSearch } from '../../context/SearchContext';
import { referenceService } from '../../services/referenceService';
import { notificationsService, type InboxItem } from '../../services/notificationsService';
import { since } from '../../services/workflowService';
import { onSave } from '../../utils/saveIndicator';
import { tokens } from '../../theme';
import { VocxNavButton } from "../vocx/VocxLauncher";

const MOBILE = '@media (max-width:760px)';

export default function Navbar() {
  const { user, signOut } = useAuth();
  const { search, setSearch } = useSearch();
  const nav = useNavigate();

  // v11 save indicator: flash "✓ <action> · HH:MM" then fade out after 2.5s.
  const [ind, setInd] = useState({ msg: '', on: false });
  const timer = useRef<number>();
  useEffect(() => onSave((msg) => {
    const t = new Date().toTimeString().slice(0, 5);
    setInd({ msg: `✓ ${msg} · ${t}`, on: true });
    if (timer.current) clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setInd((s) => ({ ...s, on: false })), 2500);
  }), []);

  // v17 header live-sync indicator: green dot "Online — sync ready" when connected,
  // amber "Offline copy — saved locally" otherwise.
  const [online, setOnline] = useState(typeof navigator !== 'undefined' ? navigator.onLine : true);
  useEffect(() => {
    const on = () => setOnline(true), off = () => setOnline(false);
    window.addEventListener('online', on); window.addEventListener('offline', off);
    return () => { window.removeEventListener('online', on); window.removeEventListener('offline', off); };
  }, []);

  const [menuEl, setMenuEl] = useState<null | HTMLElement>(null);
  const closeMenu = () => setMenuEl(null);
  const roleLabel = (role: string, name: string) => `${role} · ${name}`;

  // The BELL: the same inbox Today's "Decisions on your work" strip reads (shared
  // query key, so the two never disagree). The badge is the unread count, refreshed
  // every 45s; the dropdown shows each decision and clears per-item or all at once.
  const { data: inbox = { items: [] as InboxItem[], unread: 0 }, refetch: refetchInbox }
    = useQuery({
      queryKey: ['inbox', 'unread'],
      queryFn: () => notificationsService.unread(),
      refetchInterval: 45000,
      staleTime: 20000,
    });
  const [bellEl, setBellEl] = useState<null | HTMLElement>(null);
  const closeBell = () => setBellEl(null);
  const urgent = inbox.items.some((n) => n.severity !== 'info');

  // News about YOUR work means the registers around you changed — a conversion landed,
  // a committee decided, a checker returned something. Grids don't poll (deliberate,
  // for load), so the bell's own 45s heartbeat doubles as the refresh trigger: when the
  // unread count RISES, the main register queries are invalidated and any open grid
  // refetches. This is what makes a converted lead leave the Leads page by itself.
  const qc = useQueryClient();
  const prevUnread = useRef(0);
  useEffect(() => {
    if (inbox.unread > prevUnread.current) {
      ['leads', 'deals', 'lending'].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
    }
    prevUnread.current = inbox.unread;
  }, [inbox.unread, qc]);
  const readOne = async (n: InboxItem) => {
    await notificationsService.markRead(n.id);
    await refetchInbox();
  };
  const readAll = async () => {
    await Promise.all(inbox.items.map((n) => notificationsService.markRead(n.id)));
    await refetchInbox();
    closeBell();
  };

  return (
    <Box component="header" className="no-print" sx={{ position: 'sticky', top: 0, zIndex: 60, color: '#fff',
      background: 'linear-gradient(100deg,#14213C 0%,#1B2A4A 46%,#0E3A40 100%)',
      boxShadow: '0 2px 14px rgba(6,14,26,.35)',
      display: 'flex', alignItems: 'center', gap: '13px', px: '20px', py: '11px', flexWrap: 'wrap',
      [MOBILE]: { px: '12px', py: '9px', gap: '8px', pt: 'calc(9px + env(safe-area-inset-top))' } }}>
      <Stack direction="row" alignItems="baseline" sx={{ gap: '9px', cursor: 'pointer', fontWeight: 700, fontSize: 17, letterSpacing: '.8px', [MOBILE]: { fontSize: 15 } }} onClick={() => nav('/today')}>
        <Box sx={{ width: 10, height: 10, borderRadius: '50%', bgcolor: tokens.tealHi, alignSelf: 'center',
          boxShadow: '0 0 0 3px rgba(18,145,126,.28),0 0 12px rgba(18,145,126,.85)' }} />
        EVAM&nbsp;ATLAS <Typography component="small" sx={{ fontWeight: 400, fontSize: 10.5, color: '#9FB3BA', opacity: 0.85, [MOBILE]: { display: 'none' } }}>v17 · MIS-linked</Typography>
      </Stack>

      {/* v17 hdrnet — live in-sync / offline indicator. */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: 10.8, color: '#9FB3BA', whiteSpace: 'nowrap', [MOBILE]: { display: 'none' } }}>
        <Box sx={{ width: 7, height: 7, borderRadius: '50%', bgcolor: online ? '#4ADE80' : '#C98A1B', boxShadow: online ? '0 0 6px #4ADE80' : 'none' }} />
        {online ? 'Online — sync ready' : 'Offline copy — saved locally'}
      </Box>

      <Typography component="span" sx={{ fontSize: 11.6, color: '#8FE3C2', whiteSpace: 'nowrap',
        opacity: ind.on ? 1 : 0, transition: 'opacity .5s', [MOBILE]: { display: 'none' } }}>{ind.msg}</Typography>

      <Box sx={{ flex: 1, minWidth: 130, maxWidth: 300, display: 'flex', alignItems: 'center', gap: 0.75,
        bgcolor: 'rgba(11,28,44,.75)', border: '1px solid #31536B', borderRadius: '8px', px: '10px',
        '&:focus-within': { bgcolor: '#0B1C2C', borderColor: tokens.tealHi },
        [MOBILE]: { order: 5, flexBasis: '100%', maxWidth: 'none' } }}>
        <SearchIcon sx={{ fontSize: 15, color: '#7F959C' }} />
        <InputBase value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder="Search company or Group Code…"
          sx={{ color: '#fff', fontSize: 12.5, py: '4px', width: '100%', '::placeholder': { color: '#7F959C' } }} />
      </Box>

      <Stack direction="row" alignItems="center" sx={{ gap: '9px', ml: 'auto', [MOBILE]: { gap: '6px' } }}>
        {/* VocX beside the search box, where a pointer looks for a toolbar. On a phone
            the floating draggable button (mounted at the layout root) takes over, so this
            one steps aside rather than being duplicated. */}
        <Box sx={{ display: 'inline-flex', [MOBILE]: { display: 'none' } }}>
          <VocxNavButton />
        </Box>
        <IconButton onClick={(e) => setBellEl(e.currentTarget)} aria-label="Notifications"
          sx={{ color: '#C8D6E2', p: '5px',
            '&:hover': { color: '#fff', background: 'rgba(255,255,255,.07)' } }}>
          <Badge badgeContent={inbox.unread || 0} max={99}
            color={urgent ? 'warning' : 'success'}
            sx={{ '& .MuiBadge-badge': { fontSize: 9.5, height: 15, minWidth: 15 } }}>
            <NotificationsNoneIcon sx={{ fontSize: 21 }} />
          </Badge>
        </IconButton>
        <Typography sx={{ fontSize: 12.5, color: '#C8D6E2', whiteSpace: 'nowrap', [MOBILE]: { display: 'none' } }}>
          {roleLabel(user.role, user.name)}
        </Typography>
        <IconButton onClick={(e) => setMenuEl(e.currentTarget)} aria-label="Account"
          sx={{ color: '#C8D6E2', border: '1px solid #3A5273', borderRadius: '9px', p: '5px',
            '&:hover': { borderColor: tokens.tealHi, color: '#fff', background: 'rgba(255,255,255,.07)' } }}>
          <AccountCircleIcon sx={{ fontSize: 22 }} />
        </IconButton>
      </Stack>

      <Menu anchorEl={bellEl} open={!!bellEl} onClose={closeBell}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
        slotProps={{ paper: { sx: { minWidth: 320, maxWidth: 420, mt: 0.5 } } }}>
        {inbox.items.length === 0 ? (
          <Box sx={{ px: 2, py: 1.4 }}>
            <Typography sx={{ fontSize: 12.5, color: tokens.muted }}>
              Nothing unread — decisions on your work land here.
            </Typography>
          </Box>
        ) : ([
          ...inbox.items.map((n) => (
            <MenuItem key={n.id} onClick={() => void readOne(n)}
              title="Click to mark read"
              sx={{ alignItems: 'flex-start', gap: 1, whiteSpace: 'normal', py: 0.8 }}>
              <Box aria-hidden sx={{ width: 8, height: 8, borderRadius: '50%', mt: '5px',
                flexShrink: 0,
                bgcolor: n.severity === 'warning' ? '#c46a16'
                  : n.severity === 'critical' ? '#b0223a' : '#1B7F5C' }} />
              <Box sx={{ minWidth: 0 }}>
                <Typography sx={{ fontSize: 12.6, fontWeight: 700, lineHeight: 1.3 }}>
                  {n.title}
                </Typography>
                {n.body && (
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted, lineHeight: 1.35 }}>
                    {n.body}
                  </Typography>
                )}
                {n.created_at && (
                  <Typography sx={{ fontSize: 10.6, color: tokens.muted }}>
                    {since(n.created_at)}
                  </Typography>
                )}
              </Box>
            </MenuItem>
          )),
          <Divider key="bell-div" />,
          <Box key="bell-actions" sx={{ display: 'flex', px: 1.2, py: 0.5, gap: 1 }}>
            <Button size="small" onClick={() => void readAll()}
              sx={{ fontSize: 11.5, textTransform: 'none' }}>✓ Mark all read</Button>
            <Box sx={{ flex: 1 }} />
            <Button size="small" onClick={() => { closeBell(); nav('/today'); }}
              sx={{ fontSize: 11.5, textTransform: 'none' }}>Open Today</Button>
          </Box>,
        ])}
      </Menu>

      <Menu anchorEl={menuEl} open={!!menuEl} onClose={closeMenu}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
        slotProps={{ paper: { sx: { minWidth: 220, mt: 0.5 } } }}>
        {/* Identity only — the signed-in user, never a switcher. Impersonation belongs to
            Access, not to a client-side dropdown. */}
        <Box sx={{ px: 2, py: 1 }}>
          <Typography sx={{ fontWeight: 700, fontSize: 13 }}>{user.name}</Typography>
          <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>{roleLabel(user.role, user.name)}</Typography>
        </Box>
        <Divider />
        <MenuItem onClick={() => { referenceService.exportData(); closeMenu(); }} sx={{ fontSize: 12.5 }}>
          <ListItemIcon sx={{ minWidth: 30 }}><FileDownloadIcon fontSize="small" /></ListItemIcon>Export data
        </MenuItem>
        {can(user.roles, 'backupRestore') && (
          <MenuItem onClick={() => { if (confirm('Reset session data to seed?')) { referenceService.reset(); location.reload(); } }} sx={{ fontSize: 12.5 }}>
            <ListItemIcon sx={{ minWidth: 30 }}><RestartAltIcon fontSize="small" /></ListItemIcon>Reset session
          </MenuItem>
        )}
        <Divider />
        <MenuItem onClick={() => { closeMenu(); signOut(); }} sx={{ fontSize: 12.5, color: tokens.bad }}>
          <ListItemIcon sx={{ minWidth: 30 }}><LogoutIcon fontSize="small" sx={{ color: tokens.bad }} /></ListItemIcon>Sign out
        </MenuItem>
      </Menu>
    </Box>
  );
}
