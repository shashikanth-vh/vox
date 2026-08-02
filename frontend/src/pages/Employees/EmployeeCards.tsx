import { useEffect, useState } from 'react';
import { Box, Chip, Typography, IconButton, Tooltip, Stack, Accordion, AccordionSummary, AccordionDetails } from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import EditIcon from '@mui/icons-material/Edit';
import DeleteIcon from '@mui/icons-material/Delete';
import { useQuery } from '@tanstack/react-query';
import { useSearch } from '../../context/SearchContext';
import { employeesService } from '../../services/employeesService';
import { tokens } from '../../theme';
import type { Employee } from './employee.types';

function ReadField({ label, value }: { label: string; value?: string }) {
  return (
    <Box>
      <Typography sx={{ fontSize: 10.4, textTransform: 'uppercase', letterSpacing: '.6px', color: tokens.muted, fontWeight: 700 }}>{label}</Typography>
      <Typography sx={{ fontSize: 12.6, color: tokens.ink, mt: 0.2, wordBreak: 'break-word' }}>{value?.trim() ? value : '—'}</Typography>
    </Box>
  );
}

const cardId = (name: string) => `emp-card-${name.replace(/[^A-Za-z0-9_-]/g, '_')}`;

export default function EmployeeCards({ openName, onEdit, onDelete }: {
  openName?: string | null;
  onEdit?: (e: Employee) => void;
  onDelete?: (e: Employee) => void;
}) {
  const { search } = useSearch();
  const [expanded, setExpanded] = useState<string | null>(openName ?? null);
  const query = useQuery({
    queryKey: ['employees', 'cards', search],
    queryFn: () => employeesService.list({ pageIndex: 0, pageSize: 1000, globalFilter: search,
      searchFields: ['name', 'full', 'role', 'username', 'email'] }),
    placeholderData: (prev) => prev,
  });
  const rows: Employee[] = query.data?.rows ?? [];

  // When arriving from a table row click, expand that person's card and scroll to it.
  useEffect(() => {
    if (!openName) return;
    setExpanded(openName);
    const id = cardId(openName);
    const t = setTimeout(() => document.getElementById(id)?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 80);
    return () => clearTimeout(t);
  }, [openName, rows.length]);

  if (!rows.length) return <Typography sx={{ fontSize: 12.5, color: tokens.muted, p: 2 }}>No employees match your search.</Typography>;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {rows.map((p) => {
        const b = employeesService.bookRollup(p.name);
        return (
          <Accordion key={p.name} id={cardId(p.name)} disableGutters elevation={0}
            expanded={expanded === p.name} onChange={(_, isEx) => setExpanded(isEx ? p.name : null)}
            sx={{ border: `1px solid ${tokens.line}`, borderRadius: 2, opacity: p.inactive ? 0.75 : 1,
              '&:before': { display: 'none' }, '&.Mui-expanded': { m: 0 } }}>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}
              sx={{ '& .MuiAccordionSummary-content': { alignItems: 'center', gap: 1, flexWrap: 'wrap', my: 1 } }}>
              <Typography component="b" sx={{ fontWeight: 700, fontSize: 14 }}>{p.name}</Typography>
              <Typography component="span" sx={{ fontSize: 11.6, color: tokens.muted }}>{p.full}</Typography>
              <Chip label={p.inactive ? 'Inactive' : 'Active'} color={p.inactive ? 'default' : 'success'} variant="outlined" />
              <Typography component="span" sx={{ fontSize: 11.6, color: tokens.muted }}>
                {p.role}{p.geography ? ` · ${p.geography}` : ''}
              </Typography>
              <Box sx={{ flex: 1 }} />
              <Typography component="span" sx={{ fontSize: 11.4, color: tokens.muted }}>
                book: {b.leads} leads ({b.activeLeads} active) · {b.deals} deals · {b.lend} lend · {b.syn} syn · {b.am} AM
              </Typography>
              <Stack direction="row" spacing={0.25} onClick={(e) => e.stopPropagation()}>
                {onEdit && <Tooltip title="Edit"><IconButton onClick={() => onEdit(p)}><EditIcon fontSize="small" /></IconButton></Tooltip>}
                {onDelete && <Tooltip title="Delete"><IconButton color="error" onClick={() => onDelete(p)}><DeleteIcon fontSize="small" /></IconButton></Tooltip>}
              </Stack>
            </AccordionSummary>
            <AccordionDetails sx={{ borderTop: `1px solid ${tokens.line}`, pt: 1.4 }}>
              <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '12px 16px' }}>
                <ReadField label="Full name" value={p.full} />
                <ReadField label="Username" value={p.username} />
                <ReadField label="Role" value={p.role} />
                <ReadField label="Email" value={p.email} />
                <ReadField label="Phone" value={p.phone} />
                <ReadField label="Reports to" value={p.reportsTo} />
                <ReadField label="Geography covered" value={p.geography} />
                <ReadField label="Sector specialisation" value={p.sectors} />
                <ReadField label="Started on" value={p.startedOn} />
              </Box>
              {p.notes?.trim() && (
                <Box sx={{ mt: 1.4 }}><ReadField label="Notes" value={p.notes} /></Box>
              )}
            </AccordionDetails>
          </Accordion>
        );
      })}
    </Box>
  );
}
