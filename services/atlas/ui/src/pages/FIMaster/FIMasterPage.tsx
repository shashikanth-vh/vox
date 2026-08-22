import { useMemo, useState } from 'react';
import { Chip, Box, Typography, ToggleButtonGroup, ToggleButton } from '@mui/material';
import TableRowsIcon from '@mui/icons-material/TableRows';
import GridViewIcon from '@mui/icons-material/GridView';
import AddIcon from '@mui/icons-material/Add';
import { useQueryClient } from '@tanstack/react-query';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, FormControlLabel, Switch } from '@mui/material';
import { TextFld } from '../../components/common/Field';
import FICards from './FICards';
import AddFIDialog from './AddFIDialog';
import BankLedgerDialog from './BankLedgerDialog';
import CompanyDrawer from '../Deals/CompanyDrawer';
import { fiService } from '../../services/fiService';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import ExportBar, { toCsv, saveCsv } from '../../components/common/ExportBar';
import { tokens } from '../../theme';
import type { FiRow } from './fi.types';

// `mode`/`onModeChange` let a parent (Masters) own the view toggle and place it on
// the sub-tab line; standalone, the page keeps its own toggle and internal state.
export default function FIMasterPage({ mode: modeProp, onModeChange }: { mode?: 'table' | 'cards'; onModeChange?: (m: 'table' | 'cards') => void } = {}) {
  const { user } = useAuth();
  const ro = !can(user.roles, 'editFI');
  const qc = useQueryClient();
  const [modeInternal, setModeInternal] = useState<'table' | 'cards'>('table');
  const controlled = modeProp !== undefined;
  const mode = modeProp ?? modeInternal;
  const setMode = (m: 'table' | 'cards') => { if (onModeChange) onModeChange(m); else setModeInternal(m); };
  const [focus, setFocus] = useState<string | null>(null);
  const [edit, setEdit] = useState<FiRow | null>(null);
  const [view, setView] = useState<FiRow | null>(null);
  const [company, setCompany] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [form, setForm] = useState<Partial<FiRow>>({});
  const refresh = () => qc.invalidateQueries({ queryKey: ['fi'] });

  // Card view has no MRT toolbar, so it gets its own "Export this view" (the full FI list).
  const exportCsv = async () => {
    const res = await fiService.list({ pageIndex: 0, pageSize: 100000, globalFilter: '', sorting: [], columnFilters: [] });
    const rows = res.rows.map((r: FiRow) => [r.name, r.inactive ? 'Inactive' : 'Active', r.type || '', r.preferredSectors || r.sectors || '', r.pursued || 0, r.live || 0, r.ip || 0, r.sanc || 0, r.decl || 0, r.notes || '']);
    saveCsv(toCsv(['Bank', 'Status', 'Type', 'Preferred sectors', '# Pursued', 'Live', 'IP', 'Approved', 'Declined', 'Notes'], rows), 'atlas_fi_master');
  };

  // Right-aligned count column, "—" when zero (v12 renders the em dash for 0).
  const num = (id: keyof FiRow, header: string, size = 100): MRT_ColumnDef<FiRow> => ({
    accessorKey: id as string, header, size,
    muiTableHeadCellProps: { align: 'right' },
    muiTableBodyCellProps: { align: 'right', sx: { fontVariantNumeric: 'tabular-nums' } },
    Cell: ({ cell }) => <>{cell.getValue<number>() || '—'}</>,
  });

  // Columns mirror v12 vFI() table mode.
  const columns = useMemo<MRT_ColumnDef<FiRow>[]>(() => [
    { accessorKey: 'name', header: 'Bank', size: 220, meta: { localFilter: true }, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b> },
    { id: 'inactive', header: 'Status', size: 100, meta: { localFilter: true },
      accessorFn: (r: any) => (r.inactive ? 'INACTIVE' : 'ACTIVE'),
      Cell: ({ cell }) => <Chip size="small" variant="outlined" label={cell.getValue<string>()}
        color={cell.getValue() === 'INACTIVE' ? 'default' : 'success'} /> },
    { accessorKey: 'type', header: 'Type', size: 130, meta: { localFilter: true } },
    { id: 'sectors', header: 'Preferred sectors', size: 240, meta: { localFilter: true },
      accessorFn: (r) => r.preferredSectors || r.sectors || '',
      muiTableBodyCellProps: { sx: { whiteSpace: 'normal', minWidth: 170, maxWidth: 330 } } },
    num('pursued', '# Pursued'),
    num('live', 'Live', 80),
    num('ip', 'IP', 80),
    num('sanc', 'Approved', 110),
    num('decl', 'Declined', 100),
    { accessorKey: 'notes', header: 'Notes', size: 260,
      Cell: ({ cell }) => {
        const s = cell.getValue<string>() || '';
        return <span title={s} style={{ color: tokens.muted }}>{s.slice(0, 80)}</span>;
      },
      muiTableBodyCellProps: { sx: { whiteSpace: 'normal', minWidth: 170, maxWidth: 330 } } },
  ], []);

  const openEdit = (r: FiRow) => { setEdit(r); setForm({ preferredSectors: r.preferredSectors || r.sectors, notes: r.notes, inactive: r.inactive }); };
  const save = () => { if (edit) fiService.update(edit._i, form, user.full); setEdit(null); refresh(); };

  const addBtn = !ro && <Button startIcon={<AddIcon />} variant="contained" onClick={() => setAddOpen(true)}>Add bank / FI</Button>;

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
          Detailed view. Use the card actions to view or edit a lender.
        </Typography>
      )}

      {mode === 'table' ? (
        <>
          <CommonTable<FiRow>
            queryKey={['fi']}
            fetcher={(q) => fiService.list(q)}
            columns={columns}
            csvName="atlas_fi_master"
            toolbarLeft={addBtn}
            onEdit={ro ? undefined : openEdit}
            // No View icon — a row click opens that bank's dialog (its full engagement
            // + ledger, v12 fiOpenDrawer → openBank), it does not switch to card view.
            onRowClick={(r) => setView(r)}
            mobileCard={{
              primary: (r) => r.name,
              value: (r) => (
                <Chip size="small" variant="outlined" label={r.inactive ? 'INACTIVE' : 'ACTIVE'}
                  color={r.inactive ? 'default' : 'success'} />
              ),
            }}
          />
          <Typography sx={{ fontSize: 11.6, color: tokens.muted, mt: 1 }}>
            Click a row to see the full deal ledger for that bank. Switch to Card view to edit notes and preferred sectors inline.
          </Typography>
        </>
      ) : (
        <>
          <Box sx={{ display: 'flex', alignItems: 'center', mb: 1.2, gap: 1, flexWrap: 'wrap' }}>
            {addBtn}<Box sx={{ flex: 1 }} /><ExportBar onCsv={exportCsv} />
          </Box>
          <FICards openName={focus} onOpenCompany={(c) => setCompany(c)}
            onEdit={ro ? undefined : openEdit} />
        </>
      )}

      {/* v12 openBank — the full deal ledger, opened by a row click or the View action. */}
      <BankLedgerDialog bankName={view?.name ?? null} onClose={() => setView(null)}
        onEdit={ro || !view ? undefined : () => openEdit(view)} onOpenCompany={(c) => setCompany(c)} />

      {/* Edit — the form behind the pencil icon. */}
      <Dialog open={!!edit} onClose={() => setEdit(null)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: 16 }}>{edit?.name}</DialogTitle>
        <DialogContent dividers>
          <Box sx={{ display: 'grid', gap: 1.4 }}>
            <TextFld label="Preferred sectors" value={form.preferredSectors} disabled={ro} placeholder="e.g. Solar, EV, MSME"
              onChange={(v) => setForm((p) => ({ ...p, preferredSectors: v }))} />
            <TextFld label="Notes (appetite, contacts, quirks)" value={form.notes} disabled={ro} multiline
              placeholder="e.g. Won’t fund merchant solar; needs DSCR>1.4"
              onChange={(v) => setForm((p) => ({ ...p, notes: v }))} />
            <FormControlLabel control={<Switch checked={!!form.inactive} disabled={ro}
              onChange={(e) => setForm((p) => ({ ...p, inactive: e.target.checked }))} />} label="Inactive" />
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEdit(null)} variant="outlined">Cancel</Button>
          {!ro && <Button onClick={save} variant="contained">Save</Button>}
        </DialogActions>
      </Dialog>

      <CompanyDrawer code={company} onClose={() => setCompany(null)} onChanged={refresh} />
      <AddFIDialog open={addOpen} onClose={() => setAddOpen(false)} onSaved={refresh} />
    </>
  );
}
