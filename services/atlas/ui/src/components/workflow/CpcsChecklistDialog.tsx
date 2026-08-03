import { useEffect, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, MenuItem, TextField, Alert,
} from '@mui/material';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import AddIcon from '@mui/icons-material/Add';
import { workflowActionsService, type WorkflowAction } from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * Prepare the CP/CS checklist — the maker's half of the second governance gate.
 *
 * This is one of the two steps a server-described form could not carry: a checklist is a
 * LIST of conditions, each with its own type, status and evidence, and `items` must hold
 * at least one. So it gets a screen rather than a JSON box pretending to be a field.
 *
 * The checker's half already exists on Today (Approve / Return / Reject). The register
 * refuses an approval by whoever prepared it — maker and checker are different people —
 * so this dialog never offers to approve what it just filed.
 */

export interface CpcsItem {
  key: string;
  label: string;
  condition_type: 'CP' | 'CS';
  required: boolean;
  status: 'Pending' | 'Completed' | 'Waived' | 'Deferred as CS';
  evidence_ref: string;
  reason: string;
}

const STATUSES: CpcsItem['status'][] = ['Pending', 'Completed', 'Waived', 'Deferred as CS'];

/** A sensible opening checklist — the conditions almost every sanction carries. */
const STARTER: Omit<CpcsItem, 'status' | 'evidence_ref' | 'reason'>[] = [
  { key: 'security-creation', label: 'Security created and charge filed', condition_type: 'CP', required: true },
  { key: 'insurance', label: 'Insurance assigned to the lender', condition_type: 'CP', required: true },
  { key: 'end-use', label: 'End-use certificate', condition_type: 'CS', required: true },
];

const blank = (n: number): CpcsItem => ({
  key: `condition-${n}`, label: '', condition_type: 'CP', required: true,
  status: 'Pending', evidence_ref: '', reason: '',
});

/**
 * Takes the ACTION rather than loose ids. Its `body` already carries `lending_id`,
 * `deal_id` and — critically — the `requested_by` the plane filled from the verified
 * caller. Building the body here from scratch is how this screen first shipped, and it
 * answered `requested_by: Field required` on the very first use: a bespoke screen that
 * re-derives what the catalogue already provides will always drift from it.
 */
