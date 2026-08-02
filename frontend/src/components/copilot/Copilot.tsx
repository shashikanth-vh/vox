import { useEffect, useRef, useState } from 'react';
import { Box, Typography, InputBase } from '@mui/material';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../auth/AuthContext';
import { useSearch } from '../../context/SearchContext';
import { computeToday } from '../../pages/Today/compute';
import { computeDashboard } from '../../pages/Dashboard/compute';
import { stageRequestService, canApproveLine, type StageLine } from '../../services/stageRequestService';
import { lendingService } from '../../services/lendingService';
import { syndicationService } from '../../services/syndicationService';
import { assetMonService } from '../../services/assetMonService';
import { clientsService } from '../../services/clientsService';
import { referenceService } from '../../services/referenceService';
import { db, today } from '../../api/atlasStore';
import { fmt } from '../../utils/format';
import { LIFE_STAGES } from '../../components/common/Pills';
import { tokens } from '../../theme';

const MOBILE = '@media (max-width:760px)';

// ---- chat building blocks (v17 .cb-* look) ----
function Bubble({ children }: { children: React.ReactNode }) {
  return (
    <Box sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: '3px 14px 14px 14px', p: '10px 12px', boxShadow: '0 1px 2px rgba(15,30,44,.06)', fontSize: 12.8, lineHeight: 1.45 }}>
      {children}
    </Box>
  );
}
function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 1.25, py: '3px', fontSize: 12.4 }}>
      <Box component="span" sx={{ color: tokens.muted }}>{k}</Box>
      <Box component="b" sx={{ textAlign: 'right' }}>{v}</Box>
    </Box>
  );
}
function ActBtn({ onClick, tone, children }: { onClick: () => void; tone?: 'ok' | 'bad'; children: React.ReactNode }) {
  const toneSx = tone === 'ok' ? { bgcolor: tokens.okBg, color: tokens.ok, borderColor: '#BFE3CC', fontWeight: 700 }
    : tone === 'bad' ? { bgcolor: tokens.badBg, color: tokens.bad, borderColor: '#F0CFC5', fontWeight: 700 } : {};
  return (
    <Box component="button" onClick={onClick}
      sx={{ border: `1px solid ${tokens.line}`, background: '#fff', borderRadius: '8px', px: '11px', py: '6px', fontSize: 11.8, cursor: 'pointer', fontFamily: 'inherit', '&:hover': { borderColor: tokens.tealHi, color: tokens.teal }, ...toneSx }}>
      {children}
    </Box>
  );
}
function Chip({ on, onClick, children }: { on?: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <Box component="button" onClick={onClick}
      sx={{ border: `1px solid ${on ? tokens.tealHi : tokens.line}`, background: '#fff', borderRadius: '99px', px: '11px', py: '5px', fontSize: 11.6, cursor: 'pointer', fontFamily: 'inherit', color: on ? tokens.teal : 'inherit', fontWeight: on ? 600 : 400, '&:hover': { borderColor: tokens.tealHi, color: tokens.teal } }}>
      {children}
    </Box>
  );
}
const Btns = ({ children }: { children: React.ReactNode }) => <Box sx={{ display: 'flex', gap: '6px', flexWrap: 'wrap', mt: '7px' }}>{children}</Box>;
const Chips = ({ children }: { children: React.ReactNode }) => <Box sx={{ display: 'flex', gap: '5px', flexWrap: 'wrap', mt: '7px' }}>{children}</Box>;
const Card = ({ children }: { children: React.ReactNode }) => <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: '9px', p: '7px 9px', mt: '7px', bgcolor: '#FBFDFD' }}>{children}</Box>;
const Pill = ({ sev }: { sev: 'red' | 'amber' }) => (
  <Box component="span" sx={{ display: 'inline-block', borderRadius: '99px', px: '8px', py: '2px', fontSize: 10.5, fontWeight: 700, mr: 0.75, bgcolor: sev === 'red' ? '#FBE9E4' : '#FBF3E1', color: sev === 'red' ? '#A93B22' : '#8F6512' }}>{sev.toUpperCase()}</Box>
);

