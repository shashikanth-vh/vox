import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Box, Button, Chip,
  CircularProgress, IconButton, LinearProgress, MenuItem, TextField, Tooltip, Typography,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import CloseIcon from '@mui/icons-material/Close';
import AddIcon from '@mui/icons-material/Add';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import RefreshIcon from '@mui/icons-material/Refresh';
import DownloadIcon from '@mui/icons-material/Download';
import {
  vocxService, captureIdOf, type VocxCapabilities, type VocxPreview,
} from '../../services/vocxService';
import { VOCX_URL } from '../../api/vocxClient';
import { currentRm } from './rm';
import { check, rulesFor, type Completeness } from './completeness';
import ApproveDialog from './ApproveDialog';
import LogToPicker, { type LogTo } from './LogToPicker';
import ClientPicker, { type ClientChoice } from './ClientPicker';
import { tokens } from '../../theme';

/**
 * One capture, reviewed and filed.
 *
 * The machine drafts, a person signs. Everything on this card was extracted from what was
 * actually said — summary, key intel, next steps, the follow-up, the sector fields — and
 * all of it is editable, because an extraction is a good first draft and never an
 * authority. Nothing here has touched the register: the preview call writes nothing, and
 * `Approve` is the single irreversible act.
 *
 * The card fills itself as far as it can. Picking a sector template does not hand the
 * user five empty boxes — it asks the service to fill them from the transcript first, and
 * they correct what it got wrong. Boxes a human has to populate by hand are the thing
 * this product exists to remove.
 */

const TEMPS = ['Hot', 'Warm', 'Cold'];
const STAGES = ['', 'Prospecting', 'Proposal', 'Negotiation', 'Sanctioned', 'Disbursed', 'Closed'];
const MODES = ['', 'in-person', 'video', 'phone', 'site', 'whatsapp'];
/** The flat report fields, in the order the prototype showed them. */
const DETAILS: [string, string][] = [
  ['sector', 'Sector'], ['project_type', 'Project type'],
  ['project_size', 'Project size'], ['location', 'Location'],
  ['loan_product', 'Loan product'], ['ticket_size', 'Ticket size'],
  ['collateral', 'Collateral'], ['equity_raised', 'Equity raised'],
  ['turnover', 'Turnover'],
];

const lbl = {
  fontSize: 10.5, textTransform: 'uppercase' as const, letterSpacing: '.7px',
  color: 'rgba(232,238,242,.55)', fontWeight: 700,
};
const sec = { ...lbl, color: tokens.tealHi, mt: 1.6, mb: 0.6 };
const field = {
  '& .MuiInputBase-root': { bgcolor: 'rgba(255,255,255,.04)', color: '#E8EEF2', fontSize: 12.5 },
  '& fieldset': { borderColor: tokens.line },
  '& .MuiInputLabel-root': { fontSize: 12.5, color: 'rgba(232,238,242,.6)' },
};

