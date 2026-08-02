import { useMemo, useState } from 'react';
import { Button, Chip, Box, Typography, ToggleButtonGroup, ToggleButton, Stack } from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import TableRowsIcon from '@mui/icons-material/TableRows';
import GridViewIcon from '@mui/icons-material/GridView';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import ConfirmDialog from '../../components/common/ConfirmDialog';
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

  // Card view has no MRT toolbar, so it gets its own "Export this view" (the full team).
  const exportCsv = async () => {
    const res = await employeesService.list({ pageIndex: 0, pageSize: 100000, globalFilter: '', sorting: [], columnFilters: [] });
    const rows = res.rows.map((e: Employee) => [e.name, e.full || '', e.role || '', e.geography || '', e.sectors || '', e.reportsTo || '', e.inactive ? 'Inactive' : 'Active', employeesService.bookRollup(e.name).total]);
    saveCsv(toCsv(['Name', 'Full name', 'Role', 'Geography', 'Sectors', 'Reports to', 'Status', 'Book size'], rows), 'atlas_employees');
  };

  const columns = useMemo<MRT_ColumnDef<Employee>[]>(() => [
    {
      accessorKey: 'name', header: 'Name', size: 210,
      Cell: ({ row }) => (
        <span>
          <Box component="b" title="Filter Dashboard to this person’s book"
            onClick={(e) => { e.stopPropagation(); filterDash(row.original.name); }}
            sx={{ color: tokens.teal, cursor: 'pointer', fontWeight: 700, '&:hover': { textDecoration: 'underline' } }}>{row.original.name}</Box>{' '}
          <Box component="span" sx={{ color: tokens.muted, fontSize: 11.6 }}>{row.original.full}</Box>
        </span>
      ),
    },
    {
      // Role can hold several stacked roles ("Admin, Management") — render each as a chip.
      accessorKey: 'role', header: 'Role', size: 170,
      Cell: ({ cell }) => (
        <Stack direction="row" gap={0.4} flexWrap="wrap">
          {String(cell.getValue() ?? '').split(',').map((s) => s.trim()).filter(Boolean).map((r) => (
            <Chip key={r} label={r} size="small" sx={{ height: 19, fontSize: 10.6, fontWeight: 600 }} />
          ))}
        </Stack>
      ),
    },
    { accessorKey: 'geography', header: 'Geography', size: 150 },
    { accessorKey: 'sectors', header: 'Sectors', size: 150 },
    { accessorKey: 'reportsTo', header: 'Reports to', size: 130 },
    { accessorKey: 'inactive', header: 'Status', size: 100, Cell: ({ cell }) => <Chip size="small" label={cell.getValue() ? 'Inactive' : 'Active'} color={cell.getValue() ? 'default' : 'success'} variant="outlined" /> },
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
      <ConfirmDialog open={!!del} title="Delete employee" message={`Remove ${del?.full}?`}
        onCancel={() => setDel(null)} onConfirm={() => { if (del) employeesService.remove(del.name, user.full); setDel(null); refresh(); }} />
    </>
  );
}
