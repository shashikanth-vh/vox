import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Box, Paper, Typography, Button, Chip, Alert, Dialog, DialogTitle, DialogContent,
  DialogActions, TextField, ToggleButtonGroup, ToggleButton, CircularProgress,
} from '@mui/material';
import { tokens } from '../../theme';
import { useAuth } from '../../auth/AuthContext';
import { reconciliationService, type ReconItem } from '../../services/reconciliationService';

/**
 * The import-reconciliation queue.
 *
 * A governed ledger import RETAINS a row whose stage demands data the sheet did not
 * carry — a facility the book says is Disbursed with no proposed drawdown amount or
 * date. Dropping it would lose a real exposure; importing it clean would put an
 * incomplete record into the workflows as though it were whole. So it lands FLAGGED,
 * and every operational read excludes it until someone closes the gap.
 *
 * That exclusion is why this screen matters: until now the count appeared once in the
 * import dialog and the rows simply were not in the lending list afterwards, which reads
 * as a broken pipeline rather than a queue nobody has worked.
 *
 * The two outcomes are deliberately not symmetric. RESOLVED means the record was
 * actually corrected — through its own update API, so the fix gets the policy engine,
 * the field locks and the history — and the register verifies that before it will close
 * the item. WAIVED leaves the record incomplete on purpose and is Management-only.
 */

const FIELD_LABEL: Record<string, string> = {
  proposed_disbursement_amount: 'Proposed drawdown amount',
  proposed_disbursement_date: 'Proposed drawdown date',
  disbursed_amount: 'Disbursed amount',
  disbursement_date: 'Disbursement date',
  sanction_date: 'Sanction date',
  amount_cr: 'Amount (₹ Cr)',
};
const nice = (f: string) => FIELD_LABEL[f] || f.replace(/_/g, ' ');

// Where the desk goes to FIX it. The register refuses to close an item while a field is
// still blank, so the useful thing a queue can do is point at the record.
const SUBJECT_PATH: Record<string, string> = {
  Lending: '/lending', Syndication: '/syndication',
  AssetMonetisation: '/asset-monetisation', Deal: '/deals', Lead: '/leads',
};