export default function ReportCard({ preview, initialStatus, onFiled, onDiscarded }: {
  preview: VocxPreview;
  initialStatus?: string;
  onFiled: (message: string) => void;
  onDiscarded: () => void;
}) {
  const rm = currentRm();
  // The extraction is mutated in place and re-rendered by a version counter — the same
  // shape the service returns and expects back, rather than a parallel model that would
  // have to be mapped in both directions.
  const extRef = useRef<any>(preview.extraction || {});
  const [, bump] = useState(0);
  const redraw = useCallback(() => bump((n) => n + 1), []);
  const ext = extRef.current;
  const R = (): any => (ext.report = ext.report || {});
  const NM = (): any => (ext.next_meeting = ext.next_meeting || {});
  const rep = R();

  const captureId = captureIdOf(preview);
  const [status, setStatus] = useState(String(initialStatus || 'draft').toLowerCase());
  const [caps, setCaps] = useState<VocxCapabilities | null>(null);
  const [logTo, setLogTo] = useState<LogTo | null>(null);
  const [client, setClient] = useState<ClientChoice | null>(null);
  const [audioUrl, setAudioUrl] = useState('');
  const [googleConnected, setGoogleConnected] = useState<boolean | null>(null);
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [note, setNote] = useState('');
  const [askApprove, setAskApprove] = useState(false);
  const [transcript, setTranscript] = useState(
    String(rep.transcript_english || ext._meta?.transcript || ''));
  const focusRef = useRef<Record<string, HTMLElement | null>>({});

  const committed = status === 'committed';
  const match = ext.entity_match || {};
  // The user's own pick wins over the resolver's guess — the rows to log against are
  // the ones belonging to whichever company this capture will actually be filed under.
  const entityId: string | undefined =
    client?.entityId || (client ? undefined : (match.entity_id || match.id || undefined));

  useEffect(() => {
    void vocxService.capabilities().then((r) => { if (r.ok) setCaps(r.data); });
    void vocxService.googleStatus(rm).then((r) => setGoogleConnected(r.ok ? r.data : null));
  }, [rm]);

  // The archived recording, when there is one. The object URL is ours to revoke.
  useEffect(() => {
    const ref = ext._meta?.transcript_ref;
    if (!ref) return;
    let url = '';
    void vocxService.audioUrl(ref).then((r) => { if (r.ok) { url = r.data; setAudioUrl(url); } });
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [ext._meta?.transcript_ref]);

  const activeTpl: string[] = (rep._tpl_active = rep._tpl_active || []);
  const rules = useMemo(() => rulesFor(caps, activeTpl),
    [caps, activeTpl.join('|')]);                                  // eslint-disable-line react-hooks/exhaustive-deps
  const state: Completeness = useMemo(() => check(rep, rules), [rep, rules, bump]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Every field of the active templates, plus any the user invented. */
  const tplFields = useMemo(() => {
    const out: any[] = [];
    for (const t of (caps?.report_templates || []) as any[]) {
      if (activeTpl.includes(t.id)) out.push(...(t.fields || []).map((f: any) => ({ ...f, from: t.label })));
    }
    out.push(...((rep._custom || []) as any[]));
    return out;
  }, [caps, activeTpl.join('|'), rep._custom]);                    // eslint-disable-line react-hooks/exhaustive-deps

  /** Ask the service to fill the given fields from the transcript. */
  const autoFill = useCallback(async (fields: any[], quiet = false) => {
    if (!fields.length) return;
    if (!quiet) { setBusy('Filling from what was said…'); setNote(''); }
    const r = await vocxService.templateFill(transcript, fields);
    if (!quiet) setBusy('');
    if (!r.ok) { if (!quiet) setErr(r.error); return; }
    const values = r.data || {};
    const got = Object.entries(values).filter(([, v]) => String(v ?? '').trim() !== '');
    if (got.length) {
      const extra = (rep.extra = rep.extra || {});
      got.forEach(([k, v]) => { if (!extra[k]) extra[k] = v; });   // never overwrite a human's edit
      redraw();
    }
    if (!quiet) setNote(got.length ? `Filled ${got.length} field(s) from the transcript.`
                                   : 'Nothing in the transcript matched those fields.');
  }, [transcript, rep, redraw]);

  /** Turning a template on auto-fills it immediately — empty boxes are the enemy. */
  const toggleTemplate = async (t: any) => {
    const i = activeTpl.indexOf(t.id);
    if (i >= 0) { activeTpl.splice(i, 1); redraw(); return; }
    activeTpl.push(t.id); redraw();
    await autoFill(t.fields || [], true);
  };

  const listOf = (key: string): any[] => (rep[key] = rep[key] || []);

  const doApprove = async () => {
    setErr(''); setBusy('Filing…');
    const r = await vocxService.commit({
      rm, extraction: ext, capture_id: captureId,
      summary: rep.summary || undefined,
      edits: { date: NM().date || null, time: NM().time || null,
               mode: NM().mode || null, temp: rep.deal_temp || null },
      ...(logTo ? { log_to: logTo } : {}),
      // Which company. '__new__' tells VocX to create it; a code links the existing row.
      // Absent, the resolver's own match stands.
      ...(client?.code === '__new__'
        ? { new_lead: true, company: client.name }
        : client?.code ? { chosen_code: client.code } : {}),
    });
    setBusy(''); setAskApprove(false);
    if (!r.ok) { setErr(r.error); return; }
    setStatus('committed');
    const results = (r.data?.writes?.results || []) as any[];
    const failed = results.filter((w) => w.status && !['ok', 'skipped'].includes(w.status));
    onFiled(failed.length
      ? `Filed — but ${failed.length} write(s) did not land. ${failed[0].reason || failed[0].error || ''}`
      : 'Filed to the register.');
  };

  const saveDraft = async () => {
    setErr(''); setBusy('Saving…');
    const r = await vocxService.saveDraft(rm, captureId,
      { extraction: ext, summary: rep.summary || '' }, 'ready');
    setBusy('');
    if (!r.ok) { setErr(r.error); return; }
    setStatus('ready'); setNote('Saved. It will wait in Reports until you approve it.');
  };

  const reanalyse = async () => {
    if (!transcript.trim()) return;
    setErr(''); setBusy('Re-reading…');
    const r = await vocxService.captureTyped(transcript.trim(), rm, {}, captureId);
    setBusy('');
    if (!r.ok) { setErr(r.error); return; }
    extRef.current = r.data.extraction || {};
    setTranscript(String(extRef.current.report?.transcript_english
      || extRef.current._meta?.transcript || transcript));
    redraw();
    setNote('Report rebuilt from the edited transcript.');
  };

  const remove = async () => {
    setBusy('Deleting…');
    if (captureId) await vocxService.remove(rm, captureId);
    setBusy('');
    onDiscarded();
  };

  const jumpTo = (key: string) => {
    setAskApprove(false);
    const el = focusRef.current[key];
    el?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    (el as HTMLInputElement | null)?.focus?.();
  };

  const pct = state.total ? Math.round((state.filled / state.total) * 100) : 0;

  return (
    <Box sx={{ p: 1.4 }}>
      {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }}
        onClose={() => setErr('')}>{err}</Alert>}
      {note && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }}
        onClose={() => setNote('')}>{note}</Alert>}

      {/* Status + temperature */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.6, flexWrap: 'wrap' }}>
        <Chip size="small" label={status.toUpperCase()}
          sx={{ height: 21, fontSize: 10, fontWeight: 800,
            bgcolor: committed ? 'rgba(45,214,163,.16)' : status === 'ready'
              ? tokens.tealHi : 'rgba(240,180,60,.18)',
            color: committed ? tokens.tealHi : status === 'ready' ? '#04241B' : '#F0B43C' }} />
        {TEMPS.map((t) => (
          <Chip key={t} size="small" label={t} clickable={!committed}
            onClick={() => { if (!committed) { rep.deal_temp = rep.deal_temp === t ? null : t; redraw(); } }}
            sx={{ height: 21, fontSize: 11,
              bgcolor: rep.deal_temp === t ? tokens.tealHi : 'rgba(255,255,255,.06)',
              color: rep.deal_temp === t ? '#04241B' : 'rgba(232,238,242,.75)' }} />
        ))}
      </Box>

      <Typography sx={{ fontSize: 17, fontWeight: 800, mt: 0.8, lineHeight: 1.25 }}>
        {rep.title || match.canonical_name || ext.company_mentioned || 'Field report'}
      </Typography>
      <Typography sx={{ fontSize: 11, color: 'rgba(232,238,242,.5)' }}>
        {[rep.sector, ext._meta?.language?.toUpperCase(),
          ext._meta?.duration ? `${Math.round(ext._meta.duration)}s` : '']
          .filter(Boolean).join(' · ')}
      </Typography>

      {/* Completeness — visible before the approve dialog, not only inside it. */}
      <Box sx={{ mt: 1 }}>
        <LinearProgress variant="determinate" value={pct}
          sx={{ height: 4, borderRadius: 2, bgcolor: 'rgba(255,255,255,.08)',
            '& .MuiLinearProgress-bar': { bgcolor: state.missingRequired.length ? tokens.warn : tokens.tealHi } }} />
        <Typography sx={{ fontSize: 10.5, color: 'rgba(232,238,242,.5)', mt: 0.3 }}>
          {state.filled}/{state.total} captured
          {state.missingRequired.length ? ` · ${state.missingRequired.length} required missing` : ''}
        </Typography>
      </Box>

      {!committed && (
        <>
          <ClientPicker match={match} value={client}
            onChange={(c) => { setClient(c); setLogTo(null); }} />
          <LogToPicker entityId={entityId} value={logTo} onChange={setLogTo} />
        </>
      )}

      {/* Actions */}
      <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap', mt: 1.2 }}>
        <Button size="small" variant="contained" disabled={!!busy || committed}
          onClick={() => setAskApprove(true)} sx={{ textTransform: 'none', fontWeight: 700 }}>
          Approve
        </Button>
        <Button size="small" variant="outlined" disabled={!!busy || committed}
          onClick={saveDraft} sx={{ textTransform: 'none' }}>Save changes</Button>
        <Tooltip title="Opens the print view — use your browser's Save as PDF">
          <Button size="small" startIcon={<DownloadIcon sx={{ fontSize: 15 }} />}
            onClick={() => window.open(
              `${VOCX_URL}/v1/reports/print?rm=${encodeURIComponent(rm)}&id=${encodeURIComponent(captureId)}`,
              '_blank', 'noopener')}
            sx={{ textTransform: 'none', color: 'rgba(232,238,242,.7)' }}>PDF</Button>
        </Tooltip>
        <Box sx={{ flex: 1 }} />
        <IconButton size="small" onClick={remove} disabled={!!busy} aria-label="Delete this report"
          sx={{ color: tokens.bad }}><DeleteOutlineIcon sx={{ fontSize: 18 }} /></IconButton>
      </Box>
      {busy && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mt: 0.8 }}>
          <CircularProgress size={13} sx={{ color: tokens.tealHi }} />
          <Typography sx={{ fontSize: 11.5, color: tokens.tealHi }}>{busy}</Typography>
        </Box>
      )}

      {audioUrl && (
        <>
          <Typography sx={sec}>Original audio</Typography>
          <audio controls src={audioUrl} style={{ width: '100%', height: 34 }} />
        </>
      )}

      <Typography sx={sec}>Summary</Typography>
      <TextField multiline minRows={2} fullWidth size="small" sx={field}
        disabled={committed}
        inputRef={(el) => { focusRef.current.summary = el; }}
        value={rep.summary || ''}
        onChange={(e) => { rep.summary = e.target.value; redraw(); }} />

      <BulletList title="Key intel" rows={listOf('key_intel')} disabled={committed}
        anchor={(el) => { focusRef.current.key_intel = el; }}
        onChange={redraw} addLabel="Add bullet" />

      <BulletList title="Nuances & soft signals" rows={listOf('nuances')} disabled={committed}
        anchor={(el) => { focusRef.current.nuances = el; }}
        onChange={redraw} addLabel="Add nuance" />

      {/* Next steps — owner / action / date, as the extraction produces them. */}
      <Typography sx={sec}>Next steps</Typography>
      {listOf('next_steps').map((s: any, i: number) => (
        <Box key={i} sx={{ display: 'flex', gap: 0.5, mb: 0.5 }}>
          <TextField size="small" placeholder="owner" sx={{ ...field, width: 92 }} disabled={committed}
            value={s.owner || ''} onChange={(e) => { s.owner = e.target.value || null; redraw(); }} />
          <TextField size="small" placeholder="action…" sx={{ ...field, flex: 1 }} disabled={committed}
            inputRef={i === 0 ? (el) => { focusRef.current.next_steps = el; } : undefined}
            value={s.action || ''} onChange={(e) => { s.action = e.target.value; redraw(); }} />
          <TextField size="small" type="date" sx={{ ...field, width: 138 }} disabled={committed}
            value={s.date || ''} onChange={(e) => { s.date = e.target.value || null; redraw(); }} />
          <IconButton size="small" disabled={committed} aria-label="Remove step"
            onClick={() => { listOf('next_steps').splice(i, 1); redraw(); }}>
            <CloseIcon sx={{ fontSize: 15, color: 'rgba(232,238,242,.5)' }} />
          </IconButton>
        </Box>
      ))}
      {!listOf('next_steps').length && <Empty />}
      <Button size="small" startIcon={<AddIcon sx={{ fontSize: 15 }} />} disabled={committed}
        onClick={() => { listOf('next_steps').push({ owner: null, action: '', date: null }); redraw(); }}
        sx={ghost}>Add next step</Button>

      {/* Next meeting + what will actually happen on approve. */}
      <Typography sx={sec}>Next meeting</Typography>
      <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap' }}>
        <TextField size="small" type="date" label="Follow-up" sx={{ ...field, flex: '1 1 140px' }}
          InputLabelProps={{ shrink: true }} disabled={committed}
          value={NM().date || ''} onChange={(e) => { NM().date = e.target.value; redraw(); }} />
        <TextField size="small" type="time" label="Time" sx={{ ...field, width: 108 }}
          InputLabelProps={{ shrink: true }} disabled={committed}
          value={NM().time || ''} onChange={(e) => { NM().time = e.target.value; redraw(); }} />
        <TextField size="small" select label="Mode" sx={{ ...field, width: 118 }} disabled={committed}
          value={NM().mode || ''} onChange={(e) => { NM().mode = e.target.value; redraw(); }}>
          {MODES.map((m) => <MenuItem key={m} value={m} sx={{ fontSize: 12.5 }}>{m || '—'}</MenuItem>)}
        </TextField>
      </Box>
      {NM().date && (
        // The honest signal is PER-RM: a client secret mounted on the server does not
        // mean THIS person has connected their calendar.
        <Alert severity={googleConnected ? 'success' : 'warning'}
          sx={{ mt: 0.8, py: 0, fontSize: 11.5 }}>
          {googleConnected
            ? `An event will be added to ${rm}'s Google Calendar when you approve.`
            : caps?.google_configured
              ? `${rm} has not connected Google — the follow-up is recorded in the register, but no calendar event is created.`
              : 'Google is not configured on this deployment — the follow-up is recorded, with no calendar event.'}
        </Alert>
      )}

      {/* Transcript */}
      <Accordion sx={acc} disableGutters>
        <AccordionSummary expandIcon={<ExpandMoreIcon sx={{ color: 'rgba(232,238,242,.6)' }} />}>
          <Typography sx={{ fontSize: 13, fontWeight: 700 }}>Full transcript</Typography>
          <Typography sx={{ fontSize: 11.5, color: 'rgba(232,238,242,.45)', ml: 1,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 150 }}>
            {transcript.slice(0, 48)}
          </Typography>
        </AccordionSummary>
        <AccordionDetails>
          <Typography sx={{ fontSize: 11, color: 'rgba(232,238,242,.55)', mb: 0.6 }}>
            Fix any mis-heard word, then re-read — the whole report is rebuilt from it.
          </Typography>
          <TextField multiline minRows={4} fullWidth size="small" sx={field} disabled={committed}
            value={transcript} onChange={(e) => setTranscript(e.target.value)} />
          <Button size="small" startIcon={<RefreshIcon sx={{ fontSize: 15 }} />}
            disabled={!!busy || committed} onClick={reanalyse}
            sx={{ ...ghost, mt: 0.8 }}>Re-analyse from transcript</Button>
        </AccordionDetails>
      </Accordion>

      {/* Additional details */}
      <Accordion sx={acc} disableGutters defaultExpanded>
        <AccordionSummary expandIcon={<ExpandMoreIcon sx={{ color: 'rgba(232,238,242,.6)' }} />}>
          <Typography sx={{ fontSize: 13, fontWeight: 700 }}>Additional details</Typography>
        </AccordionSummary>
        <AccordionDetails>
          <TextField size="small" fullWidth label="Client name" sx={{ ...field, mb: 1 }}
            disabled={committed}
            inputRef={(el) => { focusRef.current.title = el; }}
            value={rep.title || ''} onChange={(e) => { rep.title = e.target.value; redraw(); }} />

          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.8 }}>
            {DETAILS.map(([k, label]) => (
              <TextField key={k} size="small" label={label} disabled={committed}
                sx={{ ...field, flex: '1 1 45%' }}
                inputRef={(el) => { focusRef.current[k] = el; }}
                value={rep[k] || ''} onChange={(e) => { rep[k] = e.target.value || null; redraw(); }} />
            ))}
            <TextField size="small" select label="Pipeline stage" disabled={committed}
              sx={{ ...field, flex: '1 1 45%' }}
              inputRef={(el) => { focusRef.current.pipeline_stage = el; }}
              value={rep.pipeline_stage || ''}
              onChange={(e) => { rep.pipeline_stage = e.target.value || null; redraw(); }}>
              {STAGES.map((s) => <MenuItem key={s} value={s} sx={{ fontSize: 12.5 }}>{s || '—'}</MenuItem>)}
            </TextField>
          </Box>

          {/* Templates: the chips come from the service, and turning one on fills it. */}
          <Box sx={{ display: 'flex', alignItems: 'center', mt: 1.6, mb: 0.6 }}>
            <Typography sx={{ ...lbl, flex: 1 }}>Template fields</Typography>
            <Button size="small" startIcon={<AutoAwesomeIcon sx={{ fontSize: 15 }} />}
              disabled={!!busy || committed || !tplFields.length}
              onClick={() => autoFill(tplFields)}
              sx={{ ...ghost, mt: 0 }}>Auto-fill from transcript</Button>
          </Box>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, mb: 1 }}>
            {((caps?.report_templates || []) as any[]).map((t) => {
              const on = activeTpl.includes(t.id);
              return (
                <Chip key={t.id} size="small" clickable={!committed}
                  label={on ? t.label : `+ ${t.label}`}
                  onDelete={on && !committed ? () => void toggleTemplate(t) : undefined}
                  onClick={() => { if (!committed) void toggleTemplate(t); }}
                  sx={{ height: 24, fontSize: 11.5,
                    bgcolor: on ? tokens.tealHi : 'transparent',
                    color: on ? '#04241B' : tokens.tealHi,
                    border: `1px ${on ? 'solid' : 'dashed'} ${tokens.tealHi}`,
                    '& .MuiChip-deleteIcon': { color: '#04241B', fontSize: 15 } }} />
              );
            })}
          </Box>

          {tplFields.map((f: any) => (
            <Box key={f.key} sx={{ display: 'flex', alignItems: 'center', gap: 0.4, mb: 0.6 }}>
              {f.type === 'select' ? (
                <TextField size="small" select fullWidth label={f.label || f.key} disabled={committed}
                  sx={field} value={(rep.extra || {})[f.key] || ''}
                  onChange={(e) => { (rep.extra = rep.extra || {})[f.key] = e.target.value || null; redraw(); }}>
                  {(f.options || []).map((o: string) => (
                    <MenuItem key={o} value={o} sx={{ fontSize: 12.5 }}>{o || '—'}</MenuItem>))}
                </TextField>
              ) : (
                <TextField size="small" fullWidth disabled={committed}
                  label={f.label || f.key}
                  type={f.type === 'number' ? 'number' : f.type === 'date' ? 'date' : 'text'}
                  InputLabelProps={f.type === 'date' ? { shrink: true } : undefined}
                  sx={field}
                  inputRef={(el) => { focusRef.current[`extra.${f.key}`] = el; }}
                  value={(rep.extra || {})[f.key] || ''}
                  onChange={(e) => { (rep.extra = rep.extra || {})[f.key] = e.target.value || null; redraw(); }} />
              )}
              {f.required && <Typography sx={{ fontSize: 15, color: tokens.warn }} title="Required">*</Typography>}
            </Box>
          ))}
          {!tplFields.length && (
            <Typography sx={{ fontSize: 11.5, color: 'rgba(232,238,242,.5)' }}>
              Pick a template above and VocX fills it from what was said.
            </Typography>
          )}
          <Button size="small" startIcon={<AddIcon sx={{ fontSize: 15 }} />} disabled={committed}
            onClick={() => {
              const name = window.prompt('Field label:');
              if (!name) return;
              const key = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
              (rep._custom = rep._custom || []).push({ key, label: name });
              redraw();
            }} sx={ghost}>Add field</Button>

          <Typography sx={{ ...lbl, mt: 1.6, mb: 0.6 }}>Opportunity score (1–5)</Typography>
          <Box sx={{ display: 'flex', gap: 0.5 }}>
            {[1, 2, 3, 4, 5].map((n) => (
              <Box key={n} component="button" type="button" disabled={committed}
                ref={n === 1 ? (el: any) => { focusRef.current.opportunity_score = el; } : undefined}
                onClick={() => { rep.opportunity_score = rep.opportunity_score === n ? null : n; redraw(); }}
                aria-pressed={rep.opportunity_score === n}
                sx={{ width: 38, height: 34, borderRadius: '8px', cursor: 'pointer', fontWeight: 700,
                  border: `1px solid ${tokens.line}`,
                  bgcolor: rep.opportunity_score === n ? tokens.tealHi : 'rgba(255,255,255,.04)',
                  color: rep.opportunity_score === n ? '#04241B' : 'rgba(232,238,242,.75)' }}>
                {n}
              </Box>
            ))}
          </Box>

          <Typography sx={sec}>Attendees</Typography>
          {listOf('attendees').map((a: any, i: number) => (
            <Box key={i} sx={{ display: 'flex', gap: 0.5, mb: 0.5 }}>
              {(['name', 'role', 'company'] as const).map((k) => (
                <TextField key={k} size="small" placeholder={k} sx={{ ...field, flex: 1 }}
                  disabled={committed}
                  inputRef={i === 0 && k === 'name' ? (el) => { focusRef.current.attendees = el; } : undefined}
                  value={a[k] || ''} onChange={(e) => { a[k] = e.target.value || null; redraw(); }} />
              ))}
              <IconButton size="small" disabled={committed} aria-label="Remove attendee"
                onClick={() => { listOf('attendees').splice(i, 1); redraw(); }}>
                <CloseIcon sx={{ fontSize: 15, color: 'rgba(232,238,242,.5)' }} />
              </IconButton>
            </Box>
          ))}
          {!listOf('attendees').length && <Empty />}
          <Button size="small" startIcon={<AddIcon sx={{ fontSize: 15 }} />} disabled={committed}
            onClick={() => { listOf('attendees').push({ name: '', role: null, company: null }); redraw(); }}
            sx={ghost}>Add attendee</Button>
        </AccordionDetails>
      </Accordion>

      <ApproveDialog
        open={askApprove} state={state} busy={!!busy}
        onFill={jumpTo} onFile={doApprove} onClose={() => setAskApprove(false)} />
    </Box>
  );
}

