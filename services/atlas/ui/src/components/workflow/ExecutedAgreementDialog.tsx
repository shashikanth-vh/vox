import { useEffect, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  TextField, Alert, Radio,
} from '@mui/material';
import { documentsService, type DocEntry } from '../../services/documentsService';
import { workflowActionsService, type WorkflowAction } from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * Record the executed facility agreement.
 *
 * The register requires a SHA-256 because this is governance evidence: 'CP/CS Completed'
 * turns on it, and evidence you can swap for a different file afterwards proves nothing.
 * But the generic form asked a human to TYPE that digest, which is a question with no
 * answer inside the product — the only honest response was "open a terminal and run
 * sha256sum". So the digest is now produced here, two ways, and typing one by hand is
 * the last resort rather than the only route:
 *
 *   * pick the agreement off the company's Data Register — the register already computed
 *     and stored its SHA-256 when the file was uploaded, so the evidence points at the
 *     very bytes on file;
 *   * or choose the signed PDF from this machine and the browser hashes it locally
 *     (WebCrypto, no upload) — for an agreement that has not been filed yet.
 */

/** Hash a file in the browser. Nothing leaves the machine. */
async function digestOf(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  const hash = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, '0')).join('');
}

const SHA_RE = /^[0-9a-f]{64}$/i;

export default function ExecutedAgreementDialog({ action, code, entityId, onClose, onDone }: {
  action: WorkflowAction | null;
  /** The company, for reading its Data Register as the pick list. */
  code: string;
  entityId?: string;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const open = !!action;
  const [onFile, setOnFile] = useState<(DocEntry & { section: string })[]>([]);
  const [pickedId, setPickedId] = useState('');
  const [reference, setReference] = useState('');
  const [sha, setSha] = useState('');
  const [shaFrom, setShaFrom] = useState('');   // how we got it, shown to the user
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setPickedId(''); setReference(''); setSha(''); setShaFrom('');
    setNote(''); setErr(''); setBusy(false);
    documentsService.load(code, entityId).then((index) => {
      const rows: (DocEntry & { section: string })[] = [];
      Object.entries(index).forEach(([section, slots]) => {
        Object.values(slots).forEach((e: DocEntry) => {
          // Only a document whose digest the register actually holds can stand as
          // evidence — one without is a row we cannot attest to.
          if (e?.id && e.checksum) rows.push({ ...e, section });
        });
      });
      setOnFile(rows);
    }).catch(() => setOnFile([]));
  }, [open, code, entityId]);

  const pick = (d: DocEntry) => {
    setPickedId(d.id || '');
    setSha(d.checksum || '');
    setShaFrom(`from the Data Register copy of "${d.label || d.name}"`);
    if (!reference.trim()) setReference(d.label || d.name);
  };

  const fromDisk = async (file?: File | null) => {
    if (!file) return;
    if (!crypto?.subtle) {
      setErr('This browser cannot hash the file here. Pick the agreement from the Data '
             + 'Register instead, or paste its SHA-256.');
      return;
    }
    setErr('');
    try {
      const hex = await digestOf(file);
      setPickedId(''); setSha(hex);
      setShaFrom(`computed here from ${file.name} — the file was not uploaded`);
      if (!reference.trim()) setReference(file.name.replace(/\.[^.]+$/, ''));
    } catch {
      setErr('Could not read that file.');
    }
  };

  const submit = async () => {
    if (!reference.trim()) { setErr('Name the agreement — the reference is what a reader looks it up by.'); return; }
    if (!SHA_RE.test(sha.trim())) {
      setErr('The digest must be a 64-character SHA-256. Pick the agreement from the Data '
             + 'Register, or choose the signed file and let the browser compute it.');
      return;
    }
    if (!action) return;
    setBusy(true);
    const r = await workflowActionsService.run({ ...action, form: [] }, {
      reference: reference.trim(),
      sha256: sha.trim().toLowerCase(),
      ...(note.trim() ? { note: note.trim() } : {}),
    });
    setBusy(false);
    if (!r.ok) { setErr(r.error || 'The register refused the evidence.'); return; }
    onDone('Executed agreement recorded.');
    onClose();
  };

  if (!action) return null;

  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>Record the executed agreement</DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 11.8, color: tokens.muted, mb: 1.2 }}>
          This is the signed facility agreement, filed as governance evidence — CP/CS
          Completed depends on it. The register stores a SHA-256 so the evidence names
          specific bytes and not just a title.
        </Typography>
        {err && <Alert severity="warning" sx={{ mb: 1.2, py: 0, fontSize: 12 }}
          onClose={() => setErr('')}>{err}</Alert>}

        <Typography sx={{ fontSize: 10.8, textTransform: 'uppercase', letterSpacing: '.8px',
          color: tokens.muted, fontWeight: 700, mb: 0.6 }}>On this company's Data Register</Typography>

        {onFile.length === 0 ? (
          <Alert severity="info" sx={{ mb: 1, py: 0, fontSize: 12 }}>
            Nothing on the Data Register carries a digest yet. Upload the signed agreement
            under Documents — the register hashes it on the way in — or choose the file
            below and it will be hashed here.
          </Alert>
        ) : onFile.map((d) => (
          <Box key={d.id} onClick={() => pick(d)}
            sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.4, cursor: 'pointer' }}>
            <Radio size="small" checked={pickedId === d.id} />
            <Box>
              <Typography sx={{ fontSize: 13 }}>
                {d.label || d.name}
                {d.status === 'Verified' && <span style={{ color: tokens.ok }}> · verified</span>}
              </Typography>
              <Typography sx={{ fontSize: 11, color: tokens.muted, fontFamily: 'monospace' }}>
                {d.checksum?.slice(0, 16)}…
              </Typography>
            </Box>
          </Box>
        ))}

        <Box sx={{ mt: 1.4 }}>
          <Typography sx={{ fontSize: 10.8, textTransform: 'uppercase', letterSpacing: '.8px',
            color: tokens.muted, fontWeight: 700, mb: 0.6 }}>Or hash the signed file here</Typography>
          <Button size="small" variant="outlined" component="label" sx={{ textTransform: 'none' }}>
            Choose the signed agreement…
            <input hidden type="file" onChange={(e) => fromDisk(e.target.files?.[0])} />
          </Button>
          <Typography sx={{ fontSize: 11, color: tokens.muted, mt: 0.5 }}>
            The file is read in your browser to compute its digest. It is not uploaded.
          </Typography>
        </Box>

        <TextField fullWidth size="small" label="Agreement reference" required value={reference}
          onChange={(e) => setReference(e.target.value)} sx={{ mt: 1.6 }}
          placeholder="Facility agreement / execution reference" />

        <TextField fullWidth size="small" label="Document digest (SHA-256)" required value={sha}
          onChange={(e) => { setSha(e.target.value); setPickedId(''); setShaFrom('entered by hand'); }}
          sx={{ mt: 1.2 }} inputProps={{ style: { fontFamily: 'monospace', fontSize: 12 } }}
          helperText={shaFrom || 'Pick a document above, or choose the signed file — this fills itself.'} />

        <TextField fullWidth multiline minRows={2} size="small" label="Note (optional)"
          value={note} onChange={(e) => setNote(e.target.value)} sx={{ mt: 1.2 }} />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        <Button onClick={submit} variant="contained" disabled={busy}>
          {busy ? 'Recording…' : 'Record the executed agreement'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
