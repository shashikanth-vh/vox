import { useState, useEffect } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, Alert, RadioGroup, FormControlLabel, Radio, CircularProgress,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { TextFld } from '../../components/common/Field';
import { CodeText } from '../../components/common/Pills';
import { dealsService } from '../../services/dealsService';
import { useAuth } from '../../auth/AuthContext';
import { tokens } from '../../theme';

/**
 * Close a deal, recording HOW it ended.
 *
 * The three outcomes are the register's three funnel terminals, and the distinction is the
 * reason this dialog exists rather than a plain "Closed" flag:
 *
 *   Won      the deal was done
 *   Lost     Evam wanted it and did not get it   — a competitive outcome
 *   Dropped  Evam walked away from it            — a judgement call
 *
 * Collapsed into one word, the book can still say how many deals did not close but not how
 * many of those were our own decision — which is the question a head of origination asks.
 *
 * Closing is refused while the deal still owes answers (open EWS cases, unresolved covenant
 * observations, product lines mid-pipeline). Those are fetched BEFORE the button is offered
 * so the blocker is named up front, rather than surfacing as a failure after the click.
 */
const OUTCOMES: { key: 'won' | 'lost' | 'dropped'; label: string; stage: string; hint: string }[] = [
  { key: 'won', label: 'Won', stage: 'Closed Won', hint: 'The deal was done.' },
  { key: 'lost', label: 'Lost', stage: 'Closed Lost', hint: 'We wanted this deal and did not get it.' },
  { key: 'dropped', label: 'Dropped', stage: 'Dropped', hint: 'We walked away from it — our own decision.' },
];

export default function CloseDealDialog({ open, code, apiId, currentStage, onClose, onDone }: {
  open: boolean; code: string; apiId?: string; currentStage?: string;
  onClose: () => void; onDone: () => void;
}) {
  const { user } = useAuth();
  const [outcome, setOutcome] = useState<'won' | 'lost' | 'dropped' | ''>('');
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [blockers, setBlockers] = useState<{ ews: number; cov: number; lines: any[] } | null>(null);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    if (!open) return;
    setOutcome(''); setNote(''); setErr(''); setBusy(false); setBlockers(null);
    if (!apiId) return;
    setChecking(true);
    void dealsService.openItems(apiId)
      .then((r) => {
        if (!r) return;                       // pre-check unavailable — the register still decides
        setBlockers(r.blocked
          ? { ews: (r.ews_cases || []).length, cov: (r.covenants || []).length, lines: r.lines || [] }
          : null);
      })
      .finally(() => setChecking(false));
  }, [open, apiId]);

  const submit = async () => {
    if (!outcome) { setErr('Say how this deal ended — Won, Lost or Dropped.'); return; }
    if (!note.trim()) { setErr('A closing note is required. It is the record of why.'); return; }
    setBusy(true); setErr('');
    // Awaited: the register refuses a close while the deal still owes answers, and that
    // refusal has to reach this dialog — not vanish while the desk believes it closed.
    const r = await dealsService.close(code, outcome, note.trim(), user.full);
    setBusy(false);
    if (!r.ok) { setErr(r.error || 'The register refused the close.'); return; }
    onDone(); onClose();
  };

  const chosen = OUTCOMES.find((o) => o.key === outcome);

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>Close deal
        <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Group Code: <CodeText code={code} /></Typography>
        <IconButton onClick={onClose} disabled={busy} sx={{ position: 'absolute', right: 8, top: 8 }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>

      <DialogContent dividers>
        {checking && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1.2 }}>
            <CircularProgress size={14} />
            <Typography sx={{ fontSize: 12, color: tokens.muted }}>Checking open items…</Typography>
          </Box>
        )}

        {blockers && (
          <Alert severity="warning" sx={{ mb: 1.4, fontSize: 12 }}>
            <b>This deal cannot close yet.</b>
            <Box component="ul" sx={{ m: '4px 0 0', pl: 2.2 }}>
              {blockers.ews > 0 && <li>{blockers.ews} open EWS case(s) — each needs a disposition.</li>}
              {blockers.cov > 0 && <li>{blockers.cov} unresolved covenant observation(s).</li>}
              {blockers.lines.length > 0 && (
                <li>{blockers.lines.length} product line(s) still mid-pipeline
                  {blockers.lines.slice(0, 3).map((l: any) =>
                    ` · ${l.line || l.label || ''} ${l.stage || l.status || ''}`.trimEnd()).join('')}
                  {blockers.lines.length > 3 ? ' …' : ''}
                </li>
              )}
            </Box>
            Resolve these first — a deal cannot be closed out from under its own pipeline.
          </Alert>
        )}

        <Typography sx={{ fontSize: 12, fontWeight: 600, mb: 0.4 }}>How did this deal end? *</Typography>
        <RadioGroup value={outcome} onChange={(e) => setOutcome(e.target.value as any)}>
          {OUTCOMES.map((o) => (
            <FormControlLabel
              key={o.key} value={o.key} disabled={busy}
              control={<Radio size="small" sx={{ py: 0.4 }} />}
              sx={{ alignItems: 'flex-start', mb: 0.2 }}
              label={
                <Box sx={{ pt: 0.5 }}>
                  <Typography sx={{ fontSize: 13, fontWeight: 600, lineHeight: 1.2 }}>{o.label}</Typography>
                  <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>{o.hint}</Typography>
                </Box>
              }
            />
          ))}
        </RadioGroup>

        <Box sx={{ mt: 1.2 }}>
          <TextFld label="Closing note" required value={note} onChange={setNote} multiline />
        </Box>

        <Typography sx={{ fontSize: 11.6, color: tokens.muted, mt: 1 }}>
          {currentStage ? <>Current: <b>{currentStage}</b>. </> : null}
          {chosen ? <>The deal will be recorded as <b>{chosen.stage}</b>. </> : null}
          This is final — a revived opportunity is a new deal.
        </Typography>

        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>

      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        {/* Offered even when the pre-check found blockers: the pre-check is advisory and can
            be stale, and the register is the referee. It answers with the live list. */}
        <Button onClick={submit} variant="contained" disabled={busy}>
          {busy ? 'Closing…' : 'Close deal'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
