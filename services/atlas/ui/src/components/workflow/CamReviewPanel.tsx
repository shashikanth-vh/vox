import { useState } from 'react';
import { Box, Chip, MenuItem, TextField, Typography, Collapse, Button } from '@mui/material';
import { tokens } from '../../theme';
import { signalOf } from './CamMarkdown';

/**
 * What PRISM checked on the draft just generated, beside it: the Machine QA report, the
 * credit picture's findings (each signal set by the credit team's rubric), and what the
 * reviewer sent back and PRISM repaired. The analyst can set a finding's signal for the
 * NEXT draft, with a reason — the CAM then states it as the analyst's call.
 */

export interface CamFinding {
  id: string; topic: string; signal: string; evidence: string; resolve?: string;
  override?: string;
}
export interface CamQaCheck { check: string; status: string; detail: string }
export interface CamReview { section: string; issues: { subsection: string; issue: string }[] }
export type SignalOverrides = Record<string, { signal: string; note: string }>;

const STATUS: Record<string, { bg: string; fg: string }> = {
  pass: { bg: tokens.okBg, fg: tokens.ok }, fail: { bg: tokens.badBg, fg: tokens.bad },
  warn: { bg: '#FFF4DE', fg: tokens.warn }, info: { bg: tokens.paper, fg: tokens.muted },
};

export default function CamReviewPanel({ qa, findings, reviews, overrides, onOverrides }: {
  qa?: { passed?: boolean; checks?: CamQaCheck[] }; findings?: CamFinding[];
  reviews?: CamReview[]; overrides: SignalOverrides;
  onOverrides: (o: SignalOverrides) => void;
}) {
  const [open, setOpen] = useState(true);
  const checks = qa?.checks ?? [];
  const fixes = (reviews ?? []).reduce((n, r) => n + r.issues.length, 0);
  if (!checks.length && !(findings ?? []).length) return null;
  const set = (id: string, patch: Partial<{ signal: string; note: string }>) => {
    const cur = overrides[id] ?? { signal: '', note: '' };
    const next = { ...cur, ...patch };
    const o = { ...overrides };
    if (next.signal) o[id] = next; else delete o[id];
    onOverrides(o);
  };
  return (
    <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 1.5, p: 1.2, my: 1, bgcolor: '#FBFCFD' }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <Typography sx={{ fontWeight: 700, fontSize: 13.5, flex: 1 }}>
          What PRISM checked
          <Chip size="small" sx={{ ml: 1, fontWeight: 600,
            bgcolor: qa?.passed ? tokens.okBg : tokens.badBg, color: qa?.passed ? tokens.ok : tokens.bad }}
            label={qa?.passed ? 'Machine QA passed' : 'Machine QA: see below'} />
          {fixes > 0 && <Chip size="small" sx={{ ml: 0.6 }} label={`${fixes} reviewer point(s) repaired`} />}
        </Typography>
        <Button size="small" sx={{ textTransform: 'none' }} onClick={() => setOpen(!open)}>
          {open ? 'Hide' : 'Show'}</Button>
      </Box>
      <Collapse in={open}>
        {!!(findings ?? []).length && (
          <>
            <Typography sx={{ fontSize: 12.5, fontWeight: 600, mt: 1 }}>
              Credit picture — signals from the credit rubric (set one for the next draft if you disagree)
            </Typography>
            {(findings ?? []).map((f) => {
              const sig = signalOf(f.signal) ?? { bg: tokens.paper, fg: tokens.ink };
              const o = overrides[f.id];
              return (
                <Box key={f.id} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start', py: 0.6,
                  borderBottom: `1px solid ${tokens.line}` }}>
                  <Chip size="small" label={f.signal}
                    sx={{ bgcolor: sig.bg, color: sig.fg, fontWeight: 700, minWidth: 64 }} />
                  <Box sx={{ flex: 1, fontSize: 12.4 }}>
                    <b>{f.topic}</b> — {f.evidence}
                    {f.override && <i> (analyst: {f.override})</i>}
                    {f.resolve && f.signal !== 'GREEN' &&
                      <Box sx={{ color: tokens.muted, fontSize: 11.8 }}>To resolve: {f.resolve}</Box>}
                  </Box>
                  <TextField select size="small" value={o?.signal ?? ''} sx={{ width: 110 }}
                    label="Set" onChange={(e) => set(f.id, { signal: e.target.value })}>
                    <MenuItem value="">Rubric</MenuItem>
                    {['GREEN', 'AMBER', 'RED'].map((s) => <MenuItem key={s} value={s}>{s}</MenuItem>)}
                  </TextField>
                  {!!o?.signal && (
                    <TextField size="small" placeholder="Why (goes into the CAM)" value={o.note}
                      sx={{ width: 220 }} onChange={(e) => set(f.id, { note: e.target.value })} />
                  )}
                </Box>
              );
            })}
          </>
        )}
        {!!checks.length && (
          <>
            <Typography sx={{ fontSize: 12.5, fontWeight: 600, mt: 1.2 }}>Machine QA</Typography>
            {checks.map((c, i) => {
              const st = STATUS[c.status] ?? STATUS.info;
              return (
                <Box key={i} sx={{ display: 'flex', gap: 1, py: 0.4, fontSize: 12.2 }}>
                  <Chip size="small" label={c.status.toUpperCase()}
                    sx={{ bgcolor: st.bg, color: st.fg, fontWeight: 700, minWidth: 56 }} />
                  <Box sx={{ flex: 1 }}><b>{c.check}</b> — {c.detail}</Box>
                </Box>
              );
            })}
          </>
        )}
        {fixes > 0 && (
          <>
            <Typography sx={{ fontSize: 12.5, fontWeight: 600, mt: 1.2 }}>
              What the reviewer sent back (each repaired once)</Typography>
            {(reviews ?? []).filter((r) => r.issues.length).map((r, i) => (
              <Box key={i} sx={{ fontSize: 12, py: 0.3 }}>
                <b>{r.section.split(' (')[0]}</b>
                <Box component="ul" sx={{ m: 0, pl: 2.4 }}>
                  {r.issues.map((x, k) => <li key={k}><i>{x.subsection}</i>: {x.issue}</li>)}
                </Box>
              </Box>
            ))}
          </>
        )}
      </Collapse>
    </Box>
  );
}
