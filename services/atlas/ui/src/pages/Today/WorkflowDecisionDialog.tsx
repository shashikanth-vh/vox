import { useEffect, useState } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, IconButton, Alert, CircularProgress } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld } from '../../components/common/Field';
import { useAuth } from '../../auth/AuthContext';
import { getSession } from '../../auth/session';
import {
  workflowService, approvalContext, isCommitteeDecision, committeeRef, sanctionRef,
  kindLabel, since, noteRequired,
  type ApprovalContext, type PendingWorkflow, type DecisionAction,
} from '../../services/workflowService';
import { documentsService } from '../../services/documentsService';
import { tokens } from '../../theme';

// Per-verb copy. Return is the middle verb of the triad: NON-terminal — it goes back
// to the maker/requester for revision and comes round again, so it must never read like
// a refusal.
const TITLE: Record<DecisionAction, string> = {
  approve: 'Approve', return: 'Return for revision', reject: 'Reject',
};
const CTA: Record<DecisionAction, string> = {
  approve: 'Approve', return: 'Return to maker', reject: 'Reject',
};
const COLOR: Record<DecisionAction, 'primary' | 'warning' | 'error'> = {
  approve: 'primary', return: 'warning', reject: 'error',
};
const PLACEHOLDER: Record<DecisionAction, string> = {
  approve: 'What is being approved (optional)',
  return: 'What must be corrected before this comes back (required)',
  reject: 'Why this is refused, permanently (required)',
};
const EFFECT: Record<DecisionAction, string> = {
  approve: 'This releases the item and advances the flow.',
  return: 'The item goes BACK to its maker for revision — the loop continues and it returns to this queue once resubmitted.',
  reject: 'This is TERMINAL for this attempt — a revival is a fresh version/cycle.',
};

// One read-only fact from the run, laid out like the drawer's label/value pairs.
function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <Box>
      <Typography sx={{ fontSize: 10.4, textTransform: 'uppercase', letterSpacing: '.5px', color: tokens.muted, fontWeight: 700 }}>{label}</Typography>
      <Typography sx={{ fontSize: mono ? 11.6 : 12.8, fontFamily: mono ? 'ui-monospace,SFMono-Regular,Menlo,monospace' : undefined, wordBreak: 'break-all' }}>{value || '—'}</Typography>
    </Box>
  );
}

/**
 * The human decision on a workflow run. The plane parks the run and hands back the URL
 * that takes the decision, so this dialog only collects what the body carries: a note,
 * and — for a credit-committee decision — the committee and sanction-letter references.
 * References are pre-filled in the collection's shape and stay editable.
 */
export default function WorkflowDecisionDialog({ w, action, onClose, onDone }: {
  w: PendingWorkflow | null; action: DecisionAction; onClose: () => void; onDone: () => void;
}) {
  const { user } = useAuth();
  const [note, setNote] = useState('');
  const [cc, setCc] = useState('');
  const [sl, setSl] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState<any>(null);
  const [ctx, setCtx] = useState<ApprovalContext | null>(null);

  const committee = !!w && isCommitteeDecision(w);
  const approve = action === 'approve';

  // Keyed on the run, not on the object: the list refetches on a timer, and a new object
  // identity for the same run must not wipe what is being typed into the note.
  const wid = w?.workflowId;
  useEffect(() => {
    if (!w) return;
    setNote(''); setErr(''); setBusy(false); setLive(null);
    setCc(committee ? committeeRef() : '');
    setSl(committee && approve ? sanctionRef(w.subjectId) : '');
    // The list is a snapshot; read the run's own status so a decision is not taken on a
    // stale row. A failed read is not fatal — the list's stage still shows.
    let alive = true;
    workflowService.status(w).then((s) => { if (alive) setLive(s); });
    // And WHAT is being decided, in business words — the ids alone ask the approver to
    // decide blind. Best-effort: null keeps the id-only card working.
    setCtx(null);
    void approvalContext(w).then((c) => { if (alive) setCtx(c); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wid, action, committee]);

  if (!w) return null;

  const submit = async () => {
    setBusy(true); setErr('');
    const res = await workflowService.decide(w, {
      action,
      by: getSession()?.email || user.full,
      note,
      committeeReference: cc,
      sanctionLetterReference: sl,
    });
    setBusy(false);
    if (!res.ok) { setErr(res.error || 'The decision was not recorded.'); return; }
    onDone(); onClose();
  };

  const stage = live?.stage || w.stage;
  const status = live?.status || w.status;

  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>{TITLE[action]} — {kindLabel(w.kind)}
        <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>{stage || 'Awaiting a decision'} · raised {since(w.startedAt) || w.startedAt}</Typography>
        <IconButton onClick={onClose} disabled={busy} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {ctx && (
          <Box sx={{ mb: 1.4 }}>
            <Typography sx={{ fontSize: 13.4, fontWeight: 600, mb: 0.8 }}>{ctx.headline}</Typography>
            {ctx.facts.length > 0 && (
              <FieldGrid>
                {ctx.facts.map(([label, value]) => <Fact key={label} label={label} value={value} />)}
              </FieldGrid>
            )}
            {ctx.document && (
              <Button size="small" variant="outlined" sx={{ textTransform: 'none', mt: 1 }}
                onClick={() => void documentsService.download({ id: ctx.document!.id,
                  name: ctx.document!.name, size: 0, type: '', when: '', by: '', label: '' } as any)
                  .then((r) => { if (!r.ok) setErr(r.error || 'The download failed.'); })}>
                Download the filed document — {ctx.document.name}
              </Button>
            )}
            {ctx.preview && (
              <Box component="pre" sx={{
                whiteSpace: 'pre-wrap', fontSize: 12, fontFamily: 'inherit', p: 1.2, mt: 1,
                border: `1px solid ${tokens.line}`, borderRadius: 1, maxHeight: 260, overflow: 'auto',
              }}>{ctx.preview}</Box>
            )}
          </Box>
        )}
        <FieldGrid>
          <Fact label="Requested by" value={w.requestedBy} />
          <Fact label="Status" value={status} />
          <Fact label="Subject" value={w.subjectId} mono />
          <Fact label="Workflow" value={w.workflowId} mono />
        </FieldGrid>
        {committee && (
          <Box sx={{ mt: 1.4 }}>
            <FieldGrid>
              <TextFld label="Committee reference" value={cc} onChange={setCc} />
              {approve && <TextFld label="Sanction letter reference" value={sl} onChange={setSl} />}
            </FieldGrid>
          </Box>
        )}
        <Box sx={{ mt: 1.2 }}>
          <TextFld label="Note" value={note} onChange={setNote} multiline
            placeholder={PLACEHOLDER[action]}
            required={noteRequired(action)} />
        </Box>
        <Typography sx={{ fontSize: 11.6, color: tokens.muted, mt: 1 }}>
          Recorded on the workflow plane as <b>{getSession()?.email || user.full}</b>. {EFFECT[action]}
        </Typography>
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        <Button onClick={submit} variant="contained" color={COLOR[action]} disabled={busy}
          startIcon={busy ? <CircularProgress size={13} color="inherit" /> : undefined}>
          {busy ? 'Recording…' : CTA[action]}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
