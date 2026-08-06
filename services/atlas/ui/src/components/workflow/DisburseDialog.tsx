import { useEffect, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, TextField, Alert, CircularProgress, MenuItem, Divider,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import SendIcon from '@mui/icons-material/Send';
import { workflowActionsService, type WorkflowAction } from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * DISBURSE — the whole disbursement journey in ONE dialog, staged by where the line
 * actually is:
 *
 *   1. REQUEST — the proposed drawdown + every CP condition NOT met, one Send. The
 *      request is a recorded intent, generic over the partner (Advaya today).
 *   2. ANSWER — the partner replies OFFLINE; the desk records accepted / rejected here,
 *      citing the partner's reference.
 *   3. TRANCHES — each confirmed phase is one "Record disbursement" (T1, T2, …) with
 *      amount, value date and UTR. A recording lands as a PENDING BOOKING: the LMS
 *      Authorizer approves it in LMS · Servicing, and THAT is what flips the line to
 *      Disbursed and opens the loan account (maker/checker at the LOS→LMS seam).
 *      Once the account is on the servicing book, later phases are recorded there.
 *
 * Reopen the dialog any time — it lands on whichever step is live.
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
  const [info, setInfo] = useState('');
  const [unmet, setUnmet] = useState<string[]>([]);
  const [pkg, setPkg] = useState<any | null>(null);
  const [sched, setSched] = useState<any | null>(null);
  // Step 1 (request)
  const [amount, setAmount] = useState('');
  const [date, setDate] = useState('');
  const [recipient, setRecipient] = useState('Advaya (disbursement partner)');
  const [note, setNote] = useState('');
  // Step 2 (answer)
  const [answer, setAnswer] = useState<'accepted' | 'rejected'>('accepted');
  const [answerRef, setAnswerRef] = useState('');
  // Step 3 (tranche)
  const [trAmount, setTrAmount] = useState('');
  const [trDate, setTrDate] = useState('');
  const [trRef, setTrRef] = useState('');

  const load = async () => {
    if (!lendingId) return;
    setLoading(true);
    try {
      const { api } = await import('../../api/http');
      const line = await api.get<any>(`/lending/${lendingId}`);
      setAmount(String(line.proposed_disbursement_amount ?? line.amount_cr ?? ''));
      setDate(String(line.proposed_disbursement_date
        || new Date().toISOString().slice(0, 10)));
      setPkg(await api.get<any>(`/lending/${lendingId}/handover-package`)
        .catch(() => null));
      const s = await api.get<any>(`/lending/${lendingId}/tranches`).catch(() => null);
      setSched(s);
      setTrDate(new Date().toISOString().slice(0, 10));
      setTrAmount(s?.remaining != null ? String(s.remaining) : '');
      const raw = await api.get<any>('/internal/cpcs-checklists',
        { lending_id: lendingId }).catch(() => []);
      const lists: any[] = Array.isArray(raw) ? raw : (raw?.items ?? []);
      const approved = lists.filter((l) => l.status === 'Approved').pop();
      setUnmet(((approved?.items || []) as any[])
        .filter((i) => i.condition_type === 'CP' && String(i.status) !== 'Completed')
        .map((i) => `${i.label || i.key} — ${i.status}`));
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLoading(false);
  };

  useEffect(() => {
    if (!open) return;
    setErr(''); setInfo(''); setBusy(false); setNote('');
    setAnswer('accepted'); setAnswerRef(''); setTrRef('');
    setRecipient('Advaya (disbursement partner)');
    void load();
  }, [open, lendingId]);  // eslint-disable-line react-hooks/exhaustive-deps

  const status: string = pkg?.status || '';
  const tranches: any[] = sched?.items || [];
  // Which step is LIVE. A rejected answer reopens the request step.
  const step: 1 | 2 | 3 =
    !pkg || status === 'Rejected' ? 1
      : status === 'Submitted' ? 2
        : 3;   // Accepted / HandedOver — record phases

  const act = async (what: () => Promise<string>) => {
    setErr(''); setInfo(''); setBusy(true);
    try { setInfo(await what()); await load(); }
    catch (e: any) {
      setErr(e?.response?.data?.error?.detail || e?.message || String(e));
    }
    setBusy(false);
  };

  const sendRequest = async () => {
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
    setInfo('Request sent — record the partner\'s answer below when it arrives.');
    await load();
  };

  const recordAnswer = () => act(async () => {
    if (answerRef.trim().length < 3) throw new Error('Cite the partner\'s reference (3+ characters).');
    const { api } = await import('../../api/http');
    await api.post(`/lending/${lendingId}/advaya-events`,
      { event: answer, reference: answerRef.trim(),
        ...(note.trim() ? { note: note.trim() } : {}) });
    return answer === 'accepted'
      ? 'Accepted — record each disbursement phase below as it happens.'
      : 'Rejected recorded — correct what was refused and send a fresh request.';
  });

  const recordTranche = () => act(async () => {
    if (trRef.trim().length < 3) throw new Error('Cite the UTR / confirmation reference (3+ characters).');
    if (!trAmount || !(Number(trAmount) > 0)) throw new Error('Enter the confirmed tranche amount.');
    const { api } = await import('../../api/http');
    await api.post(`/lending/${lendingId}/advaya-events`,
      { event: 'disbursed', reference: trRef.trim(), amount_cr: Number(trAmount),
        disbursed_on: trDate || undefined,
        ...(note.trim() ? { note: note.trim() } : {}) });
    setTrRef('');
    onDone('Tranche recorded — awaiting the LMS Management\'s booking approval '
      + '(LMS · Servicing → Accounts). The line moves to Disbursed when it is booked.');
    return 'Recorded — pending LMS booking approval.';
  });

  if (!action) return null;

  const money = (v: any) => (v == null ? '—' : `₹ ${Number(v)} Cr`);

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
        {info && <Alert severity="success" sx={{ mb: 1.2, py: 0, fontSize: 12 }}
          onClose={() => setInfo('')}>{info}</Alert>}
        {status === 'Rejected' && (
          <Alert severity="warning" sx={{ mb: 1.2, py: 0, fontSize: 12 }}>
            The partner REJECTED the previous request{pkg?.note ? ` — “${pkg.note}”` : ''}.
            Correct what was refused and send again.
          </Alert>
        )}

        {/* ---- step 1: send the request --------------------------------------------- */}
        {step === 1 && (
          <>
            <Typography sx={{ fontSize: 12.5, color: tokens.muted, mb: 1.2 }}>
              Sends the disbursement request for the proposed drawdown to the
              disbursement partner. Conditions not yet completed are disclosed in
              the request — waived and deferred items with their decisions — and
              their collection continues in parallel.
            </Typography>
            <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1.2, mb: 1.2 }}>
              <Typography sx={{ fontSize: 12, fontWeight: 700, mb: 0.4 }}>
                {unmet.length
                  ? `Conditions disclosed in the request (${unmet.length})`
                  : 'Every CP condition is completed — the request goes clean'}
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
              value={recipient} onChange={(e) => setRecipient(e.target.value)} />
          </>
        )}

        {/* ---- request summary, once one exists ------------------------------------- */}
        {step !== 1 && (
          <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1.2, mb: 1.2 }}>
            <Typography sx={{ fontSize: 12.5 }}>
              Request sent to <b>{pkg?.recipient || 'the disbursement partner'}</b> —
              status <b>{status === 'HandedOver' ? 'Accepted' : status}</b>
              {pkg?.advaya_reference ? <> · ref {pkg.advaya_reference}</> : null}
            </Typography>
          </Box>
        )}

        {/* ---- step 2: record the partner's answer ---------------------------------- */}
        {step === 2 && (
          <>
            <Typography sx={{ fontSize: 12.5, color: tokens.muted, mb: 0.8 }}>
              The partner confirms OFFLINE — record their answer here, citing their
              reference. Acceptance opens the tranche recorder.
            </Typography>
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
              <TextField select size="small" label="Answer" value={answer}
                onChange={(e) => setAnswer(e.target.value as 'accepted' | 'rejected')}
                sx={{ width: 140 }}>
                <MenuItem value="accepted">Accepted</MenuItem>
                <MenuItem value="rejected">Rejected</MenuItem>
              </TextField>
              <TextField size="small" label="Partner reference" required sx={{ flex: 1, minWidth: 180 }}
                placeholder="Acknowledgement / letter no."
                value={answerRef} onChange={(e) => setAnswerRef(e.target.value)} />
              <Button variant="contained" size="small" disabled={busy}
                onClick={() => void recordAnswer()} sx={{ textTransform: 'none' }}>
                {busy ? 'Recording…' : 'Record answer'}
              </Button>
            </Box>
          </>
        )}

        {/* ---- step 3: record disbursement phases ----------------------------------- */}
        {step === 3 && (
          <>
            <Typography sx={{ fontSize: 12, fontWeight: 700, mb: 0.4 }}>
              Disbursement phases
            </Typography>
            {tranches.length === 0 && (
              <Typography sx={{ fontSize: 12, color: tokens.muted, mb: 0.6 }}>
                None yet — record T1 when the partner confirms the money moved.
              </Typography>
            )}
            {tranches.map((t) => (
              <Box key={t.id} sx={{ display: 'flex', gap: 1, alignItems: 'baseline',
                py: 0.3, borderBottom: `1px dashed ${tokens.line}` }}>
                <Typography sx={{ fontSize: 12, fontWeight: 700, minWidth: 26 }}>{t.tranche_no || '—'}</Typography>
                <Typography sx={{ fontSize: 12.5 }}>{money(t.amount)}</Typography>
                <Typography sx={{ fontSize: 11.5, color: tokens.muted, flex: 1 }}>
                  {[t.disbursed_on, t.advaya_reference || t.tranche_ref].filter(Boolean).join(' · ')}
                </Typography>
                {t.booking_status === 'Pending' && (
                  <Typography sx={{ fontSize: 10.5, fontWeight: 700, px: 0.7, borderRadius: 1,
                    bgcolor: '#FFF3CD', color: '#7A5C00' }}>Pending approval</Typography>
                )}
                {t.booking_status === 'Rejected' && (
                  <Typography sx={{ fontSize: 10.5, fontWeight: 700, px: 0.7, borderRadius: 1,
                    bgcolor: '#FDE8E4', color: '#7C4A3E' }}
                    title={t.booking_note || ''}>Rejected</Typography>
                )}
              </Box>
            ))}
            {sched && tranches.length > 0 && (
              <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 0.6 }}>
                Booked <b>{money(sched.total_disbursed)}</b>
                {(sched.total_pending ?? 0) > 0 && <> · pending approval {money(sched.total_pending)}</>}
                {sched.ceiling != null && <> of {money(sched.ceiling)}
                  {sched.fully_disbursed ? ' — fully disbursed' : ` · remaining ${money(sched.remaining)}`}</>}
              </Typography>
            )}
            {tranches.some((t: any) => t.booking_status === 'Booked') && !sched?.fully_disbursed && (
              <Alert severity="info" sx={{ mt: 1, py: 0.2, fontSize: 12 }}>
                This line is on the servicing book — record further tranches in
                LMS · Servicing → Accounts (open the account, Record disbursement tranche).
              </Alert>
            )}
            {!sched?.fully_disbursed && !tranches.some((t: any) => t.booking_status === 'Booked') && (
              <>
                <Divider sx={{ my: 1.2 }} />
                <Typography sx={{ fontSize: 12.5, color: tokens.muted, mb: 0.8 }}>
                  Record T{tranches.length + 1} from the partner's manual confirmation —
                  it lands as a pending booking; the LMS Management's approval moves the
                  money onto the book.
                </Typography>
                <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                  <TextField size="small" type="number" label="Amount ₹ Cr" required
                    value={trAmount} onChange={(e) => setTrAmount(e.target.value)}
                    sx={{ width: 130 }} />
                  <TextField size="small" type="date" label="Value date"
                    InputLabelProps={{ shrink: true }}
                    value={trDate} onChange={(e) => setTrDate(e.target.value)}
                    sx={{ width: 160 }} />
                  <TextField size="small" label="UTR / reference" required
                    sx={{ flex: 1, minWidth: 160 }}
                    value={trRef} onChange={(e) => setTrRef(e.target.value)} />
                  <Button variant="contained" size="small" disabled={busy}
                    onClick={() => void recordTranche()} sx={{ textTransform: 'none' }}>
                    {busy ? 'Recording…' : `Record disbursement (T${tranches.length + 1})`}
                  </Button>
                </Box>
              </>
            )}
          </>
        )}

        <TextField fullWidth size="small" multiline minRows={2} sx={{ mt: 1.2 }}
          label="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Close</Button>
        {step === 1 && (
          <Button onClick={() => void sendRequest()} variant="contained"
            disabled={busy || loading}
            startIcon={<SendIcon sx={{ fontSize: 15 }} />}>
            {busy ? 'Sending…' : 'Send disbursement request'}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