export default function ReconciliationPage() {
  const nav = useNavigate();
  const { user } = useAuth();
  // The register reserves a WAIVER to Management specifically — an Admin may work the queue and
  // close corrected items, but may not decide that a record stays incomplete. Mirror that exactly,
  // rather than the broader Admin-or-Management rule that governs the queue itself.
  const canWaive = user.roles.includes('Management');
  const [status, setStatus] = useState<'Required' | 'Resolved' | 'Waived'>('Required');
  const [rows, setRows] = useState<ReconItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [flash, setFlash] = useState('');
  const [dlg, setDlg] = useState<{ item: ReconItem; kind: 'Resolved' | 'Waived' } | null>(null);
  const [note, setNote] = useState('');
  const [ticket, setTicket] = useState('');
  const [busy, setBusy] = useState(false);

  const load = async (s = status) => {
    setLoading(true); setErr('');
    const r = await reconciliationService.list(s);
    if (r.ok) setRows(r.data || []); else setErr(r.error || 'Could not read the queue.');
    setLoading(false);
  };
  useEffect(() => { void load(status); /* eslint-disable-next-line */ }, [status]);

  const submit = async () => {
    if (!dlg) return;
    if (!note.trim()) { setErr('A note is required — the closure is audited.'); return; }
    // A waiver is break-glass: the register refuses one without a ticket, so ask here rather
    // than letting the desk write the note twice.
    if (dlg.kind === 'Waived' && !ticket.trim()) {
      setErr('A waiver needs a ticket reference — it leaves an incomplete record on the book.');
      return;
    }
    setBusy(true); setErr('');
    const r = await reconciliationService.resolve(dlg.item.id, dlg.kind, note.trim(),
      ticket.trim() || undefined);
    setBusy(false);
    if (!r.ok) { setErr(r.error || 'That did not go through.'); return; }
    setFlash(dlg.kind === 'Resolved'
      ? `${dlg.item.company || 'The record'} is complete and back in the workflows.`
      : `${dlg.item.company || 'The record'} waived — it stays incomplete on the record.`);
    setDlg(null); setNote(''); setTicket('');
    void load();
  };

  return (
    <>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2, mb: 1.2, flexWrap: 'wrap' }}>
        <ToggleButtonGroup exclusive size="small" value={status}
          onChange={(_, v) => v && setStatus(v)}>
          <ToggleButton value="Required" sx={{ fontSize: 12 }}>Open</ToggleButton>
          <ToggleButton value="Resolved" sx={{ fontSize: 12 }}>Resolved</ToggleButton>
          <ToggleButton value="Waived" sx={{ fontSize: 12 }}>Waived</ToggleButton>
        </ToggleButtonGroup>
        <Typography sx={{ fontSize: 12.4, color: tokens.muted }}>
          {status === 'Required'
            ? 'Rows an import kept but could not complete. They are HIDDEN from every '
              + 'operational list until closed — which is why a lending list can look short '
              + 'after an import.'
            : status === 'Resolved'
              ? 'Closed because the record was corrected and the register verified it.'
              : 'Closed as incomplete on purpose, by Management.'}
        </Typography>
        <Box sx={{ flex: 1 }} />
        <Button onClick={() => void load()} disabled={loading}>↻ Refresh</Button>
      </Box>

      {err && <Alert severity="error" onClose={() => setErr('')} sx={{ mb: 1 }}>{err}</Alert>}
      {flash && <Alert severity="success" onClose={() => setFlash('')} sx={{ mb: 1 }}>{flash}</Alert>}

      {loading ? (
        <Paper variant="outlined" sx={{ borderColor: tokens.line, p: 3, textAlign: 'center' }}>
          <CircularProgress size={18} />
        </Paper>
      ) : !rows.length ? (
        <Paper variant="outlined" sx={{ borderColor: tokens.line, p: 3, textAlign: 'center' }}>
          <Typography sx={{ color: tokens.muted, fontSize: 12.5 }}>
            {status === 'Required'
              ? 'Nothing to reconcile — every imported row landed complete.'
              : `No ${status.toLowerCase()} items.`}
          </Typography>
        </Paper>
      ) : rows.map((r) => (
        <Paper key={r.id} variant="outlined" sx={{ borderColor: tokens.line, p: 1.4, mb: 1 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap', mb: 0.6 }}>
            <Typography component="b" sx={{ fontSize: 13.4, fontWeight: 700 }}>
              {r.company || '(no company on the row)'}
            </Typography>
            <Chip size="small" label={`${r.subject_type} · ${r.stage_value || '—'}`} />
            {r.sheet && <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>from “{r.sheet}”</Typography>}
            <Box sx={{ flex: 1 }} />
            {r.status === 'Required' ? (
              <>
                {SUBJECT_PATH[r.subject_type] && (
                  <Button sx={{ fontSize: 12 }}
                    onClick={() => nav(SUBJECT_PATH[r.subject_type])}>
                    Open {r.subject_type.toLowerCase()} →
                  </Button>
                )}
                <Button variant="contained" sx={{ fontSize: 12 }}
                  onClick={() => { setNote(''); setTicket(''); setDlg({ item: r, kind: 'Resolved' }); }}>
                  Mark corrected
                </Button>
                {/* Waiving keeps an incomplete record in the business of record — the
                    register restricts it to Management, so the button is not offered to
                    anyone who would only be refused. */}
                {canWaive && (
                  <Button color="warning" sx={{ fontSize: 12 }}
                    onClick={() => { setNote(''); setTicket(''); setDlg({ item: r, kind: 'Waived' }); }}>
                    Waive
                  </Button>
                )}
              </>
            ) : (
              <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                {r.status} by {r.resolved_by || '—'}
                {r.resolved_at ? ` · ${r.resolved_at.slice(0, 10)}` : ''}
              </Typography>
            )}
          </Box>

          <Typography sx={{ fontSize: 12.2, color: tokens.bad, fontWeight: 600 }}>
            Missing: {r.missing_fields.map(nice).join(' · ') || '—'}
          </Typography>

          {/* WHAT THE SHEET SAID. The correction starts from the source row, and the
              import preserved it precisely so nobody has to reopen the workbook. */}
          {Object.keys(r.original_values || {}).length > 0 && (
            <Box sx={{ mt: 0.8, border: `1px solid ${tokens.line}`, borderRadius: 1, p: '8px 10px' }}>
              <Typography sx={{ fontSize: 10.6, fontWeight: 700, color: tokens.muted,
                textTransform: 'uppercase', letterSpacing: '.5px', mb: 0.4 }}>
                As imported
              </Typography>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: '2px 14px' }}>
                {Object.entries(r.original_values)
                  .filter(([, v]) => v !== null && v !== '' && v !== undefined)
                  .map(([k, v]) => (
                    <Typography key={k} sx={{ fontSize: 11.6, color: tokens.ink }}>
                      <b style={{ color: tokens.muted, fontWeight: 600 }}>{nice(k)}:</b>{' '}
                      {String(v)}
                    </Typography>
                  ))}
              </Box>
            </Box>
          )}
          {r.resolution_note && (
            <Typography sx={{ fontSize: 11.6, color: tokens.muted, mt: 0.6 }}>
              “{r.resolution_note}”
            </Typography>
          )}
        </Paper>
      ))}

      <Dialog open={!!dlg} onClose={() => !busy && setDlg(null)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: 16 }}>
          {dlg?.kind === 'Waived' ? 'Waive' : 'Mark corrected'} — {dlg?.item.company || ''}
        </DialogTitle>
        <DialogContent dividers>
          {dlg?.kind === 'Resolved' ? (
            <Alert severity="info" sx={{ fontSize: 12.2, mb: 1.2 }}>
              Correct the record first, on its own screen. This only CLOSES the item — the
              register re-reads the record and refuses while{' '}
              <b>{dlg.item.missing_fields.map(nice).join(', ')}</b> is still blank.
            </Alert>
          ) : (
            <Alert severity="warning" sx={{ fontSize: 12.2, mb: 1.2 }}>
              A waiver leaves the record incomplete permanently, and returns it to the
              operational lists in that state. Use it for history that genuinely cannot be
              reconstructed.
            </Alert>
          )}
          <TextField fullWidth multiline minRows={2} size="small" autoFocus
            label="Note (audited)" value={note} onChange={(e) => setNote(e.target.value)}
            placeholder={dlg?.kind === 'Waived'
              ? 'Why this cannot be completed — and what was checked'
              : 'What was corrected, and from what source'} />
          {dlg?.kind === 'Waived' && (
            <TextField fullWidth size="small" sx={{ mt: 1 }} required label="Ticket reference"
              helperText="Where the decision to leave this incomplete is recorded."
              value={ticket} onChange={(e) => setTicket(e.target.value)} />
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDlg(null)} disabled={busy}>Cancel</Button>
          <Button variant="contained" onClick={submit} disabled={busy}
            color={dlg?.kind === 'Waived' ? 'warning' : 'primary'}
            startIcon={busy ? <CircularProgress size={14} /> : undefined}>
            {dlg?.kind === 'Waived' ? 'Waive' : 'Close item'}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
