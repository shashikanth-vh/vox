import { useMemo, useState } from 'react';
import { Button, IconButton, Tooltip, Snackbar, Alert } from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import RocketLaunchIcon from '@mui/icons-material/RocketLaunch';
import { useQueryClient } from '@tanstack/react-query';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import PageHint from '../../components/common/PageHint';
import { LensPill, TempPill, CodeText } from '../../components/common/Pills';
import ConfirmDialog from '../../components/common/ConfirmDialog';
import AddLeadDialog from './AddLeadDialog';
import PushToDealsDialog from './PushToDealsDialog';
import LeadDrawer from './LeadDrawer';
import { leadsService } from '../../services/leadsService';
import { useAuth } from '../../auth/AuthContext';
import { can, scopeFor, whoCan } from '../../auth/rbac';
import { tokens } from '../../theme';
import type { Lead } from './lead.types';

export default function LeadsPage() {
  const { user } = useAuth();
  const ro = !can(user.roles, 'editLead');
  const qc = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [edit, setEdit] = useState<Lead | null>(null);
  const [push, setPush] = useState<Lead | null>(null);
  const [del, setDel] = useState<Lead | null>(null);
  const [delErr, setDelErr] = useState('');
  const refresh = () => qc.invalidateQueries({ queryKey: ['leads'] });

  const columns = useMemo<MRT_ColumnDef<Lead>[]>(() => [
    { accessorKey: 'id', header: 'ID', size: 90, Cell: ({ cell }) => <CodeText code={cell.getValue<string>()} /> },
    { accessorKey: 'company', header: 'Company', size: 220, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b> },
    { accessorKey: 'sector', header: 'Sector' },
    { accessorKey: 'lens', header: 'Lens', size: 80, Cell: ({ cell }) => <LensPill lens={cell.getValue<string>()} /> },
    { accessorKey: 'source', header: 'Source', size: 100 },
    { accessorKey: 'rm', header: 'RM', size: 100 },
    { accessorKey: 'temp', header: 'Temp', size: 90, Cell: ({ cell }) => <TempPill temp={cell.getValue<string>()} /> },
    { accessorKey: 'status', header: 'Status', size: 100 },
    { accessorKey: 'last', header: 'Last touch', size: 110 },
    { accessorKey: 'next', header: 'Next action', size: 220 },
  ], []);

  return (
    <>
      <PageHint>Click a lead to update it in place. Converted leads leave this register automatically.</PageHint>
      <CommonTable<Lead>
        queryKey={['leads']}
        fetcher={(q) => leadsService.list(q, scopeFor(user.roles, 'leads', user.name))}
        columns={columns}
        csvName="atlas_leads"
        toolbarLeft={can(user.roles, 'addLead') && <Button startIcon={<AddIcon />} variant="contained" onClick={() => setAddOpen(true)}>Add lead</Button>}
        // No View icon — clicking the row opens the lead.
        onRowClick={(l) => setEdit(l)}
        onEdit={(l) => setEdit(l)}
        editReason={ro ? whoCan('editLead') : ''}
        onDelete={can(user.roles, 'deleteRow') ? (l) => setDel(l) : undefined}
        // Push to deals — convert this lead into a deal (v11 doPush flow).
        extraActions={can(user.roles, 'pushToDeals') ? (l) => (
          <Tooltip title="Push to deal">
            <IconButton sx={{ color: tokens.teal }} onClick={() => setPush(l)}>
              <RocketLaunchIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        ) : undefined}
      />
      <AddLeadDialog open={addOpen} onClose={() => setAddOpen(false)} onSaved={refresh} />
      <LeadDrawer lead={edit} onClose={() => setEdit(null)} onChanged={refresh} onPush={(l) => setPush(l)} />
      <PushToDealsDialog lead={push} onClose={() => setPush(null)} onDone={refresh} />
      <ConfirmDialog open={!!del} title="Delete lead" message={`Delete ${del?.company}? This cannot be undone.`}
        onCancel={() => setDel(null)}
        onConfirm={async () => {
          if (del) {
            const r = await leadsService.remove(del, user.full);
            // A refused delete has to say so — the row is still there on the next refresh.
            if (!r.ok) setDelErr(r.error || 'Could not delete the lead.');
          }
          setDel(null); refresh();
        }} />
      <Snackbar open={!!delErr} autoHideDuration={4000} onClose={() => setDelErr('')}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="warning" onClose={() => setDelErr('')} sx={{ fontSize: 12.4 }}>{delErr}</Alert>
      </Snackbar>
    </>
  );
}
