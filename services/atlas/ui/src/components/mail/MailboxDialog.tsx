import { useEffect, useState } from 'react';
import { Alert, Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle, FormControl,
  InputLabel, MenuItem, Select, Stack, Typography } from '@mui/material';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../../auth/AuthContext';
import { requestGmailCode } from '../../auth/googleIdentity';
import { GOOGLE_SSO_CLIENT_ID } from '../../api/axiosClient';
import { mailboxService, when, type MailboxStatus } from '../../services/mailboxService';

/**
 * My mailbox — a person's own connection to the mailbox reader.
 *
 * Not connected: one Connect button and the choice of how far back to read. The
 * click opens Google's consent screen (read-only Gmail); the code comes back and
 * PRISM exchanges it on the server. Connected: what has been read, when, and the
 * counts; Read now; Disconnect (the token is deleted, what was filed stays).
 */
export default function MailboxDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user, session } = useAuth();
  const qc = useQueryClient();
  const [months, setMonths] = useState(12);
  const [busy, setBusy] = useState<'connect' | 'disconnect' | 'read' | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ['mailbox', 'me'],
    queryFn: async () => { const r = await mailboxService.me(); if (!r.ok) throw new Error(r.error); return r.data as MailboxStatus; },
    enabled: open,
    refetchInterval: open ? 15_000 : false,
  });

  useEffect(() => { if (!open) { setErr(null); setNote(null); } }, [open]);

  const refresh = () => void qc.invalidateQueries({ queryKey: ['mailbox'] });

  const connect = async () => {
    setErr(null); setNote(null); setBusy('connect');
    try {
      const clientId = data?.client_id || GOOGLE_SSO_CLIENT_ID;
      if (!clientId) throw new Error('Google sign-in is not configured on this server (GOOGLE_SSO_CLIENT_ID).');
      const code = await requestGmailCode(clientId, session?.email);
      const r = await mailboxService.connect(code, months, user.full);
      if (!r.ok) throw new Error(r.error);
      setNote(`Connected. Reading ${months ? `the last ${months} months` : 'from today'} in the background; the first pass takes a few minutes.`);
      refresh();
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally { setBusy(null); }
  };

  const disconnect = async () => {
    setErr(null); setBusy('disconnect');
    const r = await mailboxService.disconnect();
    if (!r.ok) setErr(r.error || 'Could not disconnect.'); else { setNote('Disconnected. Nothing more is read; what was filed stays on the timelines.'); refresh(); }
    setBusy(null);
  };

  const readNow = async () => {
    setErr(null); setBusy('read');
    const r = await mailboxService.readNow();
    if (!r.ok) setErr(r.error || 'Could not start.'); else setNote('Reading now. Counts refresh in a moment.');
    setBusy(null); refresh();
  };

  const st = data;
  const connected = !!st?.connected;
  const counts = st?.counts;
  const skipped = counts?.skipped ? Object.entries(counts.skipped).sort((a, b) => b[1] - a[1]) : [];

  return (
    <Dialog open={open} onClose={() => !busy && onClose()} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>My mailbox</DialogTitle>
      <DialogContent dividers>
        {isLoading && !st && <Typography sx={{ fontSize: 13 }}>Checking…</Typography>}
        {st && !st.enabled && (
          <Alert severity="info" sx={{ fontSize: 12.5 }}>The mailbox reader is switched off on this server.</Alert>
        )}
        {st && st.enabled && !connected && (
          <Stack spacing={1.5}>
            <Typography sx={{ fontSize: 13.5 }}>
              PRISM can read your work mailbox, <b>read-only</b>, and file each conversation with a company as an
              interaction on that company's timeline, at the mail's own date. Internal mail, newsletters and
              anything you label <b>Private</b> are never read. You can disconnect at any time.
            </Typography>
            <FormControl size="small" sx={{ maxWidth: 260 }}>
              <InputLabel id="mailbox-since">Read mail from</InputLabel>
              <Select labelId="mailbox-since" id="mailbox-since-select" label="Read mail from" value={months}
                onChange={(e) => setMonths(Number(e.target.value))}>
                <MenuItem value={0}>Today onwards</MenuItem>
                <MenuItem value={3}>The last 3 months</MenuItem>
                <MenuItem value={6}>The last 6 months</MenuItem>
                <MenuItem value={12}>The last 12 months</MenuItem>
                <MenuItem value={24}>The last 24 months</MenuItem>
              </Select>
            </FormControl>
            {!st.configured && (
              <Alert severity="warning" sx={{ fontSize: 12.5 }}>
                The server has no Google client secret yet (PULSE_GOOGLE_CLIENT_SECRET), so Connect cannot finish. Ask the Admin.
              </Alert>
            )}
          </Stack>
        )}
        {st && connected && (
          <Stack spacing={1.25}>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
              <Chip size="small" label={st.status === 'reconnect' ? 'Needs reconnect' : st.status === 'backfill' ? 'Reading history' : 'Connected'}
                color={st.status === 'reconnect' ? 'error' : st.status === 'backfill' ? 'warning' : 'success'} variant="outlined" />
              <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>{st.email} · since {st.since || 'today'} · connected {when(st.connected_at)}</Typography>
            </Stack>
            {st.error && <Alert severity="error" sx={{ fontSize: 12.5 }}>{st.error}</Alert>}
            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 1 }}>
              {[['Last read', when(st.last_run)], ['Last success', when(st.last_ok)],
                ['Conversations seen', String(counts?.seen ?? 0)], ['Filed', String(counts?.filed ?? 0)],
                ['Waiting for you', String(counts?.queued ?? 0)], ['Errors', String(counts?.errors ?? 0)]].map(([k, v]) => (
                <Box key={k} sx={{ border: '1px solid', borderColor: 'divider', borderRadius: 1.5, p: 1 }}>
                  <Typography sx={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.04em', color: 'text.secondary' }}>{k}</Typography>
                  <Typography sx={{ fontSize: 14, fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>{v}</Typography>
                </Box>
              ))}
            </Box>
            {st.backfill && !st.backfill.done && (
              <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
                History: {st.backfill.threads} conversations listed over {st.backfill.pages} pages so far; a few hundred more each hour.
              </Typography>
            )}
            {skipped.length > 0 && (
              <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
                Left out: {skipped.map(([k, v]) => `${k} ${v}`).join(' · ')}
              </Typography>
            )}
            {st.last_pass?.note && <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>{st.last_pass.note}</Typography>}
            {st.last_error && (
              <Typography sx={{ fontSize: 12.5, color: 'error.main' }}>
                Last error ({when(st.last_error.at)}{st.last_error.thread ? ', one conversation' : ''}): {st.last_error.error}
              </Typography>
            )}
            <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
              Conversations the reader could not place on a company wait under Tools → Mail review queue.
            </Typography>
          </Stack>
        )}
        {note && <Alert severity="success" sx={{ mt: 1.5, fontSize: 12.5 }}>{note}</Alert>}
        {err && <Alert severity="error" sx={{ mt: 1.5, fontSize: 12.5 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        {connected && <Button color="error" onClick={disconnect} disabled={!!busy}>{busy === 'disconnect' ? 'Disconnecting…' : 'Disconnect'}</Button>}
        {connected && st?.status !== 'reconnect' && <Button onClick={readNow} disabled={!!busy || !!st?.reading}>{st?.reading ? 'Reading…' : busy === 'read' ? 'Starting…' : 'Read now'}</Button>}
        <Box sx={{ flex: 1 }} />
        <Button onClick={onClose} disabled={!!busy}>Close</Button>
        {st?.enabled && (!connected || st.status === 'reconnect') && (
          <Button variant="contained" onClick={connect} disabled={!!busy || !st.configured}>
            {busy === 'connect' ? 'Waiting for Google…' : st?.status === 'reconnect' ? 'Reconnect my mailbox' : 'Connect my mailbox'}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
