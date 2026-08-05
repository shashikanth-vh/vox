import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Divider,
  IconButton, MenuItem, Table, TableBody, TableCell, TableHead, TableRow, TextField,
  Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { lmsService, type LedgerEntry, type LoanAccount, type TrancheSchedule } from '../../../services/lmsService';
import { useAuth } from '../../../auth/AuthContext';
import { can, whoCan } from '../../../auth/rbac';
import { tokens } from '../../../theme';
import type { LendingRow } from '../lending.types';

/**
 * One loan account, the way the servicing Excel reads: the facility header, the
 * statement ledger (Date | Particulars | Debit | Credit | Balance), and the verbs —
 * the OPERATOR's routine entries (computed interest via preview-then-post, EMI
 * receipts, charges) and the AUTHORIZER's classification / closure.
 */

const inr = (v?: number | null) =>
  v == null ? '—' : v.toLocaleString('en-IN', { minimumFractionDigits: 2 });

export default function AccountDialog({ row, onClose }: {
  row: LendingRow | null; onClose: () => void;
}) {
  const { user } = useAuth();
  const open = !!row;
  const operate = can(user.roles, 'lmsOperate');
  const authorize = can(user.roles, 'lmsAuthorize');

  const [acct, setAcct] = useState<LoanAccount | null>(null);
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [sched, setSched] = useState<TrancheSchedule | null>(null);
  const [missing, setMissing] = useState(false);
  const [err, setErr] = useState('');
  const [info, setInfo] = useState('');
  const [busy, setBusy] = useState('');
  // Accrual: pick a date → the register COMPUTES (preview shows the formula) → post.
  const [accrueTo, setAccrueTo] = useState('');
  const [preview, setPreview] = useState<any | null>(null);
  // Manual entry.
  const [entry, setEntry] = useState({ entry_date: '', kind: 'EMI', amount: '', particulars: '' });
  // Classification (authorizer).
  const [cls, setCls] = useState({ status: '', overdue_position: '', provisioning_amount: '', closed_on: '', note: '' });
  // The T2+ recorder (operator) — the maker's side of the booking gate.
  const [tr, setTr] = useState({ amount: '', disbursed_on: '', ref: '' });

  const load = async () => {
    if (!row) return;
    setErr('');
    try {
      setSched(await lmsService.tranches(row.id).catch(() => null));
      const out = await lmsService.account(row.id);
      if (out === null) { setMissing(true); setAcct(null); setEntries([]); return; }
      setMissing(false); setAcct(out.account); setEntries(out.entries);
    } catch (e: any) { setErr(e?.message || String(e)); }
  };

  useEffect(() => {
    if (!open) return;
    setErr(''); setInfo(''); setBusy(''); setPreview(null); setAccrueTo('');
    setEntry({ entry_date: '', kind: 'EMI', amount: '', particulars: '' });
    setCls({ status: '', overdue_position: '', provisioning_amount: '', closed_on: '', note: '' });
    setTr({ amount: '', disbursed_on: new Date().toISOString().slice(0, 10), ref: '' });
    void load();
  }, [open, row?.id]);  // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (what: string, fn: () => Promise<string>) => {
    setErr(''); setInfo(''); setBusy(what);
    try { setInfo(await fn()); await load(); }
    catch (e: any) { setErr(e?.message || String(e)); }
    setBusy('');
  };

  const doPreview = () => run('preview', async () => {
    if (!row || !accrueTo) throw new Error('Pick the date to accrue to.');
    const p = await lmsService.interestPreview(row.id, accrueTo);
    setPreview(p);
    return `Computed: ₹ ${inr(p.interest)} (${p.formula}). Review, then Post interest.`;
  });

  const doAccrue = () => run('accrue', async () => {
    if (!row || !preview) throw new Error('Preview first — the figure must be checkable.');
    const e = await lmsService.accrue(row.id, accrueTo);
    setPreview(null); setAccrueTo('');
    return `Interest row posted — balance ₹ ${inr(e.balance)}.`;
  });

  const doEntry = () => run('entry', async () => {
    if (!row) throw new Error('No line.');
    if (!entry.entry_date || !entry.amount) throw new Error('Date and amount are needed.');
    const e = await lmsService.addEntry(row.id, {
      entry_date: entry.entry_date, kind: entry.kind as any,
      amount: Number(entry.amount),
      ...(entry.particulars ? { particulars: entry.particulars } : {}),
    });
    setEntry({ entry_date: '', kind: 'EMI', amount: '', particulars: '' });
    return `${e.particulars} recorded — balance ₹ ${inr(e.balance)}.`;
  });

  const doClassify = () => run('classify', async () => {
    if (!row) throw new Error('No line.');
    const input: any = {};
    if (cls.status) input.status = cls.status;
    if (cls.overdue_position) input.overdue_position = cls.overdue_position;
    if (cls.provisioning_amount) input.provisioning_amount = Number(cls.provisioning_amount);
    if (cls.closed_on) input.closed_on = cls.closed_on;
    if (cls.note) input.note = cls.note;
    if (!Object.keys(input).length) throw new Error('Nothing to update.');
    const a = await lmsService.patchAccount(row.id, input);
    setCls({ status: '', overdue_position: '', provisioning_amount: '', closed_on: '', note: '' });
    return `Account updated — status ${a.status}.`;
  });

  const doRecordTranche = () => run('tranche', async () => {
    if (!row) throw new Error('No line.');
    if (tr.ref.trim().length < 3) throw new Error('Cite the UTR / confirmation reference (3+ characters).');
    if (!tr.amount || !(Number(tr.amount) > 0)) throw new Error('Enter the confirmed tranche amount.');
    const t = await lmsService.recordTranche(row.id, {
      tranche_ref: tr.ref.trim(), amount: Number(tr.amount),
      ...(tr.disbursed_on ? { disbursed_on: tr.disbursed_on } : {}),
    });
    setTr({ amount: '', disbursed_on: new Date().toISOString().slice(0, 10), ref: '' });
    return `${t.tranche_ref} recorded — awaiting the LMS Authorizer's booking approval; `
      + 'the account grows when it is booked.';
  });

  const closed = acct?.status === 'Closed' || !!acct?.closed_on;
  const pendingTranches = (sched?.items || []).filter((t) => t.booking_status === 'Pending');
  const nextTrancheNo = (sched?.items || []).filter((t) => t.booking_status !== 'Rejected').length + 1;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>
        Loan account {acct ? `#${acct.account_no}` : ''} — {row?._name}
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 6 }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setErr('')}>{err}</Alert>}
        {info && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setInfo('')}>{info}</Alert>}

        {missing && (
          <Alert severity="info" sx={{ py: 0.4, fontSize: 12.5 }}>
            No loan account on this line yet — one opens automatically on the first
            confirmed disbursement tranche (LOS → Disburse).
          </Alert>
        )}

        {pendingTranches.length > 0 && (
          <Alert severity="info" sx={{ mb: 1, py: 0.2, fontSize: 12 }}>
            {pendingTranches.length === 1
              ? `Tranche ${pendingTranches[0].tranche_ref} (₹ ${inr(pendingTranches[0].amount)}) is awaiting the LMS Authorizer's booking approval`
              : `${pendingTranches.length} tranches are awaiting the LMS Authorizer's booking approval`}
            {' '}— see Pending bookings on the Accounts tab.
          </Alert>
        )}

        {acct && (
          <>
            {/* ---- the facility header, like the Excel's top block ---------------- */}
            <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 1, mb: 1 }}>
              {[['Borrower', acct.borrower], ['Facility', acct.facility_type],
                ['Disbursed on', acct.disbursed_on], ['Amount ₹', inr(acct.amount)],
                ['Rate', acct.rate_pct != null ? `${acct.rate_pct}% ${acct.rate_kind || ''}` : '—'],
                ['Tenure', acct.tenor_months ? `${acct.tenor_months} months` : '—'],
                ['EMI ₹', inr(acct.emi_amount)], ['Day count', acct.day_count],
                ['Status', acct.status], ['Overdue', acct.overdue_position || 'Nil'],
                ['Provisioning ₹', inr(acct.provisioning_amount)],
                ['Closed on', acct.closed_on || '—']].map(([k, v]) => (
                <Box key={String(k)}>
                  <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>{k}</Typography>
                  <Typography sx={{ fontSize: 12.5, fontWeight: k === 'Status' ? 600 : 400 }}>{v ?? '—'}</Typography>
                </Box>
              ))}
            </Box>

            {/* ---- the statement ledger ------------------------------------------ */}
            <Box sx={{ maxHeight: 240, overflow: 'auto', border: `1px solid ${tokens.line}`, borderRadius: 1 }}>
              <Table size="small" stickyHeader sx={{ '& td, & th': { fontSize: 12 } }}>
                <TableHead>
                  <TableRow sx={{ '& th': { fontWeight: 600 } }}>
                    <TableCell>Date</TableCell><TableCell>Particulars</TableCell>
                    <TableCell align="right">Debit</TableCell>
                    <TableCell align="right">Credit</TableCell>
                    <TableCell align="right">Balance</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {entries.map((e) => (
                    <TableRow key={e.entry_no}>
                      <TableCell>{e.entry_date}</TableCell>
                      <TableCell>{e.particulars}</TableCell>
                      <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>{e.debit != null ? inr(e.debit) : ''}</TableCell>
                      <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>{e.credit != null ? inr(e.credit) : ''}</TableCell>
                      <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}><b>{inr(e.balance)}</b></TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>

            {/* ---- operator verbs -------------------------------------------------- */}
            {!closed && operate && (
              <>
                <Divider sx={{ my: 1.4 }} />
                <Typography sx={{ fontSize: 12.5, fontWeight: 600, mb: 0.6 }}>
                  Interest — computed, never hand-keyed
                </Typography>
                <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                  <TextField size="small" type="date" label="Accrue to"
                    InputLabelProps={{ shrink: true }} value={accrueTo}
                    onChange={(e) => { setAccrueTo(e.target.value); setPreview(null); }} sx={{ width: 170 }} />
                  <Button size="small" variant="outlined" disabled={!accrueTo || !!busy}
                    onClick={doPreview} sx={{ textTransform: 'none' }}>
                    {busy === 'preview' ? 'Computing…' : 'Compute'}
                  </Button>
                  {preview && (
                    <>
                      <Typography sx={{ fontSize: 12 }}>
                        ₹ <b>{inr(preview.interest)}</b> — {preview.formula}
                      </Typography>
                      <Button size="small" variant="contained" disabled={!!busy}
                        onClick={doAccrue} sx={{ textTransform: 'none' }}>
                        {busy === 'accrue' ? 'Posting…' : 'Post interest'}
                      </Button>
                    </>
                  )}
                </Box>

                <Typography sx={{ fontSize: 12.5, fontWeight: 600, mt: 1.4, mb: 0.6 }}>
                  Record entry
                </Typography>
                <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                  <TextField size="small" type="date" label="Date" InputLabelProps={{ shrink: true }}
                    value={entry.entry_date} onChange={(e) => setEntry({ ...entry, entry_date: e.target.value })} sx={{ width: 160 }} />
                  <TextField size="small" select label="Kind" value={entry.kind}
                    onChange={(e) => setEntry({ ...entry, kind: e.target.value })} sx={{ width: 130 }}>
                    {['EMI', 'Receipt', 'Charge', 'Adjustment'].map((k) => <MenuItem key={k} value={k}>{k}</MenuItem>)}
                  </TextField>
                  <TextField size="small" type="number" label="Amount ₹" value={entry.amount}
                    onChange={(e) => setEntry({ ...entry, amount: e.target.value })} sx={{ width: 140 }} />
                  <TextField size="small" label="Particulars (optional)" value={entry.particulars}
                    onChange={(e) => setEntry({ ...entry, particulars: e.target.value })} sx={{ flex: 1, minWidth: 160 }} />
                  <Button size="small" variant="contained" disabled={!!busy}
                    onClick={doEntry} sx={{ textTransform: 'none' }}>
                    {busy === 'entry' ? 'Recording…' : 'Record'}
                  </Button>
                </Box>

                {/* ---- the T2+ recorder: the maker's side of the booking gate ------ */}
                {sched && !sched.fully_disbursed && (
                  <>
                    <Typography sx={{ fontSize: 12.5, fontWeight: 600, mt: 1.4, mb: 0.6 }}>
                      Record disbursement tranche (T{nextTrancheNo})
                    </Typography>
                    <Typography sx={{ fontSize: 11.5, color: tokens.muted, mb: 0.6 }}>
                      Later phases are recorded here from the partner's confirmation —
                      each lands as a pending booking for the LMS Authorizer.
                      {sched.remaining != null && <> Remaining headroom ₹ {inr(sched.remaining)} Cr.</>}
                    </Typography>
                    <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                      <TextField size="small" type="number" label="Amount ₹ Cr" value={tr.amount}
                        onChange={(e) => setTr({ ...tr, amount: e.target.value })} sx={{ width: 140 }} />
                      <TextField size="small" type="date" label="Value date" InputLabelProps={{ shrink: true }}
                        value={tr.disbursed_on} onChange={(e) => setTr({ ...tr, disbursed_on: e.target.value })} sx={{ width: 160 }} />
                      <TextField size="small" label="UTR / reference" value={tr.ref}
                        onChange={(e) => setTr({ ...tr, ref: e.target.value })} sx={{ flex: 1, minWidth: 160 }} />
                      <Button size="small" variant="contained" disabled={!!busy}
                        onClick={doRecordTranche} sx={{ textTransform: 'none' }}>
                        {busy === 'tranche' ? 'Recording…' : 'Record for approval'}
                      </Button>
                    </Box>
                  </>
                )}
              </>
            )}
            {!closed && !operate && (
              <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 1.4 }}>
                Ledger entries are recorded by: {whoCan('lmsOperate')}.
              </Typography>
            )}

            {/* ---- authorizer verbs ------------------------------------------------ */}
            {!closed && authorize && (
              <>
                <Divider sx={{ my: 1.4 }} />
                <Typography sx={{ fontSize: 12.5, fontWeight: 600, mb: 0.6 }}>
                  Classification & closure — LMS Authorizer
                </Typography>
                <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                  <TextField size="small" select label="Status" value={cls.status}
                    onChange={(e) => setCls({ ...cls, status: e.target.value })} sx={{ width: 150 }}>
                    <MenuItem value="">(unchanged)</MenuItem>
                    {['Standard', 'SMA', 'Sub-Standard', 'Doubtful', 'Loss'].map((s) => <MenuItem key={s} value={s}>{s}</MenuItem>)}
                  </TextField>
                  <TextField size="small" label="Overdue position" value={cls.overdue_position}
                    onChange={(e) => setCls({ ...cls, overdue_position: e.target.value })} sx={{ width: 170 }} />
                  <TextField size="small" type="number" label="Provisioning ₹" value={cls.provisioning_amount}
                    onChange={(e) => setCls({ ...cls, provisioning_amount: e.target.value })} sx={{ width: 150 }} />
                  <TextField size="small" type="date" label="Close on" InputLabelProps={{ shrink: true }}
                    value={cls.closed_on} onChange={(e) => setCls({ ...cls, closed_on: e.target.value })} sx={{ width: 160 }} />
                  <TextField size="small" label="Note" value={cls.note}
                    onChange={(e) => setCls({ ...cls, note: e.target.value })} sx={{ flex: 1, minWidth: 140 }} />
                  <Button size="small" variant="outlined" color={cls.closed_on ? 'error' : 'primary'}
                    disabled={!!busy} onClick={doClassify} sx={{ textTransform: 'none' }}>
                    {busy === 'classify' ? 'Updating…' : cls.closed_on ? 'Update & close' : 'Update'}
                  </Button>
                </Box>
              </>
            )}
            {closed && (
              <Alert severity="info" sx={{ mt: 1.4, py: 0.2, fontSize: 12 }}>
                This account is closed{acct.closed_on ? ` (on ${acct.closed_on})` : ''} —
                the ledger is frozen.
              </Alert>
            )}
          </>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined">Close</Button>
      </DialogActions>
    </Dialog>
  );
}
