import { useEffect, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, MenuItem, TextField, Alert,
} from '@mui/material';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import AddIcon from '@mui/icons-material/Add';
import { documentsService } from '../../services/documentsService';
import { workflowActionsService, type WorkflowAction } from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * Prepare the Advaya handover package — the maker's half of the final gate.
 *
 * The other step a flat form could not carry: the package names a SET of executed
 * documents, and which ones go is a judgement made against what is actually on file. So
 * the dialog offers the company's Data Register as the pick list, and lets anything not
 * yet uploaded be typed as a reference.
 *
 * Handing a facility to Advaya is a money-movement authorisation, so a different checker
 * approves the package on Today before it can be submitted.
 */
/**
 * Takes the ACTION, so the ids and the plane-filled `requested_by` come from the
 * catalogue rather than being rebuilt here — see the note on CpcsChecklistDialog.
 */
export default function HandoverPackageDialog({ action, code, entityId, onClose, onDone }: {
  action: WorkflowAction | null;
  /** The company, for reading its documents as the pick list. */
  code: string;
  entityId?: string;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const open = !!action;
  const [onFile, setOnFile] = useState<{ ref: string; label: string }[]>([]);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [extra, setExtra] = useState<string[]>([]);
  const [recipient, setRecipient] = useState('');
  const [delivery, setDelivery] = useState('SFTP');
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setPicked({}); setExtra([]); setRecipient(''); setDelivery('SFTP');
    setNote(''); setErr(''); setBusy(false);
    documentsService.load(code, entityId).then((index) => {
      const rows: { ref: string; label: string }[] = [];
      Object.values(index).forEach((section) => {
        Object.values(section).forEach((e: any) => {
          if (e?.id) rows.push({ ref: e.id, label: `${e.label || e.name}${e.status === 'Verified' ? ' · verified' : ''}` });
        });
      });
      setOnFile(rows);
    }).catch(() => setOnFile([]));
  }, [open, code, entityId]);

  const refs = () => [
    ...onFile.filter((d) => picked[d.ref]).map((d) => d.ref),
    ...extra.map((r) => r.trim()).filter(Boolean),
  ];

  const submit = async () => {
    if (!recipient.trim()) { setErr('Name the recipient at Advaya.'); return; }
    const documents = refs();
    if (!documents.length) { setErr('A handover package must name at least one executed document.'); return; }
    if (!action) return;
    setBusy(true);
    const r = await workflowActionsService.run({ ...action, form: [] }, {
      recipient: recipient.trim(),
      delivery_method: delivery,
      executed_document_refs: documents,
      ...(note.trim() ? { note: note.trim() } : {}),
    });
    setBusy(false);
    if (!r.ok) { setErr(r.error || 'The workflow plane refused the package.'); return; }
    onDone('Handover package prepared and sent for checking.');
    onClose();
  };

  if (!action) return null;

  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>Prepare the Advaya handover package</DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 11.8, color: tokens.muted, mb: 1.2 }}>
          Handing a facility over is a money-movement authorisation: a different checker
          approves this package before it can be submitted.
        </Typography>
        {err && <Alert severity="warning" sx={{ mb: 1.2, py: 0, fontSize: 12 }}
          onClose={() => setErr('')}>{err}</Alert>}

        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mb: 1.4 }}>
          <TextField size="small" label="Recipient at Advaya" required value={recipient}
            onChange={(e) => setRecipient(e.target.value)} sx={{ flex: '2 1 260px' }} />
          <TextField size="small" select label="Delivery" value={delivery}
            onChange={(e) => setDelivery(e.target.value)} sx={{ width: 150 }}>
            {['SFTP', 'Email', 'Portal'].map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
          </TextField>
        </Box>

        <Typography sx={{ fontSize: 10.8, textTransform: 'uppercase', letterSpacing: '.8px',
          color: tokens.muted, fontWeight: 700, mb: 0.6 }}>Executed documents</Typography>

        {onFile.length === 0 && (
          <Alert severity="info" sx={{ mb: 1, py: 0, fontSize: 12 }}>
            Nothing is on this company's Data Register yet — add references below, or upload
            the executed documents first so the package can point at them.
          </Alert>
        )}
        {onFile.map((d) => (
          <Box key={d.ref} sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.4 }}>
            <input type="checkbox" checked={!!picked[d.ref]}
              onChange={(e) => setPicked((p) => ({ ...p, [d.ref]: e.target.checked }))} />
            <Typography sx={{ fontSize: 13 }}>{d.label}</Typography>
          </Box>
        ))}

        {extra.map((v, i) => (
          <Box key={i} sx={{ display: 'flex', gap: 1, mt: 0.8 }}>
            <TextField size="small" fullWidth label="Document reference" value={v}
              onChange={(e) => setExtra((rows) => rows.map((r, n) => (n === i ? e.target.value : r)))} />
            <IconButton size="small" onClick={() => setExtra((rows) => rows.filter((_, n) => n !== i))}>
              <DeleteOutlineIcon fontSize="small" />
            </IconButton>
          </Box>
        ))}
        <Button size="small" startIcon={<AddIcon />} sx={{ textTransform: 'none', mt: 0.6 }}
          onClick={() => setExtra((rows) => [...rows, ''])}>Add a reference</Button>

        <TextField fullWidth multiline minRows={2} size="small" label="Note for the checker"
          value={note} onChange={(e) => setNote(e.target.value)} sx={{ mt: 1.4 }} />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        <Button onClick={submit} variant="contained" disabled={busy}>
          {busy ? 'Sending…' : 'Send for checking'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
