import { useEffect, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, TextField, Alert, MenuItem, CircularProgress, Divider,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { camService, newestOf, type SanctionTermsOut, type EntityDoc } from '../../services/camService';
import { documentsService } from '../../services/documentsService';
import type { WorkflowAction } from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * The sanction-LETTER workshop: download the letterhead template (and the approved CAM,
 * which the letter is written from), file the signed letter, and let the engine fill the
 * numeric terms out of it. Saving records the terms once and quietly seeds the covenant
 * register from the letter — CP/CS live entirely in their own screens, which read the
 * letter themselves.
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
      setLetter(newestOf(docs, 'sanction_letter'));
    } catch { /* the letter block simply shows no file yet */ }
  };

  // The letter ON FILE — the one thing a second visit to this dialog is usually for.
  const downloadLetter = async () => {
    if (!letter) return;
    setErr(''); setLetterBusy('letter');
    const ext = /wordprocessingml/.test(letter.content_type || '') ? '.docx'
      : /pdf/.test(letter.content_type || '') ? '.pdf' : '';
    const r = await documentsService.download({ id: letter.id,
      name: `${letter.title}${ext}` } as any);
    if (!r.ok) setErr(r.error || 'The letter download failed.');
    setLetterBusy('');
  };

  const downloadTemplate = async () => {
    if (!tmpl) return;
    setLetterBusy('template');
    const r = await documentsService.download({ id: tmpl.id,
      name: 'sanction_letter_template.docx' } as any);
    if (!r.ok) setErr(r.error || 'Could not fetch the template.');
    setLetterBusy('');
  };

  // The letter is WRITTEN FROM the approved CAM — hand the analyst the latest one
  // right here: the filed .docx if a completed CAM is on record, else the newest
  // drafted text as a .md file. No CAM yet → say so.
  const downloadCam = async () => {
    setErr(''); setLetterBusy('cam');
    try {
      const reports = await camService.list(lendingId);
      const filed = [...reports].reverse().find((r) => r.document_id);
      if (filed?.document_id) {
        const docs = await camService.lendingDocs(lendingId);
        const d = docs.find((x) => x.id === filed.document_id);
        const ext = /wordprocessingml/.test(d?.content_type || '') ? '.docx'
          : /pdf/.test(d?.content_type || '') ? '.pdf'
            : /markdown/.test(d?.content_type || '') ? '.md' : '';
        const out = await documentsService.download({ id: filed.document_id,
          name: `${d?.title || 'CAM'}${ext}` } as any);
        if (!out.ok) setErr(out.error || 'The CAM download failed.');
      } else {
        const drafted = [...reports].reverse().find((r) => r.draft_md);
        if (!drafted?.draft_md) {
          setErr('No CAM on this line yet — prepare it in the CAM workbench first.');
        } else {
          // A DRAFT is still a CAM the letter can be written from — rendered to Word
          // so it reads like the filed one would.
          await camService.exportDocx(lendingId, drafted.draft_md,
            `CAM v${drafted.report_version} draft`);
        }
      }
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLetterBusy('');
  };

  // The engine reads the FIGURES out of a document + the committee's credit note —
  // amount, rate, tenor, EMI, day count, … — and fills the form (only fields still
  // empty). Covenants ride along (their chase starts at disbursement). The analyst
  // reviews, then saves & seeds. Two sources, same mechanics: the approved CAM
  // (BEFORE the letter exists — these figures are what the letter is written from)
  // and the signed letter itself once it is filed.
  const applyExtract = (out: Awaited<ReturnType<typeof camService.extractTerms>>,
                        source: string) => {
    const t = out.terms || {};
    setF((p) => {
      const next = { ...p };
      for (const [k, v] of Object.entries(t)) {
        if (v !== null && v !== undefined && !p[k]) next[k] = String(v);
      }
      return next;
    });
    if (out.covenants.length) {
      setCovs(out.covenants.map((c) => ({
        ...blankCov(), name: c.name + (c.timeline ? ` (${c.timeline})` : ''),
        frequency: c.frequency,
      })));
    }
    setInfo(`Filled from ${source}: ${Object.keys(t).length} term field(s), `
      + `${out.covenants.length} covenant(s). Review, then Save terms & seed.`);
  };

  const fillFromLetter = async (letterDoc: EntityDoc | null, noteText?: string) => {
    if (!letterDoc) return;
    setErr(''); setLetterBusy('parse');
    try {
      applyExtract(await camService.extractTerms(letterDoc.id, noteText),
        `the letter${noteText ? ' + credit note' : ''}`);
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLetterBusy('');
  };

  // The whole letter in one go: the engine fills the TEMPLATE with the CAM's /
  // credit note's / typed terms' figures and the draft downloads as Word. The
  // analyst edits it there, signs, and uploads it through the normal lane.
  const generateLetter = async () => {
    if (!tmpl) return;
    setErr(''); setLetterBusy('draft');
    try {
      const reports = await camService.list(lendingId);
      const filed = [...reports].reverse().find((r) => r.document_id);
      const typed: Record<string, string> = {};
      for (const [k, v] of Object.entries(f)) if (v?.trim()) typed[k] = v.trim();
      // What the CAM cannot know — the committee's references and today's date —
      // rides along too, so Ref. No. / Date come out filled, not as [____] blanks.
      if (decision?.committee_reference) typed.committee_reference = decision.committee_reference;
      if (decision?.sanction_letter_reference) typed.sanction_letter_reference = decision.sanction_letter_reference;
      typed.letter_date = new Date().toLocaleDateString('en-GB',
        { day: '2-digit', month: 'short', year: 'numeric' });
      await camService.draftLetter(lendingId, {
        template_doc_id: tmpl.id,
        ...(filed?.document_id ? { cam_doc_id: filed.document_id } : {}),
        ...(decision?.note ? { credit_note: decision.note } : {}),
        ...(Object.keys(typed).length ? { terms: typed } : {}),
      });
      setInfo('Draft letter downloaded — review and edit it in Word, then upload the '
        + 'signed letter here.');
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLetterBusy('');
  };

  // Terms out of the COMPLETED CAM — usable before any letter exists, which is
  // exactly when the analyst is drafting one.
  const fillFromCam = async () => {
    setErr(''); setLetterBusy('cam-parse');
    try {
      const reports = await camService.list(lendingId);
      const filed = [...reports].reverse().find((r) => r.document_id);
      if (!filed?.document_id) {
        setErr('No completed CAM on this line yet — prepare it in the CAM workbench first.');
      } else {
        applyExtract(await camService.extractTerms(filed.document_id, decision?.note || undefined),
          `the CAM${decision?.note ? ' + credit note' : ''}`);
      }
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLetterBusy('');
  };

  const uploadLetter = async (file: File | null) => {
    if (!file || !lendingId) return;
    setErr(''); setLetterBusy('upload');
    try {
      await camService.uploadDoc(lendingId, file, 'sanction_letter', 'Sanction');
      setInfo(`Sanction letter "${file.name}" filed — reading the terms out of it…`);
      await loadLetter(lendingId);
      const docs = await camService.lendingDocs(lendingId);
      // The one just uploaded — NOT whatever `.pop()` happened to land on. This is the
      // line that read the previous letter while announcing it was reading this one.
      const fresh = newestOf(docs, 'sanction_letter');
      setLetterBusy('');
      // The upload IS the trigger: the terms fill themselves from what was just filed.
      await fillFromLetter(fresh, decision?.note || undefined);
      return;
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLetterBusy('');
  };

  const [f, setF] = useState<Record<string, string>>({});
  // Covenants come OUT OF THE LETTER (fillFromLetter) and are seeded silently on save —
  // no editor here; their dates stamp themselves one cycle after the first tranche.
  const [covs, setCovs] = useState<CovRow[]>([]);
  const set = (k: string, v: string) => setF((p) => ({ ...p, [k]: v }));

  useEffect(() => {
    if (!open || !lendingId) return;
    setErr(''); setInfo(''); setBusy(false);
    setF({ rate_kind: 'Fixed', day_count: '365', schedule_kind: 'EMI' });
    setCovs([]); setLetter(null);
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
    // An EMI schedule amortises over its tenor, and interest accrues at a rate — the
    // loan account refuses to accrue at all without one ("this account has no interest
    // rate"), and a recorded term can only be corrected by amendment afterwards. So
    // catch a gap HERE, while the letter is still in hand.
    //
    // Name ONLY what is actually missing. The message used to demand "Rate % and Tenor"
    // whenever either was blank, so a desk that had just typed the tenor was told to
    // fill it in again — and it justified itself with "the EMI is computed from them"
    // while sitting beside an EMI field the desk had typed off the letter. A typed EMI
    // is honoured verbatim; the rate is still required, because it prices the interest,
    // not the instalment.
    if ((f.schedule_kind || 'EMI') === 'EMI') {
      const missing = [
        !num('rate_pct') && 'Rate %',
        !num('tenor_months') && 'Tenor (months)',
      ].filter(Boolean) as string[];
      if (missing.length) {
        setErr(`An EMI schedule needs ${missing.join(' and ')}. `
          + (num('emi_amount')
            ? 'The instalment you have entered is kept as it stands — the rate is what '
              + 'the interest accrues at, and the tenor is how long it runs. '
            : 'The instalment is computed from the rate and the tenor. ')
          + `Fill ${missing.length > 1 ? 'them' : 'it'} in, or pick Bullet/Custom as `
          + 'the schedule.');
        return;
      }
    }
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
        // CP/CS are worked in their own screens, which read the letter directly.
        cp_items: [], cs_items: [],
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
          <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
            {letter && (
              <Button size="small" variant="contained" disabled={letterBusy === 'letter'}
                onClick={() => void downloadLetter()} sx={{ textTransform: 'none' }}
                title="The signed sanction letter on file for this facility">
                {letterBusy === 'letter' ? 'Fetching…' : 'Download sanction letter'}
              </Button>
            )}
            <Button size="small" variant="outlined" disabled={!tmpl || letterBusy === 'template'}
              onClick={() => void downloadTemplate()} sx={{ textTransform: 'none' }}>
              {letterBusy === 'template' ? 'Fetching…' : tmpl ? 'Download template' : 'No template on record'}
            </Button>
            <Button size="small" variant="outlined" disabled={letterBusy === 'cam'}
              onClick={() => void downloadCam()} sx={{ textTransform: 'none' }}
              title="The latest CAM on this line — the letter is written from it">
              {letterBusy === 'cam' ? 'Fetching…' : 'Download CAM'}
            </Button>
            {!existing && (
              <Button size="small" variant="outlined" disabled={!!letterBusy}
                onClick={() => void fillFromCam()} sx={{ textTransform: 'none' }}
                title="The engine reads the amount, rate, tenor, EMI … out of the completed CAM (and the credit note) and fills the fields below — before the letter exists, while you draft it">
                {letterBusy === 'cam-parse' ? 'Reading CAM…' : 'Fill terms from CAM'}
              </Button>
            )}
            {!letter && (
              <Button size="small" variant="contained" disabled={!tmpl || !!letterBusy}
                onClick={() => void generateLetter()} sx={{ textTransform: 'none' }}
                title="The engine fills the letterhead template with the CAM's, credit note's and typed terms' figures — the draft downloads as Word for you to edit, sign, and upload">
                {letterBusy === 'draft' ? 'Drafting…' : 'Generate letter (draft)'}
              </Button>
            )}
            <Button size="small" component="label" variant={letter ? 'outlined' : 'contained'}
              disabled={letterBusy === 'upload'} sx={{ textTransform: 'none' }}>
              {letterBusy === 'upload' ? 'Uploading…' : letter ? 'Replace sanction letter…' : 'Upload sanction letter…'}
              <input hidden type="file" accept=".docx,.pdf"
                onChange={(e) => { void uploadLetter(e.target.files?.[0] || null); e.target.value = ''; }} />
            </Button>
            {letter && !existing && (
              <Button size="small" variant="contained" disabled={!!letterBusy}
                onClick={() => void fillFromLetter(letter, decision?.note || undefined)}
                sx={{ textTransform: 'none' }}
                title="The engine reads the amount, rate, tenor, EMI … out of the letter and the credit note, and fills the fields below — you review and save">
                {letterBusy === 'parse' ? 'Reading…' : 'Fill terms from the letter'}
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
              Entered once, at committee approval — the letter fills these on upload; review
              and save. CP/CS are worked in their own screens, which read the letter directly.
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

            {covs.filter((c) => c.name.trim()).length > 0 && (
              <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 1 }}>
                {covs.filter((c) => c.name.trim()).length} covenant(s) read from the letter
                will be opened on save — their reporting cycles start automatically one
                cycle after the first disbursement.
              </Typography>
            )}

            <TextField fullWidth size="small" sx={{ mt: 1 }} label="Note (optional)"
              value={f.note || ''} onChange={(e) => set('note', e.target.value)} />
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy || letterBusy === 'upload'}>
          Close
        </Button>
        {!existing && (
          // Terms are entered ONCE — saving mid-extraction would record them half-filled
          // and throw the engine's reading away, so the button waits for the letter work.
          <Button onClick={() => void save()} variant="contained"
            disabled={busy || loading || !!letterBusy}>
            {busy ? 'Saving…'
              : letterBusy === 'upload' ? 'Filing the letter…'
                : letterBusy ? 'Reading the letter…'
                  : 'Save terms & seed'}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
