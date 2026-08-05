import { useMemo, useState } from 'react';
import { Alert, Box, Button, Chip, TextField, Typography } from '@mui/material';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../../components/table/CommonTable';
import { CodeText } from '../../../components/common/Pills';
import { applyQuery } from '../../../api/queryEngine';
import { tokens } from '../../../theme';
import { fmt } from '../../../utils/format';
import { lmsService, type TrancheItem } from '../../../services/lmsService';
import { useAuth } from '../../../auth/AuthContext';
import { can } from '../../../auth/rbac';
import type { LendingRow } from '../lending.types';
import AccountDrawer from './AccountDrawer';

/**
 * The serviced BOOK, LOS-style: the same house table (filters, sort, exports, sticky
 * Actions), one row per LOAN ACCOUNT. Clicking a row opens the account drawer — the
 * post-disbursement life of the loan, tracked till closure. On top sits the BOOKING
 * QUEUE: every human-recorded tranche waiting for the LMS Authorizer; approval opens
 * or grows the account in the register's own transaction.
 */

interface AcctRow extends Record<string, any> {
  id: string;            // lending id — the drawer's key
  accountNo: string;     // "#1"
  code: string;
  _name: string;
  facility: string;
  principal?: number;    // cumulative booked principal (₹ Cr)
  balance?: number;      // running statement balance (₹ Cr)
  rate: string;
  status: string;
  overdue: string;
  closedOn: string;
  an?: string;
  _row: LendingRow;      // the lending row the drawer needs
}

// Row tint follows the classification: stressed assets warn (SMA left border), the
// NPA classes go red, a closed account grays out — same v12 states LOS uses for stages.
const statusRowSx = (status: string) => {
  if (status === 'SMA') return { boxShadow: `inset 3px 0 0 ${tokens.warn}` };
  if (['Sub-Standard', 'Doubtful', 'Loss'].includes(status)) {
    return { backgroundColor: `${tokens.badBg} !important`, color: '#7C4A3E !important' };
  }
  if (status === 'Closed') return { backgroundColor: '#F2F4F5 !important', color: '#5F6E76 !important' };
  return {};
};

