import { useEffect, useMemo, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, TextField, Alert, Chip, Checkbox, CircularProgress, Divider,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
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
  // The documents stay folded out of the way until the analyst wants them.
  const [docsOpen, setDocsOpen] = useState(false);
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
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLoading(false);
  };

  useEffect(() => {
    if (!open) return;
    setSel(new Set()); setInstruction(''); setTitle('');
    setNote(''); setErr(''); setInfo(''); setBusy('');
    void load();
  }, [open, subjectId]);  // eslint-disable-line react-hooks/exhaustive-deps

  // The line's LIVE version — the one the workbench writes into or the committee decides.
  const live = useMemo(
    () => [...reports].reverse().find((r) => ['Draft', 'Returned', 'Submitted'].includes(r.status)),
    [reports]);
  const working = live && (live.status === 'Draft' || live.status === 'Returned') ? live : undefined;
  const submitted = live && live.status === 'Submitted' ? live : undefined;

  // Everything pickable, in BOTH states: the company's own file, PLUS the credit team's
  // default EVAM CAM prompt as the first row — tick it and it rides with the next Ask
  // exactly like a document, so the engine answers under the team's own instructions.
  // (The example CAM is a DOWNLOAD — a Word template to fill — not a competing row.)
  const sources: EntityDoc[] = useMemo(() => [
    ...(defaults.prompt ? [{
      id: defaults.prompt.id, title: defaults.prompt.title,
      section: 'EVAM default', doc_type: 'CAM prompt', content_type: '', status: '',
    }] : []),
    ...docs,
  ], [docs, defaults.prompt]);


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
  // into the box — the analyst then asks on, and hand-fills the Word template.
  const SUMMARY_BRIEF =
    'Summarise each of the supplied documents for a credit analyst: key facts, all '
    + 'figures with periods and units, parties, and NAME every gap (what a CAM would '
    + 'need that these documents do not contain). Do not draft the CAM yet.';

  // ONE conversation surface: whatever is in the box goes to Claude, and the answer
  // comes back INTO the box — editable, so the analyst appends the next question (or
  // pastes document text) and sends again. Ticked documents ride along, then untick.
  const ask = (text?: string) => run('ask', async () => {
    const out = await camService.refine(subjectId, (text || instruction).trim(), false, [...sel]);
    setInstruction(out.draft_md || '');
    setSel(new Set());
    const skipped = (out.documents || []).filter((d: any) => !d.included);
    return 'The answer is in the box — edit it, or add your next question under it and ask again.'
      + (skipped.length ? ` ${skipped.length} document(s) could not be read.` : '');
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

  const downloadPrompt = async () => {
    if (!defaults.prompt) return;
    setErr('');
    const out = await documentsService.download(
      asEntry(defaults.prompt.id, `${defaults.prompt.title}.docx`) as any);
    if (!out.ok) setErr(out.error || 'The prompt download failed.');
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

        {/* ---- analyst: the calm workbench ------------------------------------------- */}
        {/* The CAM itself lives in WORD: download the template + the prompt document,
            fill the template there, and upload the finished file — that document goes to
            the committee. The box below is the conversation with Claude: prompts go down,
            answers come back into the SAME box to edit and build on. */}
        {!submitted && !loading && (
          <Box>
            <Box sx={{ display: 'flex', gap: 1, mb: 1, flexWrap: 'wrap', alignItems: 'center' }}>
              {defaults.example && (
                <Button variant="outlined" size="small" sx={{ textTransform: 'none' }}
                  onClick={() => void downloadTemplate()}>Download CAM template</Button>
              )}
              {defaults.prompt && (
                <Button variant="outlined" size="small" sx={{ textTransform: 'none' }}
                  onClick={() => void downloadPrompt()}>Download EVAM CAM prompt</Button>
              )}
              <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                Fill the template in Word while you work below.
              </Typography>
            </Box>
            {working?.status === 'Returned' && (
              <Alert severity="warning" sx={{ py: 0, fontSize: 12, mb: 1 }}>
                Returned by the committee{working?.decision_note ? ` — “${working.decision_note}”` : ''}.
                Amend the CAM and upload it again.
              </Alert>
            )}

            {/* The conversation — one box, both directions. */}
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
              <TextField fullWidth size="small" multiline minRows={8} maxRows={18}
                label="Ask anything — the answer appears here; edit it or add your next question"
                value={instruction} onChange={(e) => setInstruction(e.target.value)}
                sx={{ '& textarea': { fontSize: 12.6, lineHeight: 1.5 } }} />
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.6 }}>
                <Button variant="contained" size="small" startIcon={<AutoAwesomeIcon sx={{ fontSize: 15 }} />}
                  disabled={!instruction.trim() || !!busy} onClick={() => void ask()}
                  title="Sends everything in the box; the reply replaces it"
                  sx={{ whiteSpace: 'nowrap' }}>
                  {busy === 'ask' ? 'Asking…' : 'Ask'}
                </Button>
                <Button variant="outlined" size="small" disabled={!instruction || !!busy}
                  onClick={() => void navigator.clipboard?.writeText(instruction)}
                  sx={{ whiteSpace: 'nowrap' }}>Copy</Button>
                <Button variant="outlined" size="small" disabled={!instruction || !!busy}
                  onClick={() => setInstruction('')}
                  sx={{ whiteSpace: 'nowrap' }}>Clear</Button>
              </Box>
            </Box>
            {/* Documents — folded until wanted. */}
            {sources.length > 0 && (
              <Box sx={{ mt: 1.2, border: `1px solid ${tokens.line}`, borderRadius: 1 }}>
                <Box onClick={() => setDocsOpen((v) => !v)}
                  sx={{ display: 'flex', alignItems: 'center', px: 1, py: 0.6, cursor: 'pointer',
                    '&:hover': { bgcolor: '#FAFBFC' } }}>
                  <Typography sx={{ fontSize: 12.5, fontWeight: 600, flex: 1 }}>
                    {docsOpen ? '▾' : '▸'} Documents ({sources.length})
                    {sel.size > 0 && ` — ${sel.size} will ride with the next Ask/Summarise`}
                  </Typography>
                  <Button size="small" variant="outlined" disabled={!sel.size || !!busy}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (!sel.size) { setDocsOpen(true); return; }
                      void ask(SUMMARY_BRIEF);
                    }}
                    title="The engine digests the ticked documents — facts, figures, gaps — and the summary appears as an answer"
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
              <Box sx={{ mt: 1, border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1 }}>
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

            <Divider sx={{ my: 1.4 }} />
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
