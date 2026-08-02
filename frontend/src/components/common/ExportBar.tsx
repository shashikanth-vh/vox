import { Box, Button, Typography } from '@mui/material';
import FileDownloadIcon from '@mui/icons-material/FileDownload';
import PictureAsPdfIcon from '@mui/icons-material/PictureAsPdf';
import { tokens } from '../../theme';

const q = (v: any) => `"${String(v ?? '').replace(/"/g, '""')}"`;

export function toCsv(headers: string[], rows: (string | number)[][]): string {
  return [headers.map(q).join(','), ...rows.map((r) => r.map(q).join(','))].join('\n');
}

export function saveCsv(csv: string, name: string) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  a.download = name.endsWith('.csv') ? name : `${name}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// A consistent "Export this view" control (CSV + PDF) for the non-table views. PDF just
// prints the current view — a global @media print rule (in AppLayout) hides the app
// chrome so the printout / saved PDF is clean. The bar itself is .no-print so it never
// shows up in the export.
export default function ExportBar({ onCsv, label = 'Export this view' }: { onCsv: () => void; label?: string }) {
  return (
    <Box className="no-print" sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
      <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.5px', color: tokens.muted, fontWeight: 700 }}>{label}</Typography>
      <Button size="small" variant="outlined" startIcon={<FileDownloadIcon fontSize="small" />} onClick={onCsv}>CSV</Button>
      <Button size="small" variant="outlined" startIcon={<PictureAsPdfIcon fontSize="small" />} onClick={() => window.print()}>PDF</Button>
    </Box>
  );
}
