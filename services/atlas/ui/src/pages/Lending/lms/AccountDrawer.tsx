import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Divider, Drawer, IconButton, MenuItem, Table, TableBody,
  TableCell, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { DrawerSection } from '../../../components/common/Field';
import { CodeText } from '../../../components/common/Pills';
import DataRegisterDialog from '../../Deals/DataRegisterDialog';
import CovenantComplianceDialog from './CovenantComplianceDialog';
import {
  lmsService, type AccountCondition, type LedgerEntry, type LoanAccount,
  type Observation, type TrancheSchedule,
} from '../../../services/lmsService';
import { useAuth } from '../../../auth/AuthContext';
import { can, whoCan } from '../../../auth/rbac';
import { tokens } from '../../../theme';
import type { LendingRow } from '../lending.types';

/**
 * The LOAN ACCOUNT DRAWER — one account's whole post-disbursement life, LOS-drawer
 * style, tracked till closure:
 *
 *   Disbursement — the tranche schedule with its booking gate (T2+ recorded here);
 *   Statement    — the ledger with computed interest and receipts (operator verbs);
 *   Covenants    — this line's compliance read (recorded on the Covenants tab);
 *   Classification & closure — SMA/NPA, overdue, provisioning, close (authorizer).
 *
 * Collections (DPD buckets) and EWS graduate into their own tabs; until then the
 * overdue position lives in the classification section.
 */

const inr = (v?: number | null) =>
  v == null ? '—' : v.toLocaleString('en-IN', { minimumFractionDigits: 2 });

// The desk's Excel keeps the statement in ABSOLUTE RUPEES (₹57,535 interest rows,
// ₹4,47,608 EMIs) — amounts are stored in ₹ Cr, so ×1e7 renders the real figure.
const rs = (vCr?: number | null) =>
  vCr == null ? '—' : Math.round(vCr * 1e7).toLocaleString('en-IN');

const STATUS_TONE: Record<string, { bg: string; fg: string }> = {
  Standard: { bg: '#E5F5EC', fg: '#175E3B' },
  SMA: { bg: '#FFF3CD', fg: '#7A5C00' },
  'Sub-Standard': { bg: '#FDE8E4', fg: '#7C4A3E' },
  Doubtful: { bg: '#FDE8E4', fg: '#7C4A3E' },
  Loss: { bg: '#FDE8E4', fg: '#7C4A3E' },
  Closed: { bg: '#EEF1F3', fg: '#5F6E76' },
};

const StatusChip = ({ label }: { label: string }) => {
  const t = STATUS_TONE[label] || STATUS_TONE.Standard;
  return (
    <Typography component="span" sx={{ fontSize: 10.5, fontWeight: 700, px: 0.8,
      py: 0.2, borderRadius: 1, bgcolor: t.bg, color: t.fg }}>{label}</Typography>
  );
};

const OBS_TONE: Record<string, { bg: string; fg: string }> = {
  Pending: { bg: '#EEF1F3', fg: '#5F6E76' },
  Compliant: { bg: '#E5F5EC', fg: '#175E3B' },
  Breached: { bg: '#FDE8E4', fg: '#7C4A3E' },
  Waived: { bg: '#FFF3CD', fg: '#7A5C00' },
  Overdue: { bg: '#FFF3CD', fg: '#7A5C00' },
};

