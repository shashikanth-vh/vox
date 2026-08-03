import { useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import PageHint from '../../components/common/PageHint';
import { LensPill, TempPill, CodeText, ProductFlags } from '../../components/common/Pills';
import CompanyDrawer from './CompanyDrawer';
import AddProductDialog from './AddProductDialog';
import ConfirmDialog from '../../components/common/ConfirmDialog';
import { dealsService } from '../../services/dealsService';
import { useAuth } from '../../auth/AuthContext';
import { scopeFor, can, whoCan } from '../../auth/rbac';
import type { DealRow } from './deal.types';

export default function DealsPage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);
  const [del, setDel] = useState<DealRow | null>(null);
  const [addProd, setAddProd] = useState<string | null>(null);
  const refreshAll = () => { qc.invalidateQueries(); };

  const columns = useMemo<MRT_ColumnDef<DealRow>[]>(() => [
    { accessorKey: 'code', header: 'Group Code', size: 120, Cell: ({ cell }) => <CodeText code={cell.getValue<string>()} /> },
    { accessorKey: '_name', header: 'Company', size: 220, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b> },
    { accessorKey: 'temp', header: 'Temp', size: 90, Cell: ({ cell }) => <TempPill temp={cell.getValue<string>()} /> },
    { accessorKey: 'lens', header: 'Lens', size: 80, Cell: ({ row }) => <LensPill lens={(row.original as any).lens} /> },
    { accessorKey: 'rm', header: 'RM', size: 100 },
    { accessorKey: 'an', header: 'Analyst', size: 110 },
    { accessorKey: 'products', header: 'Products', size: 120, enableSorting: false, Cell: ({ row }) => <ProductFlags lend={row.original.lend} syn={row.original.syn} am={row.original.am} /> },
    { accessorKey: 'remarks', header: 'Remarks', size: 220 },
  ], []);

  return (
    <>
      <PageHint>One row per client relationship. Click a company to open its full profile — editable in place, with Add product.</PageHint>
      <CommonTable<DealRow>
        queryKey={['deals']}
        fetcher={(q) => dealsService.list(q, scopeFor(user.roles, 'deals', user.name))}
        columns={columns}
        csvName="atlas_deals"
        // No View icon — a row click opens the profile. Edit opens the same drawer;
        // Delete (Admin only) removes the deal row; the row-CSV icon is built in.
        onRowClick={(d) => setOpen(d.code)}
        onEdit={(d) => setOpen(d.code)}
        editReason={can(user.roles, 'editDealProfile') ? '' : whoCan('editDealProfile')}
        onDelete={can(user.roles, 'deleteRow') ? (d) => setDel(d) : undefined}
      />
      <CompanyDrawer code={open} onClose={() => setOpen(null)} onChanged={refreshAll} onAddProduct={(c) => setAddProd(c)} />
      <AddProductDialog code={addProd} onClose={() => setAddProd(null)} onDone={refreshAll} />
      <ConfirmDialog open={!!del} title="Delete deal" message={`Delete the deal for ${del?._name || del?.code}? This cannot be undone.`}
        onCancel={() => setDel(null)} onConfirm={() => { if (del) dealsService.remove(del.code, user.full); setDel(null); refreshAll(); }} />
    </>
  );
}
