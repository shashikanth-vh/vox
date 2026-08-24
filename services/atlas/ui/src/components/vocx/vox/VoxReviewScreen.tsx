/**
 * Report / review, Atlas resolve, Approved, Processing and Failed — the
 * blueprint's screens with the blueprint's markup, driven by the registry and
 * the real conversation row.
 *
 * Behavior kept from the build so far: fields render from the registry (zero
 * hard-coded blocks), flagged fields are OPTIONAL (approve warns once, never
 * blocks — owner decision), edits autosave through the atomic edit path (the
 * header's "Saved" tick is real), company resolve never merges silently, a new
 * company becomes a Leads-register row with an RM, and approval files the
 * timeline interaction against the entity or the lead.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import vocxClient from '../../../api/vocxClient';
import { api } from '../../../api/http';
import { useAuth } from '../../../auth/AuthContext';
import { getSession } from '../../../auth/session';
import { referenceService } from '../../../services/referenceService';
import { needsYou, voxService } from '../../../services/voxService';
import type { VoxCell, VoxConversation, VoxRegistry, VoxReport } from '../../../services/voxService';
import { Ic } from './VoxApp';

type SubView = 'auto' | 'atlas' | 'submitted';

const dotCls = (conf?: string) =>
  conf === 'high' ? 'hi' : conf === 'medium' ? 'md' : conf === 'low' ? 'lo' : 'na';

const mmss = (s?: number | null) => s == null ? ''
  : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

/** Chip labels the way the blueprint prints them: "entire_project" reads
 *  "Entire project", and the acronyms stay acronyms ("ppa" -> "PPA"). Free-text
 *  additions pass through as typed. */
const ACRONYMS = new Set(['ppa', 'epc', 'spv', 'ipp', 'ev', 'bess', 'lc', 'nbfc']);
const chipLabel = (v: string) => v.split('_').map((w, i) =>
  ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase()
    : i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w).join(' ');

interface Candidate { name: string; code?: string; entity_id?: string; kind?: string; meta?: string }

