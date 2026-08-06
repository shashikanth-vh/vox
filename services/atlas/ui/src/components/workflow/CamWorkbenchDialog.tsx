import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, TextField, Alert, Chip, Checkbox, CircularProgress,
  FormControlLabel, Tooltip,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import RefreshIcon from '@mui/icons-material/Refresh';
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
 * The layout is the one every AI workbench has taught people (field feedback: "like
 * Claude's"): a LEFT RAIL holds everything that is set up once — template and prompt
 * downloads, the prompt choice, the document ticks, the filing lane — and the
 * CONVERSATION owns the rest of the surface, full height, question box at the bottom.
 * The transcript is durable: the register records every turn, so reopening the
 * workbench reopens the conversation, and "why does the CAM say this?" has an answer.
 *
 * Generate CAM is the point of the conversation: the whole exchange, the chosen
 * prompt, and the ticked documents become the draft in one click.
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

/** The rail's tiny section heading. */
const railHead = {
  fontSize: 10.5, fontWeight: 700, letterSpacing: '.08em', color: tokens.muted,
  textTransform: 'uppercase' as const, mt: 1.1, mb: 0.35,
};

/** Rail buttons stay dense — the rail is a toolbar, not a page. */
const railBtn = { textTransform: 'none' as const, py: 0.3, fontSize: 12 };

/** Turns the register's turn annotations into something a reader wants to see. */
function displayTurn(text: string): string {
  return text
    .replace(/\n?\[documents sent: [^\]]*\]/g, '\n(documents attached)')
    .replace(/^\[generate\][^\n]*/, 'Generate the CAM from the prompt and the attached documents.');
}

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
  // The company's file, folded until the analyst clicks Documents.
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
  const [tplBusy, setTplBusy] = useState(false);

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
    setNote(''); setErr(''); setInfo(''); setBusy(''); setPreview(null);
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
        .map((t) => ({ role: t.role, text: displayTurn(String(t.content || '')) }));
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
  // ticked documents becomes the CAM draft — SHAPED BY THE CAM TEMPLATE, which rides
  // along so the engine mirrors its exact sections. The finished draft downloads as
  // Word immediately: verify it, adjust if needed, upload it as the completed CAM.
  const generateCam = () => {
    if (busy) return;
    // A typed-but-unsent question still counts — it rides inside the brief.
    const pending = question.trim();
    const brief =
      'Prepare the complete CAM report now. '
      + (defaults.example
        ? 'A CAM TEMPLATE document is attached: reproduce its exact structure — the same '
          + 'sections, in the same order, under the same headings; fill each with this '
          + "case's content. "
        : '')
      + (activePromptId ? "Follow the prompt document's instructions exactly. " : '')
      + 'Use everything established in this conversation and every document provided. '
      + 'Where information is missing, name the gap rather than inventing a figure. '
      + 'Output the full CAM in Markdown with headings and tables, ready to render to Word.'
      + (pending ? `\n\nAlso take this into account: ${pending}` : '');
    setQuestion('');
    setChat((c) => [...c, { role: 'user',
      text: 'Generate the CAM report from this conversation, in the CAM template\'s format.'
        + (pending ? `\n(Also: ${pending})` : '') }]);
    void run('generate', async () => {
      const ride = [...(activePromptId ? [activePromptId] : []),
                    ...(defaults.example ? [defaults.example.id] : []), ...sel];
      const out = await camService.refine(subjectId, brief, true, ride);
      setChat((c) => [...c, { role: 'assistant', text: out.draft_md || '' }]);
      setSel(new Set());
      // The draft is FOR VERIFICATION — hand it over as a Word file straight away.
      if (out.draft_md) {
        await camService.exportDocx(subjectId, out.draft_md,
          title.trim() || `CAM v${working?.report_version ?? 1} draft`);
      }
      return 'CAM generated and downloaded as Word — verify it, make any updates, then upload the completed CAM.';
    });
  };

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

  const downloadPrompt = async () => {
    const p = defaults.prompt;
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
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth
      PaperProps={{ sx: { height: '92vh' } }}>
      <DialogTitle sx={{ fontSize: 16, py: 1.2 }}>
        CAM workbench
        {loading && <CircularProgress size={13} sx={{ ml: 1, verticalAlign: 'middle' }} />}
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 6 }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers sx={{ p: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        {(err || info) && (
          <Box sx={{ px: 1.5, pt: 1 }}>
            {err && <Alert severity="warning" sx={{ mb: 0.5, py: 0, fontSize: 12 }} onClose={() => setErr('')}>{err}</Alert>}
            {info && <Alert severity="success" sx={{ mb: 0.5, py: 0, fontSize: 12 }} onClose={() => setInfo('')}>{info}</Alert>}
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
          <Box sx={{ p: 2, overflowY: 'auto' }}>
            {reports.length > 0 && (
              <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap', mb: 1.2 }}>
                {reports.map((r) => (
                  <Chip key={r.id} size="small" color={TONE[r.status] || 'default'}
                    variant={live?.id === r.id ? 'filled' : 'outlined'}
                    label={`v${r.report_version} · ${r.status}`} />
                ))}
              </Box>
            )}
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

        {/* ---- analyst: rail on the left, the conversation owns the rest ------------- */}
        {!submitted && !loading && (
          <Box sx={{ display: 'flex', flex: 1, minHeight: 0,
            flexDirection: { xs: 'column', md: 'row' } }}>

            {/* THE RAIL — set up once, then talk. */}
            <Box sx={{ width: { xs: '100%', md: 260 }, flexShrink: 0,
              borderRight: { md: `1px solid ${tokens.line}` },
              borderBottom: { xs: `1px solid ${tokens.line}`, md: 'none' },
              overflowY: 'auto', px: 1.2, pb: 1.2,
              maxHeight: { xs: '38vh', md: 'none' } }}>

              {reports.length > 0 && (
                <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap', mt: 1.2 }}>
                  {reports.map((r) => (
                    <Chip key={r.id} size="small" color={TONE[r.status] || 'default'}
                      variant={live?.id === r.id ? 'filled' : 'outlined'}
                      label={`v${r.report_version} · ${r.status}`} />
                  ))}
                </Box>
              )}

              {defaults.example && (
                <Button fullWidth variant="outlined" size="small"
                  sx={{ ...railBtn, mt: 0.8 }}
                  title="The Word template the CAM must follow — Generate mirrors its structure"
                  onClick={() => void downloadTemplate()}>Download CAM template</Button>
              )}

              <Typography sx={railHead}>Prompts</Typography>
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.6 }}>
                {defaults.prompt && (
                  <Button fullWidth variant="outlined" size="small" sx={railBtn}
                    onClick={() => void downloadPrompt()}>Download CAM Prompt</Button>
                )}
                <Button fullWidth size="small" component="label" variant="outlined"
                  disabled={tplBusy || !!busy} sx={railBtn}
                  title="File a case-specific prompt on this line — tick it to use it instead of the default">
                  {tplBusy ? 'Uploading…' : 'Upload custom CAM prompt…'}
                  <input hidden type="file" accept=".docx,.md,.txt,.pdf"
                    onChange={(e) => { void uploadCustomPrompt(e.target.files?.[0] || null); e.target.value = ''; }} />
                </Button>
              </Box>
              <FormControlLabel sx={{ display: 'flex', mr: 0, mt: 0.4, alignItems: 'flex-start',
                '& .MuiTypography-root': { fontSize: 12.3, lineHeight: 1.35, mt: 0.3 } }}
                control={<Checkbox size="small" sx={{ py: 0.3 }} checked={promptUse === 'default'}
                  disabled={!defaults.prompt} onChange={() => pickPrompt('default')} />}
                title={defaults.prompt?.title || ''}
                label={defaults.prompt ? 'Default Prompt' : 'Default Prompt (none on record)'} />
              <FormControlLabel sx={{ display: 'flex', mr: 0, alignItems: 'flex-start',
                '& .MuiTypography-root': { fontSize: 12.3, lineHeight: 1.35, mt: 0.3 } }}
                control={<Checkbox size="small" sx={{ py: 0.3 }} checked={promptUse === 'custom'}
                  disabled={!customPrompt} onChange={() => pickPrompt('custom')} />}
                title={customPrompt?.title || ''}
                label={customPrompt ? 'Custom Prompt' : 'Custom Prompt (upload one first)'} />
              {promptUse === 'none' && (
                <Typography sx={{ fontSize: 11, color: tokens.muted, mt: 0.4 }}>
                  No prompt — questions go with the ticked documents only.
                </Typography>
              )}
              {sources.length > 0 && (
                <>
                  {/* Click to unfold the company's whole file — every document, tickable. */}
                  <Typography onClick={() => setDocsOpen((v) => !v)}
                    sx={{ ...railHead, cursor: 'pointer', userSelect: 'none',
                      '&:hover': { color: tokens.ink } }}>
                    {docsOpen ? '▾' : '▸'} Documents ({sources.length})
                    {!docsOpen && sel.size > 0 ? ` — ${sel.size} ticked` : ''}
                  </Typography>
                  {docsOpen && (
                    <>
                      <Box sx={{ maxHeight: 190, overflowY: 'auto',
                        border: `1px solid ${tokens.line}`, borderRadius: 1 }}>
                        {sources.map((d, i) => (
                          <Box key={d.id} sx={{ display: 'flex', alignItems: 'flex-start', px: 0.3,
                            py: 0.2, borderTop: i ? `1px solid ${tokens.line}` : 'none' }}>
                            <Checkbox size="small" sx={{ py: 0.4 }} checked={sel.has(d.id)}
                              onChange={() => toggle(d.id)} />
                            <Box sx={{ flex: 1, minWidth: 0, pt: 0.5 }}>
                              <Typography sx={{ fontSize: 11.8, lineHeight: 1.3 }}>{d.title}</Typography>
                              <Typography sx={{ fontSize: 10, color: tokens.muted }}>
                                {[d.section, d.doc_type].filter(Boolean).join(' · ')}
                              </Typography>
                            </Box>
                            <Button size="small" onClick={() => void viewDoc(d.id, d.title)}
                              sx={{ textTransform: 'none', fontSize: 10.5, minWidth: 0, px: 0.5 }}>view</Button>
                          </Box>
                        ))}
                      </Box>
                      <Button fullWidth size="small" variant="outlined" disabled={!sel.size || !!busy}
                        onClick={() => send(SUMMARY_BRIEF,
                          `Summarise the ${sel.size} ticked document(s) — facts, figures, gaps.`)}
                        title="The engine digests the ticked documents — facts, figures, gaps — into the conversation"
                        sx={{ ...railBtn, fontSize: 11.5, mt: 0.5 }}>
                        {busy === 'ask' ? 'Summarising…' : `Summarise selected (${sel.size})`}
                      </Button>
                      <Typography sx={{ fontSize: 10.5, color: tokens.muted, mt: 0.4 }}>
                        Ticked documents ride with the next question / Generate, then untick.
                      </Typography>
                    </>
                  )}
                </>
              )}

              {(working?.draft_md || working?.document_id) && (
                <>
                  <Typography sx={railHead}>On record</Typography>
                  {!!working?.draft_md && (
                    <Typography sx={{ fontSize: 11.8, color: tokens.muted, mb: 0.4 }}>
                      Draft v{working.report_version}
                      <Box component="span" onClick={() => void run('draft-word', async () => {
                          await camService.exportDocx(subjectId, working!.draft_md!,
                            `CAM v${working!.report_version} draft`);
                          return 'Draft downloaded as Word — continue in Word, then upload it below.';
                        })}
                        sx={{ color: 'primary.main', cursor: 'pointer', ml: 0.6,
                          textDecoration: 'underline' }}>
                        {busy === 'draft-word' ? 'Rendering…' : 'Download as Word'}
                      </Box>
                    </Typography>
                  )}
                  {!!working?.document_id && (
                    <Typography sx={{ fontSize: 11.8, color: tokens.muted }}>
                      ✓ Completed CAM on file
                      <Box component="span" onClick={() => void downloadFiled(working!.document_id!)}
                        sx={{ color: 'primary.main', cursor: 'pointer', ml: 0.6,
                          textDecoration: 'underline' }}>
                        Download
                      </Box>
                    </Typography>
                  )}
                </>
              )}

              <Typography sx={railHead}>File the CAM</Typography>
              <TextField fullWidth size="small" label="Filed title (optional)"
                placeholder={`CAM v${working?.report_version ?? 1}`}
                value={title} onChange={(e) => setTitle(e.target.value)} sx={{ mb: 0.6 }} />
              <Button fullWidth component="label" variant="contained" size="small" disabled={!!busy}
                title="Files the document on this line only — the committee request is the separate 'Send to credit committee' step"
                sx={railBtn}>
                {busy === 'upload-final' ? 'Filing…' : 'Upload the completed CAM'}
                <input hidden type="file" accept=".docx,.pdf,.md,.txt"
                  onChange={(e) => { uploadFinal(e.target.files?.[0] || null); e.target.value = ''; }} />
              </Button>
            </Box>

            {/* THE CONVERSATION — full height, question box at the bottom. */}
            <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0,
              minHeight: 240, position: 'relative' }}>
              {/* Refresh CLEARS THE VIEW only — the register keeps the transcript, and
                  the engine still sees the whole conversation on the next question. */}
              {chat.length > 0 && (
                <Tooltip title="Clear the conversation area (the recorded transcript is kept)">
                  <IconButton size="small" onClick={() => setChat([])}
                    sx={{ position: 'absolute', top: 6, right: 14, zIndex: 1,
                      bgcolor: '#fff', border: `1px solid ${tokens.line}`,
                      '&:hover': { bgcolor: '#F6F8FA' } }}>
                    <RefreshIcon sx={{ fontSize: 16 }} />
                  </IconButton>
                </Tooltip>
              )}
              {working?.status === 'Returned' && (
                <Alert severity="warning" sx={{ py: 0, fontSize: 12, m: 1, mb: 0 }}>
                  Returned by the committee{working?.decision_note ? ` — “${working.decision_note}”` : ''}.
                  Amend the CAM and upload it again.
                </Alert>
              )}
              <Box sx={{ flex: 1, overflowY: 'auto', p: 1.4 }}>
                {!chat.length && !thinking && (
                  <Typography sx={{ fontSize: 12.3, color: tokens.muted, textAlign: 'center',
                    maxWidth: 460, mx: 'auto', py: 5, lineHeight: 1.6 }}>
                    Ask about the documents, the figures, the risks — answers stack up here.
                    Tick documents on the left to send them with a question. When the picture
                    is complete, <b>Generate CAM</b> turns the whole conversation into the draft.
                  </Typography>
                )}
                {chat.map((m, i) => (
                  <Box key={i} sx={{ display: 'flex', mb: 0.8,
                    justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start' }}>
                    <Box sx={{ maxWidth: m.role === 'user' ? '78%' : '94%',
                      px: 1.2, py: 0.7, borderRadius: 2,
                      bgcolor: m.role === 'user' ? '#E7F3F0' : '#F6F8FA',
                      border: `1px solid ${tokens.line}` }}>
                      <Box component="pre" sx={{ m: 0, whiteSpace: 'pre-wrap',
                        fontFamily: 'inherit', fontSize: 12.6, lineHeight: 1.55 }}>{m.text}</Box>
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
              {preview && (
                <Box sx={{ mx: 1.4, mb: 0.6, border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1 }}>
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
                      fontFamily: 'inherit', maxHeight: 140, overflow: 'auto', m: 0 }}>
                      {preview.text}
                    </Box>
                  )}
                </Box>
              )}
              {/* The question box — Enter sends, Shift+Enter for a new line. */}
              <Box sx={{ display: 'flex', gap: 0.8, alignItems: 'flex-end', p: 1,
                borderTop: `1px solid ${tokens.line}` }}>
                <TextField fullWidth size="small" multiline maxRows={6}
                  placeholder={'Ask anything — Enter to send, Shift+Enter for a new line'
                    + (sel.size ? ` · ${sel.size} document(s) will ride along` : '')}
                  value={question} onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
                  }}
                  sx={{ '& textarea': { fontSize: 12.8, lineHeight: 1.5 } }} />
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
          </Box>
        )}

      </DialogContent>
      <DialogActions sx={{ py: 0.8 }}>
        <Button onClick={onClose} variant="outlined" disabled={!!busy}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
