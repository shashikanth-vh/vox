import { useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Snackbar, Alert, Button } from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import ConfirmDialog from '../../components/common/ConfirmDialog';
import { useAuth } from '../../auth/AuthContext';
import { canWrite, can, whoCan } from '../../auth/rbac';
import { CodeText, LensPill, LifePill } from '../../components/common/Pills';
import CompanyDrawer from '../Deals/CompanyDrawer';
import AddProductDialog from '../Deals/AddProductDialog';
import AddClientDialog from './AddClientDialog';
import { clientsService } from '../../services/clientsService';
import type { ClientRow } from './client.types';

export default function ClientsPage() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const ro = !canWrite(user.roles, 'clients');
  const [open, setOpen] = useState<string | null>(null);
  const [addProd, setAddProd] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [del, setDel] = useState<ClientRow | null>(null);
  const [err, setErr] = useState('');
  const refresh = () => qc.invalidateQueries();

  const doDelete = async () => {
    if (!del) return;
    const r = await clientsService.remove(del.code, user.full);
    setDel(null);
    if (!r.ok) { setErr(r.error || 'Could not delete'); return; }
    refresh();
  };

  // Columns mirror v12's vClients(): code · name · sector · lens · lifecycle · state · about.
  const columns = useMemo<MRT_ColumnDef<ClientRow>[]>(() => [
    { accessorKey: 'code', header: 'Group Code', size: 120, meta: { localFilter: true }, Cell: ({ cell }) => <CodeText code={cell.getValue<string>()} /> },
    // v12 marks this column `wrap` — long legal names run to a second line.
    { accessorKey: 'name', header: 'Legal name', size: 260, meta: { localFilter: true }, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b>,
      muiTableBodyCellProps: { sx: { whiteSpace: 'normal' } } },
    { accessorKey: 'sector', header: 'Sector', size: 160, meta: { localFilter: true } },
    { accessorKey: 'lens', header: 'Lens', size: 80, meta: { localFilter: true }, Cell: ({ cell }) => <LensPill lens={cell.getValue<string>()} /> },
    { id: 'lifecycle', header: 'Lifecycle', size: 150, meta: { localFilter: true },
      accessorFn: (r: any) => r.lifecycle || 'Prospect',
      Cell: ({ cell }) => <LifePill stage={cell.getValue<string>()} /> },
    { accessorKey: 'state', header: 'State', size: 120, meta: { localFilter: true } },
    // v12: `wrap` + trunc(about, 100) — wraps to a few lines, then ellipsis, full text on hover.
    { accessorKey: 'about', header: 'About', size: 330,
      Cell: ({ cell }) => {
        const s = cell.getValue<string>() || '';
        return <span title={s} style={{ display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{s}</span>;
      },
      muiTableBodyCellProps: { sx: { whiteSpace: 'normal', minWidth: 170, maxWidth: 330 } } },
  ], []);

  return (
    <>
      <CommonTable<ClientRow>
        queryKey={['clients']}
        fetcher={(q) => clientsService.list(q)}
        columns={columns}
        csvName="atlas_clients"
        toolbarLeft={!ro && <Button startIcon={<AddIcon />} variant="contained" onClick={() => setAddOpen(true)}>Add client</Button>}
        // Actions: edit · download row CSV (built into CommonTable) · delete.
        // v12 gates the delete on Admin; everyone else sees edit + download only.
        onEdit={(r) => setOpen(r.code)}
        editReason={ro ? whoCan('editDealProfile') : ''}
        onDelete={can(user.roles, 'deleteRow') ? (r) => setDel(r) : undefined}
        // v12: the whole row is clickable and opens the company drawer.
        onRowClick={(r) => setOpen(r.code)}
        mobileCard={{
          primary: (r) => r.name,
          value: (r) => <LifePill stage={(r as any).lifecycle || 'Prospect'} />,
        }}
      />
      <CompanyDrawer code={open} onClose={() => setOpen(null)} onChanged={refresh} onAddProduct={(c) => setAddProd(c)} />
      <AddProductDialog code={addProd} onClose={() => setAddProd(null)} onDone={refresh} />
      <AddClientDialog open={addOpen} onClose={() => setAddOpen(false)} onSaved={refresh} />
      <ConfirmDialog open={!!del} title="Delete client"
        message={`Delete ${del?.name || del?.code}? This cannot be undone.`}
        onCancel={() => setDel(null)} onConfirm={doDelete} />
      <Snackbar open={!!err} autoHideDuration={4000} onClose={() => setErr('')}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="warning" onClose={() => setErr('')} sx={{ fontSize: 12.4 }}>{err}</Alert>
      </Snackbar>
    </>
  );
}