export default function AccountDrawer({ row, onClose, onChanged }: {
  row: LendingRow | null; onClose: () => void; onChanged?: () => void;
}) {
  const { user } = useAuth();
  const open = !!row;
  const operate = can(user.roles, 'lmsOperate');
  const authorize = can(user.roles, 'lmsAuthorize');

  const [acct, setAcct] = useState<LoanAccount | null>(null);
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [sched, setSched] = useState<TrancheSchedule | null>(null);
  const [obs, setObs] = useState<Observation[]>([]);
  const [condReg, setCondReg] = useState<{ items: AccountCondition[]; open: number } | null>(null);
  const [receiving, setReceiving] = useState<{ key: string; evidence: string } | null>(null);
  const [dataRegOpen, setDataRegOpen] = useState(false);
  const [covOpen, setCovOpen] = useState(false);
  const [missing, setMissing] = useState(false);
  const [err, setErr] = useState('');
  const [info, setInfo] = useState('');
  const [busy, setBusy] = useState('');
  // Interest: pick a date → the register COMPUTES (preview shows the formula) → post.
  const [accrueTo, setAccrueTo] = useState('');
  const [preview, setPreview] = useState<any | null>(null);
  const [entry, setEntry] = useState({ entry_date: '', kind: 'EMI', amount: '', particulars: '' });
  const [cls, setCls] = useState({ status: '', overdue_position: '', provisioning_amount: '', closed_on: '', note: '' });
  const [tr, setTr] = useState({ amount: '', disbursed_on: '', ref: '' });
  const [touched, setTouched] = useState(false);

  const load = async () => {
    if (!row) return;
    setErr('');
    try {
      setSched(await lmsService.tranches(row.id).catch(() => null));
      setCondReg(await lmsService.accountConditions(row.id).catch(() => null));
      if (row.entityId) {
        setObs(await lmsService.observations(row.entityId, row.id).catch(() => []));
      } else setObs([]);
      const out = await lmsService.account(row.id);
      if (out === null) { setMissing(true); setAcct(null); setEntries([]); return; }
      setMissing(false); setAcct(out.account); setEntries(out.entries);
    } catch (e: any) { setErr(e?.message || String(e)); }
  };

  useEffect(() => {
    if (!open) return;
    const today = new Date().toISOString().slice(0, 10);
    setErr(''); setInfo(''); setBusy(''); setPreview(null); setAccrueTo(today);
    setTouched(false); setDataRegOpen(false); setCovOpen(false);
    setEntry({ entry_date: today, kind: 'EMI', amount: '', particulars: '' });
    setCls({ status: '', overdue_position: '', provisioning_amount: '', closed_on: '', note: '' });
    setTr({ amount: '', disbursed_on: new Date().toISOString().slice(0, 10), ref: '' });
    void load();
  }, [open, row?.id]);  // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (what: string, fn: () => Promise<string>) => {
    setErr(''); setInfo(''); setBusy(what);
    try { setInfo(await fn()); setTouched(true); await load(); }
    catch (e: any) { setErr(e?.message || String(e)); }
    setBusy('');
  };

  const close = () => { if (touched) onChanged?.(); onClose(); };

  const doPreview = () => run('preview', async () => {
    if (!row || !accrueTo) throw new Error('Pick the date to accrue to.');
    const p = await lmsService.interestPreview(row.id, accrueTo);
    setPreview(p);
    return `Computed: ₹ ${rs(p.interest)} for ${p.days} days. Review, then Post interest.`;
  });

  const doAccrue = () => run('accrue', async () => {
    if (!row || !preview) throw new Error('Preview first — the figure must be checkable.');
    const e = await lmsService.accrue(row.id, accrueTo);
    setPreview(null); setAccrueTo(new Date().toISOString().slice(0, 10));
    return `Interest row posted — balance ₹ ${rs(e.balance)}.`;
  });

  const doEntry = () => run('entry', async () => {
    if (!row) throw new Error('No line.');
    if (!entry.entry_date || !entry.amount) throw new Error('Date and amount are needed.');
    // The desk types RUPEES (like their Excel); the register stores ₹ Cr.
    const e = await lmsService.addEntry(row.id, {
      entry_date: entry.entry_date, kind: entry.kind as any,
      amount: Number(entry.amount) / 1e7,
      ...(entry.particulars ? { particulars: entry.particulars } : {}),
    });
    setEntry({ entry_date: new Date().toISOString().slice(0, 10), kind: 'EMI',
      amount: '', particulars: '' });
    return `${e.particulars} recorded — balance ₹ ${rs(e.balance)}.`;
  });

  const exportStatement = () => {
    const q = (v: string) => `"${v.replace(/"/g, '""')}"`;
    const lines = [
      ['Date', 'Particulars', 'Debit', 'Credit', 'Balance'].map(q).join(','),
      ...entries.map((e) => [
        e.entry_date, e.particulars,
        e.debit != null ? String(Math.round(e.debit * 1e7)) : '',
        e.credit != null ? String(Math.round(e.credit * 1e7)) : '',
        String(Math.round(e.balance * 1e7)),
      ].map(q).join(',')),
    ];
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/csv' }));
    a.download = `statement_${row?.code || 'account'}.csv`;
    document.body.appendChild(a); a.click(); a.remove();
  };

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
    return `${t.tranche_ref} recorded — awaiting the LMS Management's booking approval.`;
  });

  const closed = acct?.status === 'Closed' || !!acct?.closed_on;
  const tranches = sched?.items || [];
  const nextTrancheNo = tranches.filter((t) => t.booking_status !== 'Rejected').length + 1;

  return (
    <Drawer anchor="right" open={open} onClose={close}
      PaperProps={{ sx: { width: 640, maxWidth: '100vw', height: '100%',
        display: 'flex', flexDirection: 'column' } }}>
      {/* Header bar — same shell as the company drawer. */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2, p: '9px 16px',
        bgcolor: '#F1F3F5', borderBottom: 1, borderColor: 'divider', flexShrink: 0 }}>
        <Typography sx={{ fontSize: 15.6, fontWeight: 700, flex: 1 }}>
          {acct ? `Loan account #${acct.account_no}` : 'Loan account'} — {row?._name}
        </Typography>
        {row?.code && <CodeText code={row.code} />}
        {acct && <StatusChip label={acct.status} />}
        {row?.code && (
          <Button size="small" variant="outlined" onClick={() => setDataRegOpen(true)}
            sx={{ textTransform: 'none', fontSize: 11.5, py: 0.2 }}>
            📁 Data register
          </Button>
        )}
        <IconButton onClick={close}><CloseIcon fontSize="small" /></IconButton>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', p: 2 }}>
        {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setErr('')}>{err}</Alert>}
        {info && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setInfo('')}>{info}</Alert>}

        {missing && (
          <Alert severity="info" sx={{ mb: 1.4, py: 0.4, fontSize: 12.5 }}>
            No loan account on this line yet — it opens when the LMS Management books
            the first disbursement tranche (recorded in LOS → Disburse).
          </Alert>
        )}

        {acct && (
          <DrawerSection title="Facility">
            <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: '8px 14px' }}>
              {[['Borrower', acct.borrower], ['Facility', acct.facility_type],
                ['Disbursed on', acct.disbursed_on], ['Principal ₹', rs(acct.amount)],
                ['Rate', acct.rate_pct != null ? `${acct.rate_pct}% ${acct.rate_kind || ''}` : '—'],
                ['Tenure', acct.tenor_months ? `${acct.tenor_months} months` : '—'],
                ['EMI ₹', rs(acct.emi_amount)], ['Day count', acct.day_count],
              ].map(([k, v]) => (
                <Box key={String(k)}>
                  <Typography sx={{ fontSize: 10.4, color: tokens.muted, fontWeight: 700,
                    textTransform: 'uppercase', letterSpacing: '.5px' }}>{k}</Typography>
                  <Typography sx={{ fontSize: 12.6 }}>{v ?? '—'}</Typography>
                </Box>
              ))}
            </Box>
          </DrawerSection>
        )}

        {/* ---- ① Disbursement completion — the schedule + its booking gate --------- */}
        {sched && (
          <DrawerSection title="Disbursement schedule">
            {tranches.length === 0 && (
              <Typography sx={{ fontSize: 12, color: tokens.muted }}>
                No tranches recorded yet.
              </Typography>
            )}
            {tranches.map((t) => (
              <Box key={t.id} sx={{ display: 'flex', gap: 1, alignItems: 'baseline',
                py: 0.35, borderBottom: `1px dashed ${tokens.line}`,
                '&:last-of-type': { borderBottom: 'none' } }}>
                <Typography sx={{ fontSize: 12, fontWeight: 700, minWidth: 26 }}>{t.tranche_no || '—'}</Typography>
                <Typography sx={{ fontSize: 12.5 }}>₹ {inr(t.amount)} Cr</Typography>
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
                {(t.conditions_open?.length ?? 0) > 0 && (
                  <Typography sx={{ fontSize: 10.5, fontWeight: 700, px: 0.7, borderRadius: 1,
                    bgcolor: '#EEF1F3', color: '#5F6E76' }}
                    title={`Open when recorded: ${t.conditions_open!.map((c) => c.label).join(' · ')}`}>
                    {t.conditions_open!.length} open @ record
                  </Typography>
                )}
              </Box>
            ))}
            {tranches.length > 0 && (
              <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 0.6 }}>
                Booked <b>₹ {inr(sched.total_disbursed)} Cr</b>
                {(sched.total_pending ?? 0) > 0 && <> · pending approval ₹ {inr(sched.total_pending)} Cr</>}
                {sched.ceiling != null && <> of ₹ {inr(sched.ceiling)} Cr
                  {sched.fully_disbursed ? ' — fully disbursed' : ` · remaining ₹ ${inr(sched.remaining)} Cr`}</>}
              </Typography>
            )}
            {acct && !closed && operate && !sched.fully_disbursed && (
              <>
                <Divider sx={{ my: 1 }} />
                <Typography sx={{ fontSize: 11.5, color: tokens.muted, mb: 0.6 }}>
                  Record T{nextTrancheNo} from the partner's confirmation — it lands as a
                  pending booking for the LMS Management.
                </Typography>
                <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                  <TextField size="small" type="number" label="Amount ₹ Cr" value={tr.amount}
                    onChange={(e) => setTr({ ...tr, amount: e.target.value })} sx={{ width: 130 }} />
                  <TextField size="small" type="date" label="Value date" InputLabelProps={{ shrink: true }}
                    value={tr.disbursed_on} onChange={(e) => setTr({ ...tr, disbursed_on: e.target.value })} sx={{ width: 150 }} />
                  <TextField size="small" label="UTR / reference" value={tr.ref}
                    onChange={(e) => setTr({ ...tr, ref: e.target.value })} sx={{ flex: 1, minWidth: 150 }} />
                  <Button size="small" variant="contained" disabled={!!busy}
                    onClick={doRecordTranche} sx={{ textTransform: 'none' }}>
                    {busy === 'tranche' ? 'Recording…' : 'Record for approval'}
                  </Button>
                </Box>
              </>
            )}
          </DrawerSection>
        )}

        {/* ---- ②b The conditions register — LMS-owned since the handover ----------- */}
        {condReg && condReg.items.length > 0 && (() => {
          const today = new Date().toISOString().slice(0, 10);
          const openItems = condReg.items.filter(
            (c) => !['Completed', 'Waived'].includes(c.status));
          const doneN = condReg.items.length - openItems.length;
          return (
            <DrawerSection title={`Conditions register — ${openItems.length} open`}>
              {openItems.length === 0 && (
                <Typography sx={{ fontSize: 12, color: tokens.muted }}>
                  Every condition is settled — the full handover record is kept below.
                </Typography>
              )}
              {openItems.map((c) => {
                const overdue = !!c.expiry_date && c.expiry_date < today;
                return (
                  <Box key={c.key} sx={{ py: 0.35, borderBottom: `1px dashed ${tokens.line}`,
                    '&:last-of-type': { borderBottom: 'none' } }}>
                    <Box sx={{ display: 'flex', gap: 1, alignItems: 'center' }}>
                      <Typography sx={{ fontSize: 10.5, fontWeight: 700, px: 0.7, borderRadius: 1,
                        bgcolor: c.status === 'Deferred as CS' ? '#E8EDF9' : '#EEF1F3',
                        color: c.status === 'Deferred as CS' ? '#2A4B8D' : '#5F6E76' }}>
                        {c.status === 'Deferred as CS' ? 'CP · deferred' : c.condition_type}
                      </Typography>
                      <Typography sx={{ fontSize: 12.3, flex: 1 }} title={c.reason || ''}>
                        {c.label}
                      </Typography>
                      {c.expiry_date && (
                        <Typography sx={{ fontSize: 11.5,
                          color: overdue ? '#7C4A3E' : tokens.muted }}>
                          due {c.expiry_date}
                        </Typography>
                      )}
                      {overdue && (
                        <Typography sx={{ fontSize: 10.5, fontWeight: 700, px: 0.7,
                          borderRadius: 1, bgcolor: '#FDE8E4', color: '#7C4A3E' }}>
                          Overdue
                        </Typography>
                      )}
                      {!closed && operate && receiving?.key !== c.key && (
                        <Button size="small" variant="outlined" disabled={!!busy}
                          onClick={() => setReceiving({ key: c.key, evidence: '' })}
                          sx={{ textTransform: 'none', fontSize: 11.5, py: 0.1 }}>
                          Mark received…
                        </Button>
                      )}
                    </Box>
                    {!closed && operate && receiving?.key === c.key && (
                      <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mt: 0.5 }}>
                        <TextField size="small" label="Evidence reference (optional)" autoFocus
                          value={receiving.evidence} sx={{ flex: 1 }}
                          onChange={(e) => setReceiving({ key: c.key, evidence: e.target.value })} />
                        <Button size="small" variant="contained" disabled={!!busy}
                          onClick={() => run('receive', async () => {
                            const r = await lmsService.receiveCondition(row!.id, c.key,
                              receiving.evidence.trim()
                                ? { evidence_ref: receiving.evidence.trim() } : undefined);
                            setReceiving(null);
                            return `${r.label} received — the reminder for it stops now.`;
                          })}
                          sx={{ textTransform: 'none', fontSize: 11.5 }}>
                          {busy === 'receive' ? 'Recording…' : 'Confirm received'}
                        </Button>
                        <Button size="small" onClick={() => setReceiving(null)}
                          sx={{ textTransform: 'none', fontSize: 11.5 }}>Cancel</Button>
                      </Box>
                    )}
                  </Box>
                );
              })}
              <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 0.6 }}>
                Handed over from the LOS checklist at account opening ({doneN} of{' '}
                {condReg.items.length} settled) — the LMS owns this register; the
                checklist stays frozen as the decision record.
              </Typography>
            </DrawerSection>
          );
        })()}

        {/* ---- ② The statement — the Excel's ledger, in real rupees ---------------- */}
        {acct && (
          <DrawerSection title="Statement ledger (₹)"
            action={
              <Button size="small" onClick={exportStatement}
                sx={{ textTransform: 'none', fontSize: 11 }}>⬇ CSV</Button>
            }>
            <Box sx={{ maxHeight: 300, overflow: 'auto', border: `1px solid ${tokens.line}`, borderRadius: 1 }}>
              <Table size="small" stickyHeader sx={{ '& td, & th': { fontSize: 12 } }}>
                <TableHead>
                  <TableRow sx={{ '& th': { fontWeight: 600 } }}>
                    <TableCell>Date</TableCell><TableCell>Particulars</TableCell>
                    <TableCell align="right">Debit ₹</TableCell>
                    <TableCell align="right">Credit ₹</TableCell>
                    <TableCell align="right">Balance ₹</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {entries.map((e) => (
                    <TableRow key={e.entry_no}>
                      <TableCell sx={{ whiteSpace: 'nowrap' }}>{e.entry_date}</TableCell>
                      <TableCell>{e.particulars}</TableCell>
                      <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums',
                        color: '#7C4A3E' }}>{e.debit != null ? rs(e.debit) : ''}</TableCell>
                      <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums',
                        color: '#175E3B' }}>{e.credit != null ? rs(e.credit) : ''}</TableCell>
                      <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}><b>{rs(e.balance)}</b></TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>

            {!closed && operate && (
              <>
                {/* Interest first — the desk's month-end ritual: compute (checkable),
                    then post. The date defaults to today, one click each. */}
                <Typography sx={{ fontSize: 12.5, fontWeight: 600, mt: 1.2, mb: 0.6 }}>
                  Interest — computed, never hand-keyed
                </Typography>
                <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                  <TextField size="small" type="date" label="Accrue to"
                    InputLabelProps={{ shrink: true }} value={accrueTo}
                    onChange={(e) => { setAccrueTo(e.target.value); setPreview(null); }} sx={{ width: 160 }} />
                  <Button size="small" variant="outlined" disabled={!accrueTo || !!busy}
                    onClick={doPreview} sx={{ textTransform: 'none' }}>
                    {busy === 'preview' ? 'Computing…' : 'Compute'}
                  </Button>
                  {preview && (
                    <>
                      <Typography sx={{ fontSize: 12 }}>
                        ₹ <b>{rs(preview.interest)}</b> — ₹{rs(preview.balance)} ×{' '}
                        {preview.rate_pct}% × {preview.days}/{preview.day_count}
                      </Typography>
                      <Button size="small" variant="contained" disabled={!!busy}
                        onClick={doAccrue} sx={{ textTransform: 'none' }}>
                        {busy === 'accrue' ? 'Posting…' : 'Post interest'}
                      </Button>
                    </>
                  )}
                </Box>

                {/* One row, like typing into the Excel: date (today), kind, ₹ amount —
                    EMI prefills from the sanction terms with one click. */}
                <Typography sx={{ fontSize: 12.5, fontWeight: 600, mt: 1.2, mb: 0.6 }}>
                  Record entry (₹)
                </Typography>
                <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                  <TextField size="small" type="date" label="Date" InputLabelProps={{ shrink: true }}
                    value={entry.entry_date} onChange={(e) => setEntry({ ...entry, entry_date: e.target.value })} sx={{ width: 150 }} />
                  <TextField size="small" select label="Kind" value={entry.kind}
                    onChange={(e) => {
                      const kind = e.target.value;
                      setEntry({ ...entry, kind,
                        amount: kind === 'EMI' && acct.emi_amount
                          ? String(Math.round(acct.emi_amount * 1e7)) : entry.amount });
                    }} sx={{ width: 120 }}>
                    {['EMI', 'Receipt', 'Charge', 'Adjustment'].map((k) => <MenuItem key={k} value={k}>{k}</MenuItem>)}
                  </TextField>
                  <TextField size="small" type="number" label="Amount ₹" value={entry.amount}
                    onChange={(e) => setEntry({ ...entry, amount: e.target.value })} sx={{ width: 150 }} />
                  {entry.kind === 'EMI' && acct.emi_amount != null && (
                    <Button size="small" onClick={() => setEntry({ ...entry,
                      amount: String(Math.round(acct.emi_amount! * 1e7)) })}
                      sx={{ textTransform: 'none', fontSize: 11 }}>
                      EMI ₹ {rs(acct.emi_amount)}
                    </Button>
                  )}
                  <TextField size="small" label="Particulars (optional)" value={entry.particulars}
                    onChange={(e) => setEntry({ ...entry, particulars: e.target.value })} sx={{ flex: 1, minWidth: 140 }} />
                  <Button size="small" variant="contained" disabled={!!busy}
                    onClick={doEntry} sx={{ textTransform: 'none' }}>
                    {busy === 'entry' ? 'Recording…' : 'Record'}
                  </Button>
                </Box>
              </>
            )}
            {!closed && !operate && (
              <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 1 }}>
                Ledger entries are recorded by: {whoCan('lmsOperate')}.
              </Typography>
            )}
          </DrawerSection>
        )}

        {/* ---- ③ Covenant compliance — summary here, the full ledger in its dialog -- */}
        {(acct || obs.length > 0) && (
          <DrawerSection title="Covenant compliance"
            action={
              <Button size="small" variant="outlined" onClick={() => setCovOpen(true)}
                sx={{ textTransform: 'none', fontSize: 11 }}>
                Open compliance…
              </Button>
            }>
            {obs.length === 0 ? (
              <Typography sx={{ fontSize: 12, color: tokens.muted }}>
                No covenant observations yet — the sweep raises each period as it falls
                due, and a standing reminder sits on the operator's Today until the
                result is recorded.
              </Typography>
            ) : (
              <Box sx={{ display: 'flex', gap: 0.8, flexWrap: 'wrap', alignItems: 'center' }}>
                {Object.entries(obs.reduce<Record<string, number>>((m, o) => {
                  m[o.status] = (m[o.status] || 0) + 1; return m;
                }, {})).map(([st, n]) => {
                  const t = OBS_TONE[st] || OBS_TONE.Pending;
                  return (
                    <Typography key={st} sx={{ fontSize: 11, fontWeight: 700, px: 0.8,
                      py: 0.2, borderRadius: 1, bgcolor: t.bg, color: t.fg }}>
                      {st} · {n}
                    </Typography>
                  );
                })}
                <Typography sx={{ fontSize: 11.5, color: tokens.muted, ml: 0.5 }}>
                  monthly chase reminds on Today until closure
                </Typography>
              </Box>
            )}
          </DrawerSection>
        )}

        {/* ---- ④ Classification & closure — the authorizer's verbs ----------------- */}
        {acct && !closed && authorize && (
          <DrawerSection title="Classification & closure — LMS Management">
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
              <TextField size="small" select label="Status" value={cls.status}
                onChange={(e) => setCls({ ...cls, status: e.target.value })} sx={{ width: 140 }}>
                <MenuItem value="">(unchanged)</MenuItem>
                {['Standard', 'SMA', 'Sub-Standard', 'Doubtful', 'Loss'].map((s) => <MenuItem key={s} value={s}>{s}</MenuItem>)}
              </TextField>
              <TextField size="small" label="Overdue position" value={cls.overdue_position}
                onChange={(e) => setCls({ ...cls, overdue_position: e.target.value })} sx={{ width: 160 }} />
              <TextField size="small" type="number" label="Provisioning ₹" value={cls.provisioning_amount}
                onChange={(e) => setCls({ ...cls, provisioning_amount: e.target.value })} sx={{ width: 140 }} />
              <TextField size="small" type="date" label="Close on" InputLabelProps={{ shrink: true }}
                value={cls.closed_on} onChange={(e) => setCls({ ...cls, closed_on: e.target.value })} sx={{ width: 150 }} />
              <TextField size="small" label="Note" value={cls.note}
                onChange={(e) => setCls({ ...cls, note: e.target.value })} sx={{ flex: 1, minWidth: 130 }} />
              <Button size="small" variant="outlined" color={cls.closed_on ? 'error' : 'primary'}
                disabled={!!busy} onClick={doClassify} sx={{ textTransform: 'none' }}>
                {busy === 'classify' ? 'Updating…' : cls.closed_on ? 'Update & close' : 'Update'}
              </Button>
            </Box>
            <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 0.8 }}>
              Current: {acct.status} · overdue {acct.overdue_position || 'Nil'} ·
              provisioning ₹ {inr(acct.provisioning_amount)}. Closing freezes the
              ledger; security release and the NOC are tracked offline for now.
            </Typography>
          </DrawerSection>
        )}
        {acct && closed && (
          <Alert severity="info" sx={{ py: 0.2, fontSize: 12 }}>
            This account is closed{acct.closed_on ? ` (on ${acct.closed_on})` : ''} —
            the ledger is frozen.
          </Alert>
        )}
      </Box>

      {row?.code && (
        <DataRegisterDialog code={row.code} open={dataRegOpen}
          onClose={() => setDataRegOpen(false)} />
      )}
      <CovenantComplianceDialog row={row} open={covOpen}
        onClose={() => setCovOpen(false)}
        onChanged={() => { setTouched(true); void load(); }} />
    </Drawer>
  );
}
