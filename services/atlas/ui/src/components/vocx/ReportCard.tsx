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
import { currentRm } from './rm';
import { check, rulesFor, type Completeness } from './completeness';
import ApproveDialog from './ApproveDialog';
import LogToPicker, { type LogTo } from './LogToPicker';
import ClientPicker, { type ClientChoice } from './ClientPicker';
import { vx, card, heading, microHeading, label as lbl2, input as inputSx,
         pill, pillPrimary, pillGhost, pillDanger, chip as chipSx, badge, banner } from './vocxStyles';

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

const lbl = lbl2;
const sec = microHeading;
const field = inputSx;

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
  const [audioErr, setAudioErr] = useState('');
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
    // A failed fetch must SAY WHY. This used to fail silently, so a 403 (a clip keyed
    // under another user), a 404 (archive expired) and an unreachable store all looked
    // identical: "I cannot hear the audio", with nothing to go on.
    void vocxService.audioUrl(ref).then((r) => {
      if (r.ok) { url = r.data; setAudioUrl(url); setAudioErr(''); }
      else setAudioErr(r.error);
    });
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
    // The typed lane rebuilds the extraction from text — carry the recording's ref
    // forward or the re-analysed report loses its audio player (and, autosaved, the
    // stored draft loses it for good on an older server).
    const prevRef = extRef.current?._meta?.transcript_ref;
    extRef.current = r.data.extraction || {};
    if (prevRef && !extRef.current._meta?.transcript_ref) {
      (extRef.current._meta = extRef.current._meta || {}).transcript_ref = prevRef;
    }
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

  /**
   * The print view, opened the only way it can be: fetched, then written into a window
   * this click already opened.
   *
   * Linking straight at the print URL was a plain navigation with no bearer token, so
   * the edge answered "Authentication required" and the user got JSON where a report
   * should have been. The blank tab is opened SYNCHRONOUSLY inside the click — a
   * `window.open` after an await is a pop-up, and browsers block it.
   */
  const openPrintView = async () => {
    const w = window.open('', '_blank', 'noopener');
    if (!w) { setErr('Allow pop-ups for this site to open the print view.'); return; }
    w.document.write('<!doctype html><title>Preparing…</title>'
      + '<body style="font:14px system-ui;padding:24px">Preparing the report…</body>');
    setErr('');
    const r = await vocxService.printable(rm, captureId);
    if (!r.ok) { w.close(); setErr(r.error); return; }
    w.document.open();
    w.document.write(r.data);
    w.document.close();
  };

  const jumpTo = (key: string) => {
    setAskApprove(false);
    const el = focusRef.current[key];
    el?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    (el as HTMLInputElement | null)?.focus?.();
  };

  const pct = state.total ? Math.round((state.filled / state.total) * 100) : 0;

  return (
    <Box sx={{ p: 1.4, minWidth: 0, maxWidth: '100%', overflowWrap: 'anywhere' }}>
      {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }}
        onClose={() => setErr('')}>{err}</Alert>}
      {note && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }}
        onClose={() => setNote('')}>{note}</Alert>}

      {/* Status + temperature */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <Box component="span" sx={badge(status)}>{status.toUpperCase()}</Box>
        {TEMPS.map((t) => (
          <Chip key={t} label={t} clickable={!committed}
            onClick={() => { if (!committed) { rep.deal_temp = rep.deal_temp === t ? null : t; redraw(); } }}
            sx={chipSx(rep.deal_temp === t)} />
        ))}
      </Box>

      <Typography sx={{ fontSize: 26, fontWeight: 700, mt: 1.2, lineHeight: 1.15 }}>
        {rep.title || match.canonical_name || ext.company_mentioned || 'Field report'}
      </Typography>
      {rep.sector && (
        <Typography sx={{ fontSize: 17, color: vx.grn2, mt: 0.2 }}>{rep.sector}</Typography>
      )}
      <Typography sx={{ fontSize: 13.5, color: vx.mut, mt: 0.4 }}>
        {[new Date().toLocaleString(),
          ext._meta?.duration ? `⏱ ${Math.floor(ext._meta.duration / 60)}:${String(Math.round(ext._meta.duration % 60)).padStart(2, '0')}` : '',
          (ext._meta?.language || 'en').toUpperCase()].filter(Boolean).join(' · ')}
      </Typography>

      {/* Completeness — visible before the approve dialog, not only inside it. */}
      <Box sx={{ mt: 1 }}>
        <LinearProgress variant="determinate" value={pct}
          sx={{ height: 4, borderRadius: 2, bgcolor: 'rgba(255,255,255,.08)',
            '& .MuiLinearProgress-bar': { bgcolor: state.missingRequired.length ? vx.amberInk : vx.grn } }} />
        <Typography sx={{ fontSize: 10.5, color: vx.mut, mt: 0.3 }}>
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
        <Button disabled={!!busy || committed}
          onClick={() => setAskApprove(true)} sx={pillPrimary}>Approve</Button>
        <Button disabled={!!busy || committed} onClick={saveDraft} sx={pill}>Save changes</Button>
        <Tooltip title="Opens the print view — use your browser's Save as PDF">
          <Button startIcon={<DownloadIcon sx={{ fontSize: 17 }} />}
            disabled={!!busy || !captureId} onClick={openPrintView}
            sx={pill}>Download PDF</Button>
        </Tooltip>
        <Box sx={{ flex: 1 }} />
        <IconButton onClick={remove} disabled={!!busy} aria-label="Delete this report"
          sx={{ ...pillDanger, px: 1.4 }}><DeleteOutlineIcon sx={{ fontSize: 19 }} /></IconButton>
      </Box>
      {busy && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mt: 0.8 }}>
          <CircularProgress size={13} sx={{ color: vx.grn }} />
          <Typography sx={{ fontSize: 11.5, color: vx.grn }}>{busy}</Typography>
        </Box>
      )}

      {(audioUrl || audioErr) ? (
        <>
          <Typography sx={microHeading}>Original audio</Typography>
          {audioUrl
            ? <audio controls src={audioUrl}
                style={{ width: '100%', maxWidth: '100%', height: 34 }} />
            : <Typography sx={{ fontSize: 12, color: vx.amberInk }}>
                Recording unavailable — {audioErr}
              </Typography>}
        </>
      ) : !ext._meta?.transcript_ref && (
        /* Silence used to be the only signal here — "sometimes I can't listen" with
           nothing to go on. Say WHY there is no player: this report simply carries no
           archived recording (typed note, or the archive was off at capture time). */
        <Typography sx={{ fontSize: 11.5, color: vx.mut, mt: 0.8 }}>
          No recording attached — this note was typed, or the archive was unavailable
          when it was captured.
        </Typography>
      )}

      <Box sx={card}>
        <Typography sx={microHeading}>Summary</Typography>
        <TextField multiline minRows={2} fullWidth sx={field}
          disabled={committed}
          inputRef={(el) => { focusRef.current.summary = el; }}
          value={rep.summary || ''}
          onChange={(e) => { rep.summary = e.target.value; redraw(); }} />
      </Box>

      <Box sx={card}>
        <BulletList title="Key intell" rows={listOf('key_intel')} disabled={committed}
          anchor={(el) => { focusRef.current.key_intel = el; }}
          onChange={redraw} addLabel="Add bullet" />
        <Box sx={{ mt: 2 }}>
          <Typography sx={microHeading}>Nuances &amp; soft signals</Typography>
          <BulletList title="" rows={listOf('nuances')} disabled={committed}
            anchor={(el) => { focusRef.current.nuances = el; }}
            onChange={redraw} addLabel="Add nuance" />
        </Box>
      </Box>

      <Box sx={card}>
      {/* Next steps — owner / action / date, as the extraction produces them. */}
      <Typography sx={heading}>Next steps</Typography>
      {listOf('next_steps').map((s: any, i: number) => (
        <Box key={i} sx={{ display: 'flex', gap: 0.5, mb: 0.5, flexWrap: 'wrap' }}>
          <TextField size="small" placeholder="owner" sx={{ ...field, width: 92 }} disabled={committed}
            value={s.owner || ''} onChange={(e) => { s.owner = e.target.value || null; redraw(); }} />
          <TextField size="small" placeholder="action…" sx={{ ...field, flex: '1 1 130px' }}
            disabled={committed}
            inputRef={i === 0 ? (el) => { focusRef.current.next_steps = el; } : undefined}
            value={s.action || ''} onChange={(e) => { s.action = e.target.value; redraw(); }} />
          <TextField size="small" type="date" sx={{ ...field, width: 138 }} disabled={committed}
            value={s.date || ''} onChange={(e) => { s.date = e.target.value || null; redraw(); }} />
          <IconButton size="small" disabled={committed} aria-label="Remove step"
            onClick={() => { listOf('next_steps').splice(i, 1); redraw(); }}>
            <CloseIcon sx={{ fontSize: 15, color: vx.mut }} />
          </IconButton>
        </Box>
      ))}
      {!listOf('next_steps').length && <Empty />}
      <Button size="small" startIcon={<AddIcon sx={{ fontSize: 15 }} />} disabled={committed}
        onClick={() => { listOf('next_steps').push({ owner: null, action: '', date: null }); redraw(); }}
        sx={pillGhost}>Add next step</Button>

      {/* Next meeting + what will actually happen on approve. */}
      <Typography sx={heading}>Next meeting</Typography>
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
          {!googleConnected && caps?.google_configured && (
            <Button size="small" disabled={!!busy} sx={{ ...pillGhost, ml: 1, py: 0.4 }}
              onClick={async () => {
                setErr(''); setBusy('Opening Google…');
                const r = await vocxService.googleAuthUrl();
                setBusy('');
                if (!r.ok) { setErr(r.error); return; }
                window.open(r.data, '_blank', 'noopener');
              }}>Connect Google</Button>
          )}
        </Alert>
      )}

      </Box>

      {/* Transcript */}
      <Accordion sx={acc} disableGutters>
        <AccordionSummary expandIcon={<ExpandMoreIcon sx={{ color: vx.mut }} />}>
          <Typography sx={{ fontSize: 19, fontWeight: 700 }}>Full transcript</Typography>
          <Typography sx={{ fontSize: 14, color: vx.mut, ml: 1.2, alignSelf: 'center',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 160 }}>
            {transcript.slice(0, 48)}
          </Typography>
        </AccordionSummary>
        <AccordionDetails>
          <Typography sx={{ fontSize: 14, color: vx.mut, mb: 1 }}>
            Edit any mis-heard words, then re-analyse to rebuild the report.
          </Typography>
          <TextField multiline minRows={4} fullWidth size="small" sx={field} disabled={committed}
            value={transcript} onChange={(e) => setTranscript(e.target.value)} />
          <Button size="small" startIcon={<RefreshIcon sx={{ fontSize: 15 }} />}
            disabled={!!busy || committed} onClick={reanalyse}
            sx={{ ...pillGhost, mt: 1.2 }}>Re-analyse from transcript</Button>
        </AccordionDetails>
      </Accordion>

      {/* Additional details */}
      <Accordion sx={acc} disableGutters defaultExpanded>
        <AccordionSummary expandIcon={<ExpandMoreIcon sx={{ color: vx.mut }} />}>
          <Typography sx={{ fontSize: 19, fontWeight: 700, flex: 1 }}>Additional details</Typography>
          <Typography sx={{ fontSize: 14, color: vx.mut, alignSelf: 'center', mr: 1 }}>
            {rep.sector || ''}
          </Typography>
        </AccordionSummary>
        <AccordionDetails>
          <TextField size="small" fullWidth label="Client name" sx={{ ...field, mb: 1 }}
            disabled={committed}
            inputRef={(el) => { focusRef.current.title = el; }}
            value={rep.title || ''} onChange={(e) => { rep.title = e.target.value; redraw(); }} />

          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1.2 }}>
            {DETAILS.map(([k, label]) => (
              <TextField key={k} size="small" label={label} disabled={committed}
                sx={{ ...field, flex: '1 1 200px' }}
                inputRef={(el) => { focusRef.current[k] = el; }}
                value={rep[k] || ''} onChange={(e) => { rep[k] = e.target.value || null; redraw(); }} />
            ))}
            <TextField size="small" select label="Pipeline stage" disabled={committed}
              sx={{ ...field, flex: '1 1 200px' }}
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
              sx={{ ...pillGhost, py: 0.7, fontSize: 13.5 }}>Auto-fill from transcript</Button>
          </Box>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, mb: 1 }}>
            {((caps?.report_templates || []) as any[]).map((t) => {
              const on = activeTpl.includes(t.id);
              return (
                <Chip key={t.id} size="small" clickable={!committed}
                  label={on ? t.label : `+ ${t.label}`}
                  onDelete={on && !committed ? () => void toggleTemplate(t) : undefined}
                  onClick={() => { if (!committed) void toggleTemplate(t); }}
                  sx={{ ...chipSx(on, true),
                    '& .MuiChip-deleteIcon': { color: vx.onGrn, fontSize: 17, ml: 0.5 } }} />
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
              {f.required && <Typography sx={{ fontSize: 15, color: vx.amberInk }} title="Required">*</Typography>}
            </Box>
          ))}
          {!tplFields.length && (
            <Typography sx={{ fontSize: 11.5, color: vx.mut }}>
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
            }} sx={pillGhost}>Add field</Button>

          <Typography sx={{ ...lbl, mt: 1.6, mb: 0.6 }}>Opportunity score (1–5)</Typography>
          <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
            {[1, 2, 3, 4, 5].map((n) => (
              <Box key={n} component="button" type="button" disabled={committed}
                ref={n === 1 ? (el: any) => { focusRef.current.opportunity_score = el; } : undefined}
                onClick={() => { rep.opportunity_score = rep.opportunity_score === n ? null : n; redraw(); }}
                aria-pressed={rep.opportunity_score === n}
                sx={{ width: 52, height: 52, borderRadius: '11px', cursor: 'pointer',
                  fontSize: 18, fontWeight: rep.opportunity_score === n ? 700 : 400,
                  border: `1px solid ${rep.opportunity_score === n ? vx.grn : '#2C5A44'}`,
                  bgcolor: rep.opportunity_score === n ? vx.grn : '#173A2C',
                  color: rep.opportunity_score === n ? vx.onGrn : vx.ink }}>
                {n}
              </Box>
            ))}
          </Box>

          <Typography sx={microHeading}>Attendees</Typography>
          {listOf('attendees').map((a: any, i: number) => (
            <Box key={i} sx={{ display: 'flex', gap: 0.5, mb: 0.5, flexWrap: 'wrap' }}>
              {(['name', 'role', 'company'] as const).map((k) => (
                <TextField key={k} size="small" placeholder={k} sx={{ ...field, flex: '1 1 108px' }}
                  disabled={committed}
                  inputRef={i === 0 && k === 'name' ? (el) => { focusRef.current.attendees = el; } : undefined}
                  value={a[k] || ''} onChange={(e) => { a[k] = e.target.value || null; redraw(); }} />
              ))}
              <IconButton size="small" disabled={committed} aria-label="Remove attendee"
                onClick={() => { listOf('attendees').splice(i, 1); redraw(); }}>
                <CloseIcon sx={{ fontSize: 15, color: vx.mut }} />
              </IconButton>
            </Box>
          ))}
          {!listOf('attendees').length && <Empty />}
          <Button size="small" startIcon={<AddIcon sx={{ fontSize: 15 }} />} disabled={committed}
            onClick={() => { listOf('attendees').push({ name: '', role: null, company: null }); redraw(); }}
            sx={pillGhost}>Add attendee</Button>
        </AccordionDetails>
      </Accordion>

      <ApproveDialog
        open={askApprove} state={state} busy={!!busy}
        onFill={jumpTo} onFile={doApprove} onClose={() => setAskApprove(false)} />
    </Box>
  );
}