// --------------------------------------------------------------------------------- //

const ghost = {
  textTransform: 'none' as const, fontSize: 11.5, color: tokens.tealHi,
  border: `1px dashed ${tokens.line}`, mt: 0.4,
};
const acc = {
  bgcolor: 'rgba(255,255,255,.03)', border: `1px solid ${tokens.line}`,
  borderRadius: '10px !important', mt: 1.6, color: '#E8EEF2',
  '&:before': { display: 'none' },
};

const Empty = () => (
  <Typography sx={{ fontSize: 11.5, color: 'rgba(232,238,242,.45)' }}>None yet.</Typography>
);

/** A simple string list — key intel, nuances. */
function BulletList({ title, rows, disabled, addLabel, onChange, anchor }: {
  title: string; rows: string[]; disabled: boolean; addLabel: string;
  onChange: () => void; anchor: (el: HTMLElement | null) => void;
}) {
  return (
    <>
      <Typography sx={sec}>{title}</Typography>
      {rows.map((v, i) => (
        <Box key={i} sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mb: 0.5 }}>
          <Box sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: tokens.tealHi, flexShrink: 0 }} />
          <TextField size="small" fullWidth sx={field} disabled={disabled}
            inputRef={i === 0 ? anchor : undefined}
            value={v} onChange={(e) => { rows[i] = e.target.value; onChange(); }} />
          <IconButton size="small" disabled={disabled} aria-label={`Remove from ${title}`}
            onClick={() => { rows.splice(i, 1); onChange(); }}>
            <CloseIcon sx={{ fontSize: 15, color: 'rgba(232,238,242,.5)' }} />
          </IconButton>
        </Box>
      ))}
      {!rows.length && <Empty />}
      <Button size="small" startIcon={<AddIcon sx={{ fontSize: 15 }} />} disabled={disabled}
        onClick={() => { rows.push(''); onChange(); }} sx={ghost}>{addLabel}</Button>
    </>
  );
}
