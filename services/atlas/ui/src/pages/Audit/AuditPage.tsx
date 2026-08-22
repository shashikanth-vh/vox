import React, { useMemo, useState } from 'react';
import { Box, Typography, Button, Dialog, DialogTitle, DialogContent, DialogActions } from '@mui/material';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import { CodeText } from '../../components/common/Pills';
import { auditService } from '../../services/auditService';
import type { AuditRow } from '../../services/auditService';
import { fields as detailFields, humanKey } from '../../services/auditDetail';
import { tokens } from '../../theme';

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
    { id: 'by', header: 'Who', size: 140, meta: { localFilter: true }, accessorFn: (r) => r.by || r.role || '' },
    // 'evidence.attach' is the stored verb; 'Evidence attach' is what a person reads.
    { accessorKey: 'act', header: 'Action', size: 160, meta: { localFilter: true },
      Cell: ({ cell }) => <b>{humanKey(String(cell.getValue<string>() || '').replace(/\./g, ' '))}</b> },
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
        onRowClick={(r) => setView(r)}
        mobileCard={{
          primary: (r) => humanKey(String(r.act || '').replace(/\./g, ' ')),
          value: (r) => r.t,
        }} />

      <Dialog open={!!view} onClose={() => setView(null)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: 15, fontWeight: 700 }}>Audit detail</DialogTitle>
        <DialogContent dividers>
          {view && (
            <Box sx={{ display: 'grid', gridTemplateColumns: 'auto 1fr', columnGap: 2, rowGap: 1.2, alignItems: 'baseline' }}>
              {detailRow('When', view.t)}
              {detailRow('Who', <b>{view.by || view.role || ''}</b>)}
              {detailRow('Action', humanKey(view.act.replace(/\./g, ' ')))}
              {detailRow('Code', view.code ? <CodeText code={view.code} /> : '—')}
              {/* The grid shows the summary; here every recorded field is spelled out —
                  identifiers included, because a trail you cannot drill into is not one. */}
              {view.changes
                ? detailFields(view.changes).map((f) => (
                    <React.Fragment key={f.label}>{detailRow(f.label, f.value)}</React.Fragment>
                  ))
                : detailRow('Detail', view.detail || '—')}
              {view.resourceId && detailRow('Record', <CodeText code={view.resourceId} />)}
              {view.requestId && detailRow('Request', <CodeText code={view.requestId} />)}
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
