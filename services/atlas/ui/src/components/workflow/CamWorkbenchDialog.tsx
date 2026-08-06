import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, TextField, Alert, Chip, Checkbox, CircularProgress, Divider,
  FormControlLabel,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import SendIcon from '@mui/icons-material/Send';
import { camService, type CamReport, type EntityDoc } from '../../services/camService';
import { documentsService } from '../../services/documentsService';
import type { WorkflowAction } from '../../services/workflowActionsService';
import { useAuth } from '../../auth/AuthContext';
import { getSession } from '../../auth/session';
import { tokens } from '../../theme';

/**
 * The CAM workbench — the analyst drafts a Credit Assessment Memo from documents the
 * company's file actually holds, reworks it turn by turn, and finalises it into the
 * Data Register for the committee.
 *
 * The surface is a CONVERSATION (field feedback: "like Claude's"): questions go in at
 * the bottom, answers stack above, and the whole exchange — plus the chosen prompt and
 * the ticked documents — becomes the CAM when the analyst clicks Generate CAM. The
 * transcript is durable: the register records every turn, so reopening the workbench
 * reopens the conversation, and "why does the CAM say this?" always has an answer.
 *
 * The prompt is a choice, not a setting: the credit team's default (versioned on the
 * template shelf) or a case-specific upload filed on this line. Exactly one — or none —
 * rides with every question and with Generate.
 *
 * The committee's decision also lives here (and on Today's queue): a SUBMITTED version
 * shows Approve / Return / Reject to committee authority. The register enforces
 * four-eyes — the preparer cannot decide their own CAM.
 */

const COMMITTEE = ['Credit Head', 'Management', 'Admin'];

const TONE: Record<string, 'default' | 'success' | 'warning' | 'error' | 'info'> = {
  Draft: 'info', Submitted: 'warning', Approved: 'success',
  Returned: 'warning', Rejected: 'error',
};

