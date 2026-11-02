import { useMemo, useState } from 'react';
import { Alert, Autocomplete, Box, Button, Checkbox, Chip, FormControlLabel, Paper, Snackbar, Stack,
  TextField, Typography } from '@mui/material';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../../auth/AuthContext';
import { entitiesService } from '../../services/entitiesService';
import { mailboxService, type QueueItem } from '../../services/mailboxService';
import { tokens } from '../../theme';

type Pick = { entityId: string; label: string };

const day = (iso: string) => (iso ? iso.slice(0, 10) : '');

/**
 * Tools → Mail review queue: conversations the reader could not place on a company
 * with confidence. A person picks the company (the reader's best guess is offered
 * first), optionally teaches the Register the outside domain so the next one files
 * itself, or ignores the conversation for good. Each person sees their own
 * mailbox's items; Admin and Management see everyone's.
 */
export default function MailQueue() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const oversees = (user.roles as string[]).some((r) => r === 'Admin' || r === 'Management');
  const [picks, setPicks] = useState<Record<string, Pick | null>>({});
  const [remember, setRemember] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<{ sev: 'success' | 'error'; msg: string } | null>(null);

  const { data: queue, isLoading, error } = useQuery({
    queryKey: ['mailbox', 'queue'],
    queryFn: async () => { const r = await mailboxService.queue(); if (!r.ok) throw new Error(r.error); return r.data!.items; },
    refetchInterval: 60_000,
  });
  const { data: companies = [] } = useQuery({
    queryKey: ['mailbox', 'companies'],
    queryFn: async () => (await entitiesService.list()).filter((c) => c.entityId)
      .map((c) => ({ entityId: c.entityId as string, label: c.name })),
    staleTime: 5 * 60_000,
  });
  const byId = useMemo(() => new Map(companies.map((c) => [c.entityId, c])), [companies]);

  const pickFor = (it: QueueItem): Pick | null => {
    if (it.key in picks) return picks[it.key];
    return it.suggested ? (byId.get(it.suggested.entity_id) || { entityId: it.suggested.entity_id, label: it.suggested.name }) : null;
  };

  const attach = async (it: QueueItem) => {
    const p = pickFor(it);
    if (!p) return;
    setBusy(it.key);
    const r = await mailboxService.attach(it.key, p.entityId, remember[it.key] ?? true);
    setBusy(null);
    if (!r.ok) { setToast({ sev: 'error', msg: r.error || 'Could not file it.' }); return; }
    setToast({ sev: 'success', msg: `Filed on ${p.label} as an interaction on its ${String(r.data?.subject_type || 'company').toLowerCase()}.` });
    void qc.invalidateQueries({ queryKey: ['mailbox', 'queue'] });
  };

  const ignore = async (it: QueueItem) => {
    setBusy(it.key);
    const r = await mailboxService.ignore(it.key);
    setBusy(null);
    if (!r.ok) { setToast({ sev: 'error', msg: r.error || 'Could not ignore it.' }); return; }
    void qc.invalidateQueries({ queryKey: ['mailbox', 'queue'] });
  };

  const items = queue || [];
  return (
    <Box>
      <Typography sx={{ fontSize: 13, color: tokens.muted, mb: 1 }}>
        Conversations from connected mailboxes that could not be placed on a company. Pick the company and file it;
        with <b>remember the domain</b> ticked, the next mail from that address files by itself.
        {oversees ? ' You see every mailbox’s queue.' : ' You see your own mailbox’s queue.'}
      </Typography>
      {error && <Alert severity="error" sx={{ fontSize: 12.5, mb: 1 }}>{String((error as Error).message)}</Alert>}
      {!isLoading && items.length === 0 && (
        <Typography sx={{ fontSize: 13, color: tokens.muted }}>Nothing waiting. Conversations the reader can place go straight to the timelines.</Typography>
      )}
      <Stack spacing={1}>
        {items.map((it) => {
          const p = pickFor(it);
          return (
            <Paper key={it.key} variant="outlined" sx={{ p: 1.5, borderRadius: 2 }}>
              <Stack direction="row" spacing={1} alignItems="baseline" flexWrap="wrap">
                <Typography sx={{ fontSize: 14, fontWeight: 600 }}>{it.subject}</Typography>
                <Typography sx={{ fontSize: 12, color: tokens.muted }}>
                  {it.messages} message{it.messages === 1 ? '' : 's'} · {day(it.first_at)}{it.first_at.slice(0, 10) !== it.last_at.slice(0, 10) ? ` to ${day(it.last_at)}` : ''}
                  {oversees ? ` · ${it.mailbox}` : ''}
                </Typography>
              </Stack>
              <Typography sx={{ fontSize: 12.5, color: tokens.muted, mt: 0.25 }}>
                With {it.parties.map((x) => x.name ? `${x.name} <${x.email}>` : x.email).join(', ') || 'an outside party'}
              </Typography>
              <Typography sx={{ fontSize: 12.75, mt: 0.75, fontStyle: 'italic' }}>“{it.excerpt}”</Typography>
              {it.suggested && (
                <Chip size="small" sx={{ mt: 0.75 }} variant="outlined" color="primary"
                  label={`Reader's guess: ${it.suggested.name} (${Math.round(it.suggested.confidence * 100)}%, ${it.suggested.how})`} />
              )}
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" sx={{ mt: 1 }}>
                <Autocomplete size="small" sx={{ minWidth: 280, flex: 1 }} options={companies}
                  value={p} isOptionEqualToValue={(a, b) => a.entityId === b.entityId}
                  onChange={(_, v) => setPicks((s) => ({ ...s, [it.key]: v }))}
                  renderInput={(params) => <TextField {...params} label="Company" id={`mailq-company-${it.key}`} />} />
                <FormControlLabel sx={{ mr: 0 }} label={<span style={{ fontSize: 12.5 }}>Remember the domain</span>}
                  control={<Checkbox size="small" id={`mailq-remember-${it.key}`} checked={remember[it.key] ?? true}
                    onChange={(e) => setRemember((s) => ({ ...s, [it.key]: e.target.checked }))} />} />
                <Button size="small" variant="contained" disabled={!p || busy === it.key} onClick={() => attach(it)}>
                  {busy === it.key ? 'Filing…' : 'File on this company'}
                </Button>
                <Button size="small" color="inherit" disabled={busy === it.key} onClick={() => ignore(it)}>Ignore</Button>
              </Stack>
            </Paper>
          );
        })}
      </Stack>
      <Snackbar open={!!toast} autoHideDuration={5000} onClose={() => setToast(null)} anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity={toast?.sev || 'success'} onClose={() => setToast(null)} sx={{ fontSize: 12.5 }}>{toast?.msg}</Alert>
      </Snackbar>
    </Box>
  );
}
