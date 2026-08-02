import { useMemo, useState } from 'react';
import { Box, Typography, Button, Dialog, DialogTitle, DialogContent, DialogActions } from '@mui/material';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import { CodeText } from '../../components/common/Pills';
import { auditService } from '../../services/auditService';
import { tokens } from '../../theme';

interface AuditRow { t: string; by: string; role?: string; act: string; code: string; detail: string; }

// Same labelled two-column layout as the Activity log's detail dialog.
const detailRow = (label: string, value: React.ReactNode) => (
  <>
    <Typography sx={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px', color: tokens.muted, fontWeight: 700 }}>{label}</Typography>
    <Typography component="div" sx={{ fontSize: 12.8 }}>{value}</Typography>
  </>
);

export default function AuditPage() {
  // Clicking a row opens the detail — styled exactly like the Activity log dialog.
  const [view, setView] = useState<AuditRow | null>(null);

  // Columns mirror v12's vAudit(): When · Who · Action · Code · Detail.
  const columns = useMemo<MRT_ColumnDef<AuditRow>[]>(() => [
    { accessorKey: 't', header: 'When', size: 150 },
    // v12: a.by || a.role || '' — older entries carry `role` instead of `by`.
    { id: 'by', header: 'Who', size: 140, accessorFn: (r) => r.by || r.role || '' },
    { accessorKey: 'act', header: 'Action', size: 160, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b> },
    { accessorKey: 'code', header: 'Code', size: 120, Cell: ({ cell }) => <CodeText code={cell.getValue<string>()} /> },
    // v12 marks this column `wrap` — long detail strings run to multiple lines.
    { accessorKey: 'detail', header: 'Detail', size: 360,
      muiTableBodyCellProps: { sx: { whiteSpace: 'normal', wordBreak: 'break-word' } } },
  ], []);

  return (
    <>
      {/* Append-only register: the actions column carries row CSV only — no edit/delete.
          Clicking a row opens the full detail. */}
      <CommonTable<AuditRow> queryKey={['audit']} fetcher={(q) => auditService.list(q)} columns={columns} csvName="atlas_audit"
        onRowClick={(r) => setView(r)} />

      <Dialog open={!!view} onClose={() => setView(null)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: 15, fontWeight: 700 }}>Audit detail</DialogTitle>
        <DialogContent dividers>
          {view && (
            <Box sx={{ display: 'grid', gridTemplateColumns: 'auto 1fr', columnGap: 2, rowGap: 1.2, alignItems: 'baseline' }}>
              {detailRow('When', view.t)}
              {detailRow('Who', <b>{view.by || view.role || ''}</b>)}
              {detailRow('Action', view.act)}
              {detailRow('Code', view.code ? <CodeText code={view.code} /> : '—')}
              {detailRow('Detail', view.detail || '—')}
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setView(null)}>Close</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
