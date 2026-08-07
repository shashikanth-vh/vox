import { useMemo, useState } from 'react';
import { MenuItem, TextField } from '@mui/material';
import { useQueryClient } from '@tanstack/react-query';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import ConfirmDialog from '../../components/common/ConfirmDialog';
import CompanyDrawer from '../Deals/CompanyDrawer';
import AddProductDialog from '../Deals/AddProductDialog';
import AmSummary from './AmSummary';
import { assetMonService, amStatusOptions } from '../../services/assetMonService';
import { fmt } from '../../utils/format';
import { useAuth } from '../../auth/AuthContext';
import { can, scopeFor, whoCan } from '../../auth/rbac';
import { tokens } from '../../theme';
import type { AmRow } from './am.types';

export default function AssetMonPage() {
  const { user } = useAuth();
  const ro = !can(user.roles, 'editAM');
  const qc = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);
  const [addProd, setAddProd] = useState<string | null>(null);
  const [del, setDel] = useState<AmRow | null>(null);
  const refresh = () => qc.invalidateQueries();

  // Wrap + truncate to N chars, full text on hover (v12 trunc()).
  const truncCell = (n: number) => ({
    Cell: ({ cell }: any) => {
      const s = String(cell.getValue() ?? '');
      return <span title={s}>{s.length > n ? s.slice(0, n) + '…' : s}</span>;
    },
    muiTableBodyCellProps: { sx: { whiteSpace: 'normal', minWidth: 170, maxWidth: 330 } },
  });
  const numCell = { muiTableHeadCellProps: { align: 'right' as const }, muiTableBodyCellProps: { align: 'right' as const, sx: { fontVariantNumeric: 'tabular-nums' } } };

  // Columns mirror v12 vAM(): company · state · value · MW · nature · deal type ·
  // investor · investor type · status · date teaser shared · notes.
  const columns = useMemo<MRT_ColumnDef<AmRow>[]>(() => [
    { accessorKey: '_name', header: 'Company', size: 220, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b>,
      muiTableBodyCellProps: { sx: { whiteSpace: 'normal', minWidth: 170, maxWidth: 330 } } },
    { accessorKey: 'state', header: 'State', size: 120 },
    { accessorKey: 'val', header: 'Indicative Value (₹ Cr)', size: 160, ...numCell, Cell: ({ cell }) => fmt(cell.getValue()) },
    { accessorKey: 'mw', header: 'Size (MW)', size: 100, ...numCell, Cell: ({ cell }) => (cell.getValue() ? fmt(cell.getValue(), 1) : '') },
    { accessorKey: 'nature', header: 'Nature', size: 110 },
    { accessorKey: 'dtype', header: 'Deal Type', size: 140 },
    { accessorKey: 'inv', header: 'Investor', size: 180, ...truncCell(55) },
    { accessorKey: 'itype', header: 'Investor Type', size: 140 },
    {
      accessorKey: 'status', header: 'Status', size: 160,
      // Offers only the register's legal moves (forward one, back one, Dropped).
      // 'Closed' is absent on purpose: it is recorded by the AM closure APPROVAL —
      // open the company drawer and start the mandate run; the AM Head's decision
      // in Today closes the mandate with its evidence on file.
      Cell: ({ row }) => {
        const opts = amStatusOptions(row.original.status);
        return (
          <TextField select size="small" value={row.original.status} variant="outlined"
            disabled={ro || opts.length <= 1}
            title={row.original.status === 'SPA / Documentation'
              ? 'Closed is recorded by the AM closure approval (start the mandate run in the drawer)' : ''}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => { assetMonService.update(row.original.id, 'status', e.target.value, user.full); refresh(); }}
            sx={{ minWidth: 140, '& .MuiOutlinedInput-input': { fontSize: 12, py: '5px' }, '& .MuiOutlinedInput-root': { borderRadius: '7px', bgcolor: '#fff' } }}>
            {opts.map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
          </TextField>
        );
      },
    },
    { accessorKey: 'teaser', header: 'Date Teaser Shared', size: 150, Cell: ({ cell }) => cell.getValue<string>() || '' },
    { accessorKey: 'notes', header: 'Notes', size: 200, ...truncCell(55) },
  ], [ro]);

  return (
    <>
      <AmSummary />
      <CommonTable<AmRow>
        queryKey={['am']}
        fetcher={(q) => assetMonService.list(q, scopeFor(user.roles, 'am', user.name))}
        columns={columns}
        csvName="atlas_asset_monetisation"
        // No View icon — a row click opens the drawer.
        onEdit={(r) => setOpen(r.code)}
        editReason={ro ? whoCan('editAM') : ''}
        onDelete={ro ? undefined : (r) => setDel(r)}
        onRowClick={(r) => setOpen(r.code)}
        // v12 greys out Dropped rows.
        rowSx={(r) => (r.status === 'Dropped' ? { color: tokens.muted, backgroundColor: '#FAFBFB !important' } : {})}
      />
      <CompanyDrawer code={open} onClose={() => setOpen(null)} onChanged={refresh} onAddProduct={(c) => setAddProd(c)} />
      <AddProductDialog code={addProd} onClose={() => setAddProd(null)} onDone={refresh} />
      <ConfirmDialog open={!!del} title="Delete asset" message={`Remove ${del?._name}'s asset mandate?`}
        onCancel={() => setDel(null)} onConfirm={() => { if (del) assetMonService.remove(del.id, user.full); setDel(null); refresh(); }} />
    </>
  );
}
