import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Chip, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Typography,
} from '@mui/material';
import { tokens } from '../../../theme';
import { fmt } from '../../../utils/format';
import { lmsService, type TrancheItem } from '../../../services/lmsService';
import { useAuth } from '../../../auth/AuthContext';
import { can } from '../../../auth/rbac';
import type { LendingRow } from '../lending.types';
import AccountDialog from './AccountDialog';

/**
 * The serviced book: one row per lending line in the disbursed family, plus the
 * BOOKING QUEUE on top — every human-recorded tranche waiting for the LMS Authorizer.
 * Approval opens/grows the loan account in the register's own transaction; rejection
 * returns the recording with the reason. Four-eyes is the register's rule, not ours.
 */
export default function AccountsTab({ rows }: { rows: LendingRow[] }) {
  const { user } = useAuth();
  const authorize = can(user.roles, 'lmsAuthorize');
  const [open, setOpen] = useState<LendingRow | null>(null);
  const [queue, setQueue] = useState<TrancheItem[]>([]);
  const [err, setErr] = useState('');
  const [info, setInfo] = useState('');
  const [busy, setBusy] = useState('');
  const [rejecting, setRejecting] = useState<{ id: string; note: string } | null>(null);

  const byLending = new Map(rows.map((r) => [r.id, r]));
  const loadQueue = async () => {
    try { setQueue(await lmsService.pendingBookings()); }
    catch { setQueue([]); }  // a role without the LMS verbs simply sees no queue
  };
  useEffect(() => { void loadQueue(); }, []);

  const settle = async (t: TrancheItem, action: 'approve' | 'reject', note?: string) => {
    setErr(''); setInfo(''); setBusy(t.id);
    try {
      await lmsService.book(t.lending_id, t.id, action, note);
      setInfo(action === 'approve'
        ? `${t.tranche_ref} booked — the loan account is updated and the ledger has its row.`
        : `${t.tranche_ref} rejected — the recorder corrects and records afresh.`);
      setRejecting(null);
      await loadQueue();
    } catch (e: any) { setErr(e?.message || String(e)); }
    setBusy('');
  };

  return (
    <Box sx={{ mt: 1 }}>
      {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setErr('')}>{err}</Alert>}
      {info && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setInfo('')}>{info}</Alert>}

      {/* ---- the booking queue ------------------------------------------------ */}
      {queue.length > 0 && (
        <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1.2, mb: 1.4 }}>
          <Typography sx={{ fontSize: 12.5, fontWeight: 600, mb: 0.6 }}>
            Pending bookings ({queue.length}) — awaiting the LMS Authorizer
          </Typography>
          {queue.map((t) => {
            const line = byLending.get(t.lending_id);
            return (
              <Box key={t.id} sx={{ py: 0.5, borderBottom: `1px dashed ${tokens.line}`,
                '&:last-of-type': { borderBottom: 'none' } }}>
                <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                  <Chip label="Pending" size="small"
                    sx={{ height: 20, fontSize: 11, bgcolor: '#FFF3CD', color: '#7A5C00' }} />
                  <Typography sx={{ fontSize: 12.5, fontWeight: 600 }}>
                    {line?._name || t.lending_id.slice(0, 8)}
                  </Typography>
                  <Typography sx={{ fontSize: 12.5 }}>₹ {fmt(t.amount)} Cr</Typography>
                  <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                    {[t.tranche_ref, t.disbursed_on, `by ${t.recorded_by || '—'}`]
                      .filter(Boolean).join(' · ')}
                  </Typography>
                  {authorize && rejecting?.id !== t.id && (
                    <Box sx={{ ml: 'auto', display: 'flex', gap: 0.8 }}>
                      <Button size="small" variant="contained" disabled={!!busy}
                        onClick={() => void settle(t, 'approve')}
                        sx={{ textTransform: 'none', fontSize: 11.5, py: 0.2 }}>
                        {busy === t.id ? 'Booking…' : 'Approve & book'}
                      </Button>
                      <Button size="small" variant="outlined" color="error" disabled={!!busy}
                        onClick={() => setRejecting({ id: t.id, note: '' })}
                        sx={{ textTransform: 'none', fontSize: 11.5, py: 0.2 }}>
                        Reject…
                      </Button>
                    </Box>
                  )}
                </Box>
                {authorize && rejecting?.id === t.id && (
                  <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mt: 0.6 }}>
                    <TextField size="small" label="Rejection reason (required)" autoFocus
                      value={rejecting.note} sx={{ flex: 1 }}
                      onChange={(e) => setRejecting({ id: t.id, note: e.target.value })} />
                    <Button size="small" variant="contained" color="error"
                      disabled={!!busy || !rejecting.note.trim()}
                      onClick={() => void settle(t, 'reject', rejecting.note.trim())}
                      sx={{ textTransform: 'none', fontSize: 11.5 }}>
                      {busy === t.id ? 'Rejecting…' : 'Confirm reject'}
                    </Button>
                    <Button size="small" onClick={() => setRejecting(null)}
                      sx={{ textTransform: 'none', fontSize: 11.5 }}>Cancel</Button>
                  </Box>
                )}
              </Box>
            );
          })}
          {!authorize && (
            <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 0.4 }}>
              Approval is the LMS Authorizer's verb — recordings wait here until then.
            </Typography>
          )}
        </Box>
      )}

      {/* ---- the serviced book ------------------------------------------------ */}
      {!rows.length ? (
        <Typography sx={{ fontSize: 12.5, color: tokens.muted, mt: 2 }}>
          No serviced lines yet — a loan account opens when the LMS Authorizer books
          the first disbursement tranche (LOS → Disburse records it).
        </Typography>
      ) : (
        <Table size="small" sx={{ '& td, & th': { fontSize: 12.5 } }}>
          <TableHead>
            <TableRow sx={{ '& th': { fontWeight: 600, color: tokens.muted } }}>
              <TableCell>Group Code</TableCell>
              <TableCell>Company</TableCell>
              <TableCell align="right">₹ Cr</TableCell>
              <TableCell>Stage</TableCell>
              <TableCell>Analyst</TableCell>
              <TableCell />
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.id} hover sx={{ cursor: 'pointer' }}
                onClick={() => setOpen(r)}>
                <TableCell>{r.code}</TableCell>
                <TableCell><b>{r._name}</b></TableCell>
                <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                  {fmt(r.amt)}
                </TableCell>
                <TableCell>{r.stage}</TableCell>
                <TableCell>{r.an}</TableCell>
                <TableCell align="right">
                  <Button size="small" sx={{ textTransform: 'none', fontSize: 12 }}>
                    Open account
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <AccountDialog row={open}
        onClose={() => { setOpen(null); void loadQueue(); }} />
    </Box>
  );
}
