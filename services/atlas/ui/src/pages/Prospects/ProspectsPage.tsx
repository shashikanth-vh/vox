import { useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert, Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  MenuItem, Snackbar, TextField, Tooltip, Typography,
} from '@mui/material';
import PersonAddAlt1Icon from '@mui/icons-material/PersonAddAlt1';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import ConfirmDialog from '../../components/common/ConfirmDialog';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { applyQuery } from '../../api/queryEngine';
import { apiErr } from '../../api/http';
import { tokens } from '../../theme';
import { prospectsService, type ProspectRow } from '../../services/prospectsService';

// The grid's view-model: applyQuery and the column funnels read row FIELDS by
// column id, so the derived columns (joined sector tags, lead count, the status
// label) are materialised onto each row instead of living only in accessorFns —
// an accessorFn can feed a funnel's option list but the filter match reads
// r[column id], which an accessorFn-only column doesn't have.
type Row = ProspectRow & { sectors: string; leads: number; status_label: string };

/**
 * Masters → Prospects — the curated market universe, chips first, columns second.
 *
 * The whole universe is fetched once (a few thousand rows at most) and every
 * refinement happens in the browser: the chip rows (vertical → its sub-sectors →
 * status) and the grid filter to the SAME in-memory rows, so their counts can
 * never disagree, and a chip click answers in a frame instead of a round trip.
 *
 * A prospect can spawn MANY leads over time — the Create-lead action never
 * disappears, and the row shows how many it has spawned. Status and remarks are
 * the desk's fields (any desk role); the curated master data itself is the
 * import/export tool's territory (Admin/Management), which the server enforces
 * field by field.
 */

const STATUSES = ['uncontacted', 'contacted', 'interested', 'lead_created',
  'not_relevant'] as const;
const STATUS_LABEL: Record<string, string> = {
  uncontacted: 'Uncontacted', contacted: 'Contacted', interested: 'Interested',
  lead_created: 'Lead created', not_relevant: 'Not relevant',
};
const STATUS_COLOR: Record<string, 'default' | 'primary' | 'warning' | 'success'> = {
  uncontacted: 'default', contacted: 'primary', interested: 'warning',
  lead_created: 'success', not_relevant: 'default',
};

function countTags(rows: ProspectRow[], field: 'verticals' | 'sub_sectors'):
    [string, number][] {
  const counts = new Map<string, number>();
  rows.forEach((r) => (r[field] || []).forEach(
    (t) => counts.set(t, (counts.get(t) || 0) + 1)));
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function ChipRow({ label, items, selected, onToggle }: {
  label: string; items: [string, number][]; selected: string[];
  onToggle: (v: string) => void;
}) {
  if (!items.length) return null;
  return (
    <Box sx={{ display: 'flex', gap: 0.7, alignItems: 'center', flexWrap: 'wrap' }}>
      <Typography sx={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '.08em',
        color: tokens.muted, textTransform: 'uppercase', mr: 0.3 }}>{label}</Typography>
      {items.map(([value, n]) => (
        <Chip key={value} size="small" clickable
          label={`${value} · ${n}`}
          color={selected.includes(value) ? 'primary' : 'default'}
          variant={selected.includes(value) ? 'filled' : 'outlined'}
          onClick={() => onToggle(value)} sx={{ fontSize: 11.6 }} />
      ))}
    </Box>
  );
}