export default function CpcsChecklistDialog({ action, onClose, onDone }: {
  action: WorkflowAction | null;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const open = !!action;
  const [items, setItems] = useState<CpcsItem[]>([]);
  // The maker owns the version: v1 first time, raised when re-preparing after a checker
  // returned the previous one. The register keys the checklist on (lending, version), so
  // re-sending v1 after a return is a conflict — the field is here to be seen, not hidden.
  const [version, setVersion] = useState(1);
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setItems(STARTER.map((s) => ({ ...s, status: 'Pending', evidence_ref: '', reason: '' })));
    setVersion(1); setNote(''); setErr(''); setBusy(false);
  }, [open]);

  const set = (i: number, patch: Partial<CpcsItem>) =>
    setItems((rows) => rows.map((r, n) => (n === i ? { ...r, ...patch } : r)));
  const add = () => setItems((rows) => [...rows, blank(rows.length + 1)]);
  const drop = (i: number) => setItems((rows) => rows.filter((_, n) => n !== i));

  const submit = async () => {
    const named = items.filter((r) => r.label.trim() || r.key.trim());
    if (!named.length) { setErr('A checklist needs at least one condition.'); return; }
    const unlabelled = named.findIndex((r) => !r.label.trim());
    if (unlabelled >= 0) { setErr(`Condition ${unlabelled + 1} needs a description.`); return; }
    // A waiver has to be justified — the register asks the same question later, and it is
    // cheaper to answer it here than to have the checker return the whole checklist.
    const unexplained = named.findIndex((r) => r.status === 'Waived' && !r.reason.trim());
    if (unexplained >= 0) {
      setErr(`Condition ${unexplained + 1} is waived — say why.`); return;
    }
    if (!action) return;
    setBusy(true);
    const values: Record<string, any> = {
        checklist_version: version,
        ...(note.trim() ? { note: note.trim() } : {}),
        items: named.map((r) => ({
          key: (r.key.trim() || r.label.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-')).slice(0, 80),
          label: r.label.trim(),
          condition_type: r.condition_type,
          required: r.required,
          status: r.status,
          ...(r.evidence_ref.trim() ? { evidence_ref: r.evidence_ref.trim() } : {}),
          ...(r.reason.trim() ? { reason: r.reason.trim() } : {}),
        })),
    };
    // The action's own body wins: it holds the ids and the verified identity, and a form
    // value must never be able to overwrite either.
    const r = await workflowActionsService.run({ ...action, form: [] }, values);
    setBusy(false);
    if (!r.ok) { setErr(r.error || 'The workflow plane refused the checklist.'); return; }
    onDone(`CP/CS checklist v${version} sent for checking.`);
    onClose();
  };

  if (!action) return null;

  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>
        Prepare CP/CS checklist
        <TextField size="small" type="number" label="Version" value={version}
          onChange={(e) => setVersion(Math.max(1, Number(e.target.value) || 1))}
          sx={{ ml: 1.5, width: 104 }} inputProps={{ min: 1 }} />
      </DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 11.8, color: tokens.muted, mb: 1.2 }}>
          Conditions precedent (CP) must be satisfied before disbursement; conditions
          subsequent (CS) may follow it. A different checker approves this — you cannot
          approve your own checklist. Raise the <b>version</b> when re-preparing after a
          checker returned the previous one.
        </Typography>
        {err && <Alert severity="warning" sx={{ mb: 1.2, py: 0, fontSize: 12 }}
          onClose={() => setErr('')}>{err}</Alert>}

        {items.map((r, i) => (
          <Box key={i} sx={{ border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.2, mb: 1 }}>
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start', flexWrap: 'wrap' }}>
              <TextField size="small" label="Condition" required value={r.label}
                onChange={(e) => set(i, { label: e.target.value })}
                sx={{ flex: '2 1 260px' }} />
              <TextField size="small" select label="Type" value={r.condition_type}
                onChange={(e) => set(i, { condition_type: e.target.value as 'CP' | 'CS' })}
                sx={{ width: 92 }}>
                <MenuItem value="CP">CP</MenuItem>
                <MenuItem value="CS">CS</MenuItem>
              </TextField>
              <TextField size="small" select label="Status" value={r.status}
                onChange={(e) => set(i, { status: e.target.value as CpcsItem['status'] })}
                sx={{ width: 160 }}>
                {STATUSES.map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
              </TextField>
              <TextField size="small" select label="Required" value={r.required ? 'y' : 'n'}
                onChange={(e) => set(i, { required: e.target.value === 'y' })}
                sx={{ width: 110 }}>
                <MenuItem value="y">Required</MenuItem>
                <MenuItem value="n">Optional</MenuItem>
              </TextField>
              <IconButton size="small" onClick={() => drop(i)} disabled={items.length <= 1}
                sx={{ mt: 0.4 }}><DeleteOutlineIcon fontSize="small" /></IconButton>
            </Box>
            <Box sx={{ display: 'flex', gap: 1, mt: 1, flexWrap: 'wrap' }}>
              <TextField size="small" label="Evidence reference" value={r.evidence_ref}
                onChange={(e) => set(i, { evidence_ref: e.target.value })}
                placeholder="Document or filing reference" sx={{ flex: '1 1 240px' }} />
              <TextField size="small"
                label={r.status === 'Waived' ? 'Why waived (required)' : 'Reason / note'}
                required={r.status === 'Waived'} value={r.reason}
                onChange={(e) => set(i, { reason: e.target.value })}
                sx={{ flex: '2 1 300px' }} />
            </Box>
          </Box>
        ))}

        <Button size="small" startIcon={<AddIcon />} onClick={add}
          sx={{ textTransform: 'none' }}>Add a condition</Button>

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
