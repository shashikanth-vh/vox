import { useEffect, useRef, useState } from 'react';
import { Box, Chip, CircularProgress, Popper, Paper, TextField, Typography } from '@mui/material';
import { vocxService } from '../../services/vocxService';
import { currentRm } from './rm';
import { tokens } from '../../theme';

/**
 * WHICH COMPANY this capture belongs to.
 *
 * The resolver has already made a guess from what was said, and shows it with the score
 * that earned it. That guess is a proposal, not a decision: a mis-heard name, two similar
 * companies, or a genuinely new prospect all need a human to settle it, and settling it is
 * what turns a recording into a filed interaction against the right client.
 *
 * The typeahead is VocX's own `/v1/suggest`, which ranks with the SAME scorer a commit
 * resolves with — so what this list offers is exactly what filing would link. A query that
 * matches nothing well enough comes back with `new_company`, and the honest option then is
 * to create it, not to force it onto the nearest wrong match.
 */

export interface ClientChoice {
  /** Register entity code, or '__new__' to create the company on approve. */
  code: string | null;
  name: string;
  /** Resolved register entity id, when the pick came from an existing row. */
  entityId?: string;
}

export default function ClientPicker({ match, value, onChange, disabled }: {
  /** `extraction.entity_match` — what the resolver proposed. */
  match: any;
  value: ClientChoice | null;
  onChange: (v: ClientChoice | null) => void;
  disabled?: boolean;
}) {
  const rm = currentRm();
  const [q, setQ] = useState('');
  const [rows, setRows] = useState<any[]>([]);
  const [isNew, setIsNew] = useState(false);
  const [loading, setLoading] = useState(false);
  const anchor = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);

  // Debounced: a request per keystroke would out-run its own answers and flicker.
  useEffect(() => {
    const term = q.trim();
    if (term.length < 2) { setRows([]); setIsNew(false); setOpen(false); return; }
    let alive = true;
    setLoading(true);
    const t = setTimeout(async () => {
      const r = await vocxService.suggest(term, rm);
      if (!alive) return;
      setLoading(false);
      if (!r.ok) { setRows([]); return; }
      setRows(r.data?.matches || []);
      setIsNew(!!r.data?.new_company);
      setOpen(true);
    }, 250);
    return () => { alive = false; clearTimeout(t); };
  }, [q, rm]);

  const pick = (c: ClientChoice) => {
    onChange(c);
    setQ(''); setOpen(false); setRows([]);
  };

  // What the card is currently going to file against, in one line.
  const resolved = value
    ? (value.code === '__new__'
        ? `Will be created as a new company: ${value.name}`
        : `Linked to ${value.name} (${value.code})`)
    : match?.code
      ? `${match.canonical_name || match.proposed_company || 'match'} (${match.code})`
        + (match.match_score ? ` · ${Math.round(match.match_score * 100)}%` : '')
      : match?.is_new_lead || match?.proposed_company
        ? `New company: ${match.proposed_company || 'unnamed'}`
        : 'Not linked to a client yet';

  return (
    <Box sx={{ mb: 1 }}>
      <Typography sx={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.7px',
        color: 'rgba(232,238,242,.55)', fontWeight: 700, mb: 0.5 }}>Client</Typography>

      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mb: 0.6, flexWrap: 'wrap' }}>
        <Chip size="small"
          label={resolved}
          onDelete={value && !disabled ? () => onChange(null) : undefined}
          sx={{ height: 22, fontSize: 11.5, maxWidth: '100%',
            bgcolor: value || match?.code ? 'rgba(45,214,163,.15)' : 'rgba(240,180,60,.16)',
            color: value || match?.code ? tokens.tealHi : '#F0B43C',
            '& .MuiChip-deleteIcon': { fontSize: 15 } }} />
        {!value && !match?.code && (
          <Typography sx={{ fontSize: 11, color: 'rgba(232,238,242,.5)' }}>
            Pick a client to continue.
          </Typography>
        )}
      </Box>

      <Box ref={anchor}>
        <TextField
          size="small" fullWidth disabled={disabled}
          placeholder={match?.code ? 'Change the client…' : 'Type to search ATLAS clients…'}
          value={q} onChange={(e) => setQ(e.target.value)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          inputProps={{ 'aria-label': 'Search for the client' }}
          InputProps={{ endAdornment: loading
            ? <CircularProgress size={13} sx={{ color: tokens.tealHi }} /> : null }}
          sx={{ '& .MuiInputBase-root': { bgcolor: 'rgba(255,255,255,.04)', color: '#E8EEF2', fontSize: 12.5 },
                '& fieldset': { borderColor: tokens.line } }}
        />
      </Box>

      <Popper open={open && (!!rows.length || isNew)} anchorEl={anchor.current}
        placement="bottom-start" style={{ zIndex: 1400, width: anchor.current?.clientWidth }}>
        <Paper sx={{ bgcolor: '#132539', border: `1px solid ${tokens.line}`,
          maxHeight: 220, overflowY: 'auto' }}>
          {rows.map((m) => (
            <Box key={m.code} onMouseDown={() => pick({ code: m.code, name: m.name, entityId: m.entity_id })}
              sx={{ px: 1.2, py: 0.8, cursor: 'pointer', '&:hover': { bgcolor: 'rgba(255,255,255,.07)' } }}>
              <Typography sx={{ fontSize: 12.5, color: '#E8EEF2' }}>{m.name}</Typography>
              <Typography sx={{ fontSize: 10.5, color: 'rgba(232,238,242,.5)' }}>
                {[m.code, m.kind, m.score != null ? `${Math.round(m.score * 100)}%` : '']
                  .filter(Boolean).join(' · ')}
              </Typography>
            </Box>
          ))}
          {isNew && (
            <Box onMouseDown={() => pick({ code: '__new__', name: q.trim() })}
              sx={{ px: 1.2, py: 0.8, cursor: 'pointer', color: tokens.tealHi,
                borderTop: rows.length ? `1px solid ${tokens.line}` : 0,
                '&:hover': { bgcolor: 'rgba(255,255,255,.07)' } }}>
              <Typography sx={{ fontSize: 12.5 }}>＋ Create “{q.trim()}” as a new company</Typography>
              <Typography sx={{ fontSize: 10.5, color: 'rgba(232,238,242,.5)' }}>
                Nothing on the register matched closely enough.
              </Typography>
            </Box>
          )}
        </Paper>
      </Popper>
    </Box>
  );
}