export default function ProspectsPage() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [verticals, setVerticals] = useState<string[]>([]);
  const [subs, setSubs] = useState<string[]>([]);
  const [statuses, setStatuses] = useState<string[]>([]);
  const [work, setWork] = useState<ProspectRow | null>(null);
  const [status, setStatus] = useState('uncontacted');
  const [remarks, setRemarks] = useState('');
  const [lead, setLead] = useState<ProspectRow | null>(null);
  const [leadNote, setLeadNote] = useState('');
  const [del, setDel] = useState<ProspectRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');

  const { data: universe = [] } = useQuery({
    queryKey: ['prospects-universe'],
    queryFn: () => prospectsService.list(),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ['prospects-universe'] });

  const toggle = (list: string[], set: (v: string[]) => void) => (v: string) =>
    set(list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

  // Chip pipeline: vertical chips count the whole universe; the sub-sector row
  // follows the selected verticals; the status row follows both.
  const afterVerticals = useMemo(
    () => (verticals.length
      ? universe.filter((r) => (r.verticals || []).some((v) => verticals.includes(v)))
      : universe),
    [universe, verticals]);
  const afterSubs = useMemo(
    () => (subs.length
      ? afterVerticals.filter((r) => (r.sub_sectors || []).some((s) => subs.includes(s)))
      : afterVerticals),
    [afterVerticals, subs]);
  const filtered = useMemo(
    () => (statuses.length
      ? afterSubs.filter((r) => statuses.includes(r.status)) : afterSubs),
    [afterSubs, statuses]);
  const displayRows = useMemo<Row[]>(() => filtered.map((r) => ({
    ...r,
    sectors: [...(r.verticals || []), ...(r.sub_sectors || [])].join(', '),
    leads: (r.lead_ids || []).length,
    status_label: STATUS_LABEL[r.status] || r.status,
  })), [filtered]);
  const statusCounts = useMemo(() => {
    const c = new Map<string, number>();
    afterSubs.forEach((r) => c.set(r.status, (c.get(r.status) || 0) + 1));
    return STATUSES.filter((s) => c.has(s)).map((s) => [s, c.get(s) || 0]) as
      [string, number][];
  }, [afterSubs]);

  const openWork = (r: ProspectRow) => {
    setWork(r); setStatus(r.status); setRemarks(r.remarks || '');
  };
  const saveWork = async () => {
    if (!work) return;
    setBusy(true);
    try {
      await prospectsService.update(work.id, { status, remarks });
      setWork(null); refresh();
    } catch (e: any) { setErr(apiErr(e, 'update the prospect')); }
    finally { setBusy(false); }
  };

  const runCreateLead = async () => {
    if (!lead) return;
    setBusy(true);
    try {
      const r = await prospectsService.createLead(lead.id,
        { rm: user.full, notes: leadNote.trim() || undefined });
      setLead(null); setLeadNote('');
      setMsg(`Lead ${r.lead_no} created for ${lead.name} `
        + `(${r.lead_count > 1 ? `${r.lead_count} leads now` : 'first lead'}).`);
      refresh();
    } catch (e: any) { setErr(apiErr(e, 'create the lead')); }
    finally { setBusy(false); }
  };

  const doDelete = async () => {
    if (!del) return;
    try { await prospectsService.remove(del.id); setDel(null); refresh(); }
    catch (e: any) { setDel(null); setErr(apiErr(e, 'delete the prospect')); }
  };

  // Every column carries a funnel. The categorical ones (localFilter) offer the
  // book's distinct values as checkboxes — Sectors' joined cell is comma-split
  // into one option per tag, and matched back token-wise, so ticking "Rooftop
  // EPC" finds every row that carries it. Remarks is prose, so it filters by
  // CONTAINS (textFilter) rather than offering 609 one-off sentences. Status
  // reads the materialised label field so the funnel says "Lead created", not
  // the wire's lead_created.
  const columns = useMemo<MRT_ColumnDef<Row>[]>(() => [
    { accessorKey: 'prospect_no', header: 'Code', size: 90,
      meta: { localFilter: true } },
    { accessorKey: 'name', header: 'Company', size: 240, meta: { localFilter: true },
      Cell: ({ row }) => (
        <Box>
          <b>{row.original.name}</b>
          <Typography sx={{ fontSize: 11, color: tokens.muted }}>
            {[row.original.domain, row.original.cin].filter(Boolean).join(' · ')}
          </Typography>
        </Box>),
      muiTableBodyCellProps: { sx: { whiteSpace: 'normal' } } },
    { accessorKey: 'sectors', header: 'Sectors', size: 200,
      meta: { localFilter: true },
      Cell: ({ row }) => (
        <Box sx={{ display: 'flex', gap: 0.4, flexWrap: 'wrap' }}>
          {(row.original.verticals || []).map((v) => (
            <Chip key={v} size="small" color="primary" variant="outlined" label={v}
              sx={{ fontSize: 10.6, height: 20 }} />))}
          {(row.original.sub_sectors || []).map((s) => (
            <Chip key={s} size="small" variant="outlined" label={s}
              sx={{ fontSize: 10.6, height: 20 }} />))}
        </Box>) },
    { accessorKey: 'state', header: 'State', size: 110, meta: { localFilter: true } },
    { accessorKey: 'revenue_cr', header: 'Rev ₹ Cr', size: 90,
      meta: { localFilter: true },
      Cell: ({ cell }) => {
        const v = cell.getValue<number | null>();
        return v == null ? '' : Number(v).toLocaleString('en-IN',
          { maximumFractionDigits: 1 });
      } },
    { accessorKey: 'founded_year', header: 'Founded', size: 80,
      meta: { localFilter: true } },
    { accessorKey: 'status_label', header: 'Status', size: 120,
      meta: { localFilter: true },
      Cell: ({ row }) => {
        const s = row.original.status;
        return <Chip size="small" color={STATUS_COLOR[s] || 'default'}
          label={STATUS_LABEL[s] || s} sx={{ fontSize: 10.8, height: 21 }} />;
      } },
    // Remarks sits BESIDE Status, not last: defined after Leads it lived under
    // the pinned Actions column, scrolled out of sight, and the desk thought the
    // remark only existed inside the edit dialog.
    { accessorKey: 'remarks', header: 'Remarks', size: 260,
      meta: { textFilter: true },
      Cell: ({ cell }) => {
        const s = cell.getValue<string>() || '';
        return <span title={s} style={{ display: '-webkit-box', WebkitLineClamp: 3,
          WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{s}</span>;
      },
      muiTableBodyCellProps: { sx: { whiteSpace: 'normal', minWidth: 160, maxWidth: 260 } } },
    { accessorKey: 'leads', header: 'Leads', size: 70,
      meta: { localFilter: true },
      Cell: ({ row }) => {
        const n = (row.original.lead_ids || []).length;
        return n ? <b>{n}</b> : <span style={{ color: tokens.muted }}>—</span>;
      } },
  ], []);

  const canLead = can(user.roles, 'addLead');

  return (
    <>
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.8, mb: 1.2, mt: 0.4 }}>
        <ChipRow label="Vertical" items={countTags(universe, 'verticals')}
          selected={verticals} onToggle={toggle(verticals, setVerticals)} />
        <ChipRow label="Sub-sector" items={countTags(afterVerticals, 'sub_sectors')}
          selected={subs} onToggle={toggle(subs, setSubs)} />
        <ChipRow label="Status"
          items={statusCounts.map(([s, n]) => [STATUS_LABEL[s] || s, n]) as
            [string, number][]}
          selected={statuses.map((s) => STATUS_LABEL[s] || s)}
          onToggle={(label) => {
            const key = STATUSES.find((s) => (STATUS_LABEL[s] || s) === label) || label;
            toggle(statuses, setStatuses)(key);
          }} />
      </Box>

      <CommonTable<Row>
        queryKey={['prospects', verticals, subs, statuses,
          universe.length] as unknown[]}
        fetcher={async (q) => applyQuery(displayRows,
          { ...q, searchFields: ['prospect_no', 'name', 'cin', 'domain', 'remarks'] })}
        columns={columns}
        csvName="atlas_prospects"
        onEdit={openWork}
        onRowClick={openWork}
        onDelete={can(user.roles, 'deleteRow') ? (r) => setDel(r) : undefined}
        extraActions={canLead ? (r) => (
          <Tooltip title="Create lead">
            <Button size="small" onClick={(e) => { e.stopPropagation(); setLead(r); }}
              sx={{ minWidth: 0, p: '3px' }}>
              <PersonAddAlt1Icon fontSize="small" />
            </Button>
          </Tooltip>) : undefined}
        mobileCard={{
          primary: (r) => r.name,
          value: (r) => <Chip size="small" label={STATUS_LABEL[r.status] || r.status}
            color={STATUS_COLOR[r.status] || 'default'} />,
        }}
      />

      {/* The desk verbs: status + remarks. Master data lives with the curators. */}
      <Dialog open={!!work} onClose={() => !busy && setWork(null)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontSize: 15.5 }}>{work?.name}</DialogTitle>
        <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 1.6, pt: '8px !important' }}>
          <TextField select size="small" label="Status" value={status}
            onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => (
              <MenuItem key={s} value={s}>{STATUS_LABEL[s]}</MenuItem>))}
          </TextField>
          <TextField size="small" label="Remarks" value={remarks} multiline minRows={3}
            onChange={(e) => setRemarks(e.target.value)}
            placeholder="Met at REI Expo; CFO open to a WC discussion after Diwali…" />
          <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
            Company, sector and financial fields are curated data — maintained through
            Tools → Prospects (import/export) by Management/Admin.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setWork(null)} disabled={busy}>Cancel</Button>
          <Button variant="contained" onClick={saveWork} disabled={busy}>Save</Button>
        </DialogActions>
      </Dialog>

      {/* Create lead — repeatable by design: one company, many asks over time. */}
      <Dialog open={!!lead} onClose={() => !busy && setLead(null)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontSize: 15.5 }}>Create lead — {lead?.name}</DialogTitle>
        <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 1.4, pt: '8px !important' }}>
          {(lead?.lead_ids?.length || 0) > 0 && (
            <Alert severity="info" sx={{ fontSize: 12.2 }}>
              This prospect already has {lead?.lead_ids?.length} lead(s) — creating
              another is fine: a company can carry several asks, and they all link to
              the same client master.
            </Alert>)}
          <TextField size="small" label="Note for the lead (optional)" value={leadNote}
            multiline minRows={2} onChange={(e) => setLeadNote(e.target.value)} />
          <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
            The client master is settled automatically — an existing company is linked,
            a new one is born as a Prospect master, exactly as with any lead.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setLead(null)} disabled={busy}>Cancel</Button>
          <Button variant="contained" onClick={runCreateLead} disabled={busy}>
            Create lead</Button>
        </DialogActions>
      </Dialog>

      <ConfirmDialog open={!!del} title="Delete prospect"
        message={`Delete ${del?.name}? This cannot be undone.`}
        onCancel={() => setDel(null)} onConfirm={doDelete} />

      <Snackbar open={!!msg} autoHideDuration={5000} onClose={() => setMsg('')}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="success" onClose={() => setMsg('')} sx={{ fontSize: 12.4 }}>{msg}</Alert>
      </Snackbar>
      <Snackbar open={!!err} autoHideDuration={5000} onClose={() => setErr('')}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="warning" onClose={() => setErr('')} sx={{ fontSize: 12.4 }}>{err}</Alert>
      </Snackbar>
    </>
  );
}
