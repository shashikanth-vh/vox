import { useMemo, useState } from 'react';
import { Alert, MenuItem, TextField } from '@mui/material';
import SubTabs from '../../components/common/SubTabs';
import LmsView from './lms/LmsView';
import { useQueryClient } from '@tanstack/react-query';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import PageHint from '../../components/common/PageHint';
import { CodeText } from '../../components/common/Pills';
import ConfirmDialog from '../../components/common/ConfirmDialog';
import CompanyDrawer from '../Deals/CompanyDrawer';
import AddProductDialog from '../Deals/AddProductDialog';
import { lendingService } from '../../services/lendingService';
import { referenceService } from '../../services/referenceService';
import { fmt } from '../../utils/format';
import { useAuth } from '../../auth/AuthContext';
import { can, scopeFor, whoCan } from '../../auth/rbac';
import { tokens } from '../../theme';
import { LEND_GREEN } from '../../services/lendingService';
import type { LendingRow } from './lending.types';

// v17: lending rows tint by stage — green when in the sanctioned family, red when
// rejected, a warn left-border when on hold. `!important` beats MRT's cell background.
const stageRowSx = (stage: string) => {
  if (LEND_GREEN.includes(stage)) return { backgroundColor: `${tokens.okBg} !important` };
  if (stage === 'Rejected') return { backgroundColor: `${tokens.badBg} !important`, color: '#7C4A3E !important' };
  if (stage === 'On Hold') return { '&:first-of-type': { boxShadow: `inset 3px 0 0 ${tokens.warn}` } };
  return {};
};

export default function LendingPage() {
  const { user } = useAuth();
  const ro = !can(user.roles, 'editLending');
  // The NBFC split: LOS (origination — lead to disbursement) | LMS (servicing — the
  // loan account from first disbursement to closure). One nav item, two worlds.
  const [side, setSide] = useState<'los' | 'lms'>('los');
  const qc = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);
  const [addProd, setAddProd] = useState<string | null>(null);
  const [del, setDel] = useState<LendingRow | null>(null);
  const refresh = () => qc.invalidateQueries();
  const [stageErr, setStageErr] = useState('');
  // The register REFUSES a governed stage on a direct change (Sanctioned, CP/CS
  // Completed, Ready for Disbursement, Disbursed are reached through approvals). That
  // refusal has to be visible: the row must not advance on screen while the register
  // holds the line.
  const stageTo = async (id: string, stage: string) => {
    setStageErr('');
    const r = await lendingService.updateStage(id, stage, user.full);
    if (!r.ok) setStageErr(r.error || 'The register refused that stage change.');
    refresh();
  };

  const columns = useMemo<MRT_ColumnDef<LendingRow>[]>(() => [
    { accessorKey: 'code', header: 'Group Code', size: 120, Cell: ({ cell }) => <CodeText code={cell.getValue<string>()} /> },
    { accessorKey: '_name', header: 'Company', size: 220, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b> },
    { accessorKey: 'amt', header: '₹ Cr', size: 90, Cell: ({ cell }) => <span style={{ fontVariantNumeric: 'tabular-nums' }}>{fmt(cell.getValue())}</span> },
    { accessorKey: 'rm', header: 'RM', size: 100 },
    { accessorKey: 'an', header: 'Analyst', size: 110 },
    {
      accessorKey: 'stage', header: 'Stage', size: 160,
      // inline stage edit stamps the date automatically (template behaviour)
      Cell: ({ row }) => (
        <TextField select size="small" value={row.original.stage} disabled={ro} variant="outlined"
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => { void stageTo(row.original.id, e.target.value); }}
          sx={{ minWidth: 130, '& .MuiOutlinedInput-input': { fontSize: 12, py: '5px' } }}>
          {referenceService.getRefSync('Lending Stage').map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
        </TextField>
      ),
    },
    { accessorKey: 'updated', header: 'Stage updated', size: 120 },
    { accessorKey: 'remarks', header: 'Remarks', size: 220 },
  ], [ro]);

  return (
    <>
      <SubTabs value={side} onChange={(id) => setSide(id as 'los' | 'lms')}
        items={[
          { id: 'los', label: 'LOS · Origination', icon: '📋' },
          { id: 'lms', label: 'LMS · Servicing', icon: '🏦' },
        ]} />
      {side === 'lms' && <LmsView />}
      {side === 'lms' ? null : <>
      <PageHint>Stage edits stamp the date automatically; sanction date lives in the company profile.
        Sanctioned, CP/CS Completed, Ready for Disbursement and Disbursed are reached through
        their approvals — the register refuses them here.</PageHint>
      {stageErr && <Alert severity="warning" sx={{ mb: 1, fontSize: 12.5, py: 0.2 }}
        onClose={() => setStageErr('')}>{stageErr}</Alert>}
      <CommonTable<LendingRow>
        queryKey={['lending']}
        fetcher={(q) => lendingService.list(q, scopeFor(user.roles, 'lend', user.name))}
        columns={columns}
        csvName="atlas_lending"
        // No View icon — clicking the row opens the company drawer.
        onRowClick={(r) => setOpen(r.code)}
        onEdit={(r) => setOpen(r.code)}
        editReason={ro ? whoCan('editLending') : ''}
        onDelete={ro ? undefined : (r) => setDel(r)}
        rowSx={(r) => stageRowSx(r.stage)}
        mobileCard={{
          primary: (r) => r._name,
          value: (r) => <span style={{ fontVariantNumeric: 'tabular-nums' }}>{fmt(r.amt)}</span>,
        }}
      />
      <CompanyDrawer code={open} onClose={() => setOpen(null)} onChanged={refresh} onAddProduct={(c) => setAddProd(c)} />
      <AddProductDialog code={addProd} onClose={() => setAddProd(null)} onDone={refresh} />
      <ConfirmDialog open={!!del} title="Delete lending row" message={`Delete ${del?._name}'s lending facility?`}
        onCancel={() => setDel(null)} onConfirm={() => { if (del) lendingService.remove(del.id, user.full); setDel(null); refresh(); }} />
      </>}
    </>
  );
}
