/**
 * VOX — the blueprint app, exactly as the owner's mockup draws it, hosted inside
 * the VocX panel and wired to the real conversation store.
 *
 * The markup here is the mock's own (class names and structure verbatim from
 * vox_mockup v3.3); voxMock.css is the mock's stylesheet scoped under .vox-app.
 * Screens: memory (home) · all (feed) · record · processing · failed · review ·
 * atlas (resolve) · submitted · queue · dossier. Login is skipped by owner
 * decision — ATLAS's session is the door.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useAuth } from '../../../auth/AuthContext';
import { api } from '../../../api/http';
import { voxService } from '../../../services/voxService';
import type { VoxConversation } from '../../../services/voxService';
import ReportsTab from '../ReportsTab';
import VoxRecordScreen from './VoxRecordScreen';
import VoxReviewScreen from './VoxReviewScreen';
import { VOX_SPRITE } from './sprite';
import './voxMock.css';

export type VoxScreen = 'memory' | 'all' | 'record' | 'review' | 'queue'
  | 'dossier' | 'legacy';

export const Ic = ({ i, style }: { i: string; style?: React.CSSProperties }) => (
  <svg className="ic" style={style}><use href={`#${i}`} /></svg>
);

/* ------------------------------------------------------------------ helpers */

const UC_SHORT: Record<string, string> = {
  lending: 'Lending', syndication: 'Synd', asset_monetisation: 'AM',
  credit_diligence: 'Credit', investor_relations: 'IR', banking_relations: 'Banking',
  operations: 'Ops',
};

export function timeLabel(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  const sameDay = d.toDateString() === now.toDateString();
  const yest = new Date(now.getTime() - 86400e3).toDateString() === d.toDateString();
  if (sameDay) return `Today · ${hm}`;
  if (yest) return `Yest · ${hm}`;
  return `${d.getDate()} ${d.toLocaleString('en', { month: 'short' })} · ${hm}`;
}

export function dayHead(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return `Today · ${d.getDate()} ${d.toLocaleString('en', { month: 'short' })} ${d.getFullYear()}`;
  if (new Date(now.getTime() - 86400e3).toDateString() === d.toDateString()) return `Yesterday · ${d.getDate()} ${d.toLocaleString('en', { month: 'short' })} ${d.getFullYear()}`;
  return `${d.getDate()} ${d.toLocaleString('en', { month: 'short' })} ${d.getFullYear()}`;
}

export function snipOf(c: VoxConversation): string {
  if (c.snippet) return c.snippet;
  const kdp = (c.structured_report?.common?.key_discussion_points as any)?.value as string[] | undefined;
  if (kdp?.length) return kdp.slice(0, 2).join('. ');
  if (c.status === 'processing' || c.status === 'queued' || c.status === 'uploading') return 'Processing on the server…';
  if (c.status.includes('failed')) return c.processing_error || 'Processing needs a retry — the recording is safe.';
  return c.sector ? `${c.sector} conversation` : 'Conversation';
}

export function titleOf(c: VoxConversation, names: Record<string, string>): string {
  if (c.entity_id && names[c.entity_id]) return names[c.entity_id];
  if (c.lead_id && names[`lead:${c.lead_id}`]) return names[`lead:${c.lead_id}`];
  return c.entity_candidates?.[0] || c.sector || 'Conversation';
}

export function StatusChip({ c }: { c: VoxConversation }) {
  if (c.status === 'submitted') return <span className="chip chip-ok">Approved</span>;
  if (c.status === 'ready') return <span className="chip chip-warn">To review</span>;
  if (c.status.includes('failed')) return <span className="chip chip-warn">Retry</span>;
  return <span className="chip">Processing…</span>;
}

export function ConvChips({ c }: { c: VoxConversation }) {
  return (
    <div className="chips">
      {(c.use_cases || []).map((u) => <span key={u} className="chip">{UC_SHORT[u] || u}</span>)}
      <StatusChip c={c} />
    </div>
  );
}