export default function VoxReviewScreen({ conversationId, onBack, onQueue, onDossier, onSaved, onFiled }: {
  conversationId: string;
  onBack: () => void;
  onQueue: () => void;
  onDossier: (subject: { entityId?: string; leadId?: string }) => void;
  onSaved: (v: boolean) => void;
  onFiled: () => void;
}) {
  const { user } = useAuth();
  const [row, setRow] = useState<VoxConversation | null>(null);
  const [registry, setRegistry] = useState<VoxRegistry | null>(null);
  const [report, setReport] = useState<VoxReport | null>(null);
  const [dirty, setDirty] = useState<Record<string, VoxCell>>({});
  const [confirmed, setConfirmed] = useState<Set<string>>(new Set());
  const [sub, setSub] = useState<SubView>('auto');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [entityName, setEntityName] = useState('');
  const [leadName, setLeadName] = useState('');
  const [leadRow, setLeadRow] = useState<any>(null);
  const [leads, setLeads] = useState<any[]>([]);
  const [showMore, setShowMore] = useState(false);
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const [ucSheet, setUcSheet] = useState<'' | 'add' | string>('');
  const [overflow, setOverflow] = useState(false);
  /** An approved record opens read-only; Edit report unlocks it — every change
   *  still travels the atomic edit path and lands in the audit trail. */
  const [editUnlocked, setEditUnlocked] = useState(false);
  const [toast, setToast] = useState('');
  // atlas view state
  const [resolveQ, setResolveQ] = useState('');
  const [cands, setCands] = useState<Candidate[]>([]);
  const [selected, setSelected] = useState<Candidate | null>(null);
  const [creating, setCreating] = useState(false);
  const [newLeadName, setNewLeadName] = useState('');
  const [newLeadRm, setNewLeadRm] = useState('');
  /** A company can carry several open leads and live deals at once — after the
   *  company is chosen, the recording still has to say WHICH line it is about. */
  const [lineChoice, setLineChoice] =
    useState<{ entityId: string; name: string; leads: any[]; deals: any[] } | null>(null);
  const [dealRow, setDealRow] = useState<any>(null);
  // transcript correction: the fix is written here, the original never changes
  const [fixingTranscript, setFixingTranscript] = useState(false);
  const [fixDraft, setFixDraft] = useState('');
  /** Approve tapped with nothing linked: the link screen opens first, and a
   *  successful pin resumes the approval automatically. */
  const [linkThenApprove, setLinkThenApprove] = useState(false);
  /** The follow-up card's outcome line ("On your calendar", auth guidance…). */
  const [fuMsg, setFuMsg] = useState('');
  /** Success collapses the card's actions to Done — a second Add would create
   *  a DUPLICATE calendar event (Google's insert is not idempotent). */
  const [fuDone, setFuDone] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  const say = (m: string) => { setToast(m); setTimeout(() => setToast(''), 3500); };

  const refresh = useCallback(async () => {
    try {
      const r = await voxService.get(conversationId);
      setRow(r);
      setReport((prev) => prev ?? (r.structured_report as VoxReport) ?? null);
      return r;
    } catch (e: any) { setErr(String(e?.message || e)); return null; }
  }, [conversationId]);

  const startPolling = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      const r = await refresh();
      if (r && ['ready', 'submitted', 'failed_permanently'].includes(r.status) && pollRef.current) {
        clearInterval(pollRef.current); pollRef.current = null;
      }
    }, 2500);
  }, [refresh]);

  useEffect(() => { void voxService.spec().then((s) => setRegistry(s.registry)); }, []);
  useEffect(() => {
    void refresh(); startPolling();
    return () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
  }, [refresh, startPolling]);

  useEffect(() => {
    if (!row?.entity_id) { setEntityName(''); setLeads([]); return; }
    void api.get<any>(`/entities/${row.entity_id}`)
      .then((e) => setEntityName(e.display_name || e.legal_name || e.code || '')).catch(() => {});
    void api.get<any>('/leads', { entity_id: row.entity_id, limit: 20 })
      .then((d) => setLeads((d.items || []).filter((l: any) => !l.converted_deal_id))).catch(() => {});
  }, [row?.entity_id]);
  useEffect(() => {
    if (!row?.lead_id) { setLeadName(''); setLeadRow(null); return; }
    void api.get<any>(`/leads/${row.lead_id}`)
      .then((l) => { setLeadName(l.company || ''); setLeadRow(l); }).catch(() => {});
  }, [row?.lead_id]);
  useEffect(() => {
    if (!row?.deal_id) { setDealRow(null); return; }
    void api.get<any>(`/deals/${row.deal_id}`).then(setDealRow).catch(() => {});
  }, [row?.deal_id]);

  // atlas typeahead
  useEffect(() => {
    const q = resolveQ.trim();
    if (q.length < 2) { setCands([]); return; }
    const t = setTimeout(async () => {
      try {
        const r = await vocxClient.get('/v1/suggest', { params: { q, limit: 8 } });
        const raw = r.data?.matches || [];
        // Companies AND their open leads: a recording can belong to either, so
        // both are offered — a lead row links straight to that lead.
        setCands(raw.map((c: any) => ({
          name: c.name, code: c.code, entity_id: c.entity_id, kind: c.kind,
          meta: [c.kind === 'lead' ? 'Lead' : c.code, c.rm && `RM ${c.rm}`,
                 c.sector, c.temperature, c.lens]
            .filter(Boolean).join(' · ') })));
      } catch { setCands([]); }
    }, 250);
    return () => clearTimeout(t);
  }, [resolveQ]);

  const saveEdits = useCallback(async (extra: Record<string, any> = {}) => {
    const pending = dirtyRef.current;
    const edits = Object.entries(pending).map(([field_path, new_value]) => ({ field_path, new_value }));
    if (!edits.length && !Object.keys(extra).length) return null;
    const updated = await voxService.edits(conversationId, { edits, ...extra });
    setDirty({});
    setRow(updated);
    setReport(updated.structured_report as VoxReport);
    onSaved(true);
    return updated;
  }, [conversationId, onSaved]);

  // debounced autosave — the header's "Saved" tick is a real statement
  const onCell = (path: string, cell: VoxCell) => {
    setDirty((d) => ({ ...d, [path]: cell }));
    setConfirmed((c) => new Set(c).add(path));
    setReport((r) => {
      if (!r) return r;
      const [blockKey, fieldKey] = path.split('.');
      return { ...r, [blockKey]: { ...(r as any)[blockKey], [fieldKey]: cell } };
    });
    onSaved(false);
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      void saveEdits().catch((e) => setErr(String(e?.message || e)));
    }, 1500);
  };

  const strip = useMemo(() => (registry && report ? needsYou(registry, report) : [])
    .filter((n) => !confirmed.has(n.fieldPath)), [registry, report, confirmed]);
  const approvedRow = row?.status === 'submitted';
  const readOnly = (approvedRow && !editUnlocked) || !!row?.erased_at;
  const common = (report?.common || {}) as Record<string, VoxCell>;

  const setUseCases = (next: string[]) => {
    if (!next.length) return;
    void voxService.edits(conversationId, { use_cases: next })
      .then((u) => { setRow(u); setReport(u.structured_report as VoxReport); onSaved(true); })
      .catch((e) => setErr(String(e?.message || e)));
  };

  const closeAtlas = () => {
    setSub('auto'); setResolveQ(''); setCands([]); setSelected(null); setLineChoice(null);
  };

  /** Pin the recording to a specific line — or to the company alone. */
  const pinTo = async (pins: Record<string, string>) => {
    setBusy(true); setErr('');
    try {
      const updated = await saveEdits({ proposed_lead_company: '', proposed_lead_rm: '', ...pins });
      if (!updated) return;
      // Heal the chain: a standalone lead pinned under a chosen company gets the
      // company stamped onto the LEAD row too, so its interactions roll up to
      // the company timeline from now on — for everything, not just VOX.
      if (pins.lead_id && pins.entity_id) {
        try {
          const l = await api.get<any>(`/leads/${pins.lead_id}`);
          if (!l.entity_id) await api.patch(`/leads/${pins.lead_id}`, { entity_id: pins.entity_id });
        } catch { /* best-effort data heal */ }
      }
      // A pin added to an approved record that never filed its interaction
      // files it now — post-approval linking completes the record.
      if (updated.status === 'submitted' && !updated.interaction_id) {
        await fileTouchpoint(updated);
      }
      closeAtlas();
      if (linkThenApprove) { setLinkThenApprove(false); void approve({ unlinked: true }); }
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  const linkEntity = async (c: Candidate) => {
    setBusy(true); setErr('');
    try {
      // A lead candidate links straight to that lead.
      if (c.kind === 'lead' && c.code) {
        await pinTo({ lead_id: c.code, deal_id: '', entity_id: '' });
        return;
      }
      let entityId = c.entity_id;
      if (!entityId && c.code) {
        const found = await api.get<any>('/entities', { code: c.code, limit: 1 });
        entityId = found?.items?.[0]?.id;
      }
      if (!entityId) throw new Error('That candidate has no register id yet — create it as a new lead.');
      // Survey the company's open lines: several leads and live deals can exist
      // at once, and the recording has to say which one it belongs to.
      const [byEntity, byName, ds] = await Promise.all([
        api.get<any>('/leads', { entity_id: entityId, status: 'Active', limit: 50 }).catch(() => null),
        api.get<any>('/leads', { company: c.name, status: 'Active', limit: 50 }).catch(() => null),
        api.get<any>('/deals', { entity_id: entityId, limit: 50 }).catch(() => null),
      ]);
      const seen = new Set<string>();
      const leads = [...(byEntity?.items || []), ...(byName?.items || [])]
        .filter((l: any) => !l.converted_deal_id)
        .filter((l: any) => !seen.has(String(l.id)) && seen.add(String(l.id)));
      const deals = ds?.items || [];
      if (!leads.length && !deals.length) {
        await pinTo({ entity_id: entityId, lead_id: '', deal_id: '' });
        return;
      }
      setLineChoice({ entityId, name: c.name, leads, deals });
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  /** "Create new lead" records the INTENT on the conversation — the register
   *  gains the lead when the report is approved, never on this tap. */
  const proposeLead = async () => {
    const name = newLeadName.trim();
    if (!name) { setErr('The new lead needs the company name.'); return; }
    setBusy(true); setErr('');
    try {
      await saveEdits({
        proposed_lead_company: name, proposed_lead_rm: newLeadRm || '',
        lead_id: '', deal_id: '',
        ...(lineChoice ? { entity_id: lineChoice.entityId } : { entity_id: '' }),
      });
      setCreating(false);
      closeAtlas();
      if (linkThenApprove) { setLinkThenApprove(false); void approve({ unlinked: true }); }
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  /** Save the corrected transcript, then send the conversation back through the
   *  structuring stage only — no re-transcription, original preserved, the
   *  reviewer's overridden cells re-applied on the far side. */
  const correctAndRegenerate = async () => {
    setBusy(true); setErr('');
    try {
      await saveEdits({ corrected_transcript: fixDraft });
      await api.post(`/vox/conversations/${conversationId}/regenerate`, {});
      await voxService.process(conversationId);
      setFixingTranscript(false);
      // Drop the LOCAL report copy: polling deliberately never overwrites a
      // non-null report (it would clobber in-flight edits), so the regenerated
      // one can only land after the stale copy is let go. Field bug: the old
      // report stayed on screen until a tab switch remounted the view.
      setReport(null);
      setDirty({});
      setConfirmed(new Set());
      await refresh();
      startPolling();
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  const deleteDraft = async () => {
    if (!window.confirm('Delete this conversation? The recording, transcript and report '
      + 'are removed for everyone. This cannot be undone.')) return;
    setBusy(true); setErr('');
    try {
      await voxService.erase(conversationId);
      setOverflow(false);
      onBack();
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  /** File the timeline interaction against the MOST SPECIFIC line pinned on
   *  the row — a deal beats a lead beats the company timeline. Used at approve
   *  time, and again when a pin is added to an already-approved record that
   *  never got its interaction. Idempotent by capture_id. */
  const fileTouchpoint = useCallback(async (r: VoxConversation) => {
    const subject = r.deal_id
      ? { subject_type: 'Deal', subject_id: r.deal_id }
      : r.lead_id
        ? { subject_type: 'Lead', subject_id: r.lead_id }
        : r.entity_id
          ? { subject_type: 'Entity', subject_id: r.entity_id } : null;
    if (!subject) return;
    try {
      // approve/edits responses can be COMPACT rows (no structured_report) —
      // the in-memory report is the same document, so it backs them up here;
      // reading only from the response silently filed lane-less interactions.
      const srep = (r.structured_report ?? report) as VoxReport | null;
      const c = (srep?.common || {}) as Record<string, any>;
      const kdp = (c.key_discussion_points?.value as string[]) || [];
      // the lanes the reviewer selected ride on the interaction, so every
      // timeline row says which business it belongs to
      const lanes = (srep?.detected_use_cases || r.use_cases || []) as string[];
      const tp = await vocxClient.post('/v1/touchpoints', {
        ...subject, interaction_type: 'VOX conversation',
        summary: ((c.meeting_summary?.value as string) || kdp[0] || 'VOX conversation').slice(0, 300),
        key_intel: (kdp.length || lanes.length)
          ? { ...(kdp.length ? { points: kdp } : {}),
              ...(lanes.length ? { use_cases: lanes } : {}) }
          : undefined,
        transcript: r.raw_transcript || row?.raw_transcript || undefined,
        performed_by: user.full, capture_id: `vox-conv:${conversationId}`,
      });
      const iid = tp.data?.interaction_id || tp.data?.id;
      if (iid) await voxService.edits(conversationId, { interaction_id: String(iid) });
    } catch { /* the conversation row is the durable record */ }
  }, [conversationId, user.full, report, row]);

  const approve = async (opts: { unlinked?: boolean } = {}) => {
    // Record-and-tap-approve with nothing linked used to file NOWHERE while the
    // success screen claimed a timeline. Now the link screen opens first, with
    // an explicit memory-only escape — never a silently unfiled record.
    if (!opts.unlinked && row
        && !row.entity_id && !row.lead_id && !row.deal_id && !row.proposed_lead_company) {
      setResolveQ(row.entity_candidates?.[0] || '');
      setLinkThenApprove(true);
      setSub('atlas');
      return;
    }
    if (strip.length > 0 && !window.confirm(
      `${strip.length} flagged field${strip.length > 1 ? 's are' : ' is'} still unreviewed — approve anyway?`)) return;
    setBusy(true); setErr('');
    try {
      await saveEdits();
      // The approve response is the truth: a proposed new lead materialises
      // register-side during approve, so lead_id can be born right here.
      const approvedRow = await voxService.approve(conversationId);
      setRow(approvedRow);
      await fileTouchpoint(approvedRow);
      setLinkThenApprove(false);
      setSub('submitted');
      onFiled();
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  /** The mock's "Add to Calendar", for real: create the event on the RM's
   *  connected Google Calendar with the 1-day-before reminder. Not connected is
   *  a state, not an error — the auth tab opens and the .ics still downloads so
   *  the follow-up is never lost. */
  const addToCalendar = async () => {
    const when = (common.follow_up_date as any)?.value;
    if (!when) return;
    setBusy(true); setFuMsg('');
    try {
      const r = await vocxClient.post('/v1/vox/follow_up', {
        rm: user.full,
        title: ((common.next_steps as any)?.value as string) || `Follow-up — ${entityName || leadName || 'VOX'}`,
        date: when,
        time: ((common.follow_up_time as any)?.value as string) || '',
        description: ((common.meeting_summary as any)?.value as string) || '',
      });
      if (r.data?.ok) {
        setFuMsg('On your calendar · reminder set 1 day before');
        setFuDone(true);
        // the job is done — show the confirmation for a beat, then leave on
        // our own (field feedback: "I had to exit manually")
        setTimeout(onBack, 2000);
      } else if (r.data?.needs_auth) {
        window.open(`${vocxClient.defaults.baseURL}${r.data.auth_url}`, '_blank');
        downloadIcs();
        setFuMsg('Connect Google in the new tab — the .ics downloaded meanwhile, so nothing is lost.');
      } else {
        downloadIcs();
        setFuMsg('Calendar unavailable — the .ics downloaded instead.');
      }
    } catch {
      downloadIcs();
      setFuMsg('Calendar unavailable — the .ics downloaded instead.');
    } finally { setBusy(false); }
  };

  const downloadIcs = () => {
    const when = (common.follow_up_date as any)?.value;
    if (!when) return;
    const title = `Follow-up — ${entityName || leadName || 'VOX conversation'}`;
    const d = when.replace(/-/g, '');
    const hm = (((common.follow_up_time as any)?.value as string) || '').match(/^(\d{1,2}):(\d{2})$/);
    const dtstart = hm
      ? `DTSTART:${d}T${hm[1].padStart(2, '0')}${hm[2]}00`
      : `DTSTART;VALUE=DATE:${d}`;
    const ics = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//EVAM//VOX//EN', 'BEGIN:VEVENT',
      `UID:vox-${conversationId}@evam`, dtstart,
      `SUMMARY:${title}`, `DESCRIPTION:${((common.next_steps as any)?.value || '').replace(/\n/g, ' ')}`,
      // the card promises "Reminder · 1 day before" — the .ics keeps that promise too
      'BEGIN:VALARM', 'TRIGGER:-P1D', 'ACTION:DISPLAY', `DESCRIPTION:${title}`, 'END:VALARM',
      'END:VEVENT', 'END:VCALENDAR'].join('\r\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([ics], { type: 'text/calendar' }));
    a.download = 'vox-follow-up.ics';
    a.click();
  };

  if (!row || !registry) {
    return <div className="app-body no-tabs"><div className="proc-stage"><div className="proc-title">Loading…</div></div></div>;
  }

  /* ------------------------------------------------------ processing / failed */
  if (sub === 'auto' && ['queued', 'uploading', 'processing'].includes(row.status)) {
    const STAGES = [
      ['Uploaded', 'Segments assembled & stored'],
      ['Transcribed', row.language_detected ? `${row.language_detected.toUpperCase()} detected` : 'Speech to verbatim text'],
      ['Writing the report', 'Detecting use cases & fields'],
      ['Matching company', 'Register · RM / analyst lookup'],
      ['Ready for review', 'Confidence-scored draft'],
    ];
    const idx: Record<string, number> = { uploaded: 1, transcribed: 2, structured: 3, matched: 4, ready: 5 };
    const done = idx[row.processing_stage || ''] ?? (row.status === 'processing' ? 1 : 0);
    return (
      <div className="app-body no-tabs">
        <div className="proc-stage">
          <div className="proc-title">Making sense of it</div>
          <div className="proc-sub">You can close this · it continues on the server</div>
          <div className="proc-steps">
            {STAGES.map(([name, meta], i) => (
              <div key={name} className={`proc-step${i < done ? ' done' : i === done ? ' active' : ''}`}>
                <div className="proc-icon">
                  {i < done ? <Ic i="i-check" /> : i === done ? <div className="proc-spinner" /> : <span className="proc-pending" />}
                </div>
                <div className="proc-txt"><div className="name">{name}</div><div className="meta">{meta}</div></div>
              </div>
            ))}
          </div>
          <div style={{ padding: '0 4px' }}>
            <button className="btn btn-ghost" onClick={onBack}>Close — notify me in Memory</button>
          </div>
        </div>
      </div>
    );
  }

  if (sub === 'auto' && (row.status === 'processing_failed' || row.status === 'failed_permanently')) {
    const permanent = row.status === 'failed_permanently';
    return (
      <div className="app-body no-tabs">
        <div className="proc-stage">
          <div className="proc-title">Couldn't finish</div>
          <div className="proc-sub">Your recording is safe — nothing is lost</div>
          <div className="proc-steps">
            <div className="proc-step done"><div className="proc-icon"><Ic i="i-check" /></div>
              <div className="proc-txt"><div className="name">Uploaded</div><div className="meta">Audio stored</div></div></div>
            <div className={`proc-step ${row.raw_transcript ? 'done' : 'failed'}`}>
              <div className="proc-icon">{row.raw_transcript ? <Ic i="i-check" /> : '✕'}</div>
              <div className="proc-txt"><div className="name">Transcribed</div>
                <div className="meta">{row.raw_transcript ? 'Verbatim text stored' : 'Did not complete'}</div></div></div>
            <div className="proc-step failed"><div className="proc-icon">✕</div>
              <div className="proc-txt"><div className="name">Writing the report failed</div>
                <div className="meta">{(row.processing_error || '').slice(0, 70)} · attempt {row.retry_count || 0} of 5</div></div></div>
          </div>
          <div className="proc-fail-note">
            <strong>The audio and transcript are saved.</strong> {permanent
              ? 'Five retries are spent — an admin has been alerted; a retry from here is still allowed.'
              : "We'll keep retrying in the background, or you can retry now. This conversation is waiting in your Queue."}
          </div>
          <div style={{ padding: '0 4px', display: 'flex', flexDirection: 'column', gap: 8 }}>
            <button className="btn btn-primary" disabled={busy} onClick={async () => {
              setBusy(true);
              try { await voxService.process(conversationId); await refresh(); startPolling(); } finally { setBusy(false); }
            }}><Ic i="i-refresh" /> Retry now</button>
            <button className="btn btn-ghost" onClick={onQueue}>Leave it — go to Queue</button>
            {/* a take the recorder gives up on is deletable right here — the
                review's overflow menu never renders while processing fails */}
            <button className="btn btn-ghost" style={{ color: 'var(--danger)' }}
              onClick={() => void deleteDraft()}>Discard recording</button>
          </div>
        </div>
      </div>
    );
  }

  /* ------------------------------------------------------------------- atlas */
  if (sub === 'atlas') {
    const rms = referenceService.getRefSync('RM');
    const rmLabels = referenceService.getRefLabels('RM') || {};
    return (
      <div className="app-body no-tabs" style={{ padding: '12px 20px 20px' }}>
        <button className="review-back" onClick={() => { setLinkThenApprove(false); setSub('auto'); }}>‹ Back to report</button>
        <div className="atlas-header">
          <div className="eyebrow">Register · Entity resolution</div>
          <div style={{ fontSize: 26, fontWeight: 700, letterSpacing: '-0.02em', marginBottom: 6 }}>Which one?</div>
          <div style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.5 }}>
            Search the register, pick the right company — VOX never merges silently.
          </div>
        </div>
        {linkThenApprove && (
          <div className="atlas-cand" style={{ borderColor: 'var(--warn)', marginBottom: 12 }}>
            <span className="as-heard">Approval is waiting on this</span>
            Pick where this conversation files — it approves right after. Or approve it
            to the firm's memory only:
            <button className="btn btn-ghost btn-sm" style={{ marginTop: 10 }} disabled={busy}
              onClick={() => void approve({ unlinked: true })}>Approve without linking</button>
          </div>
        )}
        {(row.entity_candidates || []).length > 0 && (
          <div className="atlas-cand"><span className="as-heard">As heard in the recording</span>
            {(row.entity_candidates || []).map((c) => `"${c}"`).join(' · ')}</div>
        )}
        {lineChoice ? (
          /* The company is chosen — now WHICH of its lines is this recording about? */
          <>
            <div className="atlas-cand" style={{ marginBottom: 12 }}>
              <span className="as-heard">{lineChoice.name}</span>
              {linkThenApprove
                ? 'Belongs to one of these lines? Pick it — otherwise continue and it files on the company timeline.'
                : 'This company has open lines. Pick the one this conversation belongs to.'}
            </div>
            {linkThenApprove ? (
              <button className="btn btn-primary" disabled={busy} style={{ marginBottom: 12 }}
                onClick={() => void pinTo({ entity_id: lineChoice.entityId, lead_id: '', deal_id: '' })}>
                Continue — file at company level</button>
            ) : (
              <div className="atlas-opt" onClick={() => !busy && void pinTo(
                { entity_id: lineChoice.entityId, lead_id: '', deal_id: '' })}>
                <div>
                  <div className="ao-name">Company level</div>
                  <div className="ao-meta">General relationship — files on the company timeline</div>
                </div>
                <div className="ao-score possible">Pick</div>
              </div>
            )}
            {lineChoice.leads.map((l: any) => (
              <div key={l.id} className="atlas-opt" onClick={() => !busy && void pinTo(
                { lead_id: String(l.id), entity_id: lineChoice.entityId, deal_id: '' })}>
                <div>
                  <div className="ao-name">Lead {l.lead_no || ''}</div>
                  <div className="ao-meta">{[l.rm && `RM ${l.rm}`, l.temperature, l.status]
                    .filter(Boolean).join(' · ')}</div>
                </div>
                <div className="ao-score possible">Pick</div>
              </div>
            ))}
            {lineChoice.deals.map((d: any) => (
              <div key={d.id} className="atlas-opt" onClick={() => !busy && void pinTo(
                { deal_id: String(d.id), entity_id: lineChoice.entityId, lead_id: '' })}>
                <div>
                  <div className="ao-name">Deal {d.deal_no || d.code || ''}</div>
                  <div className="ao-meta">{[d.product_type, d.stage, d.rm && `RM ${d.rm}`]
                    .filter(Boolean).join(' · ')}</div>
                </div>
                <div className="ao-score possible">Pick</div>
              </div>
            ))}
            <button className="atlas-create" onClick={() => {
              setCreating(true);
              setNewLeadName(lineChoice.name);
              setNewLeadRm(user.full);
            }}><Ic i="i-plus" /> None of these — <strong>&nbsp;new lead for this company</strong></button>
            {creating && (
              <div className="card" style={{ marginTop: 12 }}>
                <div className="card-eyebrow">New lead — created when you approve</div>
                <div className="form-row"><div className="fr-label">Company name</div>
                  <input className="input-field" value={newLeadName}
                    onChange={(e) => setNewLeadName(e.target.value)} /></div>
                <div className="form-row"><div className="fr-label">Relationship Manager</div>
                  <select className="select-field" value={newLeadRm}
                    onChange={(e) => setNewLeadRm(e.target.value)}>
                    <option value="">RM — unassigned</option>
                    {rms.map((o) => <option key={o} value={o}>{rmLabels[o] || o}</option>)}
                    {user.full && !rms.includes(user.full) && <option value={user.full}>{user.full}</option>}
                  </select></div>
                <button className="btn btn-primary" disabled={busy} onClick={() => void proposeLead()}>
                  Pin as new lead — created on approve</button>
              </div>
            )}
            <div style={{ padding: '14px 0 8px' }}>
              <button className="btn btn-ghost" onClick={() => setLineChoice(null)}>‹ Different company</button>
            </div>
          </>
        ) : (
          <>
            <div className="memory-search" style={{ marginBottom: 12 }}>
              <Ic i="i-search" />
              <input placeholder="Search the register…" value={resolveQ} autoFocus
                onChange={(e) => setResolveQ(e.target.value)} />
            </div>
            {cands.map((c) => (
              <div key={`${c.name}-${c.code}`}
                className={`atlas-opt${selected?.code === c.code ? ' selected' : ''}`}
                onClick={() => setSelected(c)}>
                <div>
                  <div className="ao-name">{c.name}</div>
                  <div className="ao-meta">{c.meta || c.code}</div>
                </div>
                <div className={`ao-score${selected?.code === c.code ? '' : ' possible'}`}>
                  {selected?.code === c.code ? 'Selected' : 'Possible'}
                </div>
              </div>
            ))}
            {!creating ? (
              <button className="atlas-create" onClick={() => {
                setCreating(true);
                setNewLeadName(resolveQ.trim() || row.entity_candidates?.[0] || '');
                setNewLeadRm(user.full);
              }}><Ic i="i-plus" /> Neither — <strong>&nbsp;create new lead</strong></button>
            ) : (
              <div className="card" style={{ marginTop: 12 }}>
                <div className="card-eyebrow">New lead — created when you approve</div>
                <div className="form-row"><div className="fr-label">Company name</div>
                  <input className="input-field" value={newLeadName}
                    onChange={(e) => setNewLeadName(e.target.value)} /></div>
                <div className="form-row"><div className="fr-label">Relationship Manager</div>
                  <select className="select-field" value={newLeadRm}
                    onChange={(e) => setNewLeadRm(e.target.value)}>
                    <option value="">RM — unassigned</option>
                    {rms.map((o) => <option key={o} value={o}>{rmLabels[o] || o}</option>)}
                    {user.full && !rms.includes(user.full) && <option value={user.full}>{user.full}</option>}
                  </select></div>
                <button className="btn btn-primary" disabled={busy} onClick={() => void proposeLead()}>
                  Pin as new lead — created on approve</button>
              </div>
            )}
            <div style={{ padding: '24px 0 8px' }}>
              <button className="btn btn-primary" disabled={!selected || busy}
                onClick={() => selected && void linkEntity(selected)}>Link selected</button>
            </div>
          </>
        )}
        {err && <div style={{ color: 'var(--danger)', fontSize: 12, margin: '10px 2px' }}>{err}</div>}
      </div>
    );
  }

  /* --------------------------------------------------------------- submitted */
  if (sub === 'submitted' || (readOnly && sub === 'auto' && false)) {
    const followUp = (common.follow_up_date as any)?.value;
    return (
      <div className="app-body no-tabs">
        <div className="success-stage">
          <div className="success-mark"><Ic i="i-check" /></div>
          <div className="success-h">Approved.<br />The firm knows.</div>
          <div className="success-sub">
            {(row.entity_id || row.lead_id || row.deal_id)
              ? <>Linked to {entityName || leadName || 'the register'} · on the timeline · searchable by anyone at Evam</>
              : <>In the firm's memory · searchable by anyone at Evam · not on any timeline yet — link a company any time</>}
          </div>
          {followUp && (
            <div className="followup-card">
              <div className="fu-eyebrow">Follow-up detected</div>
              <div className="fu-title">{(common.next_steps as any)?.value || 'Follow-up'}</div>
              <div className="fu-row"><span className="k">When</span><span className="v">
                {followUp}{(common.follow_up_time as any)?.value ? ` · ${(common.follow_up_time as any).value}` : ''}
              </span></div>
              <div className="fu-row"><span className="k">With</span><span className="v">{(((common.attendees_counterparty as any)?.value || [])[0]) || '—'}</span></div>
              <div className="fu-row"><span className="k">Reminder</span><span className="v">1 day before</span></div>
              {fuMsg && <div style={{ fontSize: 12, color: 'var(--accent)', margin: '8px 0 2px' }}>{fuMsg}</div>}
              {fuDone ? (
                <div className="fu-actions">
                  <button className="btn btn-primary btn-sm" style={{ flex: 1 }} onClick={onBack}>
                    <Ic i="i-check" /> Done — back to Memory
                  </button>
                </div>
              ) : (
                <div className="fu-actions">
                  <button className="btn btn-ghost btn-sm" style={{ flex: 1 }} onClick={onBack}>Skip</button>
                  <button className="btn btn-primary btn-sm" style={{ flex: 2 }} disabled={busy}
                    onClick={() => void addToCalendar()}>
                    <Ic i="i-cal" /> Add to Calendar
                  </button>
                </div>
              )}
            </div>
          )}
          {!followUp && (
            <div style={{ padding: '18px 4px 0', width: '100%' }}>
              <button className="btn btn-ghost" onClick={onBack}>Back to Memory</button>
            </div>
          )}
        </div>
      </div>
    );
  }

  /* ------------------------------------------------------------------ review */
  const detected = report?.detected_use_cases || [];
  const kdp = ((common.key_discussion_points as any)?.value as string[]) || [];
  const nuances = (((common.competitive_intelligence as any)?.value as string) || '')
    .split('\n').map((s) => s.trim()).filter(Boolean);
  const setKdp = (items: string[]) =>
    onCell('common.key_discussion_points', { ...(common.key_discussion_points as any), value: items });
  const setNuances = (items: string[]) =>
    onCell('common.competitive_intelligence', { value: items.join('\n'), confidence: 'n/a' });
  const score = common.opportunity_score as VoxCell | undefined;
  const subsector = (common.subsector as any)?.value as string | null;
  const canonicals = (subsector && registry.subsector_canonicals[subsector]) || [];
  const attendees = ((common.attendees_counterparty as any)?.value as string[]) || [];
  const flags = ((common.data_quality_flags as any)?.value as string[]) || [];

  const fieldControl = (blockKey: string, def: any) => {
    const cells = (blockKey === 'common' ? common : (report as any)?.[blockKey]) || {};
    const cell = cells[def.key] as VoxCell | undefined;
    const path = `${blockKey}.${def.key}`;
    const set = (v: any) => onCell(path, { ...(cell || { confidence: 'high' }), value: v } as VoxCell);
    const opts = (def.options || []).map((o: any) => (typeof o === 'string' ? { value: o, label: o } : o));
    const flagged = strip.some((n) => n.fieldPath === path);
    if (def.control === 'dropdown') {
      return (
        <select className="select-field" disabled={readOnly} value={cell?.value ?? ''}
          onChange={(e) => set(e.target.value || null)}>
          <option value="">—</option>
          {opts.map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
          {cell?.value && !opts.some((o: any) => o.value === cell.value) && (
            <option value={cell.value}>{String(cell.value)}</option>)}
        </select>
      );
    }
    if (def.control === 'chips') {
      const chosen: string[] = Array.isArray(cell?.value) ? cell!.value : [];
      const closed: string[] = def.closed_set || [];
      return (
        <div className="multi-select">
          {/* the mock's stylesheet keys the lit state off `.on` (adds the ✓) */}
          {[...closed, ...chosen.filter((c) => !closed.includes(c))].map((c) => (
            <div key={c} className={`ms-opt${chosen.includes(c) ? ' on' : ''}`}
              onClick={() => !readOnly && set(chosen.includes(c)
                ? chosen.filter((x) => x !== c) : [...chosen, c])}>{chipLabel(c)}</div>
          ))}
          {!readOnly && def.allow_free_text && (
            <div className="ms-opt" onClick={() => {
              const extra = window.prompt('Add a component');
              if (extra?.trim()) set([...chosen, extra.trim()]);
            }}>+ add</div>
          )}
        </div>
      );
    }
    if (def.control === 'action_items') {
      const items: any[] = Array.isArray(cell?.value) ? cell!.value : [];
      return (
        <div>
          {items.map((it, i) => (
            <div key={i} style={{ display: 'flex', gap: 6, marginBottom: 6, flexWrap: 'wrap' }}>
              <input className="input-field" style={{ flex: '1 1 100%' }} disabled={readOnly}
                placeholder="Action" value={it.action || ''}
                onChange={(e) => set(items.map((x, j) => (j === i ? { ...x, action: e.target.value } : x)))} />
              <input className="input-field" style={{ flex: '1 1 110px' }} disabled={readOnly}
                placeholder="Owner" value={it.owner || ''}
                onChange={(e) => set(items.map((x, j) => (j === i ? { ...x, owner: e.target.value } : x)))} />
              <input className="input-field" style={{ flex: '0 1 150px' }} type="date" disabled={readOnly}
                value={it.deadline || ''}
                onChange={(e) => set(items.map((x, j) => (j === i ? { ...x, deadline: e.target.value || null } : x)))} />
            </div>
          ))}
          {!readOnly && <button className="btn-add" onClick={() => set([...items, { action: '', owner: null, deadline: null }])}>+ Add action</button>}
        </div>
      );
    }
    if (def.control === 'list') {
      const items: string[] = Array.isArray(cell?.value) ? cell!.value : [];
      return (
        <div className="intel-list">
          {items.map((it, i) => (
            <div key={i} className="intel-row">
              <span className="intel-dot" />
              <textarea className="intel-text" disabled={readOnly} value={it} rows={1}
                onChange={(e) => set(items.map((x, j) => (j === i ? e.target.value : x)))} />
              {!readOnly && <button className="intel-x" onClick={() => set(items.filter((_, j) => j !== i))}>✕</button>}
            </div>
          ))}
          {!readOnly && <button className="btn-add" onClick={() => set([...items, ''])}>+ Add bullet</button>}
        </div>
      );
    }
    if (def.control === 'number') {
      return <input className={`input-field${flagged ? ' needs' : ''}`} type="number" disabled={readOnly}
        value={cell?.value ?? ''} onChange={(e) => set(e.target.value === '' ? null : Number(e.target.value))} />;
    }
    if (def.control === 'date') {
      return <input className={`input-field${flagged ? ' needs' : ''}`} type="date" disabled={readOnly}
        value={cell?.value ?? ''} onChange={(e) => set(e.target.value || null)} />;
    }
    if (def.control === 'textarea') {
      return <textarea className={`input-field${flagged ? ' needs' : ''}`} disabled={readOnly}
        value={cell?.value ?? ''} onChange={(e) => set(e.target.value || null)} />;
    }
    return <input className={`input-field${flagged ? ' needs' : ''}`} disabled={readOnly}
      value={cell?.value ?? ''} onChange={(e) => set(e.target.value || null)} />;
  };

  const formRow = (blockKey: string, def: any) => {
    const cells = (blockKey === 'common' ? common : (report as any)?.[blockKey]) || {};
    const conf = (cells[def.key] as VoxCell | undefined)?.confidence;
    return (
      <div className="form-row" key={def.key} id={`vox-${blockKey}.${def.key}`}>
        <div className="fr-label">{def.label} <span className={`conf-dot ${dotCls(conf)}`} /></div>
        {fieldControl(blockKey, def)}
      </div>
    );
  };

  const HIDDEN = new Set(['sector', 'subsector', 'attendees_counterparty', 'opportunity_assessment',
    'opportunity_score', 'opportunity_score_override_reason', 'competitive_intelligence',
    'data_quality_flags', 'key_discussion_points', 'meeting_summary']);

  const jump = (path: string) => {
    document.getElementById(`vox-${path}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };

  return (
    <>
      <div className="app-body">
        {/* Sticky: on a long review the way back must not require scrolling
            all the way up. The whole row (back + saved tick) pins to the top
            of the scroll body — sticky on a nested child would be bounded by
            this row's own height and quietly do nothing. */}
        <div style={{ position: 'sticky', top: -16, zIndex: 5, background: 'var(--bg)',
          margin: '-16px -16px 0', padding: '14px 16px 6px',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <button className="review-back" style={{ marginBottom: 0 }} onClick={onBack}>‹ All conversations</button>
          {!Object.keys(dirty).length
            ? <span className="ah-saved"><Ic i="i-check" /> Saved</span>
            : <span className="ah-saved" style={{ color: 'var(--muted)' }}>Saving…</span>}
        </div>
        <div className={`status-pill ${approvedRow || row.erased_at ? 'approved' : 'ready'}`}>
          {row.erased_at ? 'Erased' : approvedRow ? (editUnlocked ? 'Approved · editing' : 'Approved') : 'Ready for review'}
        </div>
        {approvedRow && editUnlocked && (
          <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: 9,
            color: 'var(--warn)', letterSpacing: '0.06em', margin: '2px 0 6px' }}>
            EDITING AN APPROVED RECORD — EVERY CHANGE IS LOGGED
          </div>
        )}
        <div className="review-company" onClick={() => {
          if (row.entity_id) onDossier({ entityId: row.entity_id });
          else if (row.lead_id) onDossier({ leadId: row.lead_id });
        }}>
          {entityName || leadName || row.proposed_lead_company || row.entity_candidates?.[0] || 'Unlinked conversation'}
          {!readOnly && (
            <span style={{ fontSize: 15, color: 'var(--muted)', marginLeft: 8, cursor: 'pointer' }}
              onClick={(e) => { e.stopPropagation(); setSub('atlas'); }} title="Link or change the company">
              <Ic i="i-edit" style={{ display: 'inline-block' }} />
            </span>
          )}
        </div>
        <div className="review-meta">
          {[row.sector, row.subsector, (common.location as any)?.value].filter(Boolean).join(' · ') || 'Sector not determined'}
        </div>
        <div className="review-meta-2">
          <span>{new Date(row.created_at || '').toLocaleString('en', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })}</span>
          {row.duration_seconds ? <><span className="dot">·</span><span>{mmss(row.duration_seconds)}</span></> : null}
          {row.language_detected ? <><span className="dot">·</span><span>{row.language_detected.toUpperCase()}</span></> : null}
        </div>

        {err && <div style={{ color: 'var(--danger)', fontSize: 12, margin: '6px 2px 10px' }}>{err}</div>}
        {toast && <div style={{ color: 'var(--warn)', fontSize: 12, margin: '6px 2px 10px' }}>{toast}</div>}

        {!readOnly && !approvedRow && strip.length > 0 && (
          <div className="needs-strip">
            <div className="ns-head">
              <div className="ns-count">{strip.length}</div>
              <div>
                <div className="ns-title">{strip.length} field{strip.length > 1 ? 's' : ''} to confirm</div>
                <div className="ns-sub">Low and medium confidence · tap to fix · optional</div>
              </div>
            </div>
            {strip.map((n) => (
              <div key={n.fieldPath} className="ns-item" onClick={() => jump(n.fieldPath)}>
                <span className={`ns-dot ${n.confidence === 'low' ? 'lo' : 'md'}`} />
                <span className="ns-field">{n.label} — {n.valueShort}</span>
                <span className="ns-where">{n.blockLabel}</span>
                <span className="ns-jump"><Ic i="i-chev-r" /></span>
              </div>
            ))}
          </div>
        )}

        {(((common.meeting_summary as any)?.value) || kdp.length > 0) && (
          <div className="card">
            <div className="card-h">Summary</div>
            <div className="summary-body">
              {((common.meeting_summary as any)?.value) || `${kdp.slice(0, 3).join('. ')}.`}
            </div>
          </div>
        )}

        <div className="card">
          <div className="card-h">Key intelligence</div>
          <div className="intel-list">
            {kdp.map((it, i) => (
              <div key={i} className="intel-row">
                <span className="intel-dot" />
                <textarea className="intel-text" disabled={readOnly} value={it} rows={2}
                  onChange={(e) => setKdp(kdp.map((x, j) => (j === i ? e.target.value : x)))} />
                {!readOnly && <button className="intel-x" onClick={() => setKdp(kdp.filter((_, j) => j !== i))}>✕</button>}
              </div>
            ))}
          </div>
          {!readOnly && <button className="btn-add" onClick={() => setKdp([...kdp, ''])}>+ Add bullet</button>}
          <div className="divider-h"><span>Nuances &amp; soft signals</span></div>
          <div className="intel-list">
            {nuances.map((it, i) => (
              <div key={i} className="intel-row">
                <span className="intel-dot grey" />
                <textarea className="intel-text" disabled={readOnly} value={it} rows={2}
                  onChange={(e) => setNuances(nuances.map((x, j) => (j === i ? e.target.value : x)))} />
                {!readOnly && <button className="intel-x" onClick={() => setNuances(nuances.filter((_, j) => j !== i))}>✕</button>}
              </div>
            ))}
          </div>
          {!readOnly && <button className="btn-add" onClick={() => setNuances([...nuances, ''])}>+ Add nuance</button>}
        </div>

        <div className="card">
          <div className="card-h">Structured fields</div>
          <div className="uc-chips" style={{ marginBottom: 20 }}>
            {detected.map((uc) => (
              <div key={uc} className="uc-chip">{registry.blocks[uc]?.label || uc}
                {!readOnly && <span className="x" onClick={() => setUcSheet(uc)}>✕</span>}
              </div>
            ))}
            {!readOnly && <div className="uc-chip add" onClick={() => setUcSheet('add')}>+ Add</div>}
          </div>
          {detected.map((uc) => {
            const block = registry.blocks[uc];
            if (!block) return null;
            if (!block.fields.length) {
              return (
                <div key={uc} className="uc-field-group">
                  <div className="uc-group-h">{block.label}</div>
                  <div style={{ fontSize: 12, color: 'var(--muted)' }}>Captured with the common field set — dedicated fields follow with volume.</div>
                </div>
              );
            }
            const partyRole = uc === 'asset_monetisation'
              ? ((report as any)?.[uc]?.party_role?.value as string | null) : null;
            const visible = block.fields.filter((d: any) => {
              const applies = d.applies_to || 'always';
              if (uc !== 'asset_monetisation' || applies === 'always') return true;
              if (!partyRole || partyRole === 'not_specified') return false;
              return partyRole === 'both' || applies === partyRole;
            });
            return (
              <div key={uc} className="uc-field-group">
                <div className="uc-group-h">{block.label}</div>
                {visible.map((d: any) => formRow(uc, d))}
              </div>
            );
          })}
        </div>

        {row.raw_transcript && (
          <div className="card">
            <div className="card-h with-action">Full transcript
              <span style={{ display: 'flex', gap: 14 }}>
                {!readOnly && !approvedRow && !fixingTranscript && (
                  <span className="h-action" onClick={() => {
                    setFixDraft(row.corrected_transcript || row.raw_transcript || '');
                    setFixingTranscript(true); setTranscriptOpen(false);
                  }}><Ic i="i-refresh" /> Re-analyze</span>
                )}
                <span className="h-action" onClick={() => setTranscriptOpen((v) => !v)}>
                  {transcriptOpen ? 'Hide' : 'Show original'}</span>
              </span>
            </div>
            <div className="transcript-sub">
              {row.corrected_transcript
                ? 'Corrected copy in use — the verbatim original is preserved below.'
                : 'Translated inline — word-for-word. Evidence: never editable.'}
            </div>
            {fixingTranscript ? (
              <div>
                <textarea className="input-field" rows={10} value={fixDraft}
                  style={{ width: '100%', resize: 'vertical', lineHeight: 1.5 }}
                  onChange={(e) => setFixDraft(e.target.value)} />
                <div style={{ fontSize: 11, color: 'var(--muted)', margin: '8px 2px 12px' }}>
                  Fix mis-heard names and terms here — a corrected name updates every field,
                  bullet and snippet when the report regenerates. The word-for-word original
                  stays on record. Your own confirmed field values survive the rebuild.
                </div>
                <div style={{ display: 'flex', gap: 10 }}>
                  <button className="btn btn-primary" disabled={busy || !fixDraft.trim()}
                    onClick={() => void correctAndRegenerate()}>Save &amp; re-analyze</button>
                  <button className="btn btn-ghost" style={{ width: 'auto' }}
                    onClick={() => setFixingTranscript(false)}>Cancel</button>
                </div>
              </div>
            ) : (
              <>
                {row.corrected_transcript && (
                  <div className="transcript-body">{row.corrected_transcript}</div>
                )}
                {transcriptOpen && <div className="transcript-body"
                  style={row.corrected_transcript
                    ? { opacity: 0.65, borderTop: '1px dashed var(--line-2)', marginTop: 10, paddingTop: 10 }
                    : undefined}>{row.raw_transcript}</div>}
              </>
            )}
          </div>
        )}

        <div className="card">
          <div className="card-h with-action">Additional details
            <span className="h-action" onClick={() => setShowMore((v) => !v)}>{showMore ? 'Hide' : 'Show'} <Ic i="i-chev-d" /></span>
          </div>
          {showMore && (
            <div>
              <div className="company-field">
                <div className="cf-label"><span className="lab">Company — {
                  row.deal_id ? 'deal line' : row.lead_id ? 'lead'
                    : row.proposed_lead_company ? 'new lead on approve'
                      : row.entity_id ? 'linked' : 'not linked'}</span>
                  {!readOnly && <span className="edit" onClick={() => setSub('atlas')}><Ic i="i-edit" /> Edit</span>}
                </div>
                <div className="company-input-row">
                  <input className="company-input" readOnly
                    value={entityName || leadName || row.proposed_lead_company || row.entity_candidates?.[0] || ''} />
                  <span className={`atlas-badge ${row.entity_id || row.lead_id || row.deal_id ? 'found' : 'new'}`}>
                    {row.deal_id ? 'Deal' : row.lead_id ? 'Lead'
                      : row.proposed_lead_company ? 'On approve'
                        : row.entity_id ? 'In register' : 'Unlinked'}
                  </span>
                </div>
              </div>
              {(leadRow || dealRow || row.entity_id || row.proposed_lead_company) && (
                <div className="atlas-detail">
                  {row.entity_id && <><span className="k">Register match:</span> {entityName}<br /></>}
                  {dealRow && <><span className="k">Deal:</span> {dealRow.deal_no || dealRow.code} ·
                    {' '}{[dealRow.product_type, dealRow.stage].filter(Boolean).join(' · ') || '—'}<br />
                    <span className="k">Relationship Manager:</span> {dealRow.rm || '—'}</>}
                  {leadRow && <><span className="k">Lead:</span> {leadRow.lead_no} · {leadRow.company}<br />
                    <span className="k">Relationship Manager:</span> {leadRow.rm || '—'}</>}
                  {!leadRow && !dealRow && row.proposed_lead_company && (
                    <><span className="k">New lead:</span> {row.proposed_lead_company} — created when this
                      report is approved<br />
                      <span className="k">Relationship Manager:</span> {row.proposed_lead_rm || 'unassigned'}</>)}
                </div>
              )}
              {leadRow && !readOnly && (user.roles.includes('Management') || user.roles.includes('Admin')) && (
                <div className="fg-item" style={{ marginTop: 10 }}>
                  <div className="fg-label">Relationship Manager — assignment authority</div>
                  <select className="select-field" value={leadRow.rm ?? ''}
                    onChange={(e) => {
                      void api.patch(`/leads/${leadRow.id}`, { rm: e.target.value || null })
                        .then(() => setLeadRow({ ...leadRow, rm: e.target.value }))
                        .catch((er) => setErr(String(er?.message || er)));
                    }}>
                    <option value="">RM — unassigned</option>
                    {referenceService.getRefSync('RM').map((o) => (
                      <option key={o} value={o}>{referenceService.getRefLabels('RM')?.[o] || o}</option>))}
                  </select>
                </div>
              )}

              <div className="fg-item" style={{ marginTop: 18 }}>
                <div className="fg-label">Sector <span className={`conf-dot ${dotCls((common.sector as any)?.confidence)}`} /></div>
                <select className="select-field" disabled={readOnly} value={(common.sector as any)?.value ?? ''}
                  onChange={(e) => {
                    onCell('common.sector', { ...(common.sector as any), value: e.target.value || null });
                    onCell('common.subsector', { value: null, confidence: 'n/a' });
                  }}>
                  <option value="">—</option>
                  {Object.keys(registry.taxonomy).map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <div className="fg-item" id="vox-common.subsector">
                <div className="fg-label">Subsector <span className={`conf-dot ${dotCls((common.subsector as any)?.confidence)}`} /></div>
                <select className="select-field" disabled={readOnly} value={subsector ?? ''}
                  onChange={(e) => onCell('common.subsector', { ...(common.subsector as any), value: e.target.value || null })}>
                  <option value="">—</option>
                  {(registry.taxonomy[(common.sector as any)?.value] || []).map((s: string) => (
                    <option key={s} value={s}>{s}</option>))}
                </select>
              </div>
              <div className="sector-canonical-box">
                <div className="scb-h">{subsector ? `${subsector} — key data` : 'Key data'}</div>
                <div className="scb-sub">Fields specific to this subsector</div>
                {canonicals.length === 0 && (
                  <div className="canon-empty">No data captured — pick a subsector to see its fields.</div>)}
                {canonicals.map((def: any) => {
                  const cell = (report?.subsector_details || {})[def.key] as VoxCell | undefined;
                  return (
                    <div className="form-row" key={def.key} id={`vox-subsector_details.${def.key}`}>
                      <div className="fr-label">{def.label} <span className={`conf-dot ${dotCls(cell?.confidence)}`} /></div>
                      {def.control === 'dropdown' ? (
                        <select className="select-field" disabled={readOnly} value={cell?.value ?? ''}
                          onChange={(e) => onCell(`subsector_details.${def.key}`,
                            { ...(cell || { confidence: 'high' }), value: e.target.value || null } as VoxCell)}>
                          <option value="">—</option>
                          {(def.options || []).map((o: string) => <option key={o} value={o}>{o}</option>)}
                        </select>
                      ) : (
                        <input className="input-field" disabled={readOnly} value={cell?.value ?? ''}
                          onChange={(e) => onCell(`subsector_details.${def.key}`,
                            { ...(cell || { confidence: 'high' }), value: e.target.value || null } as VoxCell)} />
                      )}
                    </div>
                  );
                })}
              </div>

              <div className="fg-label" style={{ marginTop: 16 }}>
                Opportunity score (1-5) <span className={`conf-dot ${dotCls(score?.confidence)}`} /></div>
              <div className="score-row">
                {[1, 2, 3, 4, 5].map((n) => (
                  <div key={n}
                    className={`score-cell${(score?.value || 0) >= n ? ' filled' : ''}${score?.value === n ? ' selected' : ''}`}
                    onClick={() => !readOnly && onCell('common.opportunity_score',
                      { value: score?.value === n ? null : n, confidence: 'n/a', user_override: true })}>{n}</div>
                ))}
              </div>
              <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: 9, color: 'var(--muted)',
                letterSpacing: '0.04em', marginTop: 8 }}>
                {score?.user_override ? 'You set this — user-override · confidence n/a'
                  : score?.value ? `AI-suggested · ${score.value} of 5 · tap to override` : 'Null — no evaluative language heard · tap to set'}
              </div>
              {score?.user_override && !readOnly && (
                <div style={{ marginTop: 10 }}>
                  <div className="fr-label" style={{ marginBottom: 6 }}>Reason for change <span style={{ color: 'var(--muted)', fontWeight: 400 }}>(optional)</span></div>
                  <textarea className="input-field" placeholder="Why you changed the score. Leave blank if you like."
                    value={(common.opportunity_score_override_reason as any)?.value ?? ''}
                    onChange={(e) => onCell('common.opportunity_score_override_reason',
                      { value: e.target.value || null, confidence: 'n/a' })} />
                </div>
              )}

              {(attendees.length > 0 || row.recorder_email) && (
                <>
                  <div className="fg-label" style={{ marginTop: 20 }}>Attendees</div>
                  {attendees.map((a) => (
                    <div key={a} className="attendee-row">
                      <div className="att-init">{a.split(/[\s(]/)[0].slice(0, 2).toUpperCase()}</div>
                      <div className="att-body"><div className="att-name">{a}</div></div>
                      <span className={`conf-dot ${dotCls((common.attendees_counterparty as any)?.confidence)}`} />
                    </div>
                  ))}
                  {/* The recorder is in the room too — the blueprint lists them
                      with a Recorder · Evam byline under the counterparties. */}
                  {row.recorder_email && (
                    <div className="attendee-row">
                      <div className="att-init">
                        {(row.recorder_name || row.recorder_email).split(/[\s(@]/)[0].slice(0, 2).toUpperCase()}
                      </div>
                      <div className="att-body">
                        <div className="att-name">{row.recorder_name || row.recorder_email.split('@')[0]}</div>
                        <div className="att-role">Recorder · Evam</div>
                      </div>
                      <span className="conf-dot high" />
                    </div>
                  )}
                </>
              )}
              {flags.length > 0 && (
                <div className="atlas-detail" style={{ marginTop: 14 }}>
                  <span className="k">Data quality:</span> {flags.join(' · ')}
                </div>
              )}
              <div className="audit-strip" style={{ marginTop: 16 }}>
                <span className="k">Recorded by:</span> {row.recorder_email}<br />
                {row.latitude != null && row.longitude != null && (
                  <><span className="k">GPS:</span> {Math.abs(row.latitude).toFixed(2)}°{row.latitude >= 0 ? 'N' : 'S'}, {Math.abs(row.longitude).toFixed(2)}°{row.longitude >= 0 ? 'E' : 'W'}<br /></>
                )}
                <span className="k">Registry:</span> {row.registry_version || '—'} · <span className="k">Prompt:</span> {row.prompt_version || '—'} · <span className="k">Edits:</span> {row.edits_count ?? 0}<br />
                <span className="k">Readable by all Evam staff</span>
              </div>
            </div>
          )}
        </div>
        <div style={{ height: 68 }} />
      </div>

      {!readOnly && !approvedRow && (
        <div className="review-action-bar">
          <button className="icon-btn" title="Download as PDF"
            onClick={() => say('PDF export arrives with the next round.')}><Ic i="i-download" /></button>
          <button className={`approve-pill${strip.length ? ' gated' : ''}`} disabled={busy} onClick={() => void approve()}>
            <span>{strip.length ? `Approve · ${strip.length} unreviewed` : 'Approve'}</span>
          </button>
          <button className="icon-btn" title="More" onClick={() => setOverflow(true)}><Ic i="i-more" /></button>
        </div>
      )}
      {readOnly && !row.erased_at && (
        <div className="review-action-bar">
          <button className="icon-btn" title="Edit this report"
            onClick={() => setEditUnlocked(true)}><Ic i="i-edit" /></button>
          <button className="approve-pill" onClick={onBack}><span>Back to Memory</span></button>
        </div>
      )}
      {readOnly && row.erased_at && (
        <div className="review-action-bar">
          <button className="approve-pill" onClick={onBack}><span>Back to Memory</span></button>
        </div>
      )}
      {approvedRow && editUnlocked && (
        <div className="review-action-bar">
          <button className="approve-pill" disabled={busy} onClick={async () => {
            try { await saveEdits(); setEditUnlocked(false); }
            catch (e: any) { setErr(String(e?.message || e)); }
          }}><span>Done editing</span></button>
        </div>
      )}

      {overflow && (
        <div className="sheet-scrim show" onClick={() => setOverflow(false)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="sheet-handle" />
            <div className="sheet-row" onClick={() => { setOverflow(false); say('PDF export arrives with the next round.'); }}>
              <Ic i="i-download" /> Download as PDF</div>
            <div className="sheet-row" onClick={() => { setOverflow(false); setTranscriptOpen(true); }}>
              <Ic i="i-list" /> Show original transcript</div>
            {/* A draft is still the recorder's to withdraw; an approved record is
                the firm's and needs the Admin erasure path instead. */}
            {!approvedRow && !row.erased_at && (
              <div className="sheet-row" style={{ color: 'var(--danger)' }}
                onClick={() => void deleteDraft()}>
                <Ic i="i-trash" /> Delete conversation</div>
            )}
          </div>
        </div>
      )}

      {ucSheet && (
        <div className="sheet-scrim show" onClick={() => setUcSheet('')}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="sheet-handle" />
            {ucSheet === 'add' ? (
              <>
                <div className="sheet-title">Add a use case</div>
                {registry.use_cases.filter((u) => !detected.includes(u)).map((u) => (
                  <div key={u} className="sheet-row" onClick={() => { setUseCases([...detected, u]); setUcSheet(''); }}>
                    {registry.blocks[u]?.label || u}</div>
                ))}
              </>
            ) : (
              <>
                <div className="sheet-title">Remove "{registry.blocks[ucSheet]?.label || ucSheet}"?</div>
                <div className="sheet-body">This drops the use case and its structured fields from the report. You can re-add it from the chip row.</div>
                <button className="btn btn-danger" style={{ marginBottom: 8 }} onClick={() => {
                  setUseCases(detected.filter((u) => u !== ucSheet)); setUcSheet('');
                }}>Remove use case</button>
                <button className="btn btn-ghost" onClick={() => setUcSheet('')}>Cancel</button>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
