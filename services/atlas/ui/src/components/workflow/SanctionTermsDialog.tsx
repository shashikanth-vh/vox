import { useEffect, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, TextField, Alert, MenuItem, CircularProgress, Divider,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import AddIcon from '@mui/icons-material/Add';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import { camService, type SanctionTermsOut, type EntityDoc } from '../../services/camService';
import { documentsService } from '../../services/documentsService';
import type { WorkflowAction } from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * Enter the SANCTIONED terms — once, at committee approval — and the register seeds
 * everything downstream in one transaction: the CP/CS checklist (CP + CS items) and the
 * covenant register (each covenant with its own compliance cycle).
 *
 * Terms are entered ONCE per line (the register enforces it); corrections go through an
 * amendment, not a second entry — so when terms already exist the dialog shows them
 * read-only instead of offering a form that can only 409.
 */

interface CovRow {
  name: string; covenant_type: string; metric: string; operator: string;
  threshold: string; frequency: string; first_due_on: string; breach_severity: string;
}

// Monthly by default: most covenants here are REPORTING obligations — the borrower's
// monthly financial pack — and the reminder cadence follows this frequency.
const blankCov = (): CovRow => ({
  name: '', covenant_type: 'Reporting', metric: '', operator: '>=', threshold: '',
  frequency: 'Monthly', first_due_on: '', breach_severity: 'Amber',
});

/** "Board resolution for borrowing" -> a stable checklist key. */
const slug = (s: string): string =>
  s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '').slice(0, 60) || 'item';

/** One condition per line — the register gets {key,label} items. */
const toItems = (text: string) => text.split('\n').map((l) => l.trim()).filter(Boolean)
  .map((label) => ({ key: slug(label), label, required: true }));

