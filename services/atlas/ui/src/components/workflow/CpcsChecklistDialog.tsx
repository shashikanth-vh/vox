import { useEffect, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, MenuItem, TextField, Alert, Tabs, Tab,
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
export default function CpcsChecklistDialog({ action, onClose, onDone, readOnly = false }: {
  action: WorkflowAction | null;
  onClose: () => void;
  onDone: (message: string) => void;
  /** VIEW the recorded checklist (a settled box on the pipeline strip): every field
   *  frozen, no submit — the record is on display, not up for edits. */
  readOnly?: boolean;
}) {
  const open = !!action;
  const ro = readOnly;
  // TWO steps share this screen: "Prepare CP checklist" works the pre-disbursement
  // conditions; "Update CS checklist" starts once disbursement is in motion and records
  // documents as they arrive. The OTHER half's items ride along unchanged (hidden), so
  // the register always holds the complete picture in one versioned artefact.
  // TWO TABS, because the two checklists are different documents: the conditions
  // precedent are the pre-disbursement gate, the conditions subsequent are the chase
  // that runs on afterwards. The button you came in on picks the opening tab; from
  // there the desk can read either half without leaving the screen.
  const opening: 'CP' | 'CS' = action?.key === 'cpcs.update-cs' ? 'CS' : 'CP';
  const [phase, setPhase] = useState<'CP' | 'CS'>(opening);
  // A fresh open re-seats the tab on whichever half was asked for.
  useEffect(() => { if (open) setPhase(opening); }, [open, opening]);
  // Everything the register holds, both halves, kept whole so switching tabs is a
  // filter rather than a reload.
  const [all, setAll] = useState<CpcsItem[]>([]);
  const [items, setItems] = useState<CpcsItem[]>([]);
  const [carried, setCarried] = useState<CpcsItem[]>([]);
  // The CS step writes PROGRESS onto the approved checklist row — it needs its id.
  const [latestId, setLatestId] = useState('');
  const [latestStatus, setLatestStatus] = useState('');
  // The maker owns the version: v1 first time, raised when re-preparing after a checker
  // returned the previous one. The register keys the checklist on (lending, version), so
  // re-sending v1 after a return is a conflict — the field is here to be seen, not hidden.
  const [version, setVersion] = useState(1);
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const [savedMsg, setSavedMsg] = useState('');
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
      const { camService, newestOf } = await import('../../services/camService');
      const docs = await camService.lendingDocs(lid);
      const letter = newestOf(docs, 'sanction_letter');
      if (!letter) throw new Error('No sanction letter on this line yet — upload it in "Enter sanction terms" first.');
      const out = await camService.extractTerms(letter.id);
      const labels = phase === 'CP'
        ? out.cp_items
        : out.cs_items.map((c) => c.label + (c.timeline ? ` (${c.timeline})` : ''));
      if (!labels.length) throw new Error(`The letter yielded no ${phase} conditions.`);
      // MERGE, never replace. Reading the letter used to overwrite the list, which threw
      // away everything already on this tab — a CP the checker deferred as a CS (with
      // its reason and its satisfy-by date), a receipt already recorded, a condition
      // someone typed by hand. The letter is a SOURCE of conditions, not the authority
      // on which ones exist: what is already on record wins, and only genuinely new
      // lines are appended.
      const norm = (t: string) => t.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
      setItems((rows) => {
        const seen = new Set(rows.map((r) => norm(r.label || r.key)));
        const fresh = labels
          .filter((label) => !seen.has(norm(label)))
          .map((label, n): CpcsItem => ({
            key: label.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 80)
              || `condition-${rows.length + n + 1}`,
            label, condition_type: phase, required: true,
            status: 'Pending', evidence_ref: '', reason: '', expiry_date: '',
          }));
        if (!fresh.length) setErr(`Every ${phase} condition in the letter is already on this list.`);
        return [...rows, ...fresh];
      });
    } catch (e: any) { setErr(e?.message || String(e)); }
    setParsing(false);
  };

  useEffect(() => {
    if (!open) return;
    // Empty until the seeded checklist / terms load — or the letter is read. No
    // invented starter conditions: the letter is the source of truth.
    setCarried([]);
    setItems([]);
    setAll([]);
    setLatestId(''); setLatestStatus('');
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
        // The register lists checklists NEWEST FIRST — taking the LAST element read the
        // OLDEST version. One version in, that is the same row; the moment a line had
        // history (a v1 the checker rejected, then an approved v2) this picked the
        // rejected v1: CS saves bounced off "get the CP checklist approved first" with
        // the approval sitting right there, and would have targeted the wrong row even
        // when the status happened to pass. CS progress belongs to the APPROVED
        // checklist (the register enforces exactly that), so prefer the
        // highest-version approved one; a maker re-preparing after a return/reject
        // gets the highest version outright — their newest work.
        const ver = (x: any) => Number(x?.checklist_version) || 0;
        const newest = lists.reduce((a, b) => (ver(b) >= ver(a) ? b : a), lists[0]);
        const approved = lists.filter((x) => String(x?.status) === 'Approved')
          .reduce((a: any, b: any) => (!a || ver(b) >= ver(a) ? b : a), null);
        const target = approved ?? newest;
        if (alive && lists.length) {
          setLatestId(String(target.id || ''));
          setLatestStatus(String(target.status || ''));
        }
        let seeded: any[] = lists.length ? (target.items || []) : [];
        // A REJECTED or RETURNED version's deferral was refused along with everything
        // else on it. Carrying it forward as settled smuggled the refused claim into
        // the next version — the CP read 9/9, the item sat on the CS tab, and the
        // checker's "no" changed nothing on screen. It comes back as OPEN CP work
        // (reason and satisfy-by kept for context): the maker re-decides deliberately —
        // complete it, waive it, or propose the deferral afresh for the next checker.
        // (A READ-ONLY open shows the record AS DECIDED — the reset is for re-preparing.)
        if (!ro && lists.length
            && ['Rejected', 'Returned'].includes(String(target?.status))) {
          seeded = seeded.map((s: any) => (
            s.status === 'Deferred as CS' && s.condition_type !== 'CS'
              ? { ...s, status: 'Pending' } : s));
        }
        // The sanction terms seeded these conditions once — they are the source for
        // BOTH halves. This fallback used to fire only on a COMPLETELY empty record,
        // so a checklist carrying just its CP items opened with a blank CS tab and
        // the desk re-read the letter by hand for conditions the terms held all
        // along. Fill per HALF instead: whichever half the record lacks appears
        // automatically. (A read-only open stays faithful to the record — no merge.)
        const hasCp = seeded.some((s: any) => s.condition_type !== 'CS');
        const hasCs = seeded.some((s: any) => s.condition_type === 'CS');
        if (!ro && (!hasCp || !hasCs)) {
          const { camService, newestOf } = await import('../../services/camService');
          const t = await camService.terms(lid).catch(() => null);
          if (!hasCp) {
            seeded = [...seeded,
              ...(t?.cp_items || []).map((x: any) => ({ ...x, condition_type: 'CP' }))];
          }
          if (!hasCs) {
            seeded = [...seeded,
              ...(t?.cs_items || []).map((x: any) => ({ ...x, condition_type: 'CS' }))];
          }
          // Still short a half — the terms were never typed in (the sanction came
          // through the committee flow with only the LETTER on file). The button's
          // extraction runs by itself: the engine reads the letter, the conditions
          // appear, and the button remains only for a deliberate re-read. Best-effort —
          // no letter, or the engine down, leaves the empty state and the button.
          const stillNoCp = !seeded.some((s: any) => s.condition_type !== 'CS');
          const stillNoCs = !seeded.some((s: any) => s.condition_type === 'CS');
          if (stillNoCp || stillNoCs) {
            try {
              const docs = await camService.lendingDocs(lid);
              const letter = newestOf(docs, 'sanction_letter');
              if (letter && alive) {
                setParsing(true);
                const out = await camService.extractTerms(letter.id);
                const asItem = (label: string, half: 'CP' | 'CS') => ({
                  key: label.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 80),
                  label, condition_type: half, required: true, status: 'Pending',
                });
                if (stillNoCp) {
                  seeded = [...seeded,
                    ...(out.cp_items || []).map((l) => asItem(l, 'CP'))];
                }
                if (stillNoCs) {
                  seeded = [...seeded, ...(out.cs_items || []).map((c) =>
                    asItem(c.label + (c.timeline ? ` (${c.timeline})` : ''), 'CS'))];
                }
              }
            } catch { /* the read-the-letter button stays as the manual path */ }
            if (alive) setParsing(false);
          }
        }
        if (alive && seeded.length) {
          const loaded = seeded.map((s: any): CpcsItem => ({
            key: String(s.key || ''), label: String(s.label || s.key || ''),
            condition_type: s.condition_type === 'CS' ? 'CS' : 'CP',
            required: s.required !== false,
            status: STATUSES.includes(s.status) ? s.status : 'Pending',
            evidence_ref: String(s.evidence_ref || ''),
            reason: String(s.reason || ''),
            expiry_date: String(s.expiry_date || ''),
          }));
          setAll(loaded);
        }
      } catch { /* the phase's starter stays */ }
    })();
    return () => { alive = false; };
  }, [open, action]);  // eslint-disable-line react-hooks/exhaustive-deps

  // The active tab's items are edited; the other half rides along untouched so the
  // register always receives the complete artefact.
  // A CP the checker DEFERRED AS A CS belongs on the CS tab: the deferral converted it
  // into a post-disbursement obligation, and it stays typed "CP" until its first
  // progress flips it. Splitting on the type alone hid it from BOTH tabs while Today
  // kept chasing it — visible in the reminder, unreachable on the screen.
  const isCs = (x: CpcsItem) => x.condition_type === 'CS' || x.status === 'Deferred as CS';
  useEffect(() => {
    if (!all.length) return;
    const mine = (x: CpcsItem) => (phase === 'CS' ? isCs(x) : !isCs(x));
    setItems(all.filter(mine));
    setCarried(all.filter((x) => !mine(x)));
  }, [all, phase]);

  /** "(3/9)" for a half that has items, nothing for one that has none yet. */
  const tally = (half: 'CP' | 'CS') => {
    const rel = (phase === half ? items
      : all.filter((x) => (half === 'CS' ? isCs(x) : !isCs(x))));
    if (!rel.length) return '';
    const doneStates = half === 'CP'
      ? ['Completed', 'Waived', 'Deferred as CS'] : ['Completed', 'Waived'];
    return ` (${rel.filter((r) => doneStates.includes(r.status)).length}/${rel.length})`;
  };

  const itemOut = (r: CpcsItem) => ({
    key: (r.key.trim() || r.label.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-')).slice(0, 80),
    label: r.label.trim(),
    condition_type: r.condition_type,
    required: r.required,
    status: r.status,
    ...(r.evidence_ref.trim() ? { evidence_ref: r.evidence_ref.trim() } : {}),
    ...(r.reason.trim() ? { reason: r.reason.trim() } : {}),
    ...(r.expiry_date ? { expiry_date: r.expiry_date } : {}),
  });

  // Evidence arrives over days: a DRAFT saves whatever stands — pending CPs and
  // all — straight onto the register (same version, updated in place), so the
  // desk closes the dialog without losing the chase. Send for checking stays the
  // all-conditions-settled gate.
  const saveDraft = async () => {
    const named = items.filter((r) => r.label.trim());
    if (!named.length) { setErr('Nothing to save yet — add or read the conditions first.'); return; }
    setBusy(true); setErr(''); setSavedMsg('');
    try {
      const { api } = await import('../../api/http');
      await api.post('/internal/cpcs-checklists', {
        lending_id: String(action?.body?.lending_id || ''),
        ...(action?.body?.deal_id ? { deal_id: String(action.body.deal_id) } : {}),
        checklist_version: version, status: 'Draft',
        ...(note.trim() ? { note: note.trim() } : {}),
        items: [...named, ...carried].map(itemOut),
      });
      setLatestStatus('Draft');
      setSavedMsg(`Draft v${version} saved — continue anytime; Send for checking when every condition is settled.`);
    } catch (e: any) {
      setErr(e?.response?.data?.error?.detail || e?.message || String(e));
    }
    setBusy(false);
  };

  const set = (i: number, patch: Partial<CpcsItem>) =>
    setItems((rows) => rows.map((r, n) => (n === i ? { ...r, ...patch } : r)));
  const add = () => setItems((rows) => [...rows, blank(rows.length + 1, phase)]);
  const drop = (i: number) => setItems((rows) => rows.filter((_, n) => n !== i));

  const submit = async () => {
    // The CS lane is a LIVE record, not a decision: receipt is saved straight onto the
    // approved checklist — no new version, no checker round-trip. The chase reminders
    // shrink the moment this saves.
    if (phase === 'CS') {
      const namedCs = items.filter((r) => r.label.trim());
      if (!namedCs.length) { setErr('Nothing to save — add or read the CS conditions first.'); return; }
      // Evidence reference is OPTIONAL — the desk names it when a document exists;
      // a phone confirmation or a site visit completes a condition too.
      if (!latestId || latestStatus !== 'Approved') {
        setErr('CS progress is recorded on the APPROVED checklist — get the CP checklist approved first.');
        return;
      }
      setBusy(true);
      try {
        const { api } = await import('../../api/http');
        await api.post(`/internal/cpcs-checklists/${latestId}/cs-progress`, {
          items: namedCs.map((r) => ({
            key: (r.key.trim() || r.label.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-')).slice(0, 80),
            label: r.label.trim(),
            status: r.status === 'Completed' ? 'Completed' : 'Pending',
            ...(r.evidence_ref.trim() ? { evidence_ref: r.evidence_ref.trim() } : {}),
            ...(r.reason.trim() ? { note: r.reason.trim() } : {}),
            required: r.required,
          })),
        });
        setBusy(false);
        onDone('CS progress saved — the chase reminders now cover only what is still open.');
        onClose();
      } catch (e: any) {
        setBusy(false);
        setErr(e?.response?.data?.error?.detail || e?.message || String(e));
      }
      return;
    }
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
    // Evidence reference is OPTIONAL on a Completed condition — name it when a
    // filed document exists; the checker's approval is the four-eyes control.
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
      <DialogTitle sx={{ fontSize: 16, pb: 1 }}>
        {ro ? 'CP / CS checklist — on record'
          : phase === 'CP' ? 'Prepare CP checklist' : 'Update CS checklist'}
        {phase === 'CP' && !ro && (
          <TextField size="small" type="number" label="Version" value={version}
            onChange={(e) => setVersion(Math.max(1, Number(e.target.value) || 1))}
            sx={{ ml: 1.5, width: 104 }} inputProps={{ min: 1 }} />
        )}
        {/* Two documents, two tabs. Each shows how much of its own half is done, so the
            desk can see at a glance which one still needs work. */}
        <Tabs value={phase} onChange={(_e, v) => setPhase(v)} sx={{ mt: 1, minHeight: 36 }}>
          <Tab value="CP" label={`Conditions Precedent${tally('CP')}`}
            sx={{ minHeight: 36, fontSize: 12.5, textTransform: 'none' }} />
          <Tab value="CS" label={`Conditions Subsequent${tally('CS')}`}
            sx={{ minHeight: 36, fontSize: 12.5, textTransform: 'none' }} />
        </Tabs>
      </DialogTitle>
      <DialogContent dividers>
        {ro && (
          <Alert severity="info" sx={{ mb: 1, py: 0.2, fontSize: 12.3 }}>
            Viewing the checklist on record
            {latestStatus ? <> — latest version is <b>{latestStatus}</b></> : null}.
            Nothing here is editable.
          </Alert>
        )}
        {!ro && phase === 'CS' && latestStatus !== 'Approved' && (
          <Alert severity="info" sx={{ mb: 1, py: 0.2, fontSize: 12.3 }}>
            Conditions subsequent are recorded on the APPROVED checklist — get the CP
            half approved first, then chase these here.
          </Alert>
        )}
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
            disbursement runs. As documents arrive, mark them Completed and <b>Save</b> —
            no approval round: this updates the record directly, and the chase reminders
            on Today keep running for whatever is still open, until nothing is left.</>
          )}
          {carried.length > 0 && (
            <> The {phase === 'CP' ? 'CS' : 'CP'} half ({carried.length} item{carried.length === 1 ? '' : 's'})
            rides along unchanged.</>
          )}
        </Typography>
        {err && <Alert severity="warning" sx={{ mb: 1.2, py: 0, fontSize: 12 }}
          onClose={() => setErr('')}>{err}</Alert>}
        <Button size="small" variant="outlined" disabled={parsing || busy || ro}
          onClick={() => void readLetter()} sx={{ textTransform: 'none', mb: 1.2 }}
          title={`The engine reads the ${phase === 'CP' ? 'Conditions Precedent' : 'Conditions Subsequent'} out of the filed sanction letter — one row each`}>
          {parsing ? 'Reading the letter…' : `Read ${phase} conditions from the sanction letter`}
        </Button>
        {items.length === 0 && (
          <Typography sx={{ fontSize: 12, color: tokens.muted, mb: 1 }}>
            {ro ? `No ${phase} conditions on record.`
              : `No ${phase} conditions yet — read them from the sanction letter above, `
                + 'or add them by hand.'}
          </Typography>
        )}

        {items.map((r, i) => (
          <Box key={i} sx={{ border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.2, mb: 1 }}>
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start', flexWrap: 'wrap' }}>
              <TextField size="small" label="Condition" required value={r.label}
                disabled={ro} onChange={(e) => set(i, { label: e.target.value })}
                sx={{ flex: '2 1 260px' }} />
              <TextField size="small" select label="Type" value={r.condition_type}
                disabled={ro}
                onChange={(e) => set(i, { condition_type: e.target.value as 'CP' | 'CS' })}
                sx={{ width: 92 }}>
                <MenuItem value="CP">CP</MenuItem>
                <MenuItem value="CS">CS</MenuItem>
              </TextField>
              <TextField size="small" select label="Status" value={r.status}
                disabled={ro}
                onChange={(e) => set(i, { status: e.target.value as CpcsItem['status'] })}
                sx={{ width: 160 }}>
                {/* The CS tab offers Pending | Completed: deferring is a CP decision the
                    checker makes, not something to pick here. But an item CARRYING
                    'Deferred as CS' must still render its own state — without the option
                    present the Select showed blank, and saving that blank sent the item
                    up as a plain Pending, losing the deferral on the screen. */}
                {(phase === 'CS'
                  ? ([...(['Pending', 'Completed'] as const),
                      ...(r.status === 'Deferred as CS' ? (['Deferred as CS'] as const) : [])])
                  : STATUSES)
                  .map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
              </TextField>
              <TextField size="small" select label="Required" value={r.required ? 'y' : 'n'}
                disabled={ro} onChange={(e) => set(i, { required: e.target.value === 'y' })}
                sx={{ width: 110 }}>
                <MenuItem value="y">Required</MenuItem>
                <MenuItem value="n">Optional</MenuItem>
              </TextField>
              <IconButton size="small" onClick={() => drop(i)}
                disabled={ro || items.length <= 1}
                sx={{ mt: 0.4 }}><DeleteOutlineIcon fontSize="small" /></IconButton>
            </Box>
            <Box sx={{ display: 'flex', gap: 1, mt: 1, flexWrap: 'wrap' }}>
              <TextField size="small" label="Evidence reference" value={r.evidence_ref}
                disabled={ro} onChange={(e) => set(i, { evidence_ref: e.target.value })}
                placeholder="Document or filing reference" sx={{ flex: '1 1 240px' }} />
              <TextField size="small"
                label={r.status === 'Waived' ? 'Why waived (required)'
                  : r.status === 'Deferred as CS' ? 'Why deferred (required)' : 'Reason / note'}
                required={r.status === 'Waived' || r.status === 'Deferred as CS'}
                value={r.reason} disabled={ro}
                onChange={(e) => set(i, { reason: e.target.value })}
                sx={{ flex: '2 1 300px' }} />
              {r.status === 'Deferred as CS' && (
                <TextField size="small" type="date" label="Satisfy by (required)" required
                  value={r.expiry_date} disabled={ro}
                  onChange={(e) => set(i, { expiry_date: e.target.value })}
                  InputLabelProps={{ shrink: true }} sx={{ width: 190 }} />
              )}
            </Box>
          </Box>
        ))}

        {!ro && (
          <Button size="small" startIcon={<AddIcon />} onClick={add}
            sx={{ textTransform: 'none' }}>Add a condition</Button>
        )}

        {!ro && (
          <TextField fullWidth multiline minRows={2} size="small" label="Note for the checker"
            value={note} onChange={(e) => setNote(e.target.value)} sx={{ mt: 1.4 }} />
        )}
        {savedMsg && <Alert severity="success" sx={{ mt: 1.2, py: 0, fontSize: 12 }}
          onClose={() => setSavedMsg('')}>{savedMsg}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>
          {ro ? 'Close' : 'Cancel'}
        </Button>
        {!ro && phase === 'CP' && (
          <Button onClick={() => void saveDraft()} variant="outlined" disabled={busy}>
            Save draft
          </Button>
        )}
        {!ro && (
          <Button onClick={submit} variant="contained" disabled={busy}>
            {busy ? (phase === 'CS' ? 'Saving…' : 'Sending…')
              : phase === 'CS' ? 'Save updates' : 'Send for checking'}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