export default function AccountsTab({ rows }: { rows: LendingRow[] }) {
  const { user } = useAuth();
  const authorize = can(user.roles, 'lmsAuthorize');
  const qc = useQueryClient();
  const [open, setOpen] = useState<LendingRow | null>(null);
  const [err, setErr] = useState('');
  const [info, setInfo] = useState('');
  const [busy, setBusy] = useState('');
  const [rejecting, setRejecting] = useState<{ id: string; note: string } | null>(null);

  const byLending = useMemo(() => new Map(rows.map((r) => [r.id, r])), [rows]);

  // The BOOK: one register read per serviced line (404 = not on the book yet).
  const rowKey = rows.map((r) => r.id).join(',');
  const bookQuery = useQuery({
    queryKey: ['lms-accounts-book', rowKey],
    queryFn: async () => {
      const results = await Promise.all(rows.map(async (r) => {
        const acct = await lmsService.account(r.id).catch(() => null);
        return { r, acct };
      }));
      const out: AcctRow[] = [];
      for (const { r, acct } of results) {
        if (!acct) continue;
        const a = acct.account;
        const last = acct.entries[acct.entries.length - 1];
        out.push({
          id: r.id, accountNo: `#${a.account_no}`, code: r.code || '',
          _name: r._name || a.borrower || '—',
          facility: a.facility_type || '—', principal: a.amount,
          balance: last ? last.balance : undefined,
          rate: a.rate_pct != null ? `${a.rate_pct}%` : '—',
          status: a.status, overdue: a.overdue_position || 'Nil',
          closedOn: a.closed_on || '', an: r.an, _row: r,
        });
      }
      return out;
    },
  });
  const acctRows = bookQuery.data ?? [];
  const noAccount = rows.length - acctRows.length;

  // The queue of pending bookings (whole-book; roles without the LMS verbs see none) —
  // each line annotated with its OVERDUE deferred conditions, so the authorizer sees
  // an expired CS chase before booking more money onto that line.
  const queueQuery = useQuery({
    queryKey: ['lms-pending-bookings'],
    queryFn: async () => {
      const items = await lmsService.pendingBookings().catch(() => [] as TrancheItem[]);
      const ids = [...new Set(items.map((t) => t.lending_id))];
      const overdue = await Promise.all(ids.map(async (id) => ({
        id, n: (await lmsService.openConditions(id)).filter((c) => c.overdue).length,
      })));
      return { items, overdueBy: Object.fromEntries(overdue.map((o) => [o.id, o.n])) };
    },
  });
  const queue = queueQuery.data?.items ?? [];
  const overdueBy: Record<string, number> = queueQuery.data?.overdueBy ?? {};

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['lms-accounts-book'] });
    void qc.invalidateQueries({ queryKey: ['lms-pending-bookings'] });
    void qc.invalidateQueries({ queryKey: ['lending'] });
  };

  const settle = async (t: TrancheItem, action: 'approve' | 'reject', note?: string) => {
    setErr(''); setInfo(''); setBusy(t.id);
    try {
      await lmsService.book(t.lending_id, t.id, action, note);
      setInfo(action === 'approve'
        ? `${t.tranche_ref} booked — the loan account is updated and the ledger has its row.`
        : `${t.tranche_ref} rejected — the recorder corrects and records afresh.`);
      setRejecting(null);
      refresh();
    } catch (e: any) { setErr(e?.message || String(e)); }
    setBusy('');
  };

  const columns = useMemo<MRT_ColumnDef<AcctRow>[]>(() => [
    { accessorKey: 'accountNo', header: 'Account', size: 96 },
    { accessorKey: 'code', header: 'Group Code', size: 120, Cell: ({ cell }) => <CodeText code={cell.getValue<string>()} /> },
    { accessorKey: '_name', header: 'Borrower', size: 200, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b> },
    { accessorKey: 'facility', header: 'Facility', size: 120 },
    { accessorKey: 'principal', header: 'Principal ₹ Cr', size: 110, Cell: ({ cell }) => <span style={{ fontVariantNumeric: 'tabular-nums' }}>{fmt(cell.getValue())}</span> },
    { accessorKey: 'balance', header: 'Balance ₹ Cr', size: 110, Cell: ({ cell }) => <span style={{ fontVariantNumeric: 'tabular-nums' }}>{fmt(cell.getValue())}</span> },
    { accessorKey: 'rate', header: 'Rate', size: 80 },
    { accessorKey: 'status', header: 'Status', size: 110, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b> },
    { accessorKey: 'overdue', header: 'Overdue', size: 120 },
    { accessorKey: 'closedOn', header: 'Closed on', size: 100 },
  ], []);

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
                  {(overdueBy[t.lending_id] ?? 0) > 0 && (
                    <Chip size="small"
                      label={`${overdueBy[t.lending_id]} condition${overdueBy[t.lending_id] > 1 ? 's' : ''} overdue`}
                      title="Deferred CP/CS conditions past their expiry on this line — check before booking more money."
                      sx={{ height: 20, fontSize: 11, bgcolor: '#FDE8E4', color: '#7C4A3E' }} />
                  )}
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

      {/* ---- the serviced book, LOS-style ------------------------------------- */}
      <CommonTable<AcctRow>
        queryKey={['lms-accounts', bookQuery.dataUpdatedAt ?? 0]}
        fetcher={(q) => Promise.resolve(applyQuery(acctRows, {
          ...q,
          searchFields: ['accountNo', 'code', '_name', 'facility', 'status', 'overdue'],
        }))}
        columns={columns}
        csvName="atlas_lms_accounts"
        onRowClick={(r) => setOpen(r._row)}
        onEdit={(r) => setOpen(r._row)}
        rowSx={(r) => statusRowSx(r.status)}
      />
      {acctRows.length === 0 && !bookQuery.isLoading && (
        <Typography sx={{ fontSize: 12.5, color: tokens.muted, mt: 1 }}>
          No loan accounts on the book yet — an account opens when the LMS Authorizer
          books the first disbursement tranche (recorded in LOS → Disburse).
        </Typography>
      )}
      {noAccount > 0 && acctRows.length > 0 && (
        <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 0.6 }}>
          {noAccount} line{noAccount > 1 ? 's' : ''} in the disbursed family {noAccount > 1 ? 'have' : 'has'} no
          loan account yet — {noAccount > 1 ? 'they appear' : 'it appears'} here once the first tranche is booked.
        </Typography>
      )}

      <AccountDrawer row={open} onClose={() => setOpen(null)} onChanged={refresh} />
    </Box>
  );
}