type ChatMsg = { role: 'user' | 'assistant'; text: string };

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
  // The prompt choice: the credit team's default, a case-specific custom upload, or none.
  const [promptUse, setPromptUse] = useState<'default' | 'custom' | 'none'>('default');
  const [customPrompt, setCustomPrompt] = useState<{ id: string; title: string } | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [docsOpen, setDocsOpen] = useState(false);
  // "Show me what the engine will read" — the extracted text of any pickable document.
  const [preview, setPreview] = useState<{ title: string; text: string; note?: string } | null>(null);

  // The conversation: answers above, the question box below — Claude-style.
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [question, setQuestion] = useState('');
  const chatEndRef = useRef<HTMLDivElement | null>(null);
  const seededFor = useRef('');

  const [title, setTitle] = useState('');
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const [info, setInfo] = useState('');
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(false);

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

  const load = async () => {
    setLoading(true);
    try {
      const [r, d, tps, te, ld] = await Promise.all([
        camService.list(subjectId),
        entityId ? camService.entityDocs(entityId) : Promise.resolve([]),
        camService.templates('cam_prompt'),
        camService.template('cam_example'),
        camService.lendingDocs(subjectId).catch(() => [] as EntityDoc[]),
      ]);
      setReports(r); setDocs(d);
      setDefaults({ prompt: tps[0] || undefined, example: te || undefined });
      // A custom prompt filed on THIS line earlier — newest wins, same as the shelf.
      const cps = ld.filter((x) => x.doc_type === 'CAM Prompt');
      if (cps.length) {
        setCustomPrompt((prev) => prev || { id: cps[cps.length - 1].id, title: cps[cps.length - 1].title });
      }
      if (!tps.length) setPromptUse((p) => (p === 'default' ? 'none' : p));
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLoading(false);
  };

  useEffect(() => {
    if (!open) return;
    setSel(new Set()); setChat([]); setQuestion(''); setTitle('');
    setNote(''); setErr(''); setInfo(''); setBusy('');
    setPromptUse('default'); setCustomPrompt(null); seededFor.current = '';
    void load();
  }, [open, subjectId]);  // eslint-disable-line react-hooks/exhaustive-deps

  // The line's LIVE version — the one the workbench writes into or the committee decides.
  const live = useMemo(
    () => [...reports].reverse().find((r) => ['Draft', 'Returned', 'Submitted'].includes(r.status)),
    [reports]);
  const working = live && (live.status === 'Draft' || live.status === 'Returned') ? live : undefined;
  const submitted = live && live.status === 'Submitted' ? live : undefined;

  // Reopening the workbench reopens the CONVERSATION — the register kept every turn.
  useEffect(() => {
    const id = working?.id;
    if (!open || !id || seededFor.current === id) return;
    seededFor.current = id;
    void camService.get(id).then((full) => {
      const msgs: ChatMsg[] = (full.turns || [])
        .filter((t) => !String(t.content || '').startsWith('[manual edit]'))
        .map((t) => ({ role: t.role, text: String(t.content || '') }));
      // Never clobber questions already asked this session.
      if (msgs.length) setChat((c) => (c.length ? c : msgs));
    }).catch(() => {});
  }, [open, working?.id]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [chat, busy]);

  const sources: EntityDoc[] = docs;
  const activePromptId = promptUse === 'default' ? defaults.prompt?.id
    : promptUse === 'custom' ? customPrompt?.id : undefined;

  const run = async (what: string, fn: () => Promise<string>) => {
    setErr(''); setInfo(''); setBusy(what);
    try {
      const message = await fn();
      if (message) setInfo(message);
      await load();
    } catch (e: any) { setErr(e?.message || String(e)); }
    setBusy('');
  };

  const SUMMARY_BRIEF =
    'Summarise each of the supplied documents for a credit analyst: key facts, all '
    + 'figures with periods and units, parties, and NAME every gap (what a CAM would '
    + 'need that these documents do not contain). Do not draft the CAM yet.';

  // One question down, one answer up. The register rebuilds the whole conversation
  // server-side on every turn (engines are stateless), so each Send carries only the
  // new question — plus the chosen prompt and the ticked documents, which untick after.
  const send = (text?: string, shownAs?: string) => {
    const q = (text ?? question).trim();
    if (!q || busy) return;
    setChat((c) => [...c, { role: 'user', text: shownAs || q }]);
    setQuestion('');
    void run('ask', async () => {
      const ride = [...(activePromptId ? [activePromptId] : []), ...sel];
      const out = await camService.refine(subjectId, q, false, ride);
      setChat((c) => [...c, { role: 'assistant', text: out.draft_md || '' }]);
      setSel(new Set());
      const skipped = (out.documents || []).filter((d: any) => !d.included && d.reason);
      return skipped.length ? `${skipped.length} document(s) could not be read.` : '';
    });
  };

  // The point of the conversation: everything established above + the prompt + the
  // ticked documents becomes the CAM draft. The reply lands in the chat AND as the
  // working draft on record — "Download as Word" below turns it into the file.
  const generateCam = () => {
    if (busy) return;
    // A typed-but-unsent question still counts — it rides inside the brief.
    const pending = question.trim();
    const brief =
      'Prepare the complete CAM report now. '
      + (activePromptId ? "Follow the prompt document's structure and instructions exactly. " : '')
      + 'Use everything established in this conversation and every document provided. '
      + 'Where information is missing, name the gap rather than inventing a figure. '
      + 'Output the full CAM in Markdown with headings and tables, ready to render to Word.'
      + (pending ? `\n\nAlso take this into account: ${pending}` : '');
    setQuestion('');
    setChat((c) => [...c, { role: 'user',
      text: 'Generate the CAM report from this conversation.'
        + (pending ? `\n(Also: ${pending})` : '') }]);
    void run('generate', async () => {
      const ride = [...(activePromptId ? [activePromptId] : []), ...sel];
      const out = await camService.refine(subjectId, brief, true, ride);
      setChat((c) => [...c, { role: 'assistant', text: out.draft_md || '' }]);
      setSel(new Set());
      return 'CAM draft ready — use "To Word" on the answer (or "Download as Word" below), review it in Word, then upload the completed CAM.';
    });
  };

  const toWord = (markdown: string) => run('to-word', async () => {
    await camService.exportDocx(subjectId, markdown,
      title.trim() || `CAM v${working?.report_version ?? 1}`);
    return 'Word file downloaded — review it in Word, then upload it below as the completed CAM.';
  });

  // ---- the Word lane: download the template, fill it OUTSIDE, upload the result ----
  const asEntry = (id: string, name: string) =>
    ({ id, name, size: 0, type: '', when: '', by: '', label: '' });
  const extFor = (ct: string) =>
    /wordprocessingml/.test(ct) ? '.docx' : /pdf/.test(ct) ? '.pdf'
      : /markdown/.test(ct) ? '.md' : /plain/.test(ct) ? '.txt' : '';

  const downloadTemplate = async () => {
    if (!defaults.example) return;
    setErr('');
    const out = await documentsService.download(
      asEntry(defaults.example.id, `${defaults.example.title}.docx`) as any);
    if (!out.ok) setErr(out.error || 'The template download failed.');
  };

  // Downloads whichever prompt is IN USE — the default, or the custom upload.
  const downloadPrompt = async () => {
    const p = promptUse === 'custom' && customPrompt ? customPrompt : defaults.prompt;
    if (!p) return;
    setErr('');
    const out = await documentsService.download(asEntry(p.id, `${p.title}.docx`) as any);
    if (!out.ok) setErr(out.error || 'The prompt download failed.');
  };

  // A CUSTOM prompt files on THIS lending line (any analyst may) — the tenant-wide
  // default on the template shelf stays credit-desk authority, managed below.
  const uploadCustomPrompt = async (file: File | null) => {
    if (!file) return;
    setErr(''); setTplBusy(true);
    try {
      const doc = await camService.uploadDoc(subjectId, file, 'CAM Prompt', 'CAM');
      setCustomPrompt({ id: String(doc.id), title: file.name.replace(/\.[^.]+$/, '') });
      setPromptUse('custom');
      setInfo(`Custom prompt "${file.name}" is on this line's file and now rides with every question.`);
    } catch (e: any) { setErr(e?.message || String(e)); }
    setTplBusy(false);
  };

  // The tenant DEFAULT is the credit team's own document — updating it is an upload
  // here (never a deploy); renaming keeps the picker honest.
  const [tplBusy, setTplBusy] = useState(false);
  const [renamingTitle, setRenamingTitle] = useState<string | null>(null);

  const uploadDefaultPrompt = async (file: File | null) => {
    if (!file) return;
    setErr(''); setTplBusy(true);
    try {
      await camService.uploadTemplate('cam_prompt', file);
      await load();
      setPromptUse('default');
    } catch (e: any) {
      setErr(e?.response?.data?.error?.detail || e?.message || String(e));
    }
    setTplBusy(false);
  };

  const renameDefaultPrompt = async () => {
    const t = (renamingTitle || '').trim();
    if (!t || !defaults.prompt) { setRenamingTitle(null); return; }
    setErr(''); setTplBusy(true);
    try {
      await camService.renameTemplate(defaults.prompt.id, t);
      await load();
      setRenamingTitle(null);
    } catch (e: any) {
      setErr(e?.response?.data?.error?.detail || e?.message || String(e));
    }
    setTplBusy(false);
  };

  // The completed CAM (a filled .docx, usually) is FILED on the line and attached to
  // the working version — nothing goes to any approver from here. The committee request
  // is the drawer's own "Send to credit committee" step, which carries this document.
  const uploadFinal = (file: File | null) => {
    if (!file) return;
    void run('upload-final', async () => {
      const doc = await camService.uploadDoc(subjectId, file, 'CAM', 'Sanction');
      await camService.finalise(subjectId,
        title.trim() || file.name.replace(/\.[^.]+$/, ''), String(doc.id));
      onDone(`Completed CAM "${file.name}" is on file — use "Send to credit committee" to raise the review.`);
      return 'Filed. Raise "Send to credit committee" when ready.';
    });
  };

  const downloadFiled = async (docId: string) => {
    setErr('');
    try {
      const all = await camService.lendingDocs(subjectId);
      const d = all.find((x) => x.id === docId);
      const out = await documentsService.download(
        asEntry(docId, `${d?.title || 'CAM'}${extFor(d?.content_type || '')}`) as any);
      if (!out.ok) setErr(out.error || 'The download failed.');
    } catch (e: any) { setErr(e?.message || String(e)); }
  };

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

  // Ticking one prompt kind unticks the other — exactly one (or none) rides.
  const pickPrompt = (kind: 'default' | 'custom') =>
    setPromptUse((p) => (p === kind ? 'none' : kind));

  const thinking = busy === 'ask' || busy === 'generate';

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
                label={`v${r.report_version} · ${r.status}`} />
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
            {!!submitted.document_id && (
              <Button size="small" variant="outlined" sx={{ textTransform: 'none', mb: 0.8 }}
                onClick={() => void downloadFiled(submitted.document_id!)}>
                Download the filed CAM document
              </Button>
            )}
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

        {/* ---- analyst: the conversation workbench ----------------------------------- */}
        {!submitted && !loading && (
          <Box>
            {/* Downloads — the Word template to fill, and the prompt in use. */}
            <Box sx={{ display: 'flex', gap: 1, mb: 1, flexWrap: 'wrap', alignItems: 'center' }}>
              {defaults.example && (
                <Button variant="outlined" size="small" sx={{ textTransform: 'none' }}
                  onClick={() => void downloadTemplate()}>Download CAM template</Button>
              )}
              {(defaults.prompt || customPrompt) && (
                <Button variant="outlined" size="small" sx={{ textTransform: 'none' }}
                  onClick={() => void downloadPrompt()}>Download CAM prompt</Button>
              )}
              <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                Ask below, then Generate CAM — or fill the template in Word yourself.
              </Typography>
            </Box>

            {working?.status === 'Returned' && (
              <Alert severity="warning" sx={{ py: 0, fontSize: 12, mb: 1 }}>
                Returned by the committee{working?.decision_note ? ` — “${working.decision_note}”` : ''}.
                Amend the CAM and upload it again.
              </Alert>
            )}

            {/* The prompt choice: default / custom / none. Exactly one rides. */}
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexWrap: 'wrap',
              border: `1px solid ${tokens.line}`, borderRadius: 1, px: 1, py: 0.3, mb: 1 }}>
              <FormControlLabel sx={{ mr: 1.5, '& .MuiTypography-root': { fontSize: 12.5 } }}
                control={<Checkbox size="small" checked={promptUse === 'default'}
                  disabled={!defaults.prompt} onChange={() => pickPrompt('default')} />}
                label={defaults.prompt
                  ? `Use default prompt — ${defaults.prompt.title}`
                  : 'Use default prompt (none on record)'} />
              <FormControlLabel sx={{ mr: 0.5, '& .MuiTypography-root': { fontSize: 12.5 } }}
                control={<Checkbox size="small" checked={promptUse === 'custom'}
                  disabled={!customPrompt} onChange={() => pickPrompt('custom')} />}
                label={customPrompt
                  ? `Use custom prompt — ${customPrompt.title}`
                  : 'Use custom prompt'} />
              <Button size="small" component="label" disabled={tplBusy || !!busy}
                sx={{ textTransform: 'none', fontSize: 11.5 }}
                title="File a case-specific prompt on this line — it rides instead of the default">
                {tplBusy ? 'Uploading…' : 'Upload custom prompt…'}
                <input hidden type="file" accept=".docx,.md,.txt,.pdf"
                  onChange={(e) => { void uploadCustomPrompt(e.target.files?.[0] || null); e.target.value = ''; }} />
              </Button>
              <Box sx={{ flex: 1 }} />
              {promptUse === 'none' && (
                <Typography sx={{ fontSize: 11, color: tokens.muted }}>
                  No prompt — questions go with the ticked documents only.
                </Typography>
              )}
              {/* The tenant-wide default stays credit-desk authority. */}
              {committee && renamingTitle === null && (
                <>
                  {defaults.prompt && (
                    <Button size="small" disabled={tplBusy} sx={{ textTransform: 'none', fontSize: 11 }}
                      onClick={() => setRenamingTitle(defaults.prompt?.title || '')}>
                      Rename default…
                    </Button>
                  )}
                  <Button size="small" component="label" disabled={tplBusy}
                    sx={{ textTransform: 'none', fontSize: 11 }}
                    title="File a new tenant-wide default prompt version — newest wins">
                    Update default…
                    <input hidden type="file" accept=".docx,.md,.txt,.pdf"
                      onChange={(e) => { void uploadDefaultPrompt(e.target.files?.[0] || null); e.target.value = ''; }} />
                  </Button>
                </>
              )}
              {committee && renamingTitle !== null && (
                <>
                  <TextField size="small" label="New name" value={renamingTitle}
                    onChange={(e) => setRenamingTitle(e.target.value)} sx={{ minWidth: 200 }} />
                  <Button size="small" variant="contained" disabled={tplBusy || !renamingTitle.trim()}
                    onClick={() => void renameDefaultPrompt()} sx={{ textTransform: 'none' }}>
                    Save name
                  </Button>
                  <Button size="small" disabled={tplBusy} sx={{ textTransform: 'none' }}
                    onClick={() => setRenamingTitle(null)}>Cancel</Button>
                </>
              )}
            </Box>

            {/* Documents — folded until wanted; ticked ones ride with the next Send /
                Summarise / Generate, then untick. */}
            {sources.length > 0 && (
              <Box sx={{ mb: 1, border: `1px solid ${tokens.line}`, borderRadius: 1 }}>
                <Box onClick={() => setDocsOpen((v) => !v)}
                  sx={{ display: 'flex', alignItems: 'center', px: 1, py: 0.6, cursor: 'pointer',
                    '&:hover': { bgcolor: '#FAFBFC' } }}>
                  <Typography sx={{ fontSize: 12.5, fontWeight: 600, flex: 1 }}>
                    {docsOpen ? '▾' : '▸'} Documents ({sources.length})
                    {sel.size > 0 && ` — ${sel.size} will ride with the next question / Generate`}
                  </Typography>
                  <Button size="small" variant="outlined" disabled={!sel.size || !!busy}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (!sel.size) { setDocsOpen(true); return; }
                      send(SUMMARY_BRIEF, `Summarise the ${sel.size} ticked document(s) — facts, figures, gaps.`);
                    }}
                    title="The engine digests the ticked documents — facts, figures, gaps — and the summary lands as an answer"
                    sx={{ textTransform: 'none', fontSize: 11.5, whiteSpace: 'nowrap' }}>
                    {busy === 'ask' ? 'Summarising…' : `Summarise selected (${sel.size})`}
                  </Button>
                </Box>
                {docsOpen && (
                  <Box sx={{ maxHeight: 150, overflow: 'auto' }}>
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
                )}
              </Box>
            )}
            {preview && (
              <Box sx={{ mb: 1, border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.5 }}>
                  <Typography sx={{ fontSize: 12, fontWeight: 600, flex: 1 }}>
                    {preview.title}
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

            {/* The conversation: answers above, the question box below. */}
            <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 1.5 }}>
              <Box sx={{ maxHeight: 340, minHeight: 120, overflowY: 'auto', p: 1.2 }}>
                {!chat.length && !thinking && (
                  <Typography sx={{ fontSize: 12, color: tokens.muted, textAlign: 'center', py: 3 }}>
                    Ask about the documents, the figures, the risks — answers stack up here.
                    Tick documents above to send them with a question. When the picture is
                    complete, <b>Generate CAM</b> turns the whole conversation into the draft.
                  </Typography>
                )}
                {chat.map((m, i) => (
                  <Box key={i} sx={{ display: 'flex', mb: 0.8,
                    justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start' }}>
                    <Box sx={{ maxWidth: '92%', px: 1.2, py: 0.7, borderRadius: 2,
                      bgcolor: m.role === 'user' ? '#E7F3F0' : '#F6F8FA',
                      border: `1px solid ${tokens.line}` }}>
                      <Box component="pre" sx={{ m: 0, whiteSpace: 'pre-wrap',
                        fontFamily: 'inherit', fontSize: 12.4, lineHeight: 1.5 }}>{m.text}</Box>
                      {m.role === 'assistant' && !!m.text && (
                        <Box sx={{ display: 'flex', gap: 0.5, mt: 0.3 }}>
                          <Button size="small" sx={{ textTransform: 'none', fontSize: 10.5, minWidth: 0, py: 0 }}
                            onClick={() => void navigator.clipboard?.writeText(m.text)}>Copy</Button>
                          <Button size="small" disabled={!!busy}
                            sx={{ textTransform: 'none', fontSize: 10.5, minWidth: 0, py: 0 }}
                            title="Renders this answer as a styled Word file"
                            onClick={() => void toWord(m.text)}>To Word</Button>
                        </Box>
                      )}
                    </Box>
                  </Box>
                ))}
                {thinking && (
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, py: 0.5 }}>
                    <CircularProgress size={13} />
                    <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                      {busy === 'generate' ? 'Drafting the CAM…' : 'Thinking…'}
                    </Typography>
                  </Box>
                )}
                <Box ref={chatEndRef} />
              </Box>
              {/* The question box — Enter sends, Shift+Enter for a new line. */}
              <Box sx={{ display: 'flex', gap: 0.8, alignItems: 'flex-end', p: 0.8,
                borderTop: `1px solid ${tokens.line}` }}>
                <TextField fullWidth size="small" multiline maxRows={6}
                  placeholder="Ask anything — Enter to send, Shift+Enter for a new line"
                  value={question} onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
                  }}
                  sx={{ '& textarea': { fontSize: 12.6, lineHeight: 1.5 } }} />
                <Button variant="outlined" size="small" endIcon={<SendIcon sx={{ fontSize: 15 }} />}
                  disabled={!question.trim() || !!busy} onClick={() => send()}
                  sx={{ whiteSpace: 'nowrap', textTransform: 'none' }}>
                  {busy === 'ask' ? 'Asking…' : 'Send'}
                </Button>
                <Button variant="contained" size="small" startIcon={<AutoAwesomeIcon sx={{ fontSize: 15 }} />}
                  disabled={!!busy || (!chat.length && !sel.size && !question.trim())}
                  onClick={() => generateCam()}
                  title="Turns the whole conversation + the prompt + the ticked documents into the full CAM draft"
                  sx={{ whiteSpace: 'nowrap', textTransform: 'none' }}>
                  {busy === 'generate' ? 'Drafting…' : 'Generate CAM'}
                </Button>
              </Box>
            </Box>

            <Divider sx={{ my: 1.4 }} />
            {!!working?.draft_md && (
              <Typography sx={{ fontSize: 12, color: tokens.muted, mb: 0.8 }}>
                Draft CAM on record (v{working.report_version})
                <Box component="span" onClick={() => void run('draft-word', async () => {
                    await camService.exportDocx(subjectId, working!.draft_md!,
                      `CAM v${working!.report_version} draft`);
                    return 'Draft downloaded as Word — continue in Word, then upload it below.';
                  })}
                  sx={{ color: 'primary.main', cursor: 'pointer', ml: 0.8,
                    textDecoration: 'underline' }}>
                  {busy === 'draft-word' ? 'Rendering…' : 'Download as Word'}
                </Box>
              </Typography>
            )}
            {!!working?.document_id && (
              <Typography sx={{ fontSize: 12, color: tokens.muted, mb: 0.8 }}>
                ✓ Completed CAM on file
                <Box component="span" onClick={() => void downloadFiled(working!.document_id!)}
                  sx={{ color: 'primary.main', cursor: 'pointer', ml: 0.8,
                    textDecoration: 'underline' }}>
                  Download
                </Box>
                {' '}— "Send to credit committee" will carry it to the approver.
              </Typography>
            )}
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center' }}>
              <TextField size="small" sx={{ flex: 1 }} label="Filed title (optional)"
                placeholder={`CAM v${working?.report_version ?? 1}`}
                value={title} onChange={(e) => setTitle(e.target.value)} />
              <Button component="label" variant="contained" size="small" disabled={!!busy}
                title="Files the document on this line only — the committee request is the separate 'Send to credit committee' step"
                sx={{ whiteSpace: 'nowrap', textTransform: 'none' }}>
                {busy === 'upload-final' ? 'Filing…' : 'Upload the completed CAM'}
                <input hidden type="file" accept=".docx,.pdf,.md,.txt"
                  onChange={(e) => { uploadFinal(e.target.files?.[0] || null); e.target.value = ''; }} />
              </Button>
            </Box>
          </Box>
        )}

      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={!!busy}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
