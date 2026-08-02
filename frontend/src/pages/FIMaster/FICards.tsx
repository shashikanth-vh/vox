import { Accordion, AccordionSummary, AccordionDetails, Box, Typography, Chip, IconButton, Tooltip } from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import EditIcon from '@mui/icons-material/Edit';
import { fiService } from '../../services/fiService';
import { useSearch } from '../../context/SearchContext';
import { tokens } from '../../theme';
import type { FiRow } from './fi.types';

// v12 vFI() card mode, read-only: the card displays the record and the actions
// on each card route to the same View / Edit dialogs the table uses.
function Field({ label, value }: { label: string; value?: string }) {
  return (
    <Box>
      <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.5px', color: tokens.muted, fontWeight: 700 }}>
        {label}
      </Typography>
      <Typography sx={{ fontSize: 12.6, wordBreak: 'break-word' }}>{value || '—'}</Typography>
    </Box>
  );
}

export default function FICards({ openName, onEdit, onOpenCompany }: {
  openName?: string | null;
  onEdit?: (r: FiRow) => void;
  onOpenCompany?: (code: string) => void;
}) {
  const { search } = useSearch();
  const q = search.trim().toLowerCase();
  const rows = fiService.rollup().filter((l) =>
    !q || [l.name, l.type, l.preferredSectors || l.sectors].some((x) => String(x || '').toLowerCase().includes(q)));

  return (
    <>
      {rows.map((l) => (
        <Accordion key={l._i} defaultExpanded={!!openName && openName === l.name} disableGutters variant="outlined"
          sx={{ borderColor: tokens.line, mb: 0.8, opacity: l.inactive ? 0.66 : 1, '&:before': { display: 'none' } }}>
          <AccordionSummary expandIcon={<ExpandMoreIcon />}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, width: '100%', flexWrap: 'wrap', pr: 1 }}>
              <Typography component="b" sx={{ fontSize: 13.2, fontWeight: 700 }}>{l.name}</Typography>
              <Chip size="small" variant="outlined" label={l.inactive ? 'INACTIVE' : 'ACTIVE'}
                color={l.inactive ? 'default' : 'success'} />
              <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                {l.type || ''}{(l.preferredSectors || l.sectors) ? ' · ' + (l.preferredSectors || l.sectors) : ''}
              </Typography>
              <Box sx={{ flex: 1 }} />
              <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                pursued {l.pursued} · live {l.live} · IP {l.ip} · sanctioned {l.sanc} · declined {l.decl}
              </Typography>
              {/* Edit action — the card view's equivalent of the table's action column.
                  No View here: expanding the card already shows the full record. */}
              {onEdit && (
                <Tooltip title="Edit">
                  <IconButton onClick={(e) => { e.stopPropagation(); onEdit(l); }}><EditIcon fontSize="small" /></IconButton>
                </Tooltip>
              )}
            </Box>
          </AccordionSummary>
          <AccordionDetails>
            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(220px,1fr))', gap: '10px 14px' }}>
              <Field label="Type" value={l.type} />
              <Field label="Preferred sectors" value={l.preferredSectors || l.sectors} />
              <Field label="Notes (appetite, contacts, quirks)" value={l.notes} />
              <Field label="Status" value={l.inactive ? 'Inactive' : 'Active'} />
            </Box>

            <Typography sx={{ fontSize: 12, fontWeight: 700, mt: 1.4, mb: 0.5 }}>Deal history ({l.cos.length})</Typography>
            {l.cos.length ? l.cos.map((c, i) => (
              <Box key={i} sx={{ display: 'flex', alignItems: 'baseline', gap: 1, py: 0.5, borderTop: `1px solid ${tokens.line}` }}>
                <Box component="b" onClick={() => onOpenCompany?.(c.code)}
                  sx={{ fontSize: 12.6, color: tokens.navy, cursor: onOpenCompany ? 'pointer' : 'default',
                    '&:hover': { textDecoration: onOpenCompany ? 'underline' : 'none' } }}>{c.co}</Box>
                <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                  {c.st || '—'} · ask ₹{c.amt} Cr · last reply {c.resp ? daysSince(c.resp) + 'd ago' : '—'}
                </Typography>
              </Box>
            )) : <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>No engagements yet.</Typography>}
          </AccordionDetails>
        </Accordion>
      ))}
      {!rows.length && <Typography sx={{ p: 2, textAlign: 'center', color: tokens.muted, fontSize: 12.5 }}>No lenders match this search.</Typography>}
    </>
  );
}

const daysSince = (d: string) => Math.max(0, Math.round((Date.now() - new Date(d).getTime()) / 86400000));