interface Msg { who: 'u' | 'b'; node: React.ReactNode; }

export default function Copilot() {
  const { user } = useAuth();
  const nav = useNavigate();
  const { setSearch } = useSearch();
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [online, setOnline] = useState(typeof navigator !== 'undefined' ? navigator.onLine : true);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const on = () => setOnline(true), off = () => setOnline(false);
    window.addEventListener('online', on); window.addEventListener('offline', off);
    return () => { window.removeEventListener('online', on); window.removeEventListener('offline', off); };
  }, []);
  useEffect(() => { if (open && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight; }, [msgs, open]);

  const pushBot = (node: React.ReactNode) => setMsgs((p) => [...p, { who: 'b', node }]);
  const cname = (code: string) => clientsService.get(code).name || code;
  const pendingForMe = () => stageRequestService.pending().filter((r) => canApproveLine(user.roles, r.line));
  const badge = pendingForMe().length;

  const findCompany = (qs: string): string | null => {
    qs = qs.toLowerCase().trim(); if (!qs) return null;
    const clients: Record<string, any> = db().clients || {};
    const codes = Object.keys(clients);
    return codes.find((c) => c.toLowerCase() === qs) || codes.find((c) => (clients[c]?.name || '').toLowerCase().includes(qs)) || null;
  };
  const go = (path: string, term?: string) => { if (term) setSearch(term); setOpen(false); nav(path); };

  // ---- intent cards ----
  const helpCard = () => (
    <Bubble>
      I answer from the live Register. Try:
      <Chips>
        <Chip onClick={() => ask('book summary')}>📊 Book summary</Chip>
        <Chip onClick={() => ask('what is stuck')}>🚨 What’s stuck?</Chip>
        <Chip onClick={() => ask('pending approvals')}>✅ Approvals</Chip>
        <Chip onClick={() => setInput('find ')}>🔎 Find a client</Chip>
        <Chip onClick={() => go('/tools')}>🧰 Tools</Chip>
      </Chips>
      <Typography sx={{ fontSize: 11, color: tokens.muted, mt: 0.75 }}>
        Stage moves with approval: <i>“move Sunvik lending to Sanctioned”</i>. Offline it computes locally from the Register.
      </Typography>
    </Bubble>
  );

  const summaryCard = () => {
    const d = computeDashboard(); const t = computeToday();
    return (
      <Bubble>
        <b>Book snapshot · {today()}</b>
        <KV k="Closed book" v={`₹${fmt(d.hero.closedAmt, 0)} Cr · ${d.hero.closedN} deals`} />
        <KV k="Active pipeline" v={`₹${fmt(d.hero.pipeAmt, 0)} Cr · ${d.hero.pipeN} deals`} />
        <KV k="Live mandates" v={d.hero.liveMandates} />
        <KV k="Needs attention" v={`${t.reds} red · ${t.ambers} amber`} />
        <Btns>
          <ActBtn onClick={() => go('/dashboard')}>Open dashboard</ActBtn>
          <ActBtn onClick={() => go('/today')}>Open Today</ActBtn>
        </Btns>
      </Bubble>
    );
  };

  const stuckCard = () => {
    const t = computeToday();
    const att = [
      ...t.stageRed.map((a) => ({ sev: a.sev as 'red' | 'amber', name: a.isLead || t.nameOf(a.code), detail: `${a.rule} · ${a.why} · with ${a.owner}`, code: a.isLead ? '' : a.code })),
      ...t.contactRed.map((a) => ({ sev: a.sev as 'red' | 'amber', name: a.co, detail: `CONTACT · quiet ${a.days}d · with ${a.owner}`, code: a.code })),
      ...t.stageAmber.map((a) => ({ sev: a.sev as 'red' | 'amber', name: a.isLead || t.nameOf(a.code), detail: `${a.rule} · ${a.why} · with ${a.owner}`, code: a.isLead ? '' : a.code })),
      ...t.contactAmber.map((a) => ({ sev: a.sev as 'red' | 'amber', name: a.co, detail: `CONTACT · quiet ${a.days}d · with ${a.owner}`, code: a.code })),
    ].slice(0, 5);
    if (!att.length) return <Bubble>Nothing is stuck. Clean slate 🎉</Bubble>;
    return (
      <Bubble>
        <b>Top of the worklist</b>
        {att.map((a, i) => (
          <Card key={i}>
            <Pill sev={a.sev} /><b>{a.name}</b>
            <Typography sx={{ fontSize: 11, color: tokens.muted, mt: 0.3 }}>{a.detail}</Typography>
            {a.code && <Btns><ActBtn onClick={() => go('/deals', cname(a.code))}>Open</ActBtn></Btns>}
          </Card>
        ))}
        <Btns><ActBtn onClick={() => go('/today')}>Full worklist →</ActBtn></Btns>
      </Bubble>
    );
  };

  const approvalsCard = () => {
    const pend = pendingForMe();
    if (!pend.length) return <Bubble>No stage-change requests waiting on you.</Bubble>;
    const decide = (id: string, ok: boolean) => { stageRequestService.decide(id, ok, user.full); pushBot(<Bubble>{ok ? 'Approved' : 'Rejected'} the request.</Bubble>); };
    return (
      <Bubble>
        <b>Pending approvals ({pend.length})</b>
        {pend.map((r) => (
          <Card key={r.id}>
            <b>{cname(r.code)}</b> <Box component="span" sx={{ color: tokens.muted, fontSize: 11.5 }}>{r.line}: {r.currentStage || '—'} → {r.targetStage}</Box>
            <Typography sx={{ fontSize: 11, color: tokens.muted, mt: 0.3 }}>by {r.by} · {r.reason}</Typography>
            <Btns>
              <ActBtn tone="ok" onClick={() => decide(r.id, true)}>Approve</ActBtn>
              <ActBtn tone="bad" onClick={() => decide(r.id, false)}>Reject</ActBtn>
            </Btns>
          </Card>
        ))}
      </Bubble>
    );
  };

  const companyCard = (code: string) => {
    const c = clientsService.get(code);
    const L = lendingService.byCode(code); const Y = syndicationService.byCode(code);
    const setLife = (v: string) => { clientsService.update(code, { lifecycle: v } as any, user.full); pushBot(<Bubble>Lifecycle for <b>{cname(code)}</b> set to <b>{v}</b>.</Bubble>); };
    return (
      <Bubble>
        <b>{c.name || code}</b> <Box component="span" sx={{ fontFamily: 'ui-monospace,Consolas,monospace', fontSize: 11.4, color: tokens.teal, fontWeight: 600 }}>{code}</Box>
        <KV k="Sector" v={`${c.sector || '—'} · ${c.lens || ''}`} />
        {!!L.length && <KV k="Lending" v={`₹${fmt(Number(L[0].amt), 1)} Cr · ${L[0].stage}`} />}
        {!!Y.length && <KV k="Platform Deals" v={`₹${fmt(Number(Y[0].amt), 1)} Cr · ${Y[0].status} · ${(Y[0].lenders || []).length} lenders`} />}
        <Btns><ActBtn onClick={() => go('/deals', c.name || code)}>Open profile</ActBtn></Btns>
        <Typography sx={{ fontSize: 11, color: tokens.muted, mt: '7px', mb: '3px' }}>Lifecycle (Vistaar journey) — tap to set:</Typography>
        <Chips>{LIFE_STAGES.map((v) => <Chip key={v} on={(c.lifecycle || 'Prospect') === v} onClick={() => setLife(v)}>{v}</Chip>)}</Chips>
      </Bubble>
    );
  };

  const stageMove = (low: string): React.ReactNode => {
    const m = low.match(/move\s+(.+?)\s+(lending|syn(?:dication)?|am|asset)\s+to\s+(.+)/);
    if (!m) return <Bubble>Say it like: <i>“move Sunvik lending to Sanctioned”</i> or <i>“move Sunvik syndication to IM Circulated”</i>.</Bubble>;
    const code = findCompany(m[1]);
    if (!code) return <Bubble>Couldn’t find a client matching “{m[1]}”.</Bubble>;
    const line: StageLine = m[2].startsWith('lend') ? 'Lending' : (m[2].startsWith('am') || m[2].startsWith('asset')) ? 'Asset Monetisation' : 'Syndication';
    const refKey = line === 'Lending' ? 'Lending Stage' : line === 'Syndication' ? 'Status of Proposal' : 'Asset Mon Status';
    const list = referenceService.getRefSync(refKey);
    const to = list.find((s) => s.toLowerCase() === m[3].trim()) || list.find((s) => s.toLowerCase().includes(m[3].trim()));
    if (!to) return <Bubble>“{m[3]}” isn’t a valid {line} stage. Valid: {list.join(' · ')}</Bubble>;
    const row: any = line === 'Lending' ? lendingService.byCode(code)[0] : line === 'Syndication' ? syndicationService.byCode(code)[0] : assetMonService.byCode(code)[0];
    if (!row) return <Bubble>No {line} line found for {cname(code)}.</Bubble>;
    const current = line === 'Lending' ? row.stage : row.status;
    if (current === to) return <Bubble>Already at <b>{to}</b>.</Bubble>;
    const req = stageRequestService.create({ code, line, refId: row.id, currentStage: current, targetStage: to, reason: 'Requested via Copilot' }, user.full);
    // Approvers get their move applied straight away; everyone else raises a request.
    if (canApproveLine(user.roles, line)) {
      stageRequestService.decide(req.id, true, user.full);
      return <Bubble>Applied: <b>{cname(code)}</b> {line} → <b>{to}</b>.</Bubble>;
    }
    return <Bubble>Request raised: {cname(code)} {line} <b>{current || '—'} → {to}</b>. Waiting for an approver.</Bubble>;
  };

  const ask = (text: string) => {
    const t = text.trim(); if (!t) return;
    setMsgs((p) => [...p, { who: 'u', node: t }]);
    const low = t.toLowerCase();
    let node: React.ReactNode;
    if (/^(hi|hello|hey|help|\?)/.test(low)) node = helpCard();
    else if (/summary|book|pipeline|snapshot|dashboard/.test(low)) node = summaryCard();
    else if (/stuck|attention|red|amber|today|worklist/.test(low)) node = stuckCard();
    else if (/approval/.test(low)) node = approvalsCard();
    else if (/^move\s/.test(low) || /advance/.test(low)) node = stageMove(low);
    else if (/mail|email|scrape/.test(low)) { node = <Bubble>Opening Tools — use <b>Mail intake</b> to paste an email.</Bubble>; go('/tools'); }
    else if (/application|apply|onboard|form/.test(low)) { node = <Bubble>Opening Tools — use the <b>Application form</b>.</Bubble>; go('/tools'); }
    else {
      const code = findCompany(low.replace(/^(find|show|open)\s+/, ''));
      node = code ? companyCard(code) : <Bubble>I didn’t catch that. Type <b>help</b> for what I can do.</Bubble>;
    }
    pushBot(node);
  };

  const toggle = () => {
    const next = !open; setOpen(next);
    if (next && !msgs.length) pushBot(helpCard());
  };
  const send = () => { const v = input.trim(); if (!v) return; ask(v); setInput(''); };

  return (
    <>
      {/* Floating bubble (FAB) */}
      <Box
        component="button"
        className="no-print"
        title="ATLAS Copilot"
        onClick={toggle}
        sx={{
          position: 'fixed', right: 18, bottom: 18, zIndex: 180, width: 56, height: 56, borderRadius: '50%', border: 'none',
          background: `linear-gradient(135deg, ${tokens.teal} 0%, ${tokens.tealHi} 100%)`, color: '#fff', fontSize: 24, cursor: 'pointer',
          boxShadow: '0 10px 30px rgba(13,115,119,.5)', display: 'flex', alignItems: 'center', justifyContent: 'center',
          transition: 'transform .15s ease', '&:hover': { transform: 'translateY(-2px)' },
          [MOBILE]: { right: 14, bottom: 'calc(74px + env(safe-area-inset-bottom))' },
        }}
      >
        💬
        {badge > 0 && (
          <Box sx={{ position: 'absolute', top: -3, right: -3, minWidth: 19, height: 19, px: '4px', borderRadius: '99px', bgcolor: tokens.bad, color: '#fff', fontSize: 10.5, fontWeight: 800, border: '2px solid #fff', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>{badge}</Box>
        )}
      </Box>

      {/* Panel */}
      <Box
        className="no-print"
        sx={{
          position: 'fixed', right: 18, bottom: 84, zIndex: 181, width: 390, maxWidth: 'calc(100vw - 24px)', height: 540, maxHeight: '72vh',
          bgcolor: tokens.card, border: `1px solid ${tokens.line}`, borderRadius: '18px', boxShadow: '0 30px 80px rgba(6,14,26,.4)',
          display: 'flex', flexDirection: 'column', transition: 'opacity .22s ease, transform .22s ease',
          opacity: open ? 1 : 0, pointerEvents: open ? 'auto' : 'none', transform: open ? 'none' : 'translateY(14px)',
          [MOBILE]: { right: 0, left: 0, bottom: 0, width: '100vw', maxWidth: '100vw', height: '78vh', maxHeight: '78vh', borderRadius: '18px 18px 0 0' },
        }}
      >
        {/* header */}
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', p: '13px 16px', borderBottom: `1px solid ${tokens.line}`, background: 'linear-gradient(100deg,#1B2A4A,#0E3A40)', color: '#fff', borderRadius: '18px 18px 0 0' }}>
          <Box>
            <Typography sx={{ fontWeight: 700, fontSize: 14 }}>ATLAS Copilot</Typography>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: 10.8, color: '#9FD8C8', mt: '2px' }}>
              <Box sx={{ width: 7, height: 7, borderRadius: '50%', bgcolor: online ? '#4ADE80' : '#C98A1B', boxShadow: online ? '0 0 6px #4ADE80' : 'none' }} />
              {online ? 'Online — sync ready' : 'Offline copy — saved locally'}
            </Box>
          </Box>
          <Box component="button" onClick={() => setOpen(false)} sx={{ background: 'none', border: 'none', color: '#C8D6E2', fontSize: 19, cursor: 'pointer', lineHeight: 1 }}>×</Box>
        </Box>

        {/* messages */}
        <Box ref={scroller} sx={{ flex: 1, overflow: 'auto', p: '13px', display: 'flex', flexDirection: 'column', gap: '9px', bgcolor: '#F6F9FA' }}>
          {msgs.map((m, i) => (
            <Box key={i} sx={{ maxWidth: '92%', alignSelf: m.who === 'u' ? 'flex-end' : 'flex-start' }}>
              {m.who === 'u'
                ? <Box sx={{ background: `linear-gradient(135deg, ${tokens.teal}, ${tokens.tealHi})`, color: '#fff', borderRadius: '14px 14px 3px 14px', p: '9px 12px', fontSize: 12.8, lineHeight: 1.45 }}>{m.node}</Box>
                : m.node}
            </Box>
          ))}
        </Box>

        {/* input */}
        <Box sx={{ display: 'flex', gap: '7px', p: '10px', borderTop: `1px solid ${tokens.line}` }}>
          <InputBase
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') send(); }}
            placeholder="Ask about the book, a client, approvals…"
            sx={{ flex: 1, border: `1px solid ${tokens.line}`, borderRadius: '99px', px: '15px', py: '4px', fontSize: 12.8 }}
          />
          <Box component="button" onClick={send} sx={{ width: 40, height: 38, borderRadius: '50%', border: 'none', bgcolor: tokens.tealHi, color: '#fff', fontSize: 15, cursor: 'pointer', flexShrink: 0 }}>➤</Box>
        </Box>
      </Box>
    </>
  );
}
