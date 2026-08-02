import { useEffect, useState } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, IconButton, Alert, CircularProgress } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld } from '../../components/common/Field';
import { useAuth } from '../../auth/AuthContext';
import { getSession } from '../../auth/session';
import {
  workflowService, isCommitteeDecision, committeeRef, sanctionRef, kindLabel, since,
  type PendingWorkflow,
} from '../../services/workflowService';
import { tokens } from '../../theme';

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
export default function WorkflowDecisionDialog({ w, approve, onClose, onDone }: {
  w: PendingWorkflow | null; approve: boolean; onClose: () => void; onDone: () => void;
}) {
  const { user } = useAuth();
  const [note, setNote] = useState('');
  const [cc, setCc] = useState('');
  const [sl, setSl] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState<any>(null);

  const committee = !!w && isCommitteeDecision(w);

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
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wid, approve, committee]);

  if (!w) return null;

  const submit = async () => {
    setBusy(true); setErr('');
    const res = await workflowService.decide(w, {
      approved: approve,
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
      <DialogTitle sx={{ fontSize: 16 }}>{approve ? 'Approve' : 'Reject'} — {kindLabel(w.kind)}
        <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>{stage || 'Awaiting a decision'} · raised {since(w.startedAt) || w.startedAt}</Typography>
        <IconButton onClick={onClose} disabled={busy} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
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
            placeholder={approve ? 'What the committee approved' : 'Why this is being rejected'} />
        </Box>
        <Typography sx={{ fontSize: 11.6, color: tokens.muted, mt: 1 }}>
          Recorded on the workflow plane as <b>{getSession()?.email || user.full}</b>. This releases the run — it does not edit the Register directly.
        </Typography>
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        <Button onClick={submit} variant="contained" color={approve ? 'primary' : 'error'} disabled={busy}
          startIcon={busy ? <CircularProgress size={13} color="inherit" /> : undefined}>
          {busy ? 'Recording…' : approve ? 'Approve run' : 'Reject run'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
