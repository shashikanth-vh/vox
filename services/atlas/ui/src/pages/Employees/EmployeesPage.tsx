import { useEffect, useMemo, useState } from 'react';
import { Button, Chip, Box, Typography, ToggleButtonGroup, ToggleButton, Stack, Dialog as MuiDialog, DialogTitle as MuiDialogTitle, DialogContent as MuiDialogContent, DialogActions as MuiDialogActions, TextField, MenuItem } from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import TableRowsIcon from '@mui/icons-material/TableRows';
import GridViewIcon from '@mui/icons-material/GridView';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import EmployeeDialog from './EmployeeDialog';
import EmployeeCards from './EmployeeCards';
import { employeesService } from '../../services/employeesService';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import ExportBar, { toCsv, saveCsv } from '../../components/common/ExportBar';
import { tokens } from '../../theme';
import type { Employee } from './employee.types';

// `mode`/`onModeChange` let a parent (Masters) own the view toggle and place it on
// the sub-tab line; standalone, the page keeps its own toggle and internal state.
export default function EmployeesPage({ mode: modeProp, onModeChange }: { mode?: 'table' | 'cards'; onModeChange?: (m: 'table' | 'cards') => void } = {}) {
  const { user } = useAuth();
  const ro = !can(user.roles, 'editEmployee');
  const qc = useQueryClient();
  const nav = useNavigate();
  const filterDash = (name: string) => nav(`/dashboard?person=${encodeURIComponent(name)}`);
  const [modeInternal, setModeInternal] = useState<'table' | 'cards'>('table');
  const controlled = modeProp !== undefined;
  const mode = modeProp ?? modeInternal;
  const setMode = (m: 'table' | 'cards') => { if (onModeChange) onModeChange(m); else setModeInternal(m); };
  const [focus, setFocus] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ mode: 'add' | 'edit'; emp: Employee | null } | null>(null);
  const [del, setDel] = useState<Employee | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ['employees'] });

  // Reconcile the roster from Access on open: a user provisioned through Postman (or
  // any path that never wrote the people table) becomes a roster row — and therefore
  // a name the BDRM/Analyst dropdowns can offer — the moment someone looks here.
  useEffect(() => {
    void employeesService.syncFromAccess().then(refresh);
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  // Card view has no MRT toolbar, so it gets its own "Export this view" (the full team).
  const exportCsv = async () => {
    const res = await employeesService.list({ pageIndex: 0, pageSize: 100000, globalFilter: '', sorting: [], columnFilters: [] });
    const rows = res.rows.map((e: Employee) => [e.name, e.full || '', e.role || '', e.geography || '', e.sectors || '', e.reportsTo || '', e.inactive ? 'Inactive' : 'Active', employeesService.bookRollup(e.name).total]);
    saveCsv(toCsv(['Name', 'Full name', 'Role', 'Geography', 'Sectors', 'Reports to', 'Status', 'Book size'], rows), 'atlas_employees');
  };

  const columns = useMemo<MRT_ColumnDef<Employee>[]>(() => [
    {
      accessorKey: 'name', header: 'Name', size: 210, meta: { localFilter: true },
      Cell: ({ row }) => (
        <span>
          {(row.original as any).noSignIn ? (
            <Chip label="no sign-in" size="small" title={'On the roster (dropdowns and conversions accept them) but they CANNOT log in — no Access identity exists. Create their user via Add employee with this e-mail to grant sign-in.'}
              sx={{ mr: 0.6, height: 18, fontSize: 10, bgcolor: '#E3F2FD', color: '#0D47A1' }} />
          ) : !employeesService.onRoster(row.original) && (
            <Chip label="sign-in only" size="small" title={'This person can sign in but has no register roster row yet — no dropdown can offer them. Save them once from this screen (or run the roster sync) to fix it.'}
              sx={{ mr: 0.6, height: 18, fontSize: 10, bgcolor: '#FFF3E0', color: '#8A5300' }} />
          )}
          <Box component="b" title="Filter Dashboard to this person’s book"
            onClick={(e) => { e.stopPropagation(); filterDash(row.original.name); }}
            sx={{ color: tokens.teal, cursor: 'pointer', fontWeight: 700, '&:hover': { textDecoration: 'underline' } }}>{row.original.name}</Box>{' '}
          <Box component="span" sx={{ color: tokens.muted, fontSize: 11.6 }}>{row.original.full}</Box>
        </span>
      ),
    },
    {
      // Role can hold several stacked roles ("Admin, Management") — render each as a chip.
      accessorKey: 'role', header: 'Role', size: 170, meta: { localFilter: true },
      Cell: ({ cell }) => (
        <Stack direction="row" gap={0.4} flexWrap="wrap">
          {String(cell.getValue() ?? '').split(',').map((s) => s.trim()).filter(Boolean).map((r) => (
            <Chip key={r} label={r} size="small" sx={{ height: 19, fontSize: 10.6, fontWeight: 600 }} />
          ))}
        </Stack>
      ),
    },
    { accessorKey: 'geography', header: 'Geography', size: 150, meta: { localFilter: true } },
    { accessorKey: 'sectors', header: 'Sectors', size: 150, meta: { localFilter: true } },
    { accessorKey: 'reportsTo', header: 'Reports to', size: 130, meta: { localFilter: true } },
    // The STRING is the value ('Active'/'Inactive'), so the facet reads as words rather
    // than true/false — and applyQuery matches the row by the same words.
    { id: 'inactive', header: 'Status', size: 100, meta: { localFilter: true },
      accessorFn: (r: any) => (r.inactive ? 'Inactive' : 'Active'),
      Cell: ({ cell }) => <Chip size="small" label={cell.getValue<string>()} color={cell.getValue() === 'Inactive' ? 'default' : 'success'} variant="outlined" /> },
    {
      id: 'book', header: 'Book size', size: 100, enableSorting: false,
      muiTableHeadCellProps: { align: 'right' }, muiTableBodyCellProps: { align: 'right' },
      Cell: ({ row }) => <span style={{ fontVariantNumeric: 'tabular-nums', fontWeight: 600 }}>{employeesService.bookRollup(row.original.name).total}</span>,
    },
  ], []);

  const addBtn = !ro && <Button startIcon={<AddIcon />} variant="contained" onClick={() => setDialog({ mode: 'add', emp: null })}>Add employee</Button>;

  return (
    <>
      {!controlled && (
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 1, mb: 1, flexWrap: 'wrap' }}>
          <ToggleButtonGroup exclusive size="small" value={mode} onChange={(_, v) => { if (v) { setMode(v); setFocus(null); } }}>
            <ToggleButton value="table"><TableRowsIcon fontSize="small" sx={{ mr: 0.5 }} />Table view</ToggleButton>
            <ToggleButton value="cards"><GridViewIcon fontSize="small" sx={{ mr: 0.5 }} />Card view</ToggleButton>
          </ToggleButtonGroup>
        </Box>
      )}

      {mode === 'cards' && (
        <Typography sx={{ fontSize: 11.6, color: tokens.muted, mb: 1 }}>
          Detailed view. Edit or remove people using the card actions.
        </Typography>
      )}

      {mode === 'table' ? (
        <>
          <CommonTable<Employee>
            queryKey={['employees']}
            fetcher={(q) => employeesService.list(q)}
            columns={columns}
            csvName="atlas_employees"
            toolbarLeft={addBtn}
            onRowClick={(e) => { setFocus(e.name); setMode('cards'); }}
            onEdit={ro ? undefined : (e) => setDialog({ mode: 'edit', emp: e })}
            onDelete={ro ? undefined : (e) => setDel(e)}
            // The name keeps its own click (filter Dashboard to this person's book) —
            // it is a separate action from the row's, on a phone as on the desktop.
            mobileCard={{
              primary: (e) => (
                <Box component="span" title="Filter Dashboard to this person’s book"
                  onClick={(ev) => { ev.stopPropagation(); filterDash(e.name); }}
                  sx={{ color: tokens.teal, cursor: 'pointer', fontWeight: 700,
                    '&:hover': { textDecoration: 'underline' } }}>{e.name}</Box>
              ),
              value: (e) => (
                <span style={{ fontVariantNumeric: 'tabular-nums', fontWeight: 600 }}>
                  {employeesService.bookRollup(e.name).total}
                </span>
              ),
            }}
          />
          <Typography sx={{ fontSize: 11.6, color: tokens.muted, mt: 1 }}>
            Click a row to open detail. Click a name to filter Dashboard to that person’s book. Switch to Card view to edit fields inline.
          </Typography>
        </>
      ) : (
        <>
          <Box sx={{ display: 'flex', alignItems: 'center', mb: 1.2, gap: 1, flexWrap: 'wrap' }}>
            {addBtn}<Box sx={{ flex: 1 }} /><ExportBar onCsv={exportCsv} />
          </Box>
          <EmployeeCards
            openName={focus}
            onEdit={ro ? undefined : (e) => setDialog({ mode: 'edit', emp: e })}
            onDelete={ro ? undefined : (e) => setDel(e)}
          />
        </>
      )}

      {dialog && <EmployeeDialog mode={dialog.mode} emp={dialog.emp} onClose={() => setDialog(null)} onDone={refresh} />}
      {del && <DeleteEmployeeDialog emp={del} by={user.full}
        onClose={() => setDel(null)} onDone={() => { setDel(null); refresh(); }} />}
    </>
  );
}