// --------------------------------------------------------------------------------- //

const acc = {
  ...card,
  p: 0,
  borderRadius: '16px !important',
  color: vx.ink,
  '&:before': { display: 'none' },
  '& .MuiAccordionSummary-root': { px: 2, minHeight: 58 },
  '& .MuiAccordionDetails-root': { px: 2, pb: 2 },
};

const Empty = () => (
  <Typography sx={{ fontSize: 15, color: vx.mut, py: 0.5 }}>None yet.</Typography>
);

/** A simple string list — key intel, nuances. */
function BulletList({ title, rows, disabled, addLabel, onChange, anchor }: {
  title: string; rows: string[]; disabled: boolean; addLabel: string;
  onChange: () => void; anchor: (el: HTMLElement | null) => void;
}) {
  return (
    <>
      {title && <Typography sx={heading}>{title}</Typography>}
      {rows.map((v, i) => (
        <Box key={i} sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.8 }}>
          <Box sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: vx.grn, flexShrink: 0 }} />
          <TextField size="small" fullWidth sx={field} disabled={disabled}
            inputRef={i === 0 ? anchor : undefined}
            value={v} onChange={(e) => { rows[i] = e.target.value; onChange(); }} />
          <IconButton size="small" disabled={disabled} aria-label={`Remove from ${title}`}
            onClick={() => { rows.splice(i, 1); onChange(); }}>
            <CloseIcon sx={{ fontSize: 15, color: vx.mut }} />
          </IconButton>
        </Box>
      ))}
      {!rows.length && <Empty />}
      <Button size="small" startIcon={<AddIcon sx={{ fontSize: 15 }} />} disabled={disabled}
        onClick={() => { rows.push(''); onChange(); }} sx={pillGhost}>{addLabel}</Button>
    </>
  );
}
