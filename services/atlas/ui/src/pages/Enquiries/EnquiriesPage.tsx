import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Box, Button, Chip, IconButton, Snackbar, Tooltip, Typography } from '@mui/material';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import ForwardToInboxIcon from '@mui/icons-material/ForwardToInbox';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import ConfirmDialog from '../../components/common/ConfirmDialog';
import PageHint from '../../components/common/PageHint';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { applyQuery } from '../../api/queryEngine';
import { apiErr } from '../../api/http';
import { enquiriesService, STAGE_COLOR, STAGE_LABEL, type EnquiryRow } from '../../services/enquiriesService';

// The grid's view-model: applyQuery and the column funnels read row FIELDS by
// column id, so the derived columns (the stage label, the joined approver list,
// the day the enquiry arrived) are materialised onto each row.
type Row = EnquiryRow & { stage_label: string; approvers_s: string; received_day: string; decided_s: string };

const day = (iso: string | null | undefined) => (iso ? iso.slice(0, 10) : '');
const when = (iso: string | null | undefined) => {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
};

/**
 * Leads → Enquiries — every website / WhatsApp enquiry and where it stands.
 *
 * The register parks each enquiry the website posts, mails the approvers their
 * Approve / Reject links, reminds them after three days, and hands an expired
 * one to the BD Head. This screen is the desk's window on that chase: who was
 * asked, whether they acted, what the enquiry became, and a Resend link for
 * the ones that stalled.
 */
