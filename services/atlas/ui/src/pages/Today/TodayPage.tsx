import { useState } from 'react';
import { Alert, Box, Paper, Typography, Button, Collapse } from '@mui/material';
import { useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../../auth/AuthContext';
import { viewAccess, can } from '../../auth/rbac';
import { getSession } from '../../auth/session';
import { useSearch } from '../../context/SearchContext';
import CompanyDrawer from '../Deals/CompanyDrawer';
import AddProductDialog from '../Deals/AddProductDialog';
import WorkflowDecisionDialog from './WorkflowDecisionDialog';
import BookingReviewDialog from './BookingReviewDialog';
import CovenantResultDialog from './CovenantResultDialog';
import { computeToday, park, unpark } from './compute';
import { stageRequestService, canApproveLine } from '../../services/stageRequestService';
import { workflowService, kindLabel, since, pendingKey, actionsFor,
  type PendingWorkflow, type DecisionAction } from '../../services/workflowService';
import { lmsService, type TrancheItem } from '../../services/lmsService';
import { notificationsService, type InboxItem } from '../../services/notificationsService';
import { clientsService } from '../../services/clientsService';
import { fmt } from '../../utils/format';
import ExportBar, { toCsv, saveCsv } from '../../components/common/ExportBar';
import type { AttnRow, ContactRow, DueRow } from './compute';
import { tokens } from '../../theme';

function Pill({ kind }: { kind: 'red' | 'amber' | 'due' | 'parked' }) {
  const map = {
    red: { bg: '#fde2e6', fg: '#b0223a', label: 'RED' },
    amber: { bg: '#fef3c7', fg: '#c46a16', label: 'AMBER' },
    due: { bg: '#fde2e6', fg: '#b0223a', label: 'DUE' },
    parked: { bg: '#EDF1F3', fg: tokens.muted, label: 'PARKED' },
  }[kind];
  return (
    <Box component="span" sx={{ px: '8px', py: '1px', borderRadius: '99px', fontSize: 10.5, fontWeight: 700, bgcolor: map.bg, color: map.fg, whiteSpace: 'nowrap' }}>{map.label}</Box>
  );
}

function ChLine({ children }: { children: React.ReactNode }) {
  return (
    <Paper variant="outlined" sx={{ borderColor: tokens.line, borderRadius: 2.75, p: '10px 13px', my: 0.9,
      display: 'flex', alignItems: 'center', gap: 1.25, flexWrap: 'wrap', boxShadow: '0 1px 2px rgba(15,30,44,.06)',
      transition: 'box-shadow .15s ease, transform .15s ease', '&:hover': { boxShadow: '0 4px 14px rgba(15,30,44,.1)', transform: 'translateY(-1px)' } }}>
      {children}
    </Paper>
  );
}

// v15: each Today section is a collapsible card — RED / due open, AMBER / parked
// collapsed by default. The count sits on the header; a ▸ arrow rotates open.
function Section({ title, count, defaultOpen = false, children }: {
  title: string; count: number; defaultOpen?: boolean; children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Box sx={{ mt: 2.75 }}>
      <Typography onClick={() => setOpen((o) => !o)}
        sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1, fontSize: 12.6, cursor: 'pointer', userSelect: 'none',
          textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.muted, fontWeight: 700,
          '&::before': { content: '""', width: 4, height: 15, borderRadius: '99px', background: `linear-gradient(180deg, ${tokens.tealHi}, ${tokens.teal})` } }}>
        <Box component="span" sx={{ fontSize: 11, transition: 'transform .15s ease', transform: open ? 'rotate(90deg)' : 'none' }}>▶</Box>
        {title} ({count})
      </Typography>
      <Collapse in={open} unmountOnExit>{children}</Collapse>
    </Box>
  );
}

