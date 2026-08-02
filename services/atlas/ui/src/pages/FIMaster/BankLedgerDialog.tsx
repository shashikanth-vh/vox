import { Drawer, Box, Typography, Button, IconButton } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { fiService } from '../../services/fiService';
import { tokens } from '../../theme';
import type { FiLedgerRow } from './fi.types';

// v12 openBank(name) — "<bank> — Full deal ledger" in the right-side drawer:
// live vs closed engagements, every row a click through to the company.
const HEAD_LIVE = ['Company', 'Status', 'Ask', 'Deal status', 'RM/Analyst', 'Last reply', 'Note'];
const HEAD_PAST = ['Company', 'Outcome', 'Ask', 'Deal status', 'RM/Analyst', 'Last reply', 'Note'];
const daysSince = (d?: string) => (d ? Math.max(0, Math.round((Date.now() - new Date(d).getTime()) / 86400000)) + 'd' : '—');

// v12 `table.grid`: 12.5px, th #f4f7f8/#475569 uppercase, 6px/8px cells, #eef1f3 rules.
const GRID_SX = {
  width: '100%', borderCollapse: 'collapse', fontSize: '12.5px',
  '& th, & td': { padding: '6px 8px', borderBottom: '1px solid #eef1f3', textAlign: 'left' },
  '& th': { background: '#f4f7f8', fontSize: '11px', color: '#475569', textTransform: 'uppercase', letterSpacing: '.4px', fontWeight: 700, whiteSpace: 'nowrap' },
  '& tbody tr': { cursor: 'pointer' },
  '& tbody tr:hover td': { background: '#f8fafc' },
} as const;

function LedgerTable({ head, rows, onOpenCompany }: {
  head: string[]; rows: FiLedgerRow[]; onOpenCompany?: (code: string) => void;
}) {
  return (
    <Box sx={{ overflowX: 'auto', '&::-webkit-scrollbar': { display: 'none' }, scrollbarWidth: 'none', msOverflowStyle: 'none' }}>
      <Box component="table" sx={GRID_SX}>
        <thead>
          <tr>{head.map((h) => <th key={h}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((x, i) => (
            <tr key={i} onClick={() => onOpenCompany?.(x.code)}>
              <td><b>{x.co}</b><br /><span style={{ color: tokens.muted, fontSize: 11 }}>{x.code}</span></td>
              <td>{x.st || '—'}</td>
              <td style={{ whiteSpace: 'nowrap' }}>₹{x.amt} Cr</td>
              <td>{x.synStatus || '—'}</td>
              <td style={{ whiteSpace: 'nowrap' }}>{x.rm || '—'} / {x.an || '—'}</td>
              <td>{daysSince(x.resp)}</td>
              <td style={{ whiteSpace: 'normal', minWidth: 140 }}>{x.note || ''}</td>
            </tr>
          ))}
        </tbody>
      </Box>
    </Box>
  );
}

// v12 `.dsection` — a titled block with an uppercase navy heading and a bottom rule.
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Box sx={{ py: '12px', borderBottom: '1px solid #eef1f3' }}>
      <Typography component="h4" sx={{ m: '0 0 8px', fontSize: 12, color: tokens.navy, letterSpacing: '.4px', textTransform: 'uppercase', fontWeight: 700 }}>
        {title}
      </Typography>
      {children}
    </Box>
  );
}

// v12 `.empty` — a soft grey callout, not bare muted text.
function Empty({ children }: { children: React.ReactNode }) {
  return (
    <Box sx={{ p: '8px 12px', background: '#f8fafc', borderRadius: '6px', color: tokens.muted, fontSize: 12 }}>
      {children}
    </Box>
  );
}

export default function BankLedgerDialog({ bankName, onClose, onEdit, onOpenCompany }: {
  bankName: string | null; onClose: () => void;
  onEdit?: () => void; onOpenCompany?: (code: string) => void;
}) {
  const led = bankName ? fiService.ledger(bankName) : { rows: [], active: [], past: [] };

  return (
    <Drawer anchor="right" open={!!bankName} onClose={onClose}
      PaperProps={{ sx: { width: 820, maxWidth: '100vw', height: '100%', display: 'flex', flexDirection: 'column' } }}>
      {/* v12 dr-hdr: bank name · "Full deal ledger" · engagement counts · Close */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2, p: '9px 16px', bgcolor: '#F1F3F5', borderBottom: 1, borderColor: 'divider', flexShrink: 0 }}>
        <Box sx={{ flex: 1 }}>
          <Typography sx={{ fontSize: 15.6, fontWeight: 700 }}>{bankName} — Full deal ledger</Typography>
          <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
            {led.rows.length} engagements · {led.active.length} live · {led.past.length} closed
          </Typography>
        </Box>
        {onEdit && <Button variant="outlined" onClick={() => { onClose(); onEdit(); }}>Edit</Button>}
        <IconButton onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', px: 2 }}>
        <Section title={`Live engagements (${led.active.length})`}>
          {led.active.length
            ? <LedgerTable head={HEAD_LIVE} rows={led.active} onOpenCompany={onOpenCompany} />
            : <Empty>No live deals with this bank.</Empty>}
        </Section>
        <Section title={`Closed / declined (${led.past.length})`}>
          {led.past.length
            ? <LedgerTable head={HEAD_PAST} rows={led.past} onOpenCompany={onOpenCompany} />
            : <Empty>No closed deals with this bank.</Empty>}
        </Section>
      </Box>
    </Drawer>
  );
}