export default function EnquiriesPage() {
  const { user } = useAuth();
  const nav = useNavigate();
  const qc = useQueryClient();
  const canResend = can(user.roles, 'addLead');
  const isAdmin = (user.roles as string[]).includes('Admin');
  const [resend, setResend] = useState<Row | null>(null);
  const [del, setDel] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ sev: 'success' | 'error'; msg: string } | null>(null);

  const { data: rows = [], isLoading } = useQuery({
    queryKey: ['enquiries'],
    queryFn: async (): Promise<Row[]> => (await enquiriesService.list()).map((r) => ({
      ...r,
      stage_label: STAGE_LABEL[r.stage] || r.stage,
      approvers_s: (r.approvers || []).join(', '),
      received_day: day(r.received_at || r.submitted_at),
      decided_s: r.approved_by ? `${r.approved_by} · ${when(r.approved_at)}` : '',
    })),
    refetchInterval: 60_000,
  });

  const waiting = rows.filter((r) => ['waiting', 'reminded', 'escalated', 'expired'].includes(r.stage)).length;

  const columns = useMemo<MRT_ColumnDef<Row>[]>(() => [
    { accessorKey: 'enquiry_no', header: 'Enquiry', size: 110,
      Cell: ({ cell }) => <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12.5 }}>{cell.getValue<string>()}</span> },
    { accessorKey: 'received_day', header: 'Received', size: 105, meta: { localFilter: true } },
    { accessorKey: 'channel', header: 'Channel', size: 95, meta: { localFilter: true } },
    { accessorKey: 'company', header: 'Company', size: 200 },
    { accessorKey: 'contact', header: 'Contact', size: 140 },
    { accessorKey: 'mobile', header: 'Mobile', size: 125 },
    { accessorKey: 'intent', header: 'Intent', size: 90, meta: { localFilter: true } },
    { accessorKey: 'need', header: 'Ask', size: 240 },
    { accessorKey: 'stage_label', header: 'Stage', size: 110, meta: { localFilter: true },
      Cell: ({ row }) => <Chip size="small" label={row.original.stage_label}
        color={STAGE_COLOR[row.original.stage] || 'default'} variant="outlined" /> },
    { accessorKey: 'approvers_s', header: 'Sent to', size: 220 },
    { id: 'expires', header: 'Links valid till', size: 120, accessorFn: (r) => day(r.expires_at) },
    { accessorKey: 'decided_s', header: 'Decided by', size: 220 },
    { accessorKey: 'lead_no', header: 'Lead', size: 100,
      Cell: ({ cell }) => cell.getValue<string>() ? <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12.5 }}>{cell.getValue<string>()}</span> : '' },
    { accessorKey: 'rm', header: 'RM', size: 100, meta: { localFilter: true } },
    { accessorKey: 'note', header: 'Note', size: 320 },
  ], []);

  const doResend = async () => {
    if (!resend) return;
    setBusy(true);
    try {
      const out = await enquiriesService.resend(resend.id);
      const to = out.links.map((l) => l.recipient).filter(Boolean).join(', ');
      setToast({ sev: 'success', msg: `Fresh links sent for ${out.enquiry_no}${to ? ' to ' + to : ''}.` });
      setResend(null);
      void qc.invalidateQueries({ queryKey: ['enquiries'] });
    } catch (e: any) {
      setToast({ sev: 'error', msg: apiErr(e, 'Re-send the links') });
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async () => {
    if (!del) return;
    setBusy(true);
    try {
      const out = await enquiriesService.remove(del.id);
      setToast({ sev: 'success', msg: `Enquiry ${out.deleted} deleted.${del.lead_no ? ` Lead ${del.lead_no} stays.` : ''}` });
      setDel(null);
      void qc.invalidateQueries({ queryKey: ['enquiries'] });
    } catch (e: any) {
      setToast({ sev: 'error', msg: apiErr(e, 'Delete the enquiry') });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box>
      <PageHint>
        Every enquiry the website or WhatsApp posted, and where it stands. PRISM mails the approvers their
        Approve / Reject links, reminds them after three days, and hands an expired one to the BD Head.
        {waiting > 0 && <> <b>{waiting}</b> still waiting for a decision.</>}
      </PageHint>
      <CommonTable<Row>
        queryKey={['enquiries', rows.length, isLoading]}
        fetcher={async (q) => applyQuery(rows, { ...q,
          searchFields: ['enquiry_no', 'company', 'contact', 'mobile', 'email', 'need', 'approvers_s', 'approved_by', 'lead_no', 'note'] })}
        columns={columns}
        csvName="atlas_enquiries"
        initialColumnVisibility={{ channel: false, intent: false, mobile: false, decided_s: false }}
        toolbarLeft={<Button startIcon={<ArrowBackIcon />} variant="outlined" onClick={() => nav('/leads')}>Leads</Button>}
        actionsEnabled={canResend || isAdmin}
        onDelete={isAdmin ? (r) => setDel(r) : undefined}
        extraActions={(r) => (
          ['waiting', 'reminded', 'escalated', 'expired'].includes(r.stage) && canResend ? (
            <Tooltip title="Re-send the Approve / Reject links">
              <IconButton size="small" onClick={(e) => { e.stopPropagation(); setResend(r); }}>
                <ForwardToInboxIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          ) : null
        )}
      />
      <ConfirmDialog open={!!resend} onCancel={() => !busy && setResend(null)} onConfirm={doResend}
        title="Re-send the links?" confirmColor="primary"
        confirmLabel={busy ? 'Sending…' : 'Re-send'}
        message={resend ? `Fresh Approve / Reject links for ${resend.enquiry_no} (${resend.company || ''}) will be mailed to ${
          resend.approvers.length ? resend.approvers.join(', ') : 'the default approvers'}. Earlier links keep working until they expire.` : ''} />
      <ConfirmDialog open={!!del} onCancel={() => !busy && setDel(null)} onConfirm={doDelete}
        title="Delete this enquiry?" confirmLabel={busy ? 'Deleting…' : 'Delete'}
        message={del ? `${del.enquiry_no} (${del.company || ''}) will be removed from this list and its links will stop working.${
          del.lead_no ? ` Lead ${del.lead_no} is not affected.` : ''} This cannot be undone.` : ''} />
      <Snackbar open={!!toast} autoHideDuration={5000} onClose={() => setToast(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity={toast?.sev || 'success'} onClose={() => setToast(null)} sx={{ fontSize: 12.5 }}>{toast?.msg}</Alert>
      </Snackbar>
      {!rows.length && !isLoading && (
        <Typography sx={{ mt: 2, fontSize: 13, color: 'text.secondary' }}>No enquiries yet. They appear here the moment the website posts one.</Typography>
      )}
    </Box>
  );
}