/** Resolve display names for linked rows (entity + lead), cached per mount. */
export function useNames(items: VoxConversation[]) {
  const [names, setNames] = useState<Record<string, string>>({});
  useEffect(() => {
    const wantE = [...new Set(items.map((c) => c.entity_id).filter(Boolean))] as string[];
    const wantL = [...new Set(items.map((c) => c.lead_id).filter(Boolean))] as string[];
    void (async () => {
      const out: Record<string, string> = {};
      await Promise.all([
        ...wantE.filter((id) => !names[id]).slice(0, 25).map(async (id) => {
          try {
            const e = await api.get<any>(`/entities/${id}`);
            out[id] = e.display_name || e.legal_name || e.code || '';
          } catch { /* keep fallback */ }
        }),
        ...wantL.filter((id) => !names[`lead:${id}`]).slice(0, 25).map(async (id) => {
          try {
            const l = await api.get<any>(`/leads/${id}`);
            out[`lead:${id}`] = l.company || '';
          } catch { /* keep fallback */ }
        }),
      ]);
      if (Object.keys(out).length) setNames((n) => ({ ...n, ...out }));
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);
  return names;
}

function BottomNav({ screen, queueCount, go }: {
  screen: VoxScreen; queueCount: number; go: (s: VoxScreen) => void;
}) {
  const memoryish = ['memory', 'all', 'dossier', 'legacy'].includes(screen);
  return (
    <div className="bottom-nav">
      <div className={`bn-item${screen === 'record' ? ' active' : ''}`} onClick={() => go('record')}>
        <div className="bn-ico"><Ic i="i-mic" /></div>Record
      </div>
      <div className={`bn-item${memoryish ? ' active' : ''}`} onClick={() => go('memory')}>
        <div className="bn-ico"><Ic i="i-search" /></div>Memory
      </div>
      <div className={`bn-item${screen === 'queue' ? ' active' : ''}`} onClick={() => go('queue')}>
        <div className="bn-ico"><Ic i="i-inbox" /></div>Queue
        {queueCount > 0 && <span className="bn-badge">{queueCount}</span>}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- screens */

function MemoryScreen({ go, openConversation, openDossier }: {
  go: (s: VoxScreen) => void;
  openConversation: (id: string) => void;
  openDossier: (subject: { entityId?: string; leadId?: string }) => void;
}) {
  const { user } = useAuth();
  const [items, setItems] = useState<VoxConversation[]>([]);
  const [q, setQ] = useState('');
  const [results, setResults] = useState<VoxConversation[] | null>(null);
  const names = useNames(results ?? items);
  useEffect(() => {
    // your desk, not the firm's: Recent shows YOUR recordings; a company's full
    // story lives in its dossier, searching reaches the whole firm's memory
    void voxService.list({ mine: true, limit: 6 }).then((r) => setItems(r.items)).catch(() => {});
  }, []);
  useEffect(() => {
    const term = q.trim();
    if (term.length < 2) { setResults(null); return; }
    const t = setTimeout(async () => {
      try {
        // the firm's memory: everything of YOURS + everyone's APPROVED record —
        // colleagues' drafts stay out of search the same way they stay out of feeds
        const [mine, firm] = await Promise.all([
          voxService.list({ q: term, mine: true, limit: 30 }),
          voxService.list({ q: term, status: 'submitted', limit: 30 }),
        ]);
        const seen = new Set<string>();
        const merged = [...mine.items, ...firm.items]
          .filter((c) => !seen.has(c.id) && seen.add(c.id))
          .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
        setResults(merged);
      } catch { setResults([]); }
    }, 300);
    return () => clearTimeout(t);
  }, [q]);
  const now = new Date();
  const greet = now.getHours() < 12 ? 'Morning' : now.getHours() < 17 ? 'Afternoon' : 'Evening';
  return (
    <div className="app-body">
      <div className="home-greet">
        <div className="small">{now.toLocaleString('en', { weekday: 'short' })} · {now.getDate()} {now.toLocaleString('en', { month: 'short' })} {now.getFullYear()}</div>
        <div className="big">{greet}, {user.full.split(' ')[0]}</div>
      </div>
      <div className="quick-record" onClick={() => go('record')}>
        <div className="qr-mic"><Ic i="i-mic" /></div>
        <div className="qr-body">
          <div className="qr-label">Tap to capture</div>
          <div className="qr-title">New conversation</div>
          <div className="qr-sub">Post-meeting note · Live meeting arrives next</div>
        </div>
      </div>
      <div className="memory-search">
        <Ic i="i-search" />
        <input placeholder="Search every Evam conversation…" value={q}
          onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="section-h">
        <span>{results !== null ? `Results · ${results.length}` : 'Recent'}</span>
        {results !== null
          ? <a onClick={() => setQ('')}>Clear ×</a>
          : <a onClick={() => go('all')}>See all →</a>}
      </div>
      {(results ?? items).map((c) => (
        <div key={c.id} className="conv-card" onClick={() => openConversation(c.id)}>
          <div className="cc-top">
            <div className="cc-co" onClick={(e) => {
              if (c.entity_id || c.lead_id) {
                e.stopPropagation();
                openDossier(c.entity_id ? { entityId: c.entity_id } : { leadId: c.lead_id! });
              }
            }}>{titleOf(c, names)}</div>
            <div className="cc-time">{timeLabel(c.created_at)}</div>
          </div>
          <div className="cc-snip">{snipOf(c)}</div>
          <ConvChips c={c} />
        </div>
      ))}
      {!(results ?? items).length && (
        <div className="conv-card"><div className="cc-snip">
          {results !== null ? 'No conversation matches that.' : 'Nothing yet — the firm remembers what gets recorded.'}
        </div></div>
      )}
      <div className="section-h" style={{ marginTop: 18 }}>
        <span />
        <a onClick={() => go('legacy')}>Legacy reports →</a>
      </div>
    </div>
  );
}

function AllScreen({ go, openConversation, openDossier }: {
  go: (s: VoxScreen) => void;
  openConversation: (id: string) => void;
  openDossier: (subject: { entityId?: string; leadId?: string }) => void;
}) {
  const { user } = useAuth();
  const [items, setItems] = useState<VoxConversation[]>([]);
  const [uc, setUc] = useState('');
  // The feed defaults to the FIRM (the blueprint's "every conversation, newest
  // first"): everyone's approved record merged with your own items in every
  // state — a colleague's half-processed draft stays out, yours never does.
  // The Mine pill flips to just your desk, and the title says whose feed it is.
  const [mineOnly, setMineOnly] = useState(false);
  const [q, setQ] = useState(() => sessionStorage.getItem('vox.q') || '');
  const names = useNames(items);
  useEffect(() => {
    const t = setTimeout(async () => {
      try {
        const term = q.trim() || undefined;
        if (mineOnly) {
          const r = await voxService.list({ q: term, use_case: uc || undefined,
            mine: true, limit: 60 });
          setItems(r.items);
          return;
        }
        const [firm, own] = await Promise.all([
          voxService.list({ q: term, use_case: uc || undefined, status: 'submitted', limit: 60 }),
          voxService.list({ q: term, use_case: uc || undefined, mine: true, limit: 60 }),
        ]);
        const seen = new Set<string>();
        setItems([...own.items, ...firm.items]
          .filter((c) => !seen.has(c.id) && seen.add(c.id))
          .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || '')));
      } catch { setItems([]); }
    }, q ? 300 : 0);
    return () => clearTimeout(t);
  }, [q, uc, mineOnly]);
  const groups = useMemo(() => {
    const by: { head: string; rows: VoxConversation[] }[] = [];
    for (const c of items) {
      const head = dayHead(c.created_at);
      const last = by[by.length - 1];
      if (last && last.head === head) last.rows.push(c);
      else by.push({ head, rows: [c] });
    }
    return by;
  }, [items]);
  const UCS = ['lending', 'syndication', 'asset_monetisation', 'credit_diligence'];
  return (
    <div className="app-body">
      <button className="review-back" onClick={() => go('memory')}>‹ Memory</button>
      <div className="home-greet" style={{ paddingBottom: 12 }}>
        <div className="small">{mineOnly ? 'Your conversations · newest first' : 'Every conversation · newest first'}</div>
        <div className="big">{mineOnly ? `${user.full.split(' ')[0]}'s conversations` : 'All conversations'}</div>
      </div>
      <div className="memory-search">
        <Ic i="i-search" />
        <input placeholder="Search every Evam conversation…" value={q}
          onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="filter-row" style={{ alignItems: 'center' }}>
        <div className={`filter-pill${!uc ? ' active' : ''}`}
          onClick={() => setUc('')}>All use cases</div>
        {UCS.map((u) => (
          <div key={u} className={`filter-pill${uc === u ? ' active' : ''}`}
            onClick={() => setUc(uc === u ? '' : u)}>{UC_SHORT[u]}</div>
        ))}
        {/* Scope is not a use case — it gets a checkbox, apart from the pills
            (the blueprint's rule: values on one row, dimensions on another).
            Ticked, the headline says whose feed this is. */}
        <label style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6,
          fontSize: 12, color: mineOnly ? 'var(--accent)' : 'var(--text-2)',
          cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap', padding: '4px 2px' }}>
          <input type="checkbox" checked={mineOnly}
            onChange={(e) => setMineOnly(e.target.checked)}
            style={{ accentColor: 'var(--accent)', width: 14, height: 14, cursor: 'pointer' }} />
          Self
        </label>
      </div>
      {groups.map((g) => (
        <div key={g.head}>
          <div className="month-head">{g.head}</div>
          {g.rows.map((c) => (
            <div key={c.id} className="conv-card" onClick={() => openConversation(c.id)}>
              <div className="cc-top">
                <div className="cc-co" onClick={(e) => {
                  if (c.entity_id || c.lead_id) {
                    e.stopPropagation();
                    openDossier(c.entity_id ? { entityId: c.entity_id } : { leadId: c.lead_id! });
                  }
                }}>{titleOf(c, names)}</div>
                <div className="cc-time">{timeLabel(c.created_at)?.replace(/^Today · /, '')} · {(c.recorder_name || c.recorder_email).split(' ')[0]}</div>
              </div>
              <div className="cc-snip">{snipOf(c)}</div>
              <ConvChips c={c} />
            </div>
          ))}
        </div>
      ))}
      {!items.length && (
        <div className="conv-card"><div className="cc-snip">{q ? 'No matches — an empty result is a real answer.' : 'Nothing yet.'}</div></div>
      )}
    </div>
  );
}

function QueueScreen({ go, openConversation }: {
  go: (s: VoxScreen) => void;
  openConversation: (id: string) => void;
}) {
  const [items, setItems] = useState<VoxConversation[]>([]);
  const names = useNames(items);
  const refresh = () => {
    void voxService.list({ status: 'ready,processing_failed,failed_permanently', limit: 100 })
      .then((r) => setItems(r.items.filter((c) => c.status !== 'ready' || (!c.entity_id && !c.lead_id))))
      .catch(() => {});
  };
  useEffect(refresh, []);
  const discard = async (c: VoxConversation) => {
    if (!window.confirm('Discard this recording? The audio, transcript and draft report '
      + 'are removed for everyone. This cannot be undone.')) return;
    try { await voxService.erase(c.id); refresh(); } catch { /* row may be gone already */ }
  };
  return (
    <div className="app-body">
      <div className="home-greet">
        <div className="small">Needs you · {items.length}</div>
        <div className="big">Queue</div>
      </div>
      <div style={{ fontSize: 12, color: 'var(--muted)', margin: '0 4px 18px', lineHeight: 1.5 }}>
        Conversations that couldn't finish on their own — company not matched, or processing needs a retry.
      </div>
      {items.map((c) => {
        const failed = c.status.includes('failed');
        return (
          <div key={c.id} className="queue-item" onClick={() => openConversation(c.id)}>
            <div className="qi-top">
              <div className="qi-heard">{c.entity_candidates?.[0] ? `"${c.entity_candidates[0]}"` : `Note · ${timeLabel(c.created_at)}`}</div>
              <div className="qi-reason" style={failed ? { color: 'var(--danger)' } : undefined}>
                {failed ? (c.status === 'failed_permanently' ? 'Permanently failed' : 'Processing failed') : 'Company · no match'}
              </div>
            </div>
            <div className="qi-snip">
              {failed
                ? `${(c.processing_error || 'Processing failed.').slice(0, 90)} Audio and transcript are safe.`
                : 'No link yet. Pick the right company, or create a new lead and attach this conversation.'}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div className="qi-action" style={failed ? { color: 'var(--danger)' } : undefined}>
                {failed ? <><Ic i="i-refresh" /> Retry &amp; open</> : <><Ic i="i-plus" /> Link or create lead</>}
              </div>
              {/* a stuck take the recorder gives up on must be deletable HERE —
                  the review's overflow menu is unreachable while it fails */}
              <div className="qi-action" style={{ color: 'var(--muted)' }}
                onClick={(e) => { e.stopPropagation(); void discard(c); }}>
                <Ic i="i-trash" /> Discard
              </div>
            </div>
          </div>
        );
      })}
      {!items.length && (
        <div className="conv-card"><div className="cc-snip">Queue clear — nothing needs a human.</div></div>
      )}
    </div>
  );
}

function DossierScreen({ subject, go, openConversation }: {
  /** A client company (entityId) — or a company that is still LEAD-ONLY, whose
   *  story is keyed by the lead until conversion creates the client. */
  subject: { entityId?: string; leadId?: string };
  go: (s: VoxScreen) => void;
  openConversation: (id: string) => void;
}) {
  const [entity, setEntity] = useState<any>(null);
  const [lead, setLead] = useState<any>(null);
  const [items, setItems] = useState<VoxConversation[]>([]);
  const [uc, setUc] = useState('');
  useEffect(() => {
    setEntity(null); setLead(null);
    if (subject.entityId) {
      void api.get<any>(`/entities/${subject.entityId}`).then(setEntity).catch(() => {});
    } else if (subject.leadId) {
      void api.get<any>(`/leads/${subject.leadId}`).then(setLead).catch(() => {});
    }
  }, [subject.entityId, subject.leadId]);
  useEffect(() => {
    void voxService.list({
      entity_id: subject.entityId || undefined, lead_id: subject.leadId || undefined,
      use_case: uc || undefined, status: 'submitted', limit: 100 })
      .then((r) => setItems(r.items)).catch(() => {});
  }, [subject.entityId, subject.leadId, uc]);
  const team = new Set(items.map((c) => c.recorder_email)).size;
  const monthsSince = items.length
    ? Math.max(1, Math.round((Date.now() - new Date(items[items.length - 1].created_at || Date.now()).getTime()) / (30 * 86400e3)))
    : 0;
  const groups = useMemo(() => {
    const by: { head: string; rows: VoxConversation[] }[] = [];
    for (const c of items) {
      const d = new Date(c.created_at || '');
      const head = `${d.toLocaleString('en', { month: 'long' })} ${d.getFullYear()}`;
      const last = by[by.length - 1];
      if (last && last.head === head) last.rows.push(c);
      else by.push({ head, rows: [c] });
    }
    return by;
  }, [items]);
  const UCS = ['lending', 'syndication', 'asset_monetisation', 'credit_diligence'];
  return (
    <div className="app-body">
      <button className="review-back" onClick={() => go('memory')}>‹ Memory</button>
      <div className="filter-row">
        <div className={`filter-pill${!uc ? ' active' : ''}`} onClick={() => setUc('')}>All use cases</div>
        {UCS.map((u) => (
          <div key={u} className={`filter-pill${uc === u ? ' active' : ''}`}
            onClick={() => setUc(uc === u ? '' : u)}>{UC_SHORT[u]}</div>
        ))}
      </div>
      <div className="co-head">
        <div className="co-name">{entity?.display_name || entity?.legal_name || lead?.company || '…'}</div>
        <div className="co-meta">
          {entity
            ? [entity.code, entity.sector, entity.state].filter(Boolean).join(' · ')
            : lead
              ? [lead.lead_no, lead.sector, lead.rm && `RM ${lead.rm}`, lead.temperature,
                 'Lead — not yet a client'].filter(Boolean).join(' · ')
              : ''}
        </div>
      </div>
      <div className="co-stats">
        <div className="co-stat"><span className="n">{items.length}</span>Convos</div>
        <div className="co-stat"><span className="n">{team}</span>Team</div>
        <div className="co-stat"><span className="n">{monthsSince}mo</span>Since first</div>
      </div>
      {groups.map((g) => (
        <div key={g.head}>
          <div className="month-head">{g.head}</div>
          {g.rows.map((c) => (
            <div key={c.id} className="tl-item" onClick={() => openConversation(c.id)} style={{ cursor: 'pointer' }}>
              <div className="tl-date">{new Date(c.created_at || '').getDate()} {new Date(c.created_at || '').toLocaleString('en', { month: 'short' })}</div>
              <div className="tl-body">
                <div className="tl-who"><strong>{(c.recorder_name || c.recorder_email).split(' ')[0]}</strong> · {(c.use_cases || []).map((u) => UC_SHORT[u] || u).join(', ') || 'Conversation'}</div>
                <div className="tl-note">{snipOf(c)}</div>
                <ConvChips c={c} />
              </div>
            </div>
          ))}
        </div>
      ))}
      {!items.length && <div className="conv-card"><div className="cc-snip">No conversations for this company yet.</div></div>}
    </div>
  );
}

/* --------------------------------------------------------------------- shell */

export default function VoxApp() {
  const [screen, setScreen] = useState<VoxScreen>('memory');
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [dossierSubject, setDossierSubject] =
    useState<{ entityId?: string; leadId?: string } | null>(null);
  const [queueCount, setQueueCount] = useState(0);

  const refreshQueueCount = useCallback(() => {
    void voxService.list({ status: 'processing_failed,failed_permanently', limit: 1 })
      .then((r) => setQueueCount(r.total)).catch(() => {});
  }, []);
  useEffect(() => { refreshQueueCount(); }, [screen, refreshQueueCount]);

  const go = (s: VoxScreen) => setScreen(s);
  const openConversation = (id: string) => { setConversationId(id); setScreen('review'); };
  const openDossier = (subject: { entityId?: string; leadId?: string }) => {
    if (!subject.entityId && !subject.leadId) return;
    setDossierSubject(subject); setScreen('dossier');
  };

  const showTabs = ['memory', 'all', 'queue', 'dossier', 'legacy'].includes(screen);

  return (
    <div className="vox-app">
      <div style={{ display: 'none' }} dangerouslySetInnerHTML={{ __html: `<svg>${VOX_SPRITE}</svg>` }} />
      <div className="screen active" data-screen={screen}>
        {screen === 'memory' && (
          <MemoryScreen go={go} openConversation={openConversation} openDossier={openDossier} />)}
        {screen === 'all' && (
          <AllScreen go={go} openConversation={openConversation} openDossier={openDossier} />)}
        {screen === 'queue' && (
          <QueueScreen go={go} openConversation={openConversation} />)}
        {screen === 'dossier' && dossierSubject && (
          <DossierScreen subject={dossierSubject} go={go} openConversation={openConversation} />)}
        {screen === 'legacy' && (
          <div className="app-body">
            <button className="review-back" onClick={() => go('memory')}>‹ Memory</button>
            <ReportsTab epoch={0} active />
          </div>
        )}
        {screen === 'record' && (
          <VoxRecordScreen onClose={() => go('memory')} onCaptured={openConversation} />)}
        {screen === 'review' && conversationId && (
          <VoxReviewScreen conversationId={conversationId}
            onBack={() => go('memory')} onQueue={() => go('queue')}
            onDossier={openDossier} onSaved={() => {}}
            onFiled={refreshQueueCount} />)}
      </div>
      {showTabs && <BottomNav screen={screen} queueCount={queueCount} go={go} />}
    </div>
  );
}
