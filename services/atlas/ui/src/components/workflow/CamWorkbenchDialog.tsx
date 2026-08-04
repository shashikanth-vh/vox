import { useEffect, useMemo, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, TextField, Alert, Chip, Checkbox, MenuItem, CircularProgress, Divider,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import { camService, type CamReport, type EntityDoc } from '../../services/camService';
import type { WorkflowAction } from '../../services/workflowActionsService';
import { useAuth } from '../../auth/AuthContext';
import { getSession } from '../../auth/session';
import { tokens } from '../../theme';

/**
 * The CAM workbench — the analyst drafts a Credit Assessment Memo from documents the
 * company's file actually holds, reworks it turn by turn, and finalises it into the
 * Data Register for the committee.
 *
 * The engine (Claude today — the plane records which) writes ONLY from the selected
 * documents plus the credit team's own PROMPT DOC; the workbench holds no judgement of
 * its own. Binary documents the plane cannot read are named as skipped, never silently
 * omitted — a CAM that pretends to cover a document it never saw is worse than one that
 * refuses.
 *
 * The committee's decision also lives here (and on Today's queue): a SUBMITTED version
 * shows Approve / Return / Reject to committee authority. The register enforces
 * four-eyes — the preparer cannot decide their own CAM.
 */

const COMMITTEE = ['Credit Head', 'Management', 'Admin'];

/** Sentinel select value: the analyst types the drafting brief instead of picking a doc. */
const TYPED = '__typed__';

const TONE: Record<string, 'default' | 'success' | 'warning' | 'error' | 'info'> = {
  Draft: 'info', Submitted: 'warning', Approved: 'success',
  Returned: 'warning', Rejected: 'error',
};

export default function CamWorkbenchDialog({ action, subjectId, entityId, onClose, onDone }: {
  action: WorkflowAction | null;
  subjectId: string;
  entityId?: string;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const { user } = useAuth();
  const open = !!action;
  const committee = (user?.roles || []).some((r: string) => COMMITTEE.includes(r));

  const [reports, setReports] = useState<CamReport[]>([]);
  const [docs, setDocs] = useState<EntityDoc[]>([]);
  const [defaults, setDefaults] = useState<{ prompt?: { id: string; title: string };
    example?: { id: string; title: string } }>({});
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [promptDoc, setPromptDoc] = useState('');
  const [typedBrief, setTypedBrief] = useState('');
  const [uploading, setUploading] = useState(false);
  // "Show me what the engine will read" — the extracted text of any pickable document,
  // in place. Copyable, so the analyst can also work with it outside the workbench.
  const [preview, setPreview] = useState<{ title: string; text: string; note?: string } | null>(null);

  const viewDoc = async (id: string, docTitle: string) => {
    setErr('');
    try {
      const out = await camService.docText(id);
      setPreview({
        title: docTitle,
        text: out.text || '',
        note: out.reason
          ? out.reason + (out.attachable ? ' — it will be attached for the engine to read visually.' : '')
          : out.truncated ? 'Truncated to the per-document limit.' : undefined,
      });
    } catch (e: any) { setErr(e?.message || String(e)); }
  };
  const [instruction, setInstruction] = useState('');
  const [editing, setEditing] = useState(false);
  const [draftText, setDraftText] = useState('');
  const [title, setTitle] = useState('');
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const [info, setInfo] = useState('');
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [r, d, tp, te] = await Promise.all([
        camService.list(subjectId),
        entityId ? camService.entityDocs(entityId) : Promise.resolve([]),
        camService.template('cam_prompt'),
        camService.template('cam_example'),
      ]);
      setReports(r); setDocs(d);
      setDefaults({ prompt: tp || undefined, example: te || undefined });
      // The deployment's default prompt is the working assumption — the analyst can
      // still pick a case-specific upload, but "no prompt chosen" should not be the
      // resting state when the credit team shipped one.
      setPromptDoc((p) => p || tp?.id || '');
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLoading(false);
  };

  const uploadPrompt = async (file: File | null) => {
    if (!file) return;
    setErr(''); setUploading(true);
    try {
      const doc = await camService.uploadDoc(subjectId, file, 'CAM Prompt');
      setInfo(`Prompt "${file.name}" filed on this line.`);
      await load();
      setPromptDoc(String(doc.id));
    } catch (e: any) { setErr(e?.message || String(e)); }
    setUploading(false);
  };

  useEffect(() => {
    if (!open) return;
    setSel(new Set()); setPromptDoc(''); setInstruction(''); setTitle('');
    setNote(''); setErr(''); setInfo(''); setBusy('');
    void load();
  }, [open, subjectId]);  // eslint-disable-line react-hooks/exhaustive-deps

  // The line's LIVE version — the one the workbench writes into or the committee decides.
  const live = useMemo(
    () => [...reports].reverse().find((r) => ['Draft', 'Returned', 'Submitted'].includes(r.status)),
    [reports]);
  const working = live && (live.status === 'Draft' || live.status === 'Returned') ? live : undefined;
  const submitted = live && live.status === 'Submitted' ? live : undefined;

  // Everything pickable, in BOTH states: the company's file plus the deployment's
  // example CAM (a format reference the engine may be shown).
  const sources: EntityDoc[] = [
    ...docs,
    ...(defaults.example ? [{ id: defaults.example.id, title: defaults.example.title,
      section: 'Template', doc_type: 'cam_example', content_type: '', status: '' }] : []),
  ];

  const run = async (what: string, fn: () => Promise<string>) => {
    setErr(''); setInfo(''); setBusy(what);
    try {
      const message = await fn();
      setInfo(message);
      await load();
    } catch (e: any) { setErr(e?.message || String(e)); }
    setBusy('');
  };

  // Summary-first: the engine digests every selected document (facts, figures, gaps)
  // as draft v1 — the analyst then applies the master prompt, asks, and hand-fills.
  const SUMMARY_BRIEF =
    'Summarise each of the supplied documents for a credit analyst: key facts, all '
    + 'figures with periods and units, parties, and NAME every gap (what a CAM would '
    + 'need that these documents do not contain). Do not draft the CAM yet.';

  const generate = (promptOverride?: string) => run('generate', async () => {
    const out = await camService.generate(subjectId, {
      source_doc_ids: [...sel],
      ...(promptOverride ? { prompt_text: promptOverride }
        : promptDoc === TYPED ? { prompt_text: typedBrief.trim() }
          : { prompt_doc_id: promptDoc }),
      ...(action?.body?.deal_id ? { deal_id: action.body.deal_id } : {}),
    });
    const skipped = (out.skipped || []).length;
    return `Draft v? ready (engine ${out.engine}).`
      + (skipped ? ` ${skipped} document(s) skipped — see the transcript.` : '');
  });

  // The ASK lane's latest answer — shown beside the draft, never written into it.
  const [answer, setAnswer] = useState('');

  // Ticked documents ride along with the NEXT Rework/Ask, then untick themselves.
  const refine = () => run('refine', async () => {
    await camService.refine(subjectId, instruction.trim(), true, [...sel]);
    setInstruction(''); setSel(new Set());
    return 'Draft reworked.';
  });

  const ask = (text?: string) => run('ask', async () => {
    const out = await camService.refine(subjectId, (text || instruction).trim(), false, [...sel]);
    setAnswer(out.draft_md || '');
    if (!text) setInstruction('');
    setSel(new Set());
    const skipped = (out.documents || []).filter((d: any) => !d.included);
    return 'Answer below — your draft is untouched; copy in what is useful.'
      + (skipped.length ? ` ${skipped.length} document(s) could not be read.` : '');
  });

  const loadExample = () => run('example', async () => {
    if (!working || !defaults.example) throw new Error('No example template on record.');
    const tpl = await camService.docText(defaults.example.id);
    if (!tpl.text?.trim()) throw new Error(tpl.reason || 'The example has no readable text.');
    await camService.saveDraft(working.id, tpl.text);
    return 'The example CAM is now your working draft — edit it and fill in the facts.';
  });

  const saveEdit = () => run('save', async () => {
    if (!working) throw new Error('No open draft to save.');
    await camService.saveDraft(working.id, draftText);
    setEditing(false);
    return 'Your edits are saved — this is now the current draft.';
  });

  const finalise = () => run('finalise', async () => {
    const out = await camService.finalise(subjectId, title.trim() || undefined);
    onDone(`CAM filed to the Data Register (document ${out.document_id}) and submitted to the committee.`);
    return 'Submitted to the committee.';
  });

  const decide = (decision: 'Approved' | 'Returned' | 'Rejected') => run(decision, async () => {
    if (decision !== 'Approved' && !note.trim()) {
      throw new Error(`A ${decision.toLowerCase()} must say why — the note reaches the analyst.`);
    }
    if (!submitted) throw new Error('No submitted CAM to decide.');
    await camService.decide(submitted.id, decision, user?.full || '', note.trim());
    onDone(`CAM v${submitted.report_version} ${decision.toLowerCase()}.`);
    return `CAM ${decision.toLowerCase()}.`;
  });

  const toggle = (id: string) => setSel((p) => {
    const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n;
  });

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>
        CAM workbench
        {loading && <CircularProgress size={13} sx={{ ml: 1, verticalAlign: 'middle' }} />}
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 6 }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setErr('')}>{err}</Alert>}
        {info && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setInfo('')}>{info}</Alert>}

        {reports.length > 0 && (
          <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap', mb: 1.2 }}>
            {reports.map((r) => (
              <Chip key={r.id} size="small" color={TONE[r.status] || 'default'}
                variant={live?.id === r.id ? 'filled' : 'outlined'}
                label={`v${r.report_version} · ${r.status}${r.engine ? ` · ${r.engine}` : ''}`} />
            ))}
          </Box>
        )}

        {/* ---- committee: a SUBMITTED version awaits the decision --------------------
            Four-eyes: the register refuses a decision by whoever prepared the version,
            so the PREPARER never sees decision buttons — a button that can only bounce
            teaches nothing. They see who decides instead. */}
        {submitted && (() => {
          const me = (getSession()?.email || '').trim().toLowerCase();
          const preparer = (submitted.prepared_by || '').trim().toLowerCase();
          const mayDecide = committee && (!me || me !== preparer);
          return (
          <Box sx={{ mb: 1.5 }}>
            <Typography sx={{ fontSize: 12.5, color: tokens.muted, mb: 0.6 }}>
              v{submitted.report_version} was submitted by <b>{submitted.prepared_by}</b> and
              awaits the committee. {mayDecide
                ? 'You hold committee authority — decide below.'
                : me && me === preparer
                  ? 'You prepared this version, so a DIFFERENT committee member decides it '
                    + '(four-eyes) — it is on Today for Credit Head / Management.'
                  : 'It appears on Today for Credit Head / Management.'}
            </Typography>
            {!!submitted.draft_md && (
              <Box component="pre" sx={{
                whiteSpace: 'pre-wrap', fontSize: 12, fontFamily: 'inherit', p: 1.2,
                border: `1px solid ${tokens.line}`, borderRadius: 1, maxHeight: 320, overflow: 'auto',
              }}>{submitted.draft_md}</Box>
            )}
            {mayDecide && (
              <>
                <TextField fullWidth size="small" multiline minRows={2} sx={{ mt: 1 }}
                  label="Committee note (required to return or reject)"
                  value={note} onChange={(e) => setNote(e.target.value)} />
                <Box sx={{ display: 'flex', gap: 1, mt: 1 }}>
                  <Button size="small" variant="contained" disabled={!!busy}
                    onClick={() => void decide('Approved')}>
                    {busy === 'Approved' ? 'Approving…' : 'Approve'}
                  </Button>
                  <Button size="small" variant="outlined" disabled={!!busy}
                    onClick={() => void decide('Returned')}>Return for rework</Button>
                  <Button size="small" color="error" variant="outlined" disabled={!!busy}
                    onClick={() => void decide('Rejected')}>Reject</Button>
                </Box>
              </>
            )}
          </Box>
          );
        })()}

        {/* ---- analyst: an open draft to rework and finalise ------------------------- */}
        {working && (
          <Box>
            <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.6 }}>
              <Typography sx={{ fontSize: 12.5, color: tokens.muted, flex: 1 }}>
                v{working.report_version} · {working.status === 'Returned'
                  ? <>returned by the committee{working.decision_note ? <> — “{working.decision_note}”</> : null}; rework it below</>
                  : 'draft in progress'} · engine {working.engine || '—'}
              </Typography>
              {!editing && defaults.example && (
                <Button size="small" disabled={!!busy} onClick={() => void loadExample()}
                  title="Overwrites the current draft with the example CAM's text — then edit and fill it in"
                  sx={{ textTransform: 'none', fontSize: 12 }}>
                  {busy === 'example' ? 'Loading…' : 'Start from the example'}
                </Button>
              )}
              {!editing && !!working.draft_md && (
                <Button size="small" disabled={!!busy}
                  onClick={() => { setDraftText(working.draft_md || ''); setEditing(true); }}
                  sx={{ textTransform: 'none', fontSize: 12 }}>Edit the text</Button>
              )}
            </Box>
            {editing ? (
              <>
                <TextField fullWidth multiline minRows={10} maxRows={18} value={draftText}
                  onChange={(e) => setDraftText(e.target.value)}
                  sx={{ '& textarea': { fontSize: 12.5 } }} />
                <Box sx={{ display: 'flex', gap: 1, mt: 0.8 }}>
                  <Button size="small" variant="contained" disabled={!!busy || !draftText.trim()}
                    onClick={() => void saveEdit()}>
                    {busy === 'save' ? 'Saving…' : 'Save edits'}
                  </Button>
                  <Button size="small" variant="outlined" disabled={!!busy}
                    onClick={() => setEditing(false)}>Cancel</Button>
                </Box>
              </>
            ) : (
              <Box component="pre" sx={{
                whiteSpace: 'pre-wrap', fontSize: 12, fontFamily: 'inherit', p: 1.2,
                border: `1px solid ${tokens.line}`, borderRadius: 1, maxHeight: 300, overflow: 'auto',
              }}>{working.draft_md || 'No draft text yet — generate below.'}</Box>
            )}
            {/* One screen for everything: the documents stay pickable WHILE drafting —
                tick, view/copy, or summarise; ticked ones ride with the next turn. */}
            {sources.length > 0 && (
              <Box sx={{ mt: 1, border: `1px solid ${tokens.line}`, borderRadius: 1 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', px: 1, py: 0.4 }}>
                  <Typography sx={{ fontSize: 11.5, color: tokens.muted, flex: 1 }}>
                    Documents — tick to send with your next Rework/Ask · view to read/copy
                  </Typography>
                  <Button size="small" disabled={!sel.size || !!busy}
                    onClick={() => void ask(SUMMARY_BRIEF)}
                    sx={{ textTransform: 'none', fontSize: 11.5, minWidth: 0 }}>
                    {busy === 'ask' ? 'Summarising…' : `Summarise selected (${sel.size})`}
                  </Button>
                </Box>
                <Box sx={{ maxHeight: 130, overflow: 'auto' }}>
                  {sources.map((d) => (
                    <Box key={d.id} sx={{ display: 'flex', alignItems: 'center', px: 0.6,
                      borderTop: `1px solid ${tokens.line}` }}>
                      <Checkbox size="small" checked={sel.has(d.id)} onChange={() => toggle(d.id)} />
                      <Typography sx={{ fontSize: 12, flex: 1 }}>{d.title}</Typography>
                      <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>
                        {[d.section, d.doc_type].filter(Boolean).join(' · ')}
                      </Typography>
                      <Button size="small" onClick={() => void viewDoc(d.id, d.title)}
                        sx={{ textTransform: 'none', fontSize: 11, minWidth: 0, ml: 0.5 }}>view</Button>
                    </Box>
                  ))}
                </Box>
              </Box>
            )}
            {preview && (
              <Box sx={{ mt: 1, border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.5 }}>
                  <Typography sx={{ fontSize: 12, fontWeight: 600, flex: 1 }}>
                    What the engine reads — {preview.title}
                  </Typography>
                  <Button size="small" sx={{ textTransform: 'none', fontSize: 11, minWidth: 0 }}
                    onClick={() => void navigator.clipboard?.writeText(preview.text)}>Copy</Button>
                  <Button size="small" sx={{ textTransform: 'none', fontSize: 11, minWidth: 0 }}
                    onClick={() => setPreview(null)}>Close</Button>
                </Box>
                {preview.note && (
                  <Typography sx={{ fontSize: 11.5, color: tokens.muted, mb: 0.5 }}>{preview.note}</Typography>
                )}
                {preview.text && (
                  <Box component="pre" sx={{ whiteSpace: 'pre-wrap', fontSize: 11.5,
                    fontFamily: 'inherit', maxHeight: 180, overflow: 'auto', m: 0 }}>
                    {preview.text}
                  </Box>
                )}
              </Box>
            )}
            <Box sx={{ display: 'flex', gap: 1, mt: 1, alignItems: 'flex-start' }}>
              <TextField fullWidth size="small" multiline minRows={2}
                label="Instruction / question — paste anything (doc extracts, figures, prompts)"
                value={instruction} onChange={(e) => setInstruction(e.target.value)} />
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.6 }}>
                <Button variant="contained" size="small" startIcon={<AutoAwesomeIcon sx={{ fontSize: 15 }} />}
                  disabled={!instruction.trim() || !!busy} onClick={() => void refine()}
                  sx={{ whiteSpace: 'nowrap' }}>
                  {busy === 'refine' ? 'Reworking…' : 'Rework draft'}
                </Button>
                <Button variant="outlined" size="small"
                  disabled={!instruction.trim() || !!busy} onClick={() => void ask()}
                  title="The answer comes back below — your draft stays untouched"
                  sx={{ whiteSpace: 'nowrap' }}>
                  {busy === 'ask' ? 'Asking…' : 'Ask only'}
                </Button>
              </Box>
            </Box>
            {defaults.prompt && (
              <Button size="small" disabled={!!busy} sx={{ textTransform: 'none', fontSize: 11.5, mt: 0.4 }}
                title="Loads the EVAM master prompt into the box — then Rework draft applies it to everything the engine has seen"
                onClick={() => void camService.docText(defaults.prompt!.id)
                  .then((t) => setInstruction(t.text || ''))
                  .catch((e: any) => setErr(e?.message || String(e)))}>
                Insert the master prompt into the box
              </Button>
            )}
            {answer && (
              <Box sx={{ mt: 1, border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.4 }}>
                  <Typography sx={{ fontSize: 12, fontWeight: 600, flex: 1 }}>
                    Engine answer — copy what is useful into your draft
                  </Typography>
                  <Button size="small" sx={{ textTransform: 'none', fontSize: 11, minWidth: 0 }}
                    onClick={() => void navigator.clipboard?.writeText(answer)}>Copy</Button>
                  <Button size="small" sx={{ textTransform: 'none', fontSize: 11, minWidth: 0 }}
                    onClick={() => setAnswer('')}>Close</Button>
                </Box>
                <Box component="pre" sx={{ whiteSpace: 'pre-wrap', fontSize: 11.5,
                  fontFamily: 'inherit', maxHeight: 220, overflow: 'auto', m: 0 }}>{answer}</Box>
              </Box>
            )}
            <Divider sx={{ my: 1.4 }} />
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center' }}>
              <TextField size="small" sx={{ flex: 1 }} label="Filed title"
                placeholder={`CAM v${working.report_version}`}
                value={title} onChange={(e) => setTitle(e.target.value)} />
              <Button variant="contained" size="small" disabled={!working.draft_md || !!busy}
                onClick={() => void finalise()}>
                {busy === 'finalise' ? 'Filing…' : 'File & submit to committee'}
              </Button>
            </Box>
          </Box>
        )}

        {/* ---- no live version: pick documents and generate -------------------------- */}
        {!live && !loading && (() => {
          return (
            <Box>
              <Typography sx={{ fontSize: 12.5, color: tokens.muted, mb: 0.8 }}>
                Pick the source documents and the credit team's <b>prompt document</b> — the
                engine drafts only from what you select, and says which documents it could
                not read. PDF and Word documents are read as text; a <b>scanned</b> PDF is
                handed to the engine to read visually.
              </Typography>
              {!docs.length && (
                <Alert severity="info" sx={{ py: 0, fontSize: 12, mb: 1 }}>
                  Nothing on the company's file yet — upload the source documents in the
                  Data Register first.
                </Alert>
              )}
              {sources.length > 0 && (
                <Box sx={{ maxHeight: 220, overflow: 'auto', border: `1px solid ${tokens.line}`,
                  borderRadius: 1, mb: 1 }}>
                  {sources.map((d) => (
                    <Box key={d.id} sx={{ display: 'flex', alignItems: 'center', px: 0.6,
                      borderBottom: `1px solid ${tokens.line}` }}>
                      <Checkbox size="small" checked={sel.has(d.id)} onChange={() => toggle(d.id)} />
                      <Typography sx={{ fontSize: 12.5, flex: 1 }}>{d.title}</Typography>
                      <Typography sx={{ fontSize: 11, color: tokens.muted }}>
                        {[d.section, d.doc_type].filter(Boolean).join(' · ')}
                      </Typography>
                      <Button size="small" onClick={() => void viewDoc(d.id, d.title)}
                        sx={{ textTransform: 'none', fontSize: 11, minWidth: 0, ml: 0.5 }}>view</Button>
                    </Box>
                  ))}
                </Box>
              )}
              <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mb: 1 }}>
                <TextField select fullWidth size="small" label="Prompt document (the drafting brief)"
                  value={promptDoc} onChange={(e) => setPromptDoc(e.target.value)}>
                  {defaults.prompt && (
                    <MenuItem value={defaults.prompt.id} sx={{ fontSize: 13 }}>
                      Default — {defaults.prompt.title}
                    </MenuItem>
                  )}
                  {docs.map((d) => (
                    <MenuItem key={d.id} value={d.id} sx={{ fontSize: 13 }}>{d.title}</MenuItem>
                  ))}
                  <MenuItem value={TYPED} sx={{ fontSize: 13 }}>Type the brief by hand…</MenuItem>
                </TextField>
                <Button component="label" variant="outlined" size="small" disabled={uploading}
                  sx={{ whiteSpace: 'nowrap', textTransform: 'none', flexShrink: 0 }}>
                  {uploading ? 'Uploading…' : 'Upload prompt…'}
                  <input hidden type="file" accept=".docx,.pdf,.md,.txt,.csv"
                    onChange={(e) => { void uploadPrompt(e.target.files?.[0] || null); e.target.value = ''; }} />
                </Button>
              </Box>
              {promptDoc === TYPED && (
                <TextField fullWidth multiline minRows={4} sx={{ mb: 1 }}
                  label="Drafting brief — what should the CAM cover, in your words"
                  placeholder={'Draft a CAM for this term loan. Cover promoter background, business model, financial analysis with DSCR, security, and risks with mitigants…'}
                  value={typedBrief} onChange={(e) => setTypedBrief(e.target.value)} />
              )}
              {promptDoc && promptDoc !== TYPED && (
                <Button size="small" onClick={() => void viewDoc(promptDoc,
                  defaults.prompt?.id === promptDoc ? defaults.prompt.title : 'Prompt document')}
                  sx={{ textTransform: 'none', fontSize: 11.5, mb: 1 }}>
                  View the prompt text
                </Button>
              )}
              {preview && (
                <Box sx={{ mb: 1, border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.5 }}>
                    <Typography sx={{ fontSize: 12, fontWeight: 600, flex: 1 }}>
                      What the engine reads — {preview.title}
                    </Typography>
                    <Button size="small" sx={{ textTransform: 'none', fontSize: 11, minWidth: 0 }}
                      onClick={() => void navigator.clipboard?.writeText(preview.text)}>Copy</Button>
                    <Button size="small" sx={{ textTransform: 'none', fontSize: 11, minWidth: 0 }}
                      onClick={() => setPreview(null)}>Close</Button>
                  </Box>
                  {preview.note && (
                    <Typography sx={{ fontSize: 11.5, color: tokens.muted, mb: 0.5 }}>{preview.note}</Typography>
                  )}
                  {preview.text && (
                    <Box component="pre" sx={{ whiteSpace: 'pre-wrap', fontSize: 11.5,
                      fontFamily: 'inherit', maxHeight: 200, overflow: 'auto', m: 0 }}>
                      {preview.text}
                    </Box>
                  )}
                </Box>
              )}
              <Box sx={{ display: 'flex', gap: 1 }}>
                <Button variant="contained" size="small"
                  startIcon={<AutoAwesomeIcon sx={{ fontSize: 15 }} />}
                  disabled={!sel.size || !promptDoc || (promptDoc === TYPED && !typedBrief.trim()) || !!busy}
                  onClick={() => void generate()}>
                  {busy === 'generate' ? 'Drafting…' : `Draft the CAM from ${sel.size || 'the'} document(s)`}
                </Button>
                <Button variant="outlined" size="small" disabled={!sel.size || !!busy}
                  title="The engine digests the documents first — facts, figures, gaps — then you apply the master prompt, ask questions, and fill the CAM"
                  onClick={() => void generate(SUMMARY_BRIEF)}>
                  Summarise the documents first
                </Button>
              </Box>
            </Box>
          );
        })()}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={!!busy}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
