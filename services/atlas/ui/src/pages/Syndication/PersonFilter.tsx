import { Box, Chip, MenuItem, Select } from '@mui/material';
import { db } from '../../api/atlasStore';
import { tokens } from '../../theme';

// The "RM / Analyst" filter the chase list and the matrix SHARE (state lives in
// SyndicationPage, so switching views keeps the same person's book on screen).
// The list is every RM, Analyst and Lender coordinator found on the mandates.

export const personNames = (): string[] => {
  const names = new Set<string>();
  (db().syn || []).forEach((r: any) => {
    [r.rm, r.an, r.lc].forEach((x: any) => { const v = String(x || '').trim(); if (v) names.add(v); });
  });
  return [...names].sort((a, b) => a.localeCompare(b));
};

/** Does this mandate belong to the person's book (RM, Analyst or Lender coordinator)? */
export const personHit = (r: any, person: string): boolean => {
  if (!person) return true;
  const p = person.toLowerCase();
  return [r.rm, r.an, r.lc].some((x: any) => String(x || '').trim().toLowerCase() === p);
};

/** Free-text also matches the people on the mandate ("vijaya" finds her book). */
export const personText = (r: any, needle: string): boolean =>
  !!needle && [r.rm, r.an, r.lc].some((x: any) => String(x || '').toLowerCase().includes(needle));

export default function PersonFilter({ person, onPerson }: { person: string; onPerson: (v: string) => void }) {
  const names = personNames();
  return (
    <Box sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.8 }}>
      <Select size="small" displayEmpty value={person}
        onChange={(e) => onPerson(e.target.value)}
        renderValue={(v) => (v ? `RM / Analyst: ${v}` : 'RM / Analyst: anyone')}
        sx={{ fontSize: 12.2, borderRadius: 999, '& .MuiSelect-select': { py: 0.55, px: 1.5 } }}>
        <MenuItem value="" sx={{ fontSize: 12.2, color: tokens.muted }}>anyone</MenuItem>
        {names.map((n) => <MenuItem key={n} value={n} sx={{ fontSize: 12.2 }}>{n}</MenuItem>)}
      </Select>
      {person && (
        <Chip size="small" label={`RM / Analyst · ${person}`} onDelete={() => onPerson('')}
          sx={{ bgcolor: '#EAF4F1', color: '#0E7A6A', fontWeight: 600, fontSize: 11.6 }} />
      )}
    </Box>
  );
}
