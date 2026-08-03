import { useEffect, useState } from 'react';
import { Box, Chip, MenuItem, TextField, Typography } from '@mui/material';
import { listAll } from '../../api/http';
import { tokens } from '../../theme';

/**
 * Where this interaction gets filed.
 *
 * "Auto" lets VocX's own resolver decide from what was said, which is right most of the
 * time. Naming a line overrides it — and the override needs a ROW, not a subject type,
 * because a company can hold three lending facilities and the note belongs to one.
 *
 * The dev console asked for a pasted UUID here, with a comment saying the real UI would
 * list them. This is that: the rows are read from the register for the resolved company,
 * shown by the number a person quotes, and the UUID never surfaces.
 */

const LINES: { type: string; label: string; path: string; }[] = [
  { type: 'Lead', label: 'Lead', path: '/leads' },
  { type: 'Deal', label: 'Deal', path: '/deals' },
  { type: 'Lending', label: 'Lending', path: '/lending' },
  { type: 'Syndication', label: 'Syndication', path: '/syndication' },
  { type: 'AssetMonetisation', label: 'Asset Monetisation', path: '/asset-monetisation' },
];

/** The human number for a row, whichever column carries it. */
const rowLabel = (r: any): string =>
  [r.tracker_no || r.deal_no || r.lead_no || r.code,
   r.company || r.stage || r.status].filter(Boolean).join(' · ') || String(r.id).slice(0, 8);

export interface LogTo { subject_type: string; subject_id: string; }

export default function LogToPicker({ entityId, value, onChange }: {
  /** The register entity the capture resolved to; without one there are no rows to list. */
  entityId?: string;
  value: LogTo | null;
  onChange: (v: LogTo | null) => void;
}) {
  const [type, setType] = useState<string>(value?.subject_type || '');
  const [rows, setRows] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    if (!type || !entityId) { setRows([]); return; }
    const line = LINES.find((l) => l.type === type);
    if (!line) return;
    let alive = true;
    setLoading(true); setErr('');
    void listAll(line.path, { params: { entity_id: entityId } })
      .then((r) => { if (alive) setRows(r); })
      .catch(() => { if (alive) setErr('Could not read that company\'s lines.'); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [type, entityId]);

  const pickType = (t: string) => {
    setType(t);
    onChange(null);                       // a new lane always needs a new row chosen
  };

  return (
    <Box sx={{ borderLeft: `3px solid ${tokens.tealHi}`, borderRadius: '4px',
      bgcolor: 'rgba(255,255,255,.03)', p: 1.2, my: 1.4 }}>
      <Typography sx={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.7px',
        color: 'rgba(232,238,242,.55)', fontWeight: 700, mb: 0.6 }}>Log to</Typography>

      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.6 }}>
        <Chip size="small" label="Auto" clickable onClick={() => pickType('')}
          sx={chipSx(!type)} />
        {LINES.map((l) => (
          <Chip key={l.type} size="small" label={l.label} clickable
            onClick={() => pickType(l.type)} sx={chipSx(type === l.type)} />
        ))}
      </Box>

      {!type && (
        <Typography sx={{ fontSize: 11, color: 'rgba(232,238,242,.5)', mt: 0.6 }}>
          VocX files it against whatever it resolved from the conversation.
        </Typography>
      )}

      {!!type && !entityId && (
        <Typography sx={{ fontSize: 11.5, color: tokens.warn, mt: 0.6 }}>
          Link this capture to a company first — the lines to choose from are its own.
        </Typography>
      )}

      {!!type && !!entityId && (
        <TextField
          select fullWidth size="small" sx={{ mt: 1 }}
          label={loading ? 'Loading…' : `${type} row`}
          value={value?.subject_id || ''}
          onChange={(e) => onChange(e.target.value
            ? { subject_type: type, subject_id: e.target.value } : null)}
          disabled={loading || !rows.length}
          helperText={err || (!loading && !rows.length ? 'This company has no such line yet.' : '')}
        >
          {rows.map((r) => (
            <MenuItem key={r.id} value={r.id} sx={{ fontSize: 12.5 }}>{rowLabel(r)}</MenuItem>
          ))}
        </TextField>
      )}
    </Box>
  );
}

const chipSx = (on: boolean) => ({
  height: 24, fontSize: 11.5, fontWeight: 700,
  bgcolor: on ? tokens.tealHi : 'rgba(255,255,255,.06)',
  color: on ? '#04241B' : 'rgba(232,238,242,.75)',
  '&:hover': { bgcolor: on ? tokens.tealHi : 'rgba(255,255,255,.12)' },
});
