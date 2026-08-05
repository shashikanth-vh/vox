import { useEffect, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, TextField, Alert, CircularProgress,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import SendIcon from '@mui/icons-material/Send';
import { workflowActionsService, type WorkflowAction } from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * DISBURSE — the desk's single verb once the Conditions Precedent are approved.
 *
 * It shows exactly what travels: the proposed drawdown and every CP condition NOT met
 * (waived / deferred / outstanding), read live from the approved checklist. Send stages
 * the line if needed, files the request package with those conditions in its note, and
 * marks it SENT to the disbursement partner — Advaya today; the flow is deliberately
 * generic so PRISM's own arm can take this seat later. The partner's answers come back
 * through "Disbursement Update", phase by phase (T1, T2, …).
 */
export default function DisburseDialog({ action, onClose, onDone }: {
  action: WorkflowAction | null;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const open = !!action;
  const lendingId = String(action?.body?.lending_id || '');

  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [unmet, setUnmet] = useState<string[]>([]);
  const [amount, setAmount] = useState('');
  const [date, setDate] = useState('');
  const [recipient, setRecipient] = useState('Advaya (disbursement partner)');
  const [note, setNote] = useState('');

  useEffect(() => {
    if (!open || !lendingId) return;
    setErr(''); setBusy(false); setNote('');
    setRecipient('Advaya (disbursement partner)');
    setLoading(true);
    void (async () => {
      try {
        const { api } = await import('../../api/http');
        const line = await api.get<any>(`/lending/${lendingId}`);
        setAmount(String(line.proposed_disbursement_amount ?? line.amount_cr ?? ''));
        setDate(String(line.proposed_disbursement_date
          || new Date().toISOString().slice(0, 10)));
        const raw = await api.get<any>('/internal/cpcs-checklists',
          { lending_id: lendingId }).catch(() => []);
        const lists: any[] = Array.isArray(raw) ? raw : (raw?.items ?? []);
        const approved = lists.filter((l) => l.status === 'Approved').pop();
        setUnmet(((approved?.items || []) as any[])
          .filter((i) => i.condition_type === 'CP' && String(i.status) !== 'Completed')
          .map((i) => `${i.label || i.key} — ${i.status}`));
      } catch (e: any) { setErr(e?.message || String(e)); }
      setLoading(false);
    })();
  }, [open, lendingId]);

  const send = async () => {
    if (!action) return;
    if (!amount || !(Number(amount) > 0) || !date) {
      setErr('Enter the proposed drawdown amount and date — they travel with the request.');
      return;
    }
    setErr(''); setBusy(true);
    const r = await workflowActionsService.run({ ...action, form: [] }, {
      proposed_amount: Number(amount), proposed_date: date,
      recipient: recipient.trim() || 'Disbursement partner',
      ...(note.trim() ? { note: note.trim() } : {}),
    });
    setBusy(false);
    if (!r.ok) { setErr(r.error || 'The disbursement request was not sent.'); return; }
    onDone(`Disbursement request sent to ${recipient.trim() || 'the partner'}`
      + (unmet.length ? ` — ${unmet.length} unmet CP condition(s) travel with it.` : '.')
      + ' Record each confirmation with "Disbursement Update".');
    onClose();
  };

  if (!action) return null;

  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>
        Disburse
        {loading && <CircularProgress size={13} sx={{ ml: 1, verticalAlign: 'middle' }} />}
        <IconButton onClick={onClose} disabled={busy}
          sx={{ position: 'absolute', right: 8, top: 6 }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {err && <Alert severity="warning" sx={{ mb: 1.2, py: 0, fontSize: 12 }}
          onClose={() => setErr('')}>{err}</Alert>}
        <Typography sx={{ fontSize: 12.5, color: tokens.muted, mb: 1.2 }}>
          Sends the disbursement request with the proposed drawdown. The conditions below
          travel with it, spelled out — collection continues in parallel while the money
          moves. Confirmation is manual: record each phase with <b>Disbursement Update</b>.
        </Typography>

        <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1.2, mb: 1.2 }}>
          <Typography sx={{ fontSize: 12, fontWeight: 700, mb: 0.4 }}>
            {unmet.length
              ? `CP conditions NOT met (${unmet.length}) — travelling with the request`
              : 'Every CP condition is met — the request goes clean'}
          </Typography>
          {unmet.map((u) => (
            <Typography key={u} sx={{ fontSize: 12, py: 0.2 }}>• {u}</Typography>
          ))}
        </Box>

        <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1 }}>
          <TextField size="small" type="number" label="Proposed drawdown ₹ Cr" required
            value={amount} onChange={(e) => setAmount(e.target.value)} />
          <TextField size="small" type="date" label="Proposed drawdown date" required
            InputLabelProps={{ shrink: true }}
            value={date} onChange={(e) => setDate(e.target.value)} />
        </Box>
        <TextField fullWidth size="small" sx={{ mt: 1 }} label="Send to"
          value={recipient} onChange={(e) => setRecipient(e.target.value)}
          helperText="Generic on purpose — Advaya today; the flow does not care who disburses." />
        <TextField fullWidth size="small" multiline minRows={2} sx={{ mt: 1 }}
          label="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        <Button onClick={() => void send()} variant="contained" disabled={busy || loading}
          startIcon={<SendIcon sx={{ fontSize: 15 }} />}>
          {busy ? 'Sending…' : 'Send disbursement request'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
