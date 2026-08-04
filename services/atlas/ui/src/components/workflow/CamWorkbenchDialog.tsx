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
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [promptDoc, setPromptDoc] = useState('');
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
      const [r, d] = await Promise.all([
        camService.list(subjectId),
        entityId ? camService.entityDocs(entityId) : Promise.resolve([]),
      ]);
      setReports(r); setDocs(d);
    } catch (e: any) { setErr(e?.message || String(e)); }
    setLoading(false);
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

  const run = async (what: string, fn: () => Promise<string>) => {
    setErr(''); setInfo(''); setBusy(what);
    try {
      const message = await fn();
      setInfo(message);
      await load();
    } catch (e: any) { setErr(e?.message || String(e)); }
    setBusy('');
  };

  const generate = () => run('generate', async () => {
    const out = await camService.generate(subjectId, {
      source_doc_ids: [...sel], prompt_doc_id: promptDoc,
      ...(action?.body?.deal_id ? { deal_id: action.body.deal_id } : {}),
    });
    const skipped = (out.skipped || []).length;
    return `Draft v? ready (engine ${out.engine}).`
      + (skipped ? ` ${skipped} document(s) skipped — see the transcript.` : '');
  });

  const refine = () => run('refine', async () => {
    await camService.refine(subjectId, instruction.trim());
    setInstruction('');
    return 'Draft reworked.';
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

        {/* ---- committee: a SUBMITTED version awaits the decision -------------------- */}
        {submitted && (
          <Box sx={{ mb: 1.5 }}>
            <Typography sx={{ fontSize: 12.5, color: tokens.muted, mb: 0.6 }}>
              v{submitted.report_version} was submitted by <b>{submitted.prepared_by}</b> and
              awaits the committee. {committee
                ? 'You hold committee authority — decide below (the preparer cannot decide their own CAM).'
                : 'It appears on Today for Credit Head / Management.'}
            </Typography>
            {!!submitted.draft_md && (
              <Box component="pre" sx={{
                whiteSpace: 'pre-wrap', fontSize: 12, fontFamily: 'inherit', p: 1.2,
                border: `1px solid ${tokens.line}`, borderRadius: 1, maxHeight: 320, overflow: 'auto',
              }}>{submitted.draft_md}</Box>
            )}
            {committee && (
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
        )}

        {/* ---- analyst: an open draft to rework and finalise ------------------------- */}
        {working && (
          <Box>
            <Typography sx={{ fontSize: 12.5, color: tokens.muted, mb: 0.6 }}>
              v{working.report_version} · {working.status === 'Returned'
                ? <>returned by the committee{working.decision_note ? <> — “{working.decision_note}”</> : null}; rework it below</>
                : 'draft in progress'} · engine {working.engine || '—'}
            </Typography>
            <Box component="pre" sx={{
              whiteSpace: 'pre-wrap', fontSize: 12, fontFamily: 'inherit', p: 1.2,
              border: `1px solid ${tokens.line}`, borderRadius: 1, maxHeight: 300, overflow: 'auto',
            }}>{working.draft_md || 'No draft text yet — generate below.'}</Box>
            <Box sx={{ display: 'flex', gap: 1, mt: 1, alignItems: 'flex-start' }}>
              <TextField fullWidth size="small" multiline minRows={1}
                label="Rework instruction — what should change?"
                value={instruction} onChange={(e) => setInstruction(e.target.value)} />
              <Button variant="outlined" size="small" startIcon={<AutoAwesomeIcon sx={{ fontSize: 15 }} />}
                disabled={!instruction.trim() || !!busy} onClick={() => void refine()}
                sx={{ whiteSpace: 'nowrap', mt: 0.3 }}>
                {busy === 'refine' ? 'Reworking…' : 'Rework'}
              </Button>
            </Box>
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
        {!live && !loading && (
          <Box>
            <Typography sx={{ fontSize: 12.5, color: tokens.muted, mb: 0.8 }}>
              Pick the source documents and the credit team's <b>prompt document</b> — the
              engine drafts only from what you select, and says which documents it could
              not read.
            </Typography>
            {!docs.length && (
              <Alert severity="info" sx={{ py: 0, fontSize: 12, mb: 1 }}>
                Nothing on the company's file yet — upload the source documents and a
                prompt document in the Data Register first.
              </Alert>
            )}
            {docs.length > 0 && (
              <>
                <Box sx={{ maxHeight: 220, overflow: 'auto', border: `1px solid ${tokens.line}`,
                  borderRadius: 1, mb: 1 }}>
                  {docs.map((d) => (
                    <Box key={d.id} sx={{ display: 'flex', alignItems: 'center', px: 0.6,
                      borderBottom: `1px solid ${tokens.line}` }}>
                      <Checkbox size="small" checked={sel.has(d.id)} onChange={() => toggle(d.id)} />
                      <Typography sx={{ fontSize: 12.5, flex: 1 }}>{d.title}</Typography>
                      <Typography sx={{ fontSize: 11, color: tokens.muted }}>
                        {[d.section, d.doc_type].filter(Boolean).join(' · ')}
                      </Typography>
                    </Box>
                  ))}
                </Box>
                <TextField select fullWidth size="small" label="Prompt document (the drafting brief)"
                  value={promptDoc} onChange={(e) => setPromptDoc(e.target.value)} sx={{ mb: 1 }}>
                  {docs.map((d) => (
                    <MenuItem key={d.id} value={d.id} sx={{ fontSize: 13 }}>{d.title}</MenuItem>
                  ))}
                </TextField>
                <Button variant="contained" size="small"
                  startIcon={<AutoAwesomeIcon sx={{ fontSize: 15 }} />}
                  disabled={!sel.size || !promptDoc || !!busy}
                  onClick={() => void generate()}>
                  {busy === 'generate' ? 'Drafting…' : `Draft the CAM from ${sel.size || 'the'} document(s)`}
                </Button>
              </>
            )}
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={!!busy}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