export default function SanctionTermsDialog({ action, onClose, onDone }: {
  action: WorkflowAction | null;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const open = !!action;
  const lendingId = String(action?.body?.lending_id || '');

  const [existing, setExisting] = useState<SanctionTermsOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [info, setInfo] = useState('');
  // The sanction LETTER: the analyst fills the credit team's template (shipped as the
  // tenant default; a case-specific upload wins) and files the signed letter here.
  const [tmpl, setTmpl] = useState<{ id: string; title: string } | null>(null);
  const [letter, setLetter] = useState<EntityDoc | null>(null);
  const [letterBusy, setLetterBusy] = useState('');
  // What the committee SAID when it approved — the terms are entered against the
  // committee's own words (their note, references, conditions), not from memory.
  const [decision, setDecision] = useState<any | null>(null);

  const loadLetter = async (id: string) => {
    try {
      const docs = await camService.lendingDocs(id);
      setLetter(docs.filter((d) => d.doc_type === 'sanction_letter').pop() || null);
    } catch { /* the letter block simply shows no file yet */ }
  };

  const downloadTemplate = async () => {
    if (!tmpl) return;
    setLetterBusy('template');
    const r = await documentsService.download({ id: tmpl.id,
      name: 'sanction_letter_template.docx' } as any);
    if (!r.ok) setErr(r.error || 'Could not fetch the template.');
    setLetterBusy('');
  };

  const uploadLetter = async (file: File | null) => {
    if (!file || !lendingId) return;
    setErr(''); setLetterBusy('upload');
    try {
      await camService.uploadDoc(lendingId, file, 'sanction_letter', 'Sanction');
      setInfo(`Sanction letter "${file.name}" filed on this line.`);
      await loadLetter(lendingId);
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLetterBusy('');
  };

  // The letter already LISTS the CPs, the CSs (with timelines) and the reporting
  // covenants — the engine reads them out and pre-fills the form. The analyst reviews,
  // sets the first-due dates, and only then saves & seeds.
  const readLetter = async () => {
    if (!letter) return;
    setErr(''); setLetterBusy('parse');
    try {
      const out = await camService.extractTerms(letter.id);
      setCpRows(out.cp_items);
      setCsRows(out.cs_items
        .map((c) => c.label + (c.timeline ? ` (${c.timeline})` : '')));
      setCovs(out.covenants.map((c) => ({
        ...blankCov(), name: c.name + (c.timeline ? ` (${c.timeline})` : ''),
        frequency: c.frequency,
      })));
      setInfo(`Read from the letter: ${out.cp_items.length} CP, ${out.cs_items.length} CS, `
        + `${out.covenants.length} covenant(s). Review below — covenant due dates may stay `
        + 'empty; they start one cycle after the first disbursement.');
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLetterBusy('');
  };

  const [f, setF] = useState<Record<string, string>>({});
  // Each condition is its OWN row (like the covenants) — the letter has many, and a
  // single textarea made ten conditions read as one blob.
  const [cpRows, setCpRows] = useState<string[]>([]);
  const [csRows, setCsRows] = useState<string[]>([]);
  const [covs, setCovs] = useState<CovRow[]>([]);
  // One date for every covenant at once — individual rows can still override after.
  const [allDue, setAllDue] = useState('');
  const set = (k: string, v: string) => setF((p) => ({ ...p, [k]: v }));

  useEffect(() => {
    if (!open || !lendingId) return;
    setErr(''); setInfo(''); setBusy(false);
    setF({ rate_kind: 'Fixed', day_count: '365', schedule_kind: 'EMI' });
    setCpRows([]); setCsRows([]); setCovs([]); setAllDue(''); setLetter(null);
    setLoading(true);
    camService.terms(lendingId)
      .then(setExisting)
      .catch((e: any) => setErr(e?.message || String(e)))
      .finally(() => setLoading(false));
    void camService.template('sanction_template').then(setTmpl);
    void loadLetter(lendingId);
    setDecision(null);
    void (async () => {
      try {
        const { api } = await import('../../api/http');
        const d = await api.get<any>(`/lending/${lendingId}/committee-decision`);
        setDecision(d);
        // The committee's note seeds the terms note — editable, never overwriting
        // something already typed.
        if (d?.note) setF((p) => ({ ...p, note: p.note || d.note }));
      } catch { /* no decision recorded yet — the banner simply does not show */ }
    })();
  }, [open, lendingId]);

  const save = async () => {
    setErr('');
    const num = (k: string) => (f[k]?.trim() ? Number(f[k]) : undefined);
    const covenants = covs.filter((c) => c.name.trim());
    // No first-due date is FINE: the covenant defers, and its schedule stamps itself
    // one cycle after the first confirmed disbursement tranche.
    for (const c of covenants) {
      if (c.covenant_type === 'Financial' && !(c.metric && c.operator && c.threshold.trim())) {
        setErr(`Covenant "${c.name}": a Financial covenant needs metric + operator + threshold.`);
        return;
      }
    }
    setBusy(true);
    try {
      const out = await camService.createTerms({
        lending_id: lendingId,
        ...(action?.body?.deal_id ? { deal_id: action.body.deal_id } : {}),
        amount_cr: num('amount_cr'), rate_kind: f.rate_kind || 'Fixed',
        rate_pct: num('rate_pct'), spread_pct: num('spread_pct'),
        tenor_months: num('tenor_months'), emi_amount: num('emi_amount'),
        repayment_start: f.repayment_start || undefined,
        day_count: f.day_count || '365', penal_rate_pct: num('penal_rate_pct'),
        moratorium_months: num('moratorium_months') ?? 0,
        schedule_kind: f.schedule_kind || 'EMI',
        cp_items: toItems(cpRows.join('\n')), cs_items: toItems(csRows.join('\n')),
        covenants: covenants.map((c) => ({
          name: c.name.trim(), covenant_type: c.covenant_type,
          ...(c.metric ? { metric: c.metric } : {}),
          ...(c.covenant_type === 'Financial' ? { operator: c.operator, threshold: Number(c.threshold) } : {}),
          frequency: c.frequency,
          ...(c.first_due_on ? { first_due_on: c.first_due_on } : {}),
          breach_severity: c.breach_severity,
        })),
        note: f.note || undefined,
      });
      onDone(`Sanction terms saved — CP/CS checklist ${out.seeded_checklist_id ? 'seeded' : 'not needed'}, `
        + `${out.seeded_covenant_ids.length} covenant(s) opened.`);
      onClose();
    } catch (e: any) { setErr(e?.message || String(e)); }
    setBusy(false);
  };

  const covSet = (i: number, k: keyof CovRow, v: string) =>
    setCovs((p) => p.map((c, j) => (j === i ? { ...c, [k]: v } : c)));

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>
        Sanction terms
        {loading && <CircularProgress size={13} sx={{ ml: 1, verticalAlign: 'middle' }} />}
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 6 }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setErr('')}>{err}</Alert>}
        {info && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setInfo('')}>{info}</Alert>}

        {decision && (
          <Alert severity="info" sx={{ mb: 1.2, py: 0.4, fontSize: 12 }}>
            <b>Committee {String(decision.decision || '').toLowerCase()}</b> by {decision.decided_by}
            {decision.committee_reference ? <> · {decision.committee_reference}</> : null}
            {decision.sanction_letter_reference ? <> · {decision.sanction_letter_reference}</> : null}
            {decision.note ? <> — “{decision.note}”</> : null}
            {decision.conditions ? <><br />Conditions: {decision.conditions}</> : null}
            {decision.valid_days ? <> · valid {decision.valid_days} days</> : null}
          </Alert>
        )}

        {/* ---- the sanction LETTER: template out, signed letter in ------------------- */}
        <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1.2, mb: 1.4 }}>
          <Typography sx={{ fontSize: 12.5, fontWeight: 600, mb: 0.4 }}>Sanction letter</Typography>
          <Typography sx={{ fontSize: 12, color: tokens.muted, mb: 0.8 }}>
            {letter
              ? <>On file: <b>{letter.title}</b> — replace it by uploading a newer version.</>
              : 'Download the letterhead template, fill it from the committee\'s approval, and file the signed letter here.'}
          </Typography>
          <Box sx={{ display: 'flex', gap: 1 }}>
            <Button size="small" variant="outlined" disabled={!tmpl || letterBusy === 'template'}
              onClick={() => void downloadTemplate()} sx={{ textTransform: 'none' }}>
              {letterBusy === 'template' ? 'Fetching…' : tmpl ? 'Download template' : 'No template on record'}
            </Button>
            <Button size="small" component="label" variant={letter ? 'outlined' : 'contained'}
              disabled={letterBusy === 'upload'} sx={{ textTransform: 'none' }}>
              {letterBusy === 'upload' ? 'Uploading…' : letter ? 'Replace letter…' : 'Upload signed letter…'}
              <input hidden type="file" accept=".docx,.pdf"
                onChange={(e) => { void uploadLetter(e.target.files?.[0] || null); e.target.value = ''; }} />
            </Button>
            {letter && !existing && (
              <Button size="small" variant="contained" disabled={!!letterBusy}
                onClick={() => void readLetter()} sx={{ textTransform: 'none' }}
                title="The engine reads the letter's CP / CS / covenant sections and pre-fills the lists below — you review and save">
                {letterBusy === 'parse' ? 'Reading the letter…' : 'Read CP / CS / covenants from the letter'}
              </Button>
            )}
          </Box>
        </Box>

        {existing ? (
          <Box>
            <Alert severity="info" sx={{ py: 0, fontSize: 12, mb: 1.2 }}>
              Terms were entered by {existing.note ? 'the credit team' : 'the credit team'} and are
              read-only — corrections go through an amendment, not a second entry.
            </Alert>
            <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 1, fontSize: 13 }}>
              {[['Amount ₹ Cr', existing.amount_cr], ['Rate', `${existing.rate_kind} ${existing.rate_pct ?? '—'}%`],
                ['Spread %', existing.spread_pct], ['Tenor (months)', existing.tenor_months],
                ['EMI', existing.emi_amount], ['Repayment from', existing.repayment_start],
                ['Day count', existing.day_count], ['Moratorium (m)', existing.moratorium_months],
                ['Schedule', existing.schedule_kind]].map(([label, v]) => (
                  <Box key={String(label)}>
                    <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>{label}</Typography>
                    <Typography sx={{ fontSize: 13 }}>{v ?? '—'}</Typography>
                  </Box>
              ))}
            </Box>
            <Divider sx={{ my: 1.2 }} />
            <Typography sx={{ fontSize: 12.5 }}>
              CP items: <b>{existing.cp_items.length}</b> · CS items: <b>{existing.cs_items.length}</b> ·
              Covenants: <b>{existing.covenants.length}</b>
              {existing.seeded_checklist_id ? ' · checklist seeded' : ''}
            </Typography>
          </Box>
        ) : (
          <Box>
            <Typography sx={{ fontSize: 12.5, color: tokens.muted, mb: 1 }}>
              Entered once, at committee approval. Saving seeds the CP/CS checklist and the
              covenant register in one step — those artefacts then run their own lifecycles.
            </Typography>
            <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 1 }}>
              <TextField size="small" label="Amount ₹ Cr" type="number" value={f.amount_cr || ''} onChange={(e) => set('amount_cr', e.target.value)} />
              <TextField size="small" select label="Rate kind" value={f.rate_kind || 'Fixed'} onChange={(e) => set('rate_kind', e.target.value)}>
                {['Fixed', 'Floating'].map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
              </TextField>
              <TextField size="small" label="Rate %" type="number" value={f.rate_pct || ''} onChange={(e) => set('rate_pct', e.target.value)} />
              <TextField size="small" label="Spread % (floating)" type="number" value={f.spread_pct || ''} onChange={(e) => set('spread_pct', e.target.value)} />
              <TextField size="small" label="Tenor (months)" type="number" value={f.tenor_months || ''} onChange={(e) => set('tenor_months', e.target.value)} />
              <TextField size="small" label="EMI amount" type="number" value={f.emi_amount || ''} onChange={(e) => set('emi_amount', e.target.value)} />
              <TextField size="small" label="Repayment starts" type="date" InputLabelProps={{ shrink: true }} value={f.repayment_start || ''} onChange={(e) => set('repayment_start', e.target.value)} />
              <TextField size="small" select label="Day count" value={f.day_count || '365'} onChange={(e) => set('day_count', e.target.value)}>
                {['365', '360'].map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
              </TextField>
              <TextField size="small" select label="Schedule" value={f.schedule_kind || 'EMI'} onChange={(e) => set('schedule_kind', e.target.value)}>
                {['EMI', 'Bullet', 'Custom'].map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
              </TextField>
              <TextField size="small" label="Penal rate %" type="number" value={f.penal_rate_pct || ''} onChange={(e) => set('penal_rate_pct', e.target.value)} />
              <TextField size="small" label="Moratorium (months)" type="number" value={f.moratorium_months || ''} onChange={(e) => set('moratorium_months', e.target.value)} />
            </Box>

            <Divider sx={{ my: 1.4 }} />
            <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1.4 }}>
              {([['Conditions PRECEDENT', cpRows, setCpRows],
                 ['Conditions SUBSEQUENT', csRows, setCsRows]] as const)
                .map(([label, rows, setRows]) => (
                <Box key={label}>
                  <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.4 }}>
                    <Typography sx={{ fontSize: 12.5, fontWeight: 600, flex: 1 }}>
                      {label} ({rows.filter((r) => r.trim()).length})
                    </Typography>
                    <Button size="small" startIcon={<AddIcon sx={{ fontSize: 15 }} />}
                      onClick={() => setRows((p: string[]) => [...p, ''])}
                      sx={{ textTransform: 'none', fontSize: 12 }}>Add</Button>
                  </Box>
                  {rows.length === 0 && (
                    <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                      None yet — Add, or read them from the letter above.
                    </Typography>
                  )}
                  {rows.map((r, i) => (
                    <Box key={i} sx={{ display: 'flex', gap: 0.5, mb: 0.5, alignItems: 'center' }}>
                      <TextField size="small" fullWidth multiline maxRows={3} value={r}
                        placeholder="Condition"
                        onChange={(e) => setRows((p: string[]) =>
                          p.map((x, j) => (j === i ? e.target.value : x)))} />
                      <IconButton size="small"
                        onClick={() => setRows((p: string[]) => p.filter((_x, j) => j !== i))}>
                        <DeleteOutlineIcon sx={{ fontSize: 17 }} />
                      </IconButton>
                    </Box>
                  ))}
                </Box>
              ))}
            </Box>

            <Divider sx={{ my: 1.4 }} />
            <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.6, gap: 1, flexWrap: 'wrap' }}>
              <Typography sx={{ fontSize: 12.5, fontWeight: 600, flex: 1 }}>Covenants</Typography>
              <TextField size="small" type="date" label="Set ALL first due"
                InputLabelProps={{ shrink: true }} value={allDue}
                onChange={(e) => setAllDue(e.target.value)} sx={{ width: 170 }} />
              <Button size="small" variant="outlined" disabled={!allDue || !covs.length}
                onClick={() => setCovs((p) => p.map((c) => ({ ...c, first_due_on: allDue })))}
                sx={{ textTransform: 'none', fontSize: 12 }}>Apply to all</Button>
              <Button size="small" startIcon={<AddIcon sx={{ fontSize: 15 }} />}
                onClick={() => setCovs((p) => [...p, blankCov()])}
                sx={{ textTransform: 'none', fontSize: 12 }}>Add covenant</Button>
            </Box>
            <Typography sx={{ fontSize: 11.5, color: tokens.muted, mb: 0.6 }}>
              First due may stay EMPTY — a covenant with no date starts automatically one
              cycle after the first disbursement (the chase runs from there until closure).
            </Typography>
            {covs.map((c, i) => (
              <Box key={i} sx={{ display: 'grid', gap: 0.6, mb: 0.8, p: 0.8,
                border: `1px solid ${tokens.line}`, borderRadius: 1,
                gridTemplateColumns: '2fr 1.2fr 1fr 0.7fr 0.9fr 1.1fr 1.2fr 0.9fr auto' }}>
                <TextField size="small" label="Name" value={c.name} onChange={(e) => covSet(i, 'name', e.target.value)} />
                <TextField size="small" select label="Type" value={c.covenant_type} onChange={(e) => covSet(i, 'covenant_type', e.target.value)}>
                  {['Financial', 'Reporting', 'Security', 'Other'].map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
                </TextField>
                <TextField size="small" label="Metric" placeholder="dscr" value={c.metric} onChange={(e) => covSet(i, 'metric', e.target.value)} />
                <TextField size="small" select label="Op" value={c.operator} onChange={(e) => covSet(i, 'operator', e.target.value)}>
                  {['>=', '<=', '>', '<', '='].map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
                </TextField>
                <TextField size="small" label="Threshold" type="number" value={c.threshold} onChange={(e) => covSet(i, 'threshold', e.target.value)} />
                <TextField size="small" select label="Frequency" value={c.frequency} onChange={(e) => covSet(i, 'frequency', e.target.value)}>
                  {/* The register's cycle vocabulary — the sweep steps periods by these. */}
                  {['Monthly', 'Quarterly', 'SemiAnnual', 'Annual'].map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
                </TextField>
                <TextField size="small" label="First due" type="date" InputLabelProps={{ shrink: true }} value={c.first_due_on} onChange={(e) => covSet(i, 'first_due_on', e.target.value)} />
                <TextField size="small" select label="Severity" value={c.breach_severity} onChange={(e) => covSet(i, 'breach_severity', e.target.value)}>
                  {['Amber', 'Red'].map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
                </TextField>
                <IconButton size="small" onClick={() => setCovs((p) => p.filter((_x, j) => j !== i))}
                  sx={{ alignSelf: 'center' }}><DeleteOutlineIcon sx={{ fontSize: 17 }} /></IconButton>
              </Box>
            ))}

            <TextField fullWidth size="small" sx={{ mt: 1 }} label="Note (optional)"
              value={f.note || ''} onChange={(e) => set('note', e.target.value)} />
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Close</Button>
        {!existing && (
          <Button onClick={() => void save()} variant="contained" disabled={busy || loading}>
            {busy ? 'Saving…' : 'Save terms & seed'}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