/**
 * Delete = revoke + retire + HAND OVER. A leaver's book (every lead/deal/tracker naming
 * them, plus their active assignments) must belong to someone the moment they go — an
 * orphaned book is work nobody is doing and nobody can see. The successor choice is
 * explicit: either a colleague (the register moves everything atomically and reports
 * counts) or a deliberate "leave the records as they are" for the person who owned
 * nothing.
 */
function DeleteEmployeeDialog({ emp, by, onClose, onDone }: {
  emp: Employee; by: string; onClose: () => void; onDone: () => void;
}) {
  const [candidates, setCandidates] = useState<Employee[]>([]);
  const [successor, setSuccessor] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    void employeesService.list({ pageIndex: 0, pageSize: 1000, globalFilter: '', sorting: [], columnFilters: [] })
      .then((res) => setCandidates((res.rows as Employee[]).filter(
        (e) => e.name !== emp.name && !e.inactive)));
  }, [emp.name]);

  const go = async () => {
    setBusy(true); setErr('');
    try {
      if (successor) {
        const to = candidates.find((c) => c.name === successor);
        if (to) {
          const moved = await employeesService.handover(emp, to);
          const parts = Object.entries(moved).filter(([, n]) => n > 0)
            .map(([k, n]) => `${k}: ${n}`).join(', ');
          if (parts) console.info(`[handover] ${emp.full} → ${to.full}: ${parts}`);
        }
      }
      await employeesService.remove(emp.name, by);
      onDone();
    } catch (e: any) {
      setErr(e?.message || 'Could not remove this employee.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <MuiDialog open onClose={busy ? undefined : onClose} maxWidth="xs" fullWidth>
      <MuiDialogTitle sx={{ fontSize: 16 }}>Delete {emp.full || emp.name}</MuiDialogTitle>
      <MuiDialogContent>
        <Typography sx={{ fontSize: 13, mb: 1.5 }}>
          Their sign-in is revoked immediately and they leave every list. Everything they
          own — leads, deals, product lines, assignments — should belong to someone:
        </Typography>
        <TextField select fullWidth size="small" label="Hand the book over to"
          value={successor} onChange={(e) => setSuccessor(e.target.value)}
          helperText={successor ? 'Every record naming them moves to this person.'
            : 'Or leave the records as they are (only right if they owned nothing).'}>
          <MenuItem value="">— no handover —</MenuItem>
          {candidates.map((c) => (
            <MenuItem key={c.name} value={c.name}>{c.full || c.name} ({c.role})</MenuItem>
          ))}
        </TextField>
        {err && <Typography sx={{ fontSize: 12, color: 'error.main', mt: 1 }}>{err}</Typography>}
      </MuiDialogContent>
      <MuiDialogActions>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button variant="contained" color="error" onClick={() => void go()} disabled={busy}>
          {successor ? 'Hand over & delete' : 'Delete'}
        </Button>
      </MuiDialogActions>
    </MuiDialog>
  );
}
