import { useEffect, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, TextField, Alert,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { api, errText, isRegisterId } from '../../api/http';
import { camService } from '../../services/camService';
import type { PendingWorkflow } from '../../services/workflowService';
import { tokens } from '../../theme';

/**
 * Close one covenant period from its reminder: the RM/analyst called the borrower, the
 * documents arrived — record them. Uploads the received pack to the lending line's file
 * (section "Covenant — post-disbursement": CP is the pre-disbursement shelf, CS and
 * covenants the post), then submits the period's result. A FINANCIAL covenant needs the
 * tested figure; a breach opens its EWS case in the same transaction, and the register
 * says so here.
 */
export default function CovenantResultDialog({ w, onClose, onDone }: {
  w: PendingWorkflow | null; onClose: () => void; onDone: (message: string) => void;
}) {
  const open = !!w;
  const [submittedOn, setSubmittedOn] = useState('');
  const [actual, setActual] = useState('');
  const [note, setNote] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setSubmittedOn(new Date().toISOString().slice(0, 10));
    setActual(''); setNote(''); setFile(null); setErr(''); setBusy(false);
  }, [open]);

  if (!w) return null;
  const financial = !!w.metric;

  const save = async () => {
    if (!w.monitoringId) { setErr('This reminder has no open observation to record against.'); return; }
    if (financial && !actual.trim()) {
      setErr(`This is a financial covenant (${w.metric}) — the tested figure is required.`);
      return;
    }
    setErr(''); setBusy(true);
    try {
      // The received documents go on the company file FIRST — the result then cites
      // them in its note, so "where is the pack this figure came from?" has an answer.
      let filed = '';
      if (file && isRegisterId(w.subjectId)) {
        const doc = await camService.uploadDoc(w.subjectId, file, 'covenant_evidence',
          'Covenant — post-disbursement');
        filed = ` Document ${file.name} filed (${doc.id}).`;
      }
      const out = await api.post<any>(`/monitoring/${w.monitoringId}/result`, {
        submitted_on: submittedOn || undefined,
        ...(financial ? { actual_value: Number(actual) } : {}),
        note: [note.trim(), file ? `Received pack: ${file.name}` : '']
          .filter(Boolean).join(' · ') || undefined,
      });
      const breached = !!out?.breached;
      onDone(breached
        ? `Recorded — BREACHED; EWS case opened (${out?.ews_case_id || 'see EWS'}).${filed}`
        : `Recorded — compliant.${filed}`);
      onClose();
    } catch (e: any) {
      setErr(errText(e?.response?.data) || e?.message || 'The result was refused.');
    }
    setBusy(false);
  };

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>
        Record — {w.covenantName || 'covenant period'}
        <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>{w.stage}</Typography>
        <IconButton onClick={onClose} disabled={busy} sx={{ position: 'absolute', right: 8, top: 8 }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Box sx={{ display: 'grid', gridTemplateColumns: financial ? '1fr 1fr' : '1fr', gap: 1 }}>
          <TextField size="small" type="date" label="Received on" InputLabelProps={{ shrink: true }}
            value={submittedOn} onChange={(e) => setSubmittedOn(e.target.value)} />
          {financial && (
            <TextField size="small" type="number" label={`Tested ${w.metric}`} required
              value={actual} onChange={(e) => setActual(e.target.value)} />
          )}
        </Box>
        <Button component="label" variant="outlined" size="small" sx={{ mt: 1.2, textTransform: 'none' }}>
          {file ? `Attached: ${file.name}` : 'Attach the received documents…'}
          <input hidden type="file" accept=".pdf,.docx,.xlsx,.csv,.zip"
            onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </Button>
        <TextField fullWidth size="small" multiline minRows={2} sx={{ mt: 1.2 }}
          label="Note (what was received, anything off)"
          value={note} onChange={(e) => setNote(e.target.value)} />
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        <Button onClick={() => void save()} variant="contained" disabled={busy}>
          {busy ? 'Recording…' : 'Record period'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