export default function TodayPage() {
  const { user } = useAuth();
  const nav = useNavigate();
  const { setSearch } = useSearch();
  const qc = useQueryClient();
  // Today is 'scoped' for every IC + Head (own book / own team) and 'full' only for
  // Admin + Management — the scoped roles see just their own worklist.
  const mine = viewAccess(user.roles, 'today') === 'scoped';
  const person = mine ? user.name : undefined;
  const [, force] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [addProd, setAddProd] = useState<string | null>(null);
  const data = computeToday(person);
  const refresh = () => { qc.invalidateQueries(); force((n) => n + 1); };

  // Stage-change requests: approvers see pending items they can decide; requesters see
  // their own submissions awaiting approval.
  const pendReq = stageRequestService.pending();
  const reqActionable = pendReq.filter((r) => canApproveLine(user.roles, r.line));
  const reqMine = pendReq.filter((r) => r.by === user.full && !canApproveLine(user.roles, r.line));
  const decide = (id: string, ok: boolean) => { stageRequestService.decide(id, ok, user.full); refresh(); };
  const coName = (code: string) => clientsService.get(code).name || code;

  // Workflow plane: runs parked on a human decision (GET /v1/workflows/pending). This is
  // Temporal's queue, separate from the local stage-change requests above — it is polled
  // because the plane releases runs on its own (a decision timeout, another approver).
  const [wfDecide, setWfDecide] = useState<{ w: PendingWorkflow; verbs: DecisionAction[] } | null>(null);
  const [covRecord, setCovRecord] = useState<PendingWorkflow | null>(null);
  const [covFlash, setCovFlash] = useState('');
  const { data: wfPending = [], refetch: refetchWf } = useQuery({
    queryKey: ['workflows', 'pending'],
    queryFn: () => workflowService.pending(),
    enabled: workflowService.enabled(),
    refetchInterval: 60000,
    staleTime: 30000,
  });
  // BOOKING APPROVALS — the LMS gate's queue, straight from the register: every
  // human-recorded tranche waiting for LMS Management. Approval opens/grows the loan
  // account in the register's own transaction; this is the servicing checker's inbox,
  // so it lands on Today, not only on the LMS page.
  const canBook = can(user.roles, 'lmsAuthorize');
  const [bookBusy, setBookBusy] = useState('');
  const [bookErr, setBookErr] = useState('');
  const [bookFlash, setBookFlash] = useState('');
  const [bookView, setBookView] = useState<TrancheItem | null>(null);
  const { data: bookings = [], refetch: refetchBookings } = useQuery({
    queryKey: ['lms-pending-bookings', 'today'],
    queryFn: () => lmsService.pendingBookings().catch(() => [] as TrancheItem[]),
    enabled: canBook,
    refetchInterval: 60000,
    staleTime: 30000,
  });
  // DECISIONS ON YOUR WORK — the maker's answer to "how do I learn a checker
  // returned/rejected my item without watching the screen": every decision writes a
  // durable inbox row (in-transaction with the decision itself), and this strip reads
  // the unread ones. Marking one read clears it here; the record stays in the store.
  const { data: inbox = { items: [] as InboxItem[], unread: 0 }, refetch: refetchInbox }
    = useQuery({
      queryKey: ['inbox', 'unread'],
      queryFn: () => notificationsService.unread(),
      refetchInterval: 60000,
      staleTime: 30000,
    });
  const dismissInbox = async (n: InboxItem) => {
    await notificationsService.markRead(n.id);
    await refetchInbox();
  };
  const todayISO = new Date().toISOString().slice(0, 10);
  const overdueOf = (t: TrancheItem) =>
    (t.conditions_open ?? []).filter((c) => c.expiry_date && c.expiry_date < todayISO).length;
  const settleBooking = async (t: TrancheItem, action: 'approve' | 'reject', note?: string) => {
    setBookErr(''); setBookFlash(''); setBookBusy(t.id);
    try {
      await lmsService.book(t.lending_id, t.id, action, note);
      setBookFlash(action === 'approve'
        ? `${t.tranche_ref} booked — the loan account is updated (LMS · Servicing has the statement).`
        : `${t.tranche_ref} rejected — the recorder corrects and records afresh.`);
      setBookView(null);
      await refetchBookings(); refresh();
    } catch (e: any) { setBookErr(e?.message || String(e)); }
    setBookBusy('');
  };

  // Approvers act on any run that exposes a decision URL; everyone else only sees the
  // runs they raised, sitting with an approver — the same split as the requests above.
  const canDecideWf = can(user.roles, 'approveRequest');
  const myEmail = (getSession()?.email || '').toLowerCase();
  // Actionable = anything the plane offers a verb on. Filtering on decisionUrl alone
  // hid every item decided by approve/reject (lead conversions) and the whole checker
  // queue (CP/CS checklists, handover packages) — the approver saw a fraction of their
  // work. The plane already scopes this list to the caller's approver roles.
  const wfActionable = canDecideWf ? wfPending.filter((w) => actionsFor(w).length > 0) : [];
  // REMINDERS — the plane's standing chases (CS conditions outstanding, covenant cycles
  // due). No verbs: nothing to approve, only documents to collect and compliance to
  // record; they stay on Today until the underlying rows say the work landed.
  const REMINDER_KINDS: Record<string, string> = { 'cs-followup': 'CS chase', 'covenant-due': 'Covenant' };
  const wfReminders = wfPending.filter((w) => w.kind in REMINDER_KINDS);
  const wfMine = wfPending.filter((w) => !wfActionable.includes(w) && !wfReminders.includes(w)
    && w.requestedBy.toLowerCase() === myEmail);
  const wfDone = () => { refetchWf(); refresh(); };

  // Export the whole worklist (every section) as one CSV.
  const exportCsv = () => {
    const rows: (string | number)[][] = [];
    data.due.forEach((i) => rows.push(['Follow-up due', i.name, `${i.nextAction} — was due ${i.nextActionDate}`, i.code]));
    [...data.contactRed, ...data.contactAmber].forEach((a) => rows.push([`Contact ${a.sev}`, a.co, `no touch ${a.days}d · with ${a.owner}`, a.code]));
    [...data.stageRed, ...data.stageAmber].forEach((a) => rows.push([`Stage ${a.sev}`, a.isLead || data.nameOf(a.code), `${a.rule} · ${a.why} · with ${a.owner}`, a.code]));
    [...reqActionable, ...reqMine].forEach((r) => rows.push(['Stage-change request', coName(r.code), `${r.line}: ${r.currentStage || '—'} → ${r.targetStage}`, r.status]));
    [...wfActionable, ...wfMine].forEach((w) => rows.push(['Workflow approval', kindLabel(w.kind), `${w.stage} · raised by ${w.requestedBy} ${since(w.startedAt)}`, w.workflowId]));
    wfReminders.forEach((w) => rows.push(['Reminder', REMINDER_KINDS[w.kind] || kindLabel(w.kind), w.stage, w.subjectId]));
    data.snoozed.forEach((s) => rows.push(['Parked', s.label, `parked ${s.when}${s.by ? ' · by ' + s.by : ''}`, '']));
    saveCsv(toCsv(['Item', 'Name', 'Detail', 'Ref'], rows), 'atlas_today');
  };

  // Open the company drawer for a group code; for leads (no client yet) filter the Leads list.
  const openCode = (code: string) => setOpen(code);
  const goto = (label: string, isLead?: boolean) => { setSearch(label); nav(isLead ? '/leads' : '/deals'); };
  const doPark = (key: string, label: string) => { park(key, label, user.full); force((n) => n + 1); };
  const doResume = (key: string) => { unpark(key); force((n) => n + 1); };
  const name = (s: string, code?: string) => (
    <Box component="b" onClick={() => (code ? openCode(code) : goto(s))} sx={{ color: tokens.navy, cursor: 'pointer', '&:hover': { textDecoration: 'underline' } }}>{s}</Box>
  );
  const hint = (s: string) => <Typography component="span" sx={{ fontSize: 11.6, color: tokens.muted }}>{s}</Typography>;
  const parkBtn = (key: string, label: string) => (
    <Button size="small" onClick={() => doPark(key, label)} sx={{ fontSize: 11, color: tokens.muted, minWidth: 0 }}>⏸ Get back later</Button>
  );

  const dueRow = (i: DueRow) => (
    <ChLine key={i.id}><Pill kind="due" />{name(i.name, i.code)}{hint(`${i.nextAction} · was due ${i.nextActionDate}`)}
      <Box sx={{ flex: 1 }} />{parkBtn('int' + i.id, `${i.name} · ${i.nextAction}`)}</ChLine>
  );
  const contactRow = (a: ContactRow) => (
    <ChLine key={a.code}><Pill kind={a.sev} />{name(a.co, a.code)}{hint(`CONTACT · no human touch for ${a.days}d · with ${a.owner}`)}
      <Box sx={{ flex: 1 }} />{parkBtn('cs' + a.code, `${a.co} · contact gone quiet`)}</ChLine>
  );
  const stageRow = (a: AttnRow) => (
    <ChLine key={a.rule + a.code}><Pill kind={a.sev} />
      {a.isLead
        ? <Box component="b" onClick={() => goto(a.isLead!, true)} sx={{ color: tokens.navy, cursor: 'pointer', '&:hover': { textDecoration: 'underline' } }}>{a.isLead}</Box>
        : name(data.nameOf(a.code), a.code)}
      {hint(`STAGE · ${a.rule} · ${a.why} · with ${a.owner}`)}
      <Box sx={{ flex: 1 }} />{parkBtn(a.rule + a.code, `${a.isLead || data.nameOf(a.code)} · ${a.rule}`)}</ChLine>
  );

  const empty = !data.due.length && !data.contactRed.length && !data.contactAmber.length && !data.stageRed.length && !data.stageAmber.length
    && !wfActionable.length && !wfMine.length && !wfReminders.length && !bookings.length
    && !inbox.items.length;

  return (
    <Box sx={{ maxWidth: 900, mx: 'auto' }}>
      <Box sx={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 1, flexWrap: 'wrap', m: '4px 0' }}>
        <Typography sx={{ fontSize: 19, fontWeight: 700 }}>
          Good day, {user.name || 'there'} — {data.reds} red, {data.ambers} amber{mine ? ' on your book' : ''}
        </Typography>
        <ExportBar onCsv={exportCsv} />
      </Box>

      {bookErr && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setBookErr('')}>{bookErr}</Alert>}
      {bookFlash && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setBookFlash('')}>{bookFlash}</Alert>}

      {inbox.items.length > 0 && (
        <Section title="Decisions on your work" count={inbox.items.length} defaultOpen>
          {inbox.items.map((n) => (
            <ChLine key={n.id}>
              <Box component="span" aria-hidden sx={{ width: 8, height: 8, borderRadius: '50%',
                flexShrink: 0,
                bgcolor: n.severity === 'warning' ? '#c46a16'
                  : n.severity === 'critical' ? '#b0223a' : '#1B7F5C' }} />
              <Box component="b" sx={{ color: tokens.ink }}>{n.title}</Box>
              {n.body && hint(n.body)}
              {n.created_at && hint(since(n.created_at))}
              <Box sx={{ flex: 1 }} />
              <Button size="small" onClick={() => void dismissInbox(n)}
                sx={{ fontSize: 11, color: tokens.muted, minWidth: 0 }}>
                ✓ Read
              </Button>
            </ChLine>
          ))}
        </Section>
      )}

      {canBook && bookings.length > 0 && (
        <Section title="Booking approvals — LMS" count={bookings.length} defaultOpen>
          {bookings.map((t) => (
            <ChLine key={t.id}>
              <Box component="span" sx={{ px: '8px', py: '1px', borderRadius: '99px', fontSize: 10.5, fontWeight: 700, bgcolor: '#FFF3CD', color: '#7A5C00', whiteSpace: 'nowrap' }}>BOOKING</Box>
              <Box component="b" sx={{ color: tokens.ink }}>{t.borrower || t.lending_id.slice(0, 8)}</Box>
              {hint(`₹ ${fmt(t.amount)} Cr · ${t.tranche_ref}${t.disbursed_on ? ' · ' + t.disbursed_on : ''} · by ${t.recorded_by || '—'}`)}
              {(t.conditions_open?.length ?? 0) > 0 && hint(`${t.conditions_open!.length} condition${t.conditions_open!.length > 1 ? 's' : ''} open`)}
              {overdueOf(t) > 0 && (
                <Box component="span" sx={{ px: '8px', py: '1px', borderRadius: '99px', fontSize: 10.5, fontWeight: 700, bgcolor: '#fde2e6', color: '#b0223a', whiteSpace: 'nowrap' }}>
                  {overdueOf(t)} OVERDUE
                </Box>
              )}
              <Box sx={{ flex: 1 }} />
              <Button size="small" variant="contained" disabled={!!bookBusy}
                onClick={() => setBookView(t)} sx={{ minWidth: 0 }}>
                Review…
              </Button>
            </ChLine>
          ))}
        </Section>
      )}

      {(wfActionable.length > 0 || wfMine.length > 0) && (
        <Section title="Workflow approvals" count={wfActionable.length + wfMine.length} defaultOpen>
          {wfActionable.map((w) => {
            const verbs = actionsFor(w);
            return (
              <ChLine key={pendingKey(w)}>
                <Box component="span" sx={{ px: '8px', py: '1px', borderRadius: '99px', fontSize: 10.5, fontWeight: 700, bgcolor: '#EDE7F6', color: '#5B3FA8', whiteSpace: 'nowrap' }}>{kindLabel(w.kind)}</Box>
                <Box component="b" sx={{ color: tokens.ink }}>{w.stage || 'Awaiting a decision'}</Box>
                {hint(`${w.status} · by ${w.requestedBy} · ${since(w.startedAt)}`
                  + (w.checklistVersion ? ` · v${w.checklistVersion}` : ''))}
                <Box sx={{ flex: 1 }} />
                {/* One door in: REVIEW the content first — the dialog shows what is
                    being decided and carries the whole triad (approve / return /
                    reject), only the verbs this item actually offers. */}
                <Button size="small" variant="contained"
                  onClick={() => setWfDecide({ w, verbs })} sx={{ minWidth: 0 }}>
                  Review…
                </Button>
              </ChLine>
            );
          })}
          {wfMine.map((w) => (
            <ChLine key={pendingKey(w)}>
              <Box component="span" sx={{ px: '8px', py: '1px', borderRadius: '99px', fontSize: 10.5, fontWeight: 700, bgcolor: '#EDF1F3', color: tokens.muted, whiteSpace: 'nowrap' }}>{kindLabel(w.kind)}</Box>
              <Box component="b" sx={{ color: tokens.ink }}>{w.stage || 'Awaiting a decision'}</Box>
              {hint(`raised ${since(w.startedAt)} · awaiting a decision`)}
            </ChLine>
          ))}
        </Section>
      )}

      {covFlash && (
        <Alert severity={covFlash.includes('BREACHED') ? 'warning' : 'success'}
          sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setCovFlash('')}>{covFlash}</Alert>
      )}
      {wfReminders.length > 0 && (
        <Section title="Chase & monitor" count={wfReminders.length} defaultOpen>
          {wfReminders.map((w, i) => (
            <ChLine key={pendingKey(w) + (w.monitoringId || i)}>
              <Box component="span" sx={{ px: '8px', py: '1px', borderRadius: '99px', fontSize: 10.5, fontWeight: 700, bgcolor: '#FFF3E0', color: '#9A6A00', whiteSpace: 'nowrap' }}>{REMINDER_KINDS[w.kind]}</Box>
              <Box component="b" sx={{ color: tokens.ink }}>{w.stage}</Box>
              {hint(w.requestedBy ? `owner ${w.requestedBy}` : 'standing reminder — clears when the work lands')}
              <Box sx={{ flex: 1 }} />
              {/* The call happened, the documents arrived — close THIS period here. */}
              {w.kind === 'covenant-due' && w.monitoringId && (
                <Button size="small" variant="outlined" onClick={() => setCovRecord(w)} sx={{ minWidth: 0 }}>
                  Record received
                </Button>
              )}
            </ChLine>
          ))}
        </Section>
      )}

      {(reqActionable.length > 0 || reqMine.length > 0) && (
        <Section title="Stage-change requests" count={reqActionable.length + reqMine.length} defaultOpen>
          {reqActionable.map((r) => (
            <ChLine key={r.id}>
              <Box component="span" sx={{ px: '8px', py: '1px', borderRadius: '99px', fontSize: 10.5, fontWeight: 700, bgcolor: '#E7EEF9', color: '#33518f', whiteSpace: 'nowrap' }}>{r.line}</Box>
              {name(coName(r.code), r.code)}{hint(`${r.currentStage || '—'} → ${r.targetStage} · ${r.reason} · by ${r.by}`)}
              <Box sx={{ flex: 1 }} />
              <Button size="small" variant="contained" onClick={() => decide(r.id, true)} sx={{ minWidth: 0 }}>Approve</Button>
              <Button size="small" color="error" onClick={() => decide(r.id, false)} sx={{ minWidth: 0 }}>Reject</Button>
            </ChLine>
          ))}
          {reqMine.map((r) => (
            <ChLine key={r.id}>
              <Box component="span" sx={{ px: '8px', py: '1px', borderRadius: '99px', fontSize: 10.5, fontWeight: 700, bgcolor: '#EDF1F3', color: tokens.muted, whiteSpace: 'nowrap' }}>{r.line}</Box>
              {name(coName(r.code), r.code)}{hint(`${r.currentStage || '—'} → ${r.targetStage} · awaiting approval`)}
            </ChLine>
          ))}
        </Section>
      )}
      {!!data.due.length && <Section title="Follow-ups due" count={data.due.length} defaultOpen>{data.due.map(dueRow)}</Section>}
      {!!data.contactRed.length && <Section title="Contact staleness — RED" count={data.contactRed.length} defaultOpen>{data.contactRed.map(contactRow)}</Section>}
      {!!data.stageRed.length && <Section title="Stage bottlenecks — RED" count={data.stageRed.length} defaultOpen>{data.stageRed.map(stageRow)}</Section>}
      {!!data.contactAmber.length && <Section title="Contact staleness — AMBER" count={data.contactAmber.length}>{data.contactAmber.slice(0, 20).map(contactRow)}</Section>}
      {!!data.stageAmber.length && <Section title="Stage bottlenecks — AMBER" count={data.stageAmber.length}>{data.stageAmber.slice(0, 20).map(stageRow)}</Section>}

      {empty && (
        <Paper variant="outlined" sx={{ borderStyle: 'dashed', borderColor: tokens.line, borderRadius: 3.5, p: '42px 26px', textAlign: 'center', mt: 2 }}>
          <Typography sx={{ fontSize: 26, color: tokens.tealHi, mb: 1 }}>☕</Typography>
          <Typography sx={{ fontSize: 13.6, color: tokens.muted }}>Clean slate. Nothing needs you today.</Typography>
        </Paper>
      )}

      {!!data.snoozed.length && (
        <Section title="⏸ Will get back to these" count={data.snoozed.length}>
          {data.snoozed.map((s) => (
            <ChLine key={s.key}><Pill kind="parked" />
              <Box component="b" sx={{ color: tokens.ink }}>{s.label}</Box>
              {hint(`parked ${s.when}${s.by ? ` · by ${s.by}` : ''}`)}
              <Box sx={{ flex: 1 }} />
              <Button size="small" onClick={() => doResume(s.key)} sx={{ fontSize: 11, color: tokens.teal, minWidth: 0 }}>▶ Resume</Button>
            </ChLine>
          ))}
        </Section>
      )}

      <WorkflowDecisionDialog w={wfDecide?.w ?? null} verbs={wfDecide?.verbs ?? []} onClose={() => setWfDecide(null)} onDone={wfDone} />
      <BookingReviewDialog t={bookView} busy={!!bookBusy}
        onClose={() => setBookView(null)}
        onDecide={(t, action, note) => void settleBooking(t, action, note)} />
      <CovenantResultDialog w={covRecord} onClose={() => setCovRecord(null)}
        onDone={(m) => { setCovFlash(m); wfDone(); }} />
      <CompanyDrawer code={open} onClose={() => setOpen(null)} onChanged={refresh} onAddProduct={(c) => setAddProd(c)} />
      <AddProductDialog code={addProd} onClose={() => setAddProd(null)} onDone={refresh} />
    </Box>
  );
}
