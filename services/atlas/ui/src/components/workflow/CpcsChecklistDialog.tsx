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
  /** Required when a CP is deferred as a CS — the date it must be satisfied by. */
  expiry_date: string;
}

const STATUSES: CpcsItem['status'][] = ['Pending', 'Completed', 'Waived', 'Deferred as CS'];

const blank = (n: number, t: 'CP' | 'CS' = 'CP'): CpcsItem => ({
  key: `condition-${n}`, label: '', condition_type: t, required: true,
  status: 'Pending', evidence_ref: '', reason: '', expiry_date: '',
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
  // TWO steps share this screen: "Prepare CP checklist" works the pre-disbursement
  // conditions; "Update CS checklist" starts once disbursement is in motion and records
  // documents as they arrive. The OTHER half's items ride along unchanged (hidden), so
  // the register always holds the complete picture in one versioned artefact.
  const phase: 'CP' | 'CS' = action?.key === 'cpcs.update-cs' ? 'CS' : 'CP';
  const [items, setItems] = useState<CpcsItem[]>([]);
  const [carried, setCarried] = useState<CpcsItem[]>([]);
  // The maker owns the version: v1 first time, raised when re-preparing after a checker
  // returned the previous one. The register keys the checklist on (lending, version), so
  // re-sending v1 after a return is a conflict — the field is here to be seen, not hidden.
  const [version, setVersion] = useState(1);
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [parsing, setParsing] = useState(false);

  // The conditions COME FROM the sanction letter — the engine reads them out, one row
  // each, so nobody re-types the letter into the checklist. Phase-aware: the CP step
  // pulls the Conditions Precedent, the CS step the Conditions Subsequent.
  const readLetter = async () => {
    const lid = String(action?.body?.lending_id || '');
    if (!lid) return;
    setErr(''); setParsing(true);
    try {
      const { camService } = await import('../../services/camService');
      const docs = await camService.lendingDocs(lid);
      const letter = docs.filter((d) => d.doc_type === 'sanction_letter').pop();
      if (!letter) throw new Error('No sanction letter on this line yet — upload it in "Enter sanction terms" first.');
      const out = await camService.extractTerms(letter.id);
      const labels = phase === 'CP'
        ? out.cp_items
        : out.cs_items.map((c) => c.label + (c.timeline ? ` (${c.timeline})` : ''));
      if (!labels.length) throw new Error(`The letter yielded no ${phase} conditions.`);
      setItems(labels.map((label, n) => ({
        key: label.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 80) || `condition-${n + 1}`,
        label, condition_type: phase, required: true,
        status: 'Pending', evidence_ref: '', reason: '', expiry_date: '',
      })));
    } catch (e: any) { setErr(e?.message || String(e)); }
    setParsing(false);
  };

  useEffect(() => {
    if (!open) return;
    // Empty until the seeded checklist / terms load — or the letter is read. No
    // invented starter conditions: the letter is the source of truth.
    setCarried([]);
    setItems([]);
    // The plane knows which version comes next — a checklist is keyed on (lending,
    // version), so opening on 1 every time hands the user a 409 after they have filled
    // the whole form in.
    const served = action?.form.find((f) => f.name === 'checklist_version')?.default;
    setVersion(Number(served) > 0 ? Number(served) : 1);
    setNote(''); setErr(''); setBusy(false);
    // The conditions were already entered ONCE — at the sanction terms (often read
    // straight out of the letter), which seeded checklist v1. Prefill from the latest
    // checklist on record (a re-prepare after a return keeps its statuses), falling
    // back to the terms' own item lists; the STARTER trio is only for a line that
    // skipped the terms step entirely.
    const lid = String(action?.body?.lending_id || '');
    if (!lid) return;
    let alive = true;
    void (async () => {
      try {
        const { api } = await import('../../api/http');
        const raw = await api.get<any>('/internal/cpcs-checklists', { lending_id: lid })
          .catch(() => []);
        const lists: any[] = Array.isArray(raw) ? raw : (raw?.items ?? []);
        let seeded: any[] = lists.length ? (lists[lists.length - 1].items || []) : [];
        if (!seeded.length) {
          const { camService } = await import('../../services/camService');
          const t = await camService.terms(lid).catch(() => null);
          seeded = [
            ...(t?.cp_items || []).map((x: any) => ({ ...x, condition_type: 'CP' })),
            ...(t?.cs_items || []).map((x: any) => ({ ...x, condition_type: 'CS' })),
          ];
        }
        if (alive && seeded.length) {
          const all = seeded.map((s: any): CpcsItem => ({
            key: String(s.key || ''), label: String(s.label || s.key || ''),
            condition_type: s.condition_type === 'CS' ? 'CS' : 'CP',
            required: s.required !== false,
            status: STATUSES.includes(s.status) ? s.status : 'Pending',
            evidence_ref: String(s.evidence_ref || ''),
            reason: String(s.reason || ''),
            expiry_date: String(s.expiry_date || ''),
          }));
          // This phase's items are edited; the other half rides along untouched.
          setItems(all.filter((x) => x.condition_type === phase));
          setCarried(all.filter((x) => x.condition_type !== phase));
        }
      } catch { /* the phase's starter stays */ }
    })();
    return () => { alive = false; };
  }, [open, action]);  // eslint-disable-line react-hooks/exhaustive-deps

  const set = (i: number, patch: Partial<CpcsItem>) =>
    setItems((rows) => rows.map((r, n) => (n === i ? { ...r, ...patch } : r)));
  const add = () => setItems((rows) => [...rows, blank(rows.length + 1, phase)]);
  const drop = (i: number) => setItems((rows) => rows.filter((_, n) => n !== i));

  const submit = async () => {
    const named = items.filter((r) => r.label.trim() || r.key.trim());
    if (!named.length) { setErr('A checklist needs at least one condition.'); return; }
    const unlabelled = named.findIndex((r) => !r.label.trim());
    if (unlabelled >= 0) { setErr(`Condition ${unlabelled + 1} needs a description.`); return; }
    // Everything below mirrors a rule the register enforces. Checking it here is not
    // belt-and-braces: this checklist is FILED as Completed, so a breach fails inside the
    // workflow seconds after the dialog closes — the run dies, nothing reaches the
    // checker's queue, and the screen has already said "sent".
    const unexplained = named.findIndex((r) => r.status === 'Waived' && !r.reason.trim());
    if (unexplained >= 0) {
      setErr(`Condition ${unexplained + 1} is waived — say why.`); return;
    }
    const deferredNotCp = named.findIndex(
      (r) => r.status === 'Deferred as CS' && r.condition_type !== 'CP');
    if (deferredNotCp >= 0) {
      setErr(`Condition ${deferredNotCp + 1} is already a CS — only a CP can be deferred as one.`);
      return;
    }
    const noExpiry = named.findIndex(
      (r) => r.status === 'Deferred as CS' && (!r.reason.trim() || !r.expiry_date));
    if (noExpiry >= 0) {
      setErr(`Condition ${noExpiry + 1} is deferred as a CS — it needs a reason and a date to be satisfied by.`);
      return;
    }
    // THE one that bit: a checklist is filed as Completed, and a Completed checklist may
    // not leave a required CP outstanding.
    const outstanding = named.filter(
      (r) => r.required && r.condition_type === 'CP' && r.status === 'Pending');
    if (outstanding.length) {
      setErr('These required CPs are still Pending, so the checklist cannot be sent: '
        + outstanding.map((r) => r.label || r.key).join(', ')
        + '. Mark each Completed, Waived (with a reason) or Deferred as CS.');
      return;
    }
    if (!action) return;
    setBusy(true);
    const values: Record<string, any> = {
        checklist_version: version,
        ...(note.trim() ? { note: note.trim() } : {}),
        items: [...named, ...carried].map((r) => ({
          key: (r.key.trim() || r.label.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-')).slice(0, 80),
          label: r.label.trim(),
          condition_type: r.condition_type,
          required: r.required,
          status: r.status,
          ...(r.evidence_ref.trim() ? { evidence_ref: r.evidence_ref.trim() } : {}),
          ...(r.reason.trim() ? { reason: r.reason.trim() } : {}),
          ...(r.expiry_date ? { expiry_date: r.expiry_date } : {}),
        })),
    };
    // The action's own body wins: it holds the ids and the verified identity, and a form
    // value must never be able to overwrite either.
    const r = await workflowActionsService.run({ ...action, form: [] }, values);
    setBusy(false);
    if (!r.ok) { setErr(r.error || 'The workflow plane refused the checklist.'); return; }
    onDone(`${phase === 'CP' ? 'CP checklist' : 'CS update'} v${version} sent for checking.`);
    onClose();
  };

  if (!action) return null;

  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>
        {phase === 'CP' ? 'Prepare CP checklist' : 'Update CS checklist'}
        <TextField size="small" type="number" label="Version" value={version}
          onChange={(e) => setVersion(Math.max(1, Number(e.target.value) || 1))}
          sx={{ ml: 1.5, width: 104 }} inputProps={{ min: 1 }} />
      </DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 11.8, color: tokens.muted, mb: 1.2 }}>
          {phase === 'CP' ? (
            <>Conditions PRECEDENT — read from the sanction letter — must be worked before
            disbursement. Chase the customer, mark each Completed / Waived / Deferred as
            CS, and send for checking: a different checker approves (never the preparer),
            and the approval releases disbursement <b>carrying whatever is not met</b> to
            Advaya. A required CP left Pending is refused. Raise the <b>version</b> when
            re-preparing after a return.</>
          ) : (
            <>Conditions SUBSEQUENT — read from the sanction letter — are collected while
            disbursement runs. Each time documents arrive, mark them Completed and send
            this updated version for checking; the chase reminders on Today keep running
            for whatever is still open, until nothing is left.</>
          )}
          {carried.length > 0 && (
            <> The {phase === 'CP' ? 'CS' : 'CP'} half ({carried.length} item{carried.length === 1 ? '' : 's'})
            rides along unchanged.</>
          )}
        </Typography>
        {err && <Alert severity="warning" sx={{ mb: 1.2, py: 0, fontSize: 12 }}
          onClose={() => setErr('')}>{err}</Alert>}
        <Button size="small" variant="outlined" disabled={parsing || busy}
          onClick={() => void readLetter()} sx={{ textTransform: 'none', mb: 1.2 }}
          title={`The engine reads the ${phase === 'CP' ? 'Conditions Precedent' : 'Conditions Subsequent'} out of the filed sanction letter — one row each`}>
          {parsing ? 'Reading the letter…' : `Read ${phase} conditions from the sanction letter`}
        </Button>
        {items.length === 0 && (
          <Typography sx={{ fontSize: 12, color: tokens.muted, mb: 1 }}>
            No {phase} conditions yet — read them from the sanction letter above, or add
            them by hand.
          </Typography>
        )}

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
                label={r.status === 'Waived' ? 'Why waived (required)'
                  : r.status === 'Deferred as CS' ? 'Why deferred (required)' : 'Reason / note'}
                required={r.status === 'Waived' || r.status === 'Deferred as CS'}
                value={r.reason}
                onChange={(e) => set(i, { reason: e.target.value })}
                sx={{ flex: '2 1 300px' }} />
              {r.status === 'Deferred as CS' && (
                <TextField size="small" type="date" label="Satisfy by (required)" required
                  value={r.expiry_date} onChange={(e) => set(i, { expiry_date: e.target.value })}
                  InputLabelProps={{ shrink: true }} sx={{ width: 190 }} />
              )}
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
