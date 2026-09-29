import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, IconButton,
  Tooltip, Typography, useMediaQuery,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import DownloadIcon from '@mui/icons-material/Download';
import TrackChangesIcon from '@mui/icons-material/TrackChanges';
import OpenInFullIcon from '@mui/icons-material/OpenInFull';
import { tokens } from '../../theme';
import { apiErr } from '../../api/http';
import { panoramaService, type Panorama, type PanoramaLine, type TracxnFinancials,
  type TracxnSeries, type RiskGrade } from '../../services/panoramaService';
import { BAND_COLOR, RiskGradeChip, RiskGradeDetails } from './RiskGrade';
import { documentsService } from '../../services/documentsService';
import { classify, fetchTerm, SEV_LABEL, type Article, type Severity }
  from '../../services/newsService';

/**
 * Company 360 — the company brief.
 *
 * Reads top to bottom the way a credit committee reads: the VERDICT (where we
 * stand, how they are doing, what is next), then TIME (every dated fact in
 * order), then MONEY (each product line, every lender and where it stands),
 * then the NUMBERS (filed financials from Tracxn with derived ratios), and
 * only then the depth tabs — engagements, people, documents, news and the
 * risk report (which reads the documents itself, so there is no ask-box). Every card is built from PRISM's own records, so it renders
 * for every company; the risk grade (a model reading) sits beside it, never
 * under it. Where a card has nothing it says what is missing and where it
 * would come from.
 */

const CHART_TEAL = '#0D9488';
const INK = '#17252B';

// One colour per financial series, everywhere it appears; different scales
// get their own small chart rather than a shared axis.
const FIN_SERIES: [key: string, label: string, color: string][] = [
  ['revenue', 'Revenue', '#0D9488'],
  ['ebitda', 'EBITDA', '#B45309'],
  ['net_profit', 'PAT', '#6D5FD3'],
];
// The snapshot table, in the order a lender reads a P&L and balance sheet.
const SNAPSHOT_ROWS: [key: string, label: string][] = [
  ['revenue', 'Revenue'], ['ebitda', 'EBITDA'], ['net_profit', 'PAT'],
  ['bs_equity', 'Net worth (equity)'], ['bs_liability', 'Total liabilities'],
  ['bs_assets', 'Total assets'], ['cf_operating', 'Operating cash flow'],
  ['cf_financing', 'Financing cash flow'], ['employees', 'Employees'],
];
const STAGE_COLOR = { flight: CHART_TEAL, done: '#1B7A45', hold: '#B45309',
  dead: '#9AA8AF' };
const SEV_BG: Record<Severity, string> = {
  RED: tokens.bad, AMBER: tokens.warn, GREEN: tokens.ok, BLUE: '#1F6FA8',
};


// ---------- small formatters ------------------------------------------------

function fmtCr(v: number | null | undefined): string {
  return v == null ? '—' : `₹${Number(v).toLocaleString('en-IN',
    { maximumFractionDigits: 1 })} Cr`;
}
function finNum(v: number): string {
  return Math.abs(v) >= 1000 ? Math.round(v).toLocaleString('en-IN')
    : v.toLocaleString('en-IN', { maximumFractionDigits: 1 });
}
function fmtDay(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso.slice(0, 10)
    : d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });
}
function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso.slice(0, 10)
    : d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}
function daysAgo(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : Math.max(0, Math.round((Date.now() - t) / 864e5));
}
function pct(n: number): string {
  return `${n >= 0 ? '+' : '−'}${Math.abs(n).toLocaleString('en-IN',
    { maximumFractionDigits: 0 })}%`;
}
/** The earliest of several ISO dates, ignoring blanks and junk. */
function earliest(...xs: (string | null | undefined)[]): string | null {
  return xs.filter((x): x is string => !!x && !Number.isNaN(Date.parse(x))).sort()[0] || null;
}

// Lender statuses, read the way the desk means them.
const DEAD_RE = /declin|dropp|reject|withdr|not interested|pass/i;
const ADV_RE = /ip received|in.?principle|sanction|approv|term sheet|disburs|credit approved/i;
function lenderBucket(status: string | null | undefined): 'dead' | 'advanced' | 'progress' {
  const s = status || '';
  if (DEAD_RE.test(s)) return 'dead';
  if (ADV_RE.test(s)) return 'advanced';
  return 'progress';
}

// ---------- small visual parts ------------------------------------------------

function StackBar({ segs, height = 5 }: {
  segs: { value: number; color: string }[]; height?: number;
}) {
  const live = segs.filter((x) => x.value > 0);
  const total = live.reduce((a, x) => a + x.value, 0);
  if (!total) return null;
  return (
    <Box sx={{ display: 'flex', gap: '2px', width: '100%', height, borderRadius: 3,
      overflow: 'hidden' }}>
      {live.map((x, i) => (
        <Box key={i} sx={{ flex: `${x.value} 0 0`, bgcolor: x.color, minWidth: 3 }} />))}
    </Box>
  );
}

function Eyebrow({ children, right }: { children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.8 }}>
      <Typography sx={{ fontSize: 10.5, fontWeight: 800, letterSpacing: '.08em',
        color: '#44535B', textTransform: 'uppercase' }}>{children}</Typography>
      <Box sx={{ flex: 1 }} />
      {right}
    </Box>
  );
}

function Card({ children, stripe, sx }: {
  children: React.ReactNode; stripe?: string; sx?: any;
}) {
  return (
    <Box sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: '12px',
      p: '12px 14px', minWidth: 0, borderTop: stripe ? `3px solid ${stripe}` : undefined,
      ...sx }}>{children}</Box>
  );
}

function Bullet({ children, dot }: { children: React.ReactNode; dot?: string }) {
  return (
    <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
      <Box sx={{ width: 6, height: 6, borderRadius: 99, mt: '7px', flexShrink: 0,
        bgcolor: dot || '#9AA8AF' }} />
      <Typography sx={{ fontSize: 12.4, color: INK, lineHeight: 1.5, minWidth: 0 }}>
        {children}</Typography>
    </Box>
  );
}

/** A grouped bar chart for one card: up to two series that share a scale
 *  (EBITDA with PAT; net worth with liabilities), every bar labelled with its
 *  value, every financial year labelled, negatives below a zero line. */
function BarChart({ title, unit, series, years, big, onExpand }: {
  title: string; unit?: string | null;
  series: { label: string; color: string; points: { year: number; value: number; fy?: string | null }[] }[];
  years: number[]; big?: boolean; onExpand?: () => void;
}) {
  const live = series.filter((sr) => sr.points.length);
  if (!live.length || !years.length) return null;
  const W = big ? 900 : 360, H = big ? 360 : 168, top = big ? 34 : 24, bottom = big ? 320 : 142, L = 8, R = 8;
  const fs = big ? { v: 13, y: 13, r: 3 } : { v: 9.2, y: 9.4, r: 2 };
  const vals = live.flatMap((sr) => sr.points.filter((x) => years.includes(x.year)).map((x) => x.value));
  const hi = Math.max(0, ...vals), lo = Math.min(0, ...vals);
  const span = hi - lo || 1;
  const y = (v: number) => top + ((hi - v) / span) * (bottom - top);
  const zero = y(0);
  const group = (W - L - R) / years.length;
  const bw = Math.min(big ? 70 : 30, (group * 0.72) / live.length);
  const at = (sr: typeof live[number], yr: number) => sr.points.find((x) => x.year === yr) || null;
  const fy = (yr: number) => `FY${String(yr).slice(2)}`;
  return (
    <Box sx={{ minWidth: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap', mb: 0.3 }}>
        <Typography sx={{ fontSize: big ? 13 : 10.8, fontWeight: 800, color: '#44535B', letterSpacing: '.04em' }}>{title}</Typography>
        {live.map((sr) => (
          <Box key={sr.label} sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
            <Box sx={{ width: 7, height: 7, borderRadius: 99, bgcolor: sr.color }} />
            <Typography sx={{ fontSize: 10, color: tokens.muted }}>{sr.label}</Typography>
          </Box>))}
        <Box sx={{ flex: 1 }} />
        {unit && <Typography sx={{ fontSize: 9.6, color: tokens.muted }}>{unit}</Typography>}
        {onExpand && <Tooltip title="Open large"><IconButton className="no-print" size="small" onClick={onExpand} sx={{ p: 0.3 }}>
          <OpenInFullIcon sx={{ fontSize: 14, color: tokens.muted }} /></IconButton></Tooltip>}
      </Box>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', display: 'block' }}>
        {[0.25, 0.5, 0.75, 1].map((f) => { const gy = top + (1 - f) * (bottom - top);
          return <line key={f} x1={L} x2={W - R} y1={gy} y2={gy} stroke="#F0F3F5" strokeWidth={1} />; })}
        <line x1={L} x2={W - R} y1={zero} y2={zero} stroke="#C9D2D7" strokeWidth={1} />
        {years.map((yr, gi) => {
          const gx = L + gi * group + (group - bw * live.length - 3 * (live.length - 1)) / 2;
          return (
            <g key={yr}>
              {live.map((sr, si) => { const pt = at(sr, yr); if (!pt) return null;
                const x = gx + si * (bw + 3); const yv = y(pt.value);
                const h = Math.max(1.5, Math.abs(yv - zero));
                return (
                  <Tooltip key={sr.label} arrow placement="top" enterDelay={100}
                    title={`${sr.label} · ${pt.fy || pt.year}: ${finNum(pt.value)}${unit ? ` ${unit}` : ''}`}>
                    <g style={{ cursor: 'default' }}>
                      <rect x={x - 2} y={top - 6} width={bw + 4} height={bottom - top + 12} fill="transparent" />
                      <rect x={x} y={Math.min(yv, zero)} width={bw} height={h} rx={fs.r} fill={sr.color} />
                      <text x={x + bw / 2} y={pt.value >= 0 ? Math.min(yv, zero) - 4 : Math.max(yv, zero) + fs.v + 2}
                        textAnchor="middle" fontSize={fs.v} fontWeight="700" fill={INK}>{finNum(pt.value)}</text>
                    </g>
                  </Tooltip>); })}
              <text x={L + gi * group + group / 2} y={H - 6} textAnchor="middle" fontSize={fs.y} fill="#7B8A92">{fy(yr)}</text>
            </g>);
        })}
      </svg>
    </Box>
  );
}

function FallbackBars({ rows }: { rows: [label: string, value: number | null, color: string][] }) {
  const live = rows.filter((r) => r[1] != null) as [string, number, string][];
  const max = Math.max(1, ...live.map((r) => Math.abs(r[1])));
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.6 }}>
      {live.map(([lab, v, col]) => (
        <Box key={lab} sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Typography sx={{ fontSize: 10.8, fontWeight: 700, color: tokens.muted,
            width: 58, flexShrink: 0 }}>{lab}</Typography>
          <Box sx={{ flex: 1, height: 7, bgcolor: '#EEF1F3', borderRadius: 4, minWidth: 0 }}>
            <Box sx={{ width: `${Math.max(2, (Math.abs(v) / max) * 100)}%`, height: 7,
              borderRadius: 4, bgcolor: col, opacity: v < 0 ? 0.55 : 1 }} />
          </Box>
          <Typography sx={{ fontSize: 11.4, fontWeight: 800, color: INK, width: 84,
            textAlign: 'right', flexShrink: 0 }}>{v < 0 ? '−' : ''}{fmtCr(Math.abs(v))}</Typography>
        </Box>))}
    </Box>
  );
}

function Ladder({ ladder, current }: { ladder: string[]; current: string | null }) {
  const idx = current ? ladder.indexOf(current) : -1;
  if (idx < 0) return null;
  const done = idx === ladder.length - 1;
  const mark = done ? STAGE_COLOR.done : STAGE_COLOR.hold;
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
      <Box sx={{ display: 'flex', gap: '5px' }}>
        {ladder.map((s, i) => (
          <Box key={s} sx={{ height: 5, flex: 1, borderRadius: '3px',
            bgcolor: i < idx ? CHART_TEAL : i === idx ? mark : '#E7ECEF' }} />))}
      </Box>
      <Box sx={{ display: 'flex' }}>
        {ladder.map((s, i) => (
          <Typography key={s} sx={{ flex: 1, fontSize: 8.6, lineHeight: 1.2,
            color: i === idx ? mark : tokens.muted, fontWeight: i === idx ? 700 : 400 }}>
            {s.replace(' Completed', '').replace('Ready for Disbursement', 'Ready')}
          </Typography>))}
      </Box>
    </Box>
  );
}

// ---------- the timeline ------------------------------------------------------

type Ev = { at: number; date: string; label: string; color: string; big?: boolean; detail?: string; who?: string };

function Timeline({ events }: { events: Ev[] }) {
  if (!events.length) return null;
  // Facts in ORDER, equally spaced: the dates carry the time, the axis does
  // not have to — one 2019 incorporation must not squeeze a busy month into
  // a smear. Labels cycle through four tiers so neighbours never overlap.
  const n = events.length;
  // Nothing is hidden behind an ellipsis: the gap between facts is sized to the
  // longest label (the card scrolls sideways), and the full text rides a tooltip.
  const longest = Math.max(...events.map((e) => e.label.length), 18);
  const step = Math.min(300, Math.max(118, Math.round(longest * 6.4) + 20));
  const W = Math.max(720, 120 + step * (n - 1)), H = 168, L = 60, R = 60, Y = 84;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} style={{ display: 'block' }}>
      <line x1={L - 12} x2={W - 48} y1={Y} y2={Y} stroke="#DCE3E6" strokeWidth={2} />
      {events.map((e, i) => {
        const cx = n > 1 ? L + i * step : W / 2;
        void R;
        const tier = i % 4;                       // 0 above near, 1 below near, 2 above far, 3 below far
        const above = tier === 0 || tier === 2;
        const far = tier >= 2;
        const stem = far ? 46 : 18;
        const ty = above ? Y - stem : Y + stem;
        return (
          <Tooltip key={i} arrow placement={above ? 'top' : 'bottom'} enterDelay={150}
            componentsProps={{ tooltip: { sx: { maxWidth: 360, bgcolor: '#fff', color: INK,
              border: `1px solid ${tokens.line}`, boxShadow: '0 6px 24px rgba(23,37,43,.14)', p: '10px 12px' } },
              arrow: { sx: { color: '#fff', '&::before': { border: `1px solid ${tokens.line}` } } } }}
            title={<Box>
              <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>{e.date}{e.who ? ` · ${e.who}` : ''}</Typography>
              <Typography sx={{ fontSize: 12.4, fontWeight: 700, color: INK }}>{e.label}</Typography>
              {e.detail && <Typography sx={{ fontSize: 11.6, color: INK, whiteSpace: 'pre-wrap', mt: 0.5,
                maxHeight: 220, overflowY: 'auto' }}>{e.detail}</Typography>}
            </Box>}>
            <g style={{ cursor: e.detail ? 'pointer' : 'default' }}>
              <rect x={cx - 60} y={above ? ty - 22 : Y - 8} width={120} height={above ? 34 : ty + 26 - Y} fill="transparent" />
              <line x1={cx} x2={cx} y1={Y} y2={ty + (above ? 6 : -6)} stroke="#DCE3E6" />
              <circle cx={cx} cy={Y} r={e.big ? 7 : 5} fill={e.color} />
              <text x={cx} y={above ? ty - 12 : ty + 22} textAnchor="middle" fontSize="9.4" fill="#7B8A92">{e.date}</text>
              <text x={cx} y={above ? ty : ty + 10} textAnchor="middle" fontSize="10.2"
                fontWeight={e.big ? 700 : 500} fill={INK}>{e.label}</text>
            </g>
          </Tooltip>);
      })}
    </svg>
  );
}

export function Company360Chip({ onClick }: { onClick: () => void }) {
  return (
    <Tooltip title="Company 360 — everything PRISM knows">
      <Chip size="small" clickable icon={<TrackChangesIcon sx={{ fontSize: 14 }} />}
        label="360°" onClick={(e) => { e.stopPropagation(); onClick(); }}
        sx={{ height: 22, fontSize: 11.5, fontWeight: 700, color: tokens.tealHi,
          border: `1.5px solid ${tokens.tealHi}`, bgcolor: '#F0F8F6',
          '& .MuiChip-icon': { color: tokens.tealHi } }} />
    </Tooltip>
  );
}

type Tab = 'engagements' | 'people' | 'documents' | 'news' | 'risk';

// ---------- the dialog ----------------------------------------------------------

export default function Company360Dialog({ open, entityId, company, onClose }: {
  open: boolean; entityId?: string | null; company?: string; onClose: () => void;
}) {
  const small = useMediaQuery('(max-width:700px)');
  // A sister company (promoter group) opens IN this dialog; × or "back" returns.
  const [target, setTarget] = useState<{ entityId?: string | null; company?: string } | null>(null);
  const effEntityId = target ? target.entityId : entityId;
  const effCompany = target ? target.company : company;
  const close = () => { setTarget(null); onClose(); };
  const [p, setP] = useState<Panorama | null>(null);
  const [err, setErr] = useState('');
  const [news, setNews] = useState<Article[] | null>(null);
  const [newsErr, setNewsErr] = useState('');
  const [tab, setTab] = useState<Tab>('engagements');
  const [briefOpen, setBriefOpen] = useState(false);
  const [newsSev, setNewsSev] = useState<Severity | null>(null);
  const [trx, setTrx] = useState<TracxnFinancials | null>(null);
  const [trxBusy, setTrxBusy] = useState(false);
  const [trxErr, setTrxErr] = useState('');
  const [grade, setGrade] = useState<RiskGrade | null>(null);
  const [gradeBusy, setGradeBusy] = useState(false);
  const [gradeErr, setGradeErr] = useState('');
  const [dlErr, setDlErr] = useState('');
  const [dlBusy, setDlBusy] = useState(false);
  const [bigChart, setBigChart] = useState<string | null>(null);
  const pRef = useRef<Panorama | null>(null);

  useEffect(() => {
    if (!open) return;
    setP(null); setErr(''); setNews(null); setNewsErr(''); pRef.current = null;
    setTrx(null); setTrxBusy(false); setTrxErr('');
    setGrade(null); setGradeBusy(false); setGradeErr(''); setDlErr(''); setDlBusy(false); setBigChart(null);
    setTab('engagements'); setNewsSev(null); setBriefOpen(false);
    let alive = true;
    panoramaService.get({ entityId: effEntityId, company: effCompany })
      .then((r) => {
        if (!alive) return;
        setP(r); pRef.current = r;
        // Filings: the register's CACHE only. Tracxn bills per call, so a fetch
        // is always someone's decision — the card offers it, and says the cost.
        if (r.anchor.cin) {
          setTrxBusy(true);
          panoramaService.financials({ entityId: r.anchor.entity_id, cin: r.anchor.cin, cachedOnly: true })
            .then((x) => { if (alive) setTrx(x); })
            .catch((e) => { if (alive) setTrxErr(apiErr(e, 'reach the market feed')
              || 'The market feed did not answer.'); })
            .finally(() => { if (alive) setTrxBusy(false); });
        }
        // A saved grade shows at once; a new one only on request (a model call).
        panoramaService.riskGrade(r.anchor.entity_id, r.anchor.name)
          .then((g) => { if (alive && g) setGrade(g); })
          .catch(() => { /* nothing saved yet */ });
        const from = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);
        fetchTerm(r.anchor.name, from)
          .then((arts) => { if (alive) setNews(arts.slice(0, 15)); })
          .catch((e) => { if (alive) setNewsErr(String(e?.message || e)); });
      })
      .catch((e) => { if (alive) setErr(apiErr(e, 'load the company 360')); });
    return () => { alive = false; };
  }, [open, effEntityId, effCompany]);

  // The one place Tracxn is spent: on request, refresh or first fetch alike.
  const fetchTrx = (refresh: boolean) => {
    if (!p || trxBusy || !p.anchor.cin) return;
    setTrxBusy(true); setTrxErr('');
    panoramaService.financials({ entityId: p.anchor.entity_id, cin: p.anchor.cin, refresh })
      .then(setTrx).catch((e) => setTrxErr(apiErr(e, refresh ? 'refresh the market feed' : 'reach the market feed')))
      .finally(() => setTrxBusy(false));
  };
  const refreshTrx = () => fetchTrx(true);
  const runGrade = async (refresh: boolean) => {
    if (!p) return;
    setGradeBusy(true); setGradeErr('');
    const live = (p.stats.deals_in_flight + p.stats.deals_done) > 0;
    const newsIn = (news || []).map((a) => ({ headline: a.headline, source: a.source,
      when: a.when, severity: a.severity || classify(a.headline, live)[0] }));
    try {
      setGrade(await panoramaService.gradeRisk(p.anchor.entity_id, p.anchor.name, newsIn, refresh));
    } catch (e: any) { setGradeErr(apiErr(e, 'grade this client')); }
    finally { setGradeBusy(false); }
  };
  // ---------- derived reading ----------------------------------------------
  const finState: 'live' | 'nocin' | 'busy' | 'unconfigured' | 'notfetched' | 'nofilings' | 'error' | 'idle'
    = trx?.resolved ? 'live'
    : !p?.anchor.cin ? 'nocin'
    : trxBusy && !trx ? 'busy'
    : trxErr && /TRACXN_ACCESS_TOKEN/.test(trxErr) ? 'unconfigured'
    : trxErr ? 'error'
    : trx && trx.cached === false ? 'notfetched'
    : trx && !trx.resolved ? 'nofilings' : 'idle';
  const pdz = p?.post_disbursement;
  const openEws = (pdz?.ews || []).filter((e) => e.status !== 'Closed');
  const sevColor = (sev: string) => /red/i.test(sev) ? tokens.bad : /amber/i.test(sev) ? tokens.warn : '#1F6FA8';
  const today = new Date().toISOString().slice(0, 10);
  const nextCov = (pdz?.covenants || []).filter((c) => c.first_due_on && c.first_due_on >= today)
    .sort((a, b) => (a.first_due_on! < b.first_due_on! ? -1 : 1))[0] || null;
  const cpPending = (pdz?.cpcs || []).flatMap((c) => c.required_pending);
  const grp = p?.group && p.group.members.length ? p.group : null;
  const series = trx?.series || {};
  const last = (k: string) => { const pts = series[k]?.points; return pts?.length ? pts[pts.length - 1] : null; };
  const prev = (k: string) => { const pts = series[k]?.points; return pts && pts.length > 1 ? pts[pts.length - 2] : null; };
  const revL = last('revenue'), revP = prev('revenue'), patL = last('net_profit');
  const growth = revL && revP && revP.value ? ((revL.value - revP.value) / Math.abs(revP.value)) * 100 : null;
  const eqL = last('bs_equity'), liabL = last('bs_liability');
  const leverage = eqL && liabL && eqL.value ? liabL.value / eqL.value : null;
  const chartYears = [...new Set(['revenue', 'ebitda', 'net_profit', 'bs_equity', 'bs_liability']
    .flatMap((k) => (series[k]?.points || []).map((x) => x.year)))].sort().slice(-5);
  const srOf = (k: string, label: string, color: string) => ({ label, color, points: series[k]?.points || [] });
  const charts = finState === 'live' ? [
    { title: 'Revenue', series: [srOf('revenue', 'Revenue', FIN_SERIES[0][2])] },
    { title: 'Profitability', series: [srOf('ebitda', 'EBITDA', FIN_SERIES[1][2]), srOf('net_profit', 'PAT', FIN_SERIES[2][2])] },
    { title: 'Balance sheet', series: [srOf('bs_equity', 'Net worth', '#1B7A45'), srOf('bs_liability', 'Liabilities', '#B45309')] },
    { title: 'Cash flow', series: [srOf('cf_operating', 'Operating', '#0D9488'), srOf('cf_financing', 'Financing', '#6D5FD3')] },
  ].filter((c) => c.series.some((x) => x.points.length)) : [];
  const fin = p?.prospect;
  const hasFin = !!fin && (fin.revenue_cr != null || fin.ebitda_cr != null || fin.net_profit_cr != null);
  const live = !!p && (p.stats.deals_in_flight + p.stats.deals_done) > 0;
  const graded = (news || []).map((a) => ({ a, sev: (a.severity || classify(a.headline, live)[0]) as Severity }));
  const sevCounts = (['GREEN', 'BLUE', 'AMBER', 'RED'] as Severity[])
    .map((s) => ({ s, n: graded.filter((g) => g.sev === s).length }));
  const worst: Severity | null = graded.some((g) => g.sev === 'RED') ? 'RED'
    : graded.some((g) => g.sev === 'AMBER') ? 'AMBER' : null;

  // Lender reading per syndication line.
  const lenderView = (r: PanoramaLine) => {
    const ls = r.lenders || [];
    const dead = ls.filter((x) => lenderBucket(x.status) === 'dead');
    const adv = ls.filter((x) => lenderBucket(x.status) === 'advanced');
    const prog = ls.filter((x) => lenderBucket(x.status) === 'progress');
    const offered = ls.reduce((a, x) => a + (lenderBucket(x.status) !== 'dead' ? (x.amount_cr || 0) : 0), 0);
    return { ls, dead, adv, prog, offered };
  };

  // The desk knows a line by product and stage ("Platform Deals · ask 1 —
  // IM Circulated"), never by the register's tracker number — the grids and
  // the drawer show none. When a company carries two of a kind, "ask 2" /
  // "line 2" tells them apart the way the drawer does.
  const nth = (list: PanoramaLine[], r: PanoramaLine, word: string) =>
    list.length > 1 ? ` · ${word} ${list.indexOf(r) + 1}` : '';
  const lendName = (r: PanoramaLine) => `Lending${nth(p?.lending || [], r, 'line')}`;
  const synName = (r: PanoramaLine) => `Platform Deal${nth(p?.syndication || [], r, 'ask')}`;
  const amName = (r: PanoramaLine) => `Asset Monetisation${nth(p?.asset_monetisation || [], r, 'line')}`;
  const events = useMemo<Ev[]>(() => {
    if (!p) return [];
    const out: Ev[] = [];
    const add = (iso: string | null | undefined, label: string, color: string, big = false,
                 detail?: string | null, who?: string | null) => {
      if (!iso) return;
      const t = Date.parse(iso);
      if (Number.isNaN(t)) return;
      out.push({ at: t, date: fmtDate(iso), label, color, big, detail: detail || undefined, who: who || undefined });
    };
    add(trx?.legal_entity?.incorporated, 'Incorporated', '#7B8A92');
    p.leads.forEach((l) => add(l.created_at, `Lead ${l.lead_no || ''} opened`, CHART_TEAL, false,
      [l.source ? `Source: ${l.source}` : null, l.notes].filter(Boolean).join('\n'), l.rm));
    p.lending.forEach((r) => {
      const start = earliest(r.created_at, r.stage_updated_at, r.sanction_date);
      const stageDay = (r.stage_updated_at || '').slice(0, 10);
      const rem = r.remarks || null;
      if (start && start.slice(0, 10) !== stageDay) add(start, `${lendName(r)} started · ${fmtCr(r.amount_cr)} ask`, CHART_TEAL, false, rem, r.rm);
      add(r.sanction_date, `Sanctioned ${fmtCr(r.amount_cr)}`, STAGE_COLOR.done, true, rem, r.rm);
      if (r.stage === 'Disbursed') add(r.stage_updated_at, `Disbursed ${fmtCr(r.disbursed_amount ?? r.amount_cr)}`, STAGE_COLOR.done, true, rem, r.rm);
      else if (r.stage === 'Rejected') add(r.stage_updated_at, `${fmtCr(r.amount_cr)} lending rejected`, STAGE_COLOR.dead, true, rem, r.rm);
      else if (r.stage_updated_at && r.stage) add(r.stage_updated_at, `${lendName(r)} · ${r.stage}`, STAGE_COLOR.hold, false,
        [r.pending_with ? `Pending with ${r.pending_with}` : null, rem].filter(Boolean).join('\n'), r.rm);
    });
    p.syndication.forEach((r) => {
      add(earliest(r.created_at, ...(r.lenders || []).map((x) => x.since || x.response_date)),
        `${synName(r)} launched · ${fmtCr(r.amount_cr)} ask`, CHART_TEAL, false,
        (r.lenders || []).length ? `${(r.lenders || []).length} lenders approached: ${(r.lenders || []).map((x) => x.name).join(', ')}` : null, r.rm);
      (r.lenders || []).filter((x) => x.response_date).slice(0, 4)
        .forEach((x) => add(x.response_date, `${x.name}: ${x.status || 'replied'}`,
          lenderBucket(x.status) === 'dead' ? STAGE_COLOR.dead
            : lenderBucket(x.status) === 'advanced' ? STAGE_COLOR.done : CHART_TEAL, false,
          [x.last_reply || x.note, x.amount_cr != null ? `Offer ${fmtCr(x.amount_cr)}` : null].filter(Boolean).join('\n')));
    });
    p.asset_monetisation.forEach((r) => add(r.created_at, `${amName(r)} started`, CHART_TEAL, false,
      [r.status, r.deal_type, r.investor ? `Investor: ${r.investor}` : null,
        r.indicative_value_cr != null ? `Indicative ${fmtCr(r.indicative_value_cr)}` : null].filter(Boolean).join(' · '), r.rm));
    p.interactions.slice(0, 5).forEach((i) => add(i.occurred_at, `${i.type}${i.contact ? ` · ${i.contact}` : ''}${i.lender ? ` · ${i.lender}` : ''}`, '#1F6FA8', false,
      i.summary && i.notes && i.summary !== i.notes ? `${i.summary}\n\n${i.notes}` : (i.summary || i.notes), i.by));
    const byDay = new Map<string, number>();
    p.documents.forEach((d) => { const k = (d.uploaded_at || '').slice(0, 10); if (k) byDay.set(k, (byDay.get(k) || 0) + 1); });
    [...byDay.entries()].sort((a, b) => b[0].localeCompare(a[0])).slice(0, 3)
      .forEach(([k, n]) => add(k, `${n} document${n > 1 ? 's' : ''} uploaded`, '#6D5FD3', false,
        p.documents.filter((d) => (d.uploaded_at || '').slice(0, 10) === k).map((d) => d.filename || d.title).join('\n'),
        p.documents.find((d) => (d.uploaded_at || '').slice(0, 10) === k)?.uploaded_by));
    graded.slice(0, 3).forEach(({ a, sev }) => add(a.when, `News: ${a.headline}`, SEV_BG[sev], false,
      [SEV_LABEL[sev], a.source].filter(Boolean).join(' · ')));
    (p.post_disbursement?.ews || []).forEach((e) => {
      add(e.opened_at, `Early warning opened · ${e.severity}`, sevColor(e.severity), true, e.title + (e.summary ? `\n${e.summary}` : ''), e.assigned_to);
      add(e.closed_at, `Early warning closed${e.disposition ? ` · ${e.disposition}` : ''}`, '#7B8A92', false, e.title);
    });
    if (grade) add(grade.generated_at, `Graded ${grade.label} · ${grade.score}`, BAND_COLOR[grade.rating], true,
      grade.verdict, grade.generated_by);
    out.sort((a, b) => a.at - b.at);
    if (out.length <= 14) return out;
    return [out[0], ...out.slice(-13)];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p, trx, grade, news]);

  const missing = p?.checklist?.missing || [];
  const isDead = (r: PanoramaLine) => r.stage === 'Rejected' || DEAD_RE.test(r.status || '');
  const liveLend = (p?.lending || []).filter((r) => !isDead(r));
  const deadLend = (p?.lending || []).filter(isDead);
  // "0 advanced · 0 declined of 2 (0%)" says nothing; say what happened.
  const lenderSummary = (v: ReturnType<typeof lenderView>) => {
    if (!v.ls.length) return 'no lenders approached yet';
    const replied = v.adv.length + v.dead.length + v.prog.filter((x) => x.response_date).length;
    if (!v.adv.length && !v.dead.length) {
      return `${v.ls.length} lender${v.ls.length > 1 ? 's' : ''} approached · ${replied ? `${replied} replied` : 'no reply yet'}`;
    }
    return `${v.adv.length} advanced · ${v.dead.length} declined of ${v.ls.length}`;
  };
  // The last touch is the latest dated contact anywhere — an interaction row,
  // or the date the lead itself carries when nothing was logged separately.
  const lastTouchIso = [p?.stats.last_touch, ...(p?.leads || []).map((l) => l.last_interaction_date)]
    .filter((x): x is string => !!x).sort().pop() || null;
  const lastTouchDays = daysAgo(lastTouchIso);

  const genStamp = useMemo(() => p ? new Date(p.generated_at).toLocaleString('en-IN',
    { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '', [p]);

  // ---------- render ----------------------------------------------------------
  // Every panel is in the DOM; only the chosen one shows on screen, all of them
  // on paper, each under its own heading.
  const Panel = ({ id, title, children }: { id: Tab; title: string; children: React.ReactNode }) => (
    <Box className="c360-panel" sx={{ display: tab === id ? 'flex' : 'none', flexDirection: 'column', gap: 1, minWidth: 0 }}>
      <Typography className="c360-panel-title" sx={{ display: 'none', fontSize: 10.5, fontWeight: 800,
        letterSpacing: '.08em', color: '#44535B', textTransform: 'uppercase', mt: 1 }}>{title}</Typography>
      {children}
    </Box>
  );
  const TabBtn = ({ id, label }: { id: Tab; label: string }) => (
    <Box component="button" onClick={() => setTab(id)}
      sx={{ background: 'none', border: 0, borderBottom: `2px solid ${tab === id ? tokens.tealHi : 'transparent'}`,
        p: '8px 12px', font: 'inherit', fontSize: 12.5, fontWeight: 700, cursor: 'pointer',
        color: tab === id ? tokens.tealHi : tokens.muted, whiteSpace: 'nowrap' }}>{label}</Box>
  );

  return (
    <Dialog open={open} onClose={close} maxWidth="lg" fullWidth fullScreen={small}
      PaperProps={{ className: 'c360-print', sx: { bgcolor: '#F4F6F7',
        borderRadius: small ? 0 : '14px', maxHeight: small ? '100vh' : '94vh' } }}>
      {/* Print = the whole brief on paper: the dialog leaves its fixed frame, every
          scroll region opens out, every tab prints under its own heading, and the
          bars keep their colour. */}
      <style>{`@media print {
        body * { visibility: hidden; }
        .c360-print, .c360-print * { visibility: visible; }
        .MuiDialog-root { position: static !important; }
        .MuiDialog-container { height: auto !important; display: block !important; }
        .c360-print { position: static !important; max-height: none !important; max-width: none !important;
          width: 100% !important; margin: 0 !important; box-shadow: none !important; border-radius: 0 !important; }
        .c360-body, .c360-body * { overflow: visible !important; max-height: none !important; }
        .c360-body { padding: 8px 0 !important; }
        .c360-panel { display: flex !important; }
        .c360-panel-title { display: block !important; }
        .c360-tabs { display: none !important; }
        .c360-print * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
        .no-print { display: none !important; }
        @page { margin: 12mm; }
      }`}</style>

      {/* ---- header ------------------------------------------------------ */}
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.4, p: '13px 20px',
        bgcolor: '#fff', borderBottom: `1px solid ${tokens.line}` }}>
        <Box sx={{ minWidth: 0 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            {target && <Chip size="small" clickable label={`← back to ${company || 'company'}`}
              onClick={() => setTarget(null)} sx={{ height: 21, fontSize: 10.8 }} />}
            <Typography sx={{ fontSize: 18, fontWeight: 800, color: INK }}>
              {p?.anchor.name || effCompany || '…'}</Typography>
            {p?.anchor.sector && <Chip size="small" variant="outlined" color="primary"
              label={p.anchor.sector} sx={{ height: 21, fontSize: 10.8 }} />}
            {p?.anchor.state && <Chip size="small" variant="outlined"
              label={[p.anchor.city, p.anchor.state].filter(Boolean).join(', ')} sx={{ height: 21, fontSize: 10.8 }} />}
            {p?.anchor.lifecycle && <Chip size="small" label={p.anchor.lifecycle}
              sx={{ height: 21, fontSize: 10.8, bgcolor: '#EEF4F3', color: tokens.tealHi, fontWeight: 700 }} />}
            {openEws.length > 0 && <Chip size="small" label={`Early warning · ${openEws[0].severity}`}
              sx={{ height: 21, fontSize: 10.8, fontWeight: 800, color: '#fff', bgcolor: sevColor(openEws[0].severity) }} />}
          </Box>
          <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
            {[p?.anchor.cin ? `CIN ${p.anchor.cin}` : null,
              p?.anchor.pan ? `PAN ${p.anchor.pan}` : null,
              p?.anchor.gstin ? `GSTIN ${p.anchor.gstin}` : null,
              trx?.legal_entity?.incorporated ? `incorporated ${fmtDate(trx.legal_entity.incorporated)}` : null,
              p?.since ? `first activity ${fmtDate(p.since)}` : null,
              p?.owners?.length ? `owners: ${p.owners.join(', ')}` : null,
              p ? (p.anchor.matched_by === 'name-only' ? 'no client master yet' : null) : null]
              .filter(Boolean).join(' · ')}
          </Typography>
        </Box>
        <Box sx={{ flex: 1 }} />
        <Box className="no-print" sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <RiskGradeChip grade={grade} busy={gradeBusy} error={gradeErr}
            canGrade={!!p && (news !== null || !!newsErr)} onGrade={runGrade} />
          <Button size="small" variant="contained" startIcon={<DownloadIcon />}
            onClick={() => window.print()}>Download</Button>
          <IconButton size="small" onClick={close}><CloseIcon fontSize="small" /></IconButton>
        </Box>
      </Box>

      {/* Every card keeps its height: a child that scrolls inside itself (the
          expanded brief) must not be shrunk to nothing by the column's overflow. */}
      <Box className="c360-body" sx={{ p: { xs: '12px 12px 16px', sm: '14px 20px 16px' }, overflowY: 'auto',
        overflowX: 'hidden', display: 'flex', flexDirection: 'column', gap: 1.4, minWidth: 0,
        '& > *': { flexShrink: 0 } }}>
        {err && <Alert severity="warning" sx={{ fontSize: 12.4 }}>{err}</Alert>}
        {!p && !err && <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
          <CircularProgress size={26} /></Box>}

        {p && <>
          {/* ---- verdict strip ------------------------------------------ */}
          <Box sx={{ display: 'grid', gap: 1.2,
            gridTemplateColumns: { xs: 'minmax(0,1fr)', md: 'repeat(3, minmax(0,1fr))' } }}>
            <Card stripe={CHART_TEAL}>
              <Eyebrow>Where we stand</Eyebrow>
              <Typography sx={{ fontSize: 17, fontWeight: 800, color: INK, lineHeight: 1.25, mb: 0.8 }}>
                {[p.stats.booked_cr != null ? `${fmtCr(p.stats.booked_cr)} disbursed` : '',
                  p.stats.exposure_ask_cr != null ? `${fmtCr(p.stats.exposure_ask_cr)} in market` : '',
                  p.stats.on_hold_cr != null ? `${fmtCr(p.stats.on_hold_cr)} on hold` : '']
                  .filter(Boolean).join(', ') || (p.stats.open_leads
                    ? `${p.stats.open_leads} open lead${p.stats.open_leads > 1 ? 's' : ''}, no exposure yet`
                    : 'No live exposure')}
              </Typography>
              <StackBar segs={[{ value: p.stats.booked_cr || 0, color: STAGE_COLOR.done },
                { value: p.stats.exposure_ask_cr || 0, color: STAGE_COLOR.flight },
                { value: p.stats.on_hold_cr || 0, color: STAGE_COLOR.hold }]} />
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.4, mt: 1 }}>
                {liveLend.map((r) => <Bullet key={r.tracker_no || 'l'}
                  dot={r.stage === 'Disbursed' ? STAGE_COLOR.done : STAGE_COLOR.hold}>
                  <b>{lendName(r)}</b> — {r.stage || '—'} · {fmtCr(r.amount_cr)}
                  {r.pending_with ? ` · pending with ${r.pending_with}` : ''}
                  {r.stage_updated_at ? ` · since ${fmtDay(r.stage_updated_at)}` : ''}</Bullet>)}
                {p.syndication.map((r) => { const v = lenderView(r); return (
                  <Bullet key={r.tracker_no || 's'} dot={isDead(r) ? STAGE_COLOR.dead : CHART_TEAL}>
                    <b>{synName(r)}</b> — {r.status || '—'} · {fmtCr(r.amount_cr)} ask · {lenderSummary(v)}
                  </Bullet>); })}
                {deadLend.length > 0 && (
                  <Bullet dot={STAGE_COLOR.dead}>
                    <b>Rejected earlier:</b> {deadLend.map((r) => `${fmtCr(r.amount_cr)} lending${r.stage_updated_at ? ` (${fmtDay(r.stage_updated_at)})` : ''}`).join(', ')}
                  </Bullet>)}
                {p.asset_monetisation.map((r) => <Bullet key={r.tracker_no || 'a'} dot={CHART_TEAL}>
                  <b>{amName(r)}</b> — {r.status || '—'}
                  {r.indicative_value_cr != null ? ` · ${fmtCr(r.indicative_value_cr)}` : ''}</Bullet>)}
                {p.leads.filter((l) => !l.converted).map((l) => <Bullet key={l.lead_no || 'ld'} dot={tokens.tealHi}>
                  <b>Lead {l.lead_no}</b>{l.temperature ? ` · ${l.temperature}` : ''}{l.rm ? ` · ${l.rm}` : ''}
                  {l.next_action ? ` — ${l.next_action}` : ''}</Bullet>)}
                {!p.lending.length && !p.syndication.length && !p.asset_monetisation.length
                  && !p.leads.filter((l) => !l.converted).length && (
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Nothing live on the register.</Typography>)}
              </Box>
            </Card>

            <Card stripe={openEws.length ? sevColor(openEws[0].severity) : grade ? BAND_COLOR[grade.rating] : '#B45309'}>
              <Eyebrow right={grade ? <Chip size="small" label={`${grade.label} · ${grade.score}/100`}
                sx={{ height: 18, fontSize: 10, fontWeight: 800, color: '#fff', bgcolor: BAND_COLOR[grade.rating] }} /> : undefined}>
                How they’re doing</Eyebrow>
              <Typography sx={{ fontSize: 17, fontWeight: 800, color: INK, lineHeight: 1.25, mb: 0.8 }}>
                {grade ? grade.verdict
                  : finState === 'live' && revL
                    ? `Revenue ${fmtCr(revL.value)}${growth != null ? ` (${pct(growth)} YoY)` : ''}`
                    : 'Not graded yet'}
              </Typography>
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.4 }}>
                {openEws.map((e, i) => (
                  <Bullet key={i} dot={sevColor(e.severity)}>
                    <b>Early warning ({e.severity}):</b> {e.title}
                    {e.assigned_to ? ` · with ${e.assigned_to}` : ''}{e.opened_at ? ` · since ${fmtDay(e.opened_at)}` : ''}</Bullet>))}
                {finState === 'live' && revL ? (
                  <Bullet dot={FIN_SERIES[0][2]}>Revenue <b>{fmtCr(revL.value)}</b> {revL.fy || revL.year}
                    {growth != null ? ` · ${pct(growth)} YoY` : ''}
                    {patL ? ` · PAT ${patL.value < 0 ? '−' : ''}${fmtCr(Math.abs(patL.value))}` : ''} <span style={{ color: tokens.muted }}>(Tracxn filings)</span></Bullet>
                ) : finState === 'nocin' ? (
                  <Bullet dot="#B45309">Filings not fetched — <b>no CIN on record</b>; add it on the lead or client master.</Bullet>
                ) : finState === 'unconfigured' ? (
                  <Bullet dot="#9AA8AF">Market feed not connected on this server.</Bullet>
                ) : finState === 'notfetched' ? (
                  <Bullet dot="#9AA8AF">Filings not fetched from Tracxn yet — fetch them from the Financial snapshot when needed (it spends API calls).</Bullet>
                ) : finState === 'nofilings' ? (
                  <Bullet dot="#9AA8AF">{trx?.note || `Tracxn has no filings for CIN ${p.anchor.cin}.`}</Bullet>
                ) : finState === 'busy' ? (
                  <Bullet dot="#9AA8AF">Fetching filings for CIN {p.anchor.cin}…</Bullet>
                ) : hasFin ? (
                  <Bullet dot={FIN_SERIES[0][2]}>Revenue {fmtCr(fin!.revenue_cr)} <span style={{ color: tokens.muted }}>(prospect universe, single year)</span></Bullet>
                ) : null}
                {leverage != null && liabL && eqL && (
                  <Bullet dot={leverage > 3 ? tokens.bad : leverage > 1.5 ? tokens.warn : tokens.ok}>
                    Liabilities {fmtCr(liabL.value)} vs net worth {fmtCr(eqL.value)} · <b>{leverage.toFixed(1)}×</b></Bullet>)}
                {p.syndication.map((r) => { const v = lenderView(r); return v.ls.length ? (
                  <Bullet key={r.tracker_no || 's'} dot={v.dead.length / v.ls.length >= 0.5 ? tokens.bad : v.dead.length ? tokens.warn : tokens.ok}>
                    Lenders on the {synName(r).toLowerCase()}: {lenderSummary(v)}
                    {v.dead.length ? ` (${Math.round((v.dead.length / v.ls.length) * 100)}% declined)` : ''}
                    {v.offered ? ` · ${fmtCr(v.offered)} on the table` : ''}</Bullet>) : null; })}
                <Bullet dot={worst ? SEV_BG[worst] : tokens.ok}>
                  News: {news === null ? (newsErr ? 'radar unavailable' : 'searching…')
                    : news.length ? `${news.length} in 30 days${worst ? ` · worst ${SEV_LABEL[worst].toLowerCase()}` : ' · nothing adverse'}` : 'quiet, 0 in 30 days'}</Bullet>
                {grade?.risks?.[0] && <Bullet dot={tokens.bad}><b>Top risk:</b> {grade.risks[0]}</Bullet>}
                {grade?.mitigants?.[0] && <Bullet dot={tokens.ok}><b>Top mitigant:</b> {grade.mitigants[0]}</Bullet>}
              </Box>
            </Card>

            <Card stripe="#1F6FA8">
              <Eyebrow>What’s next</Eyebrow>
              <Typography sx={{ fontSize: 17, fontWeight: 800, color: INK, lineHeight: 1.25, mb: 0.8 }}>
                {(() => {
                  if (grade?.action) return grade.action;
                  const pend = liveLend.find((r) => r.pending_with && r.stage !== 'Disbursed');
                  if (pend) return `${lendName(pend)} pending with ${pend.pending_with}`;
                  const syn = p.syndication.find((r) => !isDead(r));
                  if (syn) {
                    const wait = (syn.lenders || []).filter((x) => lenderBucket(x.status) === 'progress' && !x.response_date);
                    return `${synName(syn)} at ${syn.status || '—'}${syn.pending_with ? ` — pending with ${syn.pending_with}`
                      : wait.length ? ` — awaiting ${wait.length} lender repl${wait.length > 1 ? 'ies' : 'y'}` : ''}`;
                  }
                  const l = p.leads.find((x) => !x.converted && x.next_action);
                  if (l) return l.next_action!;
                  const noted = p.leads.find((x) => !x.converted && x.notes);
                  if (noted) return noted.notes!.split(/(?<=[.!?])\s/)[0];
                  return 'Nothing pending on record';
                })()}
              </Typography>
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.4 }}>
                <Bullet dot={lastTouchDays == null ? tokens.warn : lastTouchDays > 14 ? tokens.warn : tokens.ok}>
                  {lastTouchDays == null ? 'No interaction logged yet'
                    : `Last touch ${lastTouchDays === 0 ? 'today' : `${lastTouchDays} day${lastTouchDays > 1 ? 's' : ''} ago`}`}
                  {lastTouchDays != null && p.interactions[0]?.by && p.stats.last_touch === lastTouchIso ? ` · ${p.interactions[0].by}` : ''}</Bullet>
                {p.leads.filter((l) => !l.converted && l.next_action).slice(0, 2).map((l) => (
                  <Bullet key={l.lead_no || 'n'} dot={tokens.tealHi}>{l.lead_no}: {l.next_action}
                    {l.next_action_date ? ` · by ${fmtDay(l.next_action_date)}` : ''}</Bullet>))}
                {p.syndication.map((r) => { const wait = (r.lenders || []).filter((x) => lenderBucket(x.status) === 'progress' && !x.response_date);
                  return wait.length ? <Bullet key={r.tracker_no || 'w'} dot={tokens.warn}>
                    {wait.length} lender{wait.length > 1 ? 's' : ''} yet to reply on the {synName(r).toLowerCase()}: {wait.map((x) => x.name).join(', ')}</Bullet> : null; })}
                {missing.length > 0 ? (
                  <Bullet dot={tokens.warn}><b>{missing.length} required document{missing.length > 1 ? 's' : ''} to request:</b> {missing.slice(0, 4).map((m) => m.label).filter(Boolean).join(', ')}{missing.length > 4 ? ` +${missing.length - 4} more` : ''}</Bullet>
                ) : p.checklist && p.checklist.required_total > 0 ? (
                  <Bullet dot={tokens.ok}>Data Register complete — {p.checklist.required_on_file}/{p.checklist.required_total} required on file</Bullet>
                ) : null}
                {nextCov && <Bullet dot={tokens.warn}><b>Covenant due {fmtDay(nextCov.first_due_on)}:</b> {nextCov.name}
                  {nextCov.metric ? ` (${nextCov.metric} ${nextCov.operator || ''} ${nextCov.threshold ?? ''})` : ''}</Bullet>}
                {cpPending.length > 0 && <Bullet dot={tokens.warn}><b>CP/CS pending ({cpPending.length}):</b> {cpPending.join(', ')}</Bullet>}
                {grade?.conditions?.[0] && <Bullet dot="#1F6FA8"><b>Condition:</b> {grade.conditions[0]}</Bullet>}
              </Box>
            </Card>
          </Box>

          {/* ---- brief line --------------------------------------------- */}
          <Typography sx={{ fontSize: 12.6, color: INK, lineHeight: 1.6, px: 0.4,
            ...(briefOpen ? { maxHeight: 260, overflowY: 'auto', pr: 1 } : {}) }}>
            {briefOpen ? (p.brief_full || p.brief) : p.brief}
            {(p.brief_full || '') !== p.brief && (
              <Typography component="span" className="no-print" onClick={() => setBriefOpen((v) => !v)}
                sx={{ fontSize: 12.5, fontWeight: 700, color: tokens.tealHi, cursor: 'pointer', ml: 0.6,
                  userSelect: 'none', '&:hover': { textDecoration: 'underline' } }}>
                {briefOpen ? 'less' : 'more'}</Typography>)}
          </Typography>

          {/* ---- timeline ---------------------------------------------- */}
          {events.length > 0 && (
            <Card>
              <Eyebrow right={<Typography sx={{ fontSize: 10.5, color: tokens.muted }}>every dated fact PRISM holds, in order</Typography>}>
                Relationship timeline</Eyebrow>
              {/* Wider than the card when the story is long — it scrolls sideways,
                  newest facts on the right, and prints in full. */}
              <Box sx={{ overflowX: 'auto', overflowY: 'hidden', pb: 0.5 }}><Timeline events={events} /></Box>
            </Card>)}

          {/* ---- financial trend: the charts get the full width -------------- */}
          {charts.length > 0 && (
            <Card>
              <Eyebrow right={<Typography sx={{ fontSize: 10.5, color: tokens.muted }}>Tracxn filings · ₹ Cr · hover a bar for its value, ⤢ to enlarge</Typography>}>
                Financial trend · {chartYears.length ? `${chartYears[0]}–${chartYears[chartYears.length - 1]}` : ''}</Eyebrow>
              <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: 'minmax(0,1fr)', sm: 'repeat(2, minmax(0,1fr))', md: `repeat(${Math.min(charts.length, 4)}, minmax(0,1fr))` } }}>
                {charts.map((c) => <BarChart key={c.title} title={c.title} unit={series.revenue?.unit || '₹ Cr'} series={c.series} years={chartYears}
                  onExpand={() => setBigChart(c.title)} />)}
              </Box>
            </Card>)}

          {/* ---- money + numbers --------------------------------------- */}
          <Box sx={{ display: 'grid', gap: 1.4, alignItems: 'start',
            gridTemplateColumns: { xs: 'minmax(0,1fr)', md: 'minmax(0,1.25fr) minmax(0,1fr)' } }}>
            <Card>
              <Eyebrow>Money picture</Eyebrow>
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.4 }}>
                {p.lending.map((r) => (
                  <Box key={r.tracker_no || 'l'}>
                    <Typography sx={{ fontSize: 12.8, color: INK, mb: 0.5 }}>
                      <b>{lendName(r)}</b> — {r.stage || '—'} · ask {fmtCr(r.amount_cr)}
                      {r.disbursed_amount != null ? ` · disbursed ${fmtCr(r.disbursed_amount)}` : ''}
                      {r.sanction_date ? ` · sanctioned ${fmtDay(r.sanction_date)}` : ''}
                      <span style={{ color: tokens.muted }}>{r.rm ? ` · ${r.rm}` : ''}{r.analyst ? ` / ${r.analyst}` : ''}</span>
                    </Typography>
                    <Ladder ladder={r.ladder} current={r.stage || null} />
                    {r.remarks && <Typography sx={{ fontSize: 11.2, color: tokens.muted, mt: 0.4, whiteSpace: 'pre-wrap' }}>{r.remarks}</Typography>}
                  </Box>))}
                {p.syndication.map((r) => { const v = lenderView(r); const n = v.ls.length || 1;
                  const rows = [...v.adv, ...v.prog, ...v.dead];
                  return (
                    <Box key={r.tracker_no || 's'}>
                      <Typography sx={{ fontSize: 12.8, color: INK, mb: 0.6 }}>
                        <b>{synName(r)}</b> — {r.status || '—'} · ask {fmtCr(r.amount_cr)}
                        {v.offered ? ` · ${fmtCr(v.offered)} on the table` : ''}
                        <span style={{ color: tokens.muted }}>{r.rm ? ` · ${r.rm}` : ''}</span>
                      </Typography>
                      {v.ls.length > 0 && (
                        <Box sx={{ display: 'grid', gridTemplateColumns: '120px minmax(0,1fr) 34px', gap: '4px 8px', alignItems: 'center', fontSize: 11.6, mb: 0.8 }}>
                          {([['Approached', v.ls.length, CHART_TEAL], ['In progress', v.prog.length, CHART_TEAL],
                            ['Advanced', v.adv.length, STAGE_COLOR.done], ['Declined / dropped', v.dead.length, tokens.bad]] as const).map(([lab, k, col]) => (
                            <Box key={lab} sx={{ display: 'contents' }}>
                              <Typography sx={{ fontSize: 11.4, color: INK }}>{lab}</Typography>
                              <Box sx={{ height: 10, bgcolor: '#EEF4F3', borderRadius: 3 }}>
                                <Box sx={{ height: 10, width: `${(k / n) * 100}%`, bgcolor: col, borderRadius: 3 }} />
                              </Box>
                              <Typography sx={{ fontSize: 11.4, fontWeight: 700, textAlign: 'right', color: INK }}>{k}</Typography>
                            </Box>))}
                        </Box>)}
                      {rows.length > 0 && (
                        <Box sx={{ overflowX: 'auto' }}>
                          <Box component="table" sx={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.6, minWidth: 520,
                            '& th, & td': { padding: '4px 6px', borderBottom: `1px solid ${tokens.line}`, textAlign: 'left', verticalAlign: 'top' },
                            '& th': { fontSize: 10, letterSpacing: '.06em', textTransform: 'uppercase', color: tokens.muted, fontWeight: 700 } }}>
                            <thead><tr><th>Lender</th><th>Status</th><th>Offer</th><th>Why · last reply</th><th>Waiting</th></tr></thead>
                            <tbody>
                              {rows.map((x) => { const b = lenderBucket(x.status);
                                const why = x.last_reply || x.note || x.last_chase;
                                const looksDeclined = b !== 'dead' && /declin|not feasible|cannot proceed|can't proceed|not interested|no to\b|pass(ed)? on/i.test(why || '');
                                const w = x.response_date ? `${daysAgo(x.response_date)} d since reply`
                                  : x.chased_date ? `chased ${daysAgo(x.chased_date)} d ago`
                                  : x.since || x.updated_at ? `${daysAgo(x.since || x.updated_at)} d` : '—';
                                return (
                                  <tr key={x.name}>
                                    <td style={{ fontWeight: 600, color: INK, whiteSpace: 'nowrap' }}>{x.name}</td>
                                    <td><Chip size="small" label={x.status || '—'} variant="outlined"
                                      sx={{ height: 18, fontSize: 10, color: b === 'dead' ? tokens.bad : b === 'advanced' ? STAGE_COLOR.done : tokens.tealHi,
                                        borderColor: b === 'dead' ? tokens.bad : b === 'advanced' ? STAGE_COLOR.done : tokens.tealHi }} /></td>
                                    <td style={{ whiteSpace: 'nowrap' }}>{x.amount_cr != null ? fmtCr(x.amount_cr) : '—'}</td>
                                    <td style={{ color: why ? INK : tokens.warn, fontSize: 11.2, whiteSpace: 'pre-wrap' }}>{why || (b === 'dead' ? 'no reason recorded — log the reply' : 'no reply recorded')}
                                      {looksDeclined && <span style={{ color: tokens.warn }}> · reads like a decline — update the status?</span>}</td>
                                    <td style={{ color: tokens.muted, whiteSpace: 'nowrap' }}>{b === 'dead' ? '—' : w}</td>
                                  </tr>); })}
                            </tbody>
                          </Box>
                        </Box>)}
                    </Box>); })}
                {p.asset_monetisation.map((r) => (
                  <Typography key={r.tracker_no || 'a'} sx={{ fontSize: 12.8, color: INK }}>
                    <b>{amName(r)}</b> — {r.status || '—'}
                    {r.indicative_value_cr != null ? ` · indicative ${fmtCr(r.indicative_value_cr)}` : ''}
                    {r.deal_type ? ` · ${r.deal_type}` : ''}{r.investor ? ` · ${r.investor}` : ''}
                  </Typography>))}
                {!p.lending.length && !p.syndication.length && !p.asset_monetisation.length && (
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>No product lines yet — the picture starts when a lead is pushed to Deals.</Typography>)}
              </Box>
            </Card>

            <Card>
              <Eyebrow right={finState === 'live'
                ? <Chip size="small" label="Tracxn · filings" sx={{ height: 18, fontSize: 10, fontWeight: 700, color: '#fff', bgcolor: CHART_TEAL }} />
                : finState === 'nocin' ? <Chip size="small" label="CIN needed" sx={{ height: 18, fontSize: 10, fontWeight: 700, color: '#B45309', border: '1px solid #B45309', bgcolor: '#FDF3E7' }} />
                : finState === 'notfetched' ? <Chip size="small" variant="outlined" label="not fetched" sx={{ height: 18, fontSize: 10, color: tokens.muted, borderStyle: 'dashed' }} />
                : finState === 'busy' ? <CircularProgress size={12} /> : undefined}>
                Financial snapshot</Eyebrow>
              {finState === 'live' && (() => {
                const years = [...new Set(SNAPSHOT_ROWS.flatMap(([k]) => (series[k]?.points || []).map((x) => x.year)))].sort().slice(-4);
                const cell = (k: string, y: number) => series[k]?.points.find((x) => x.year === y) || null;
                const fyLabel = (y: number) => SNAPSHOT_ROWS.map(([k]) => cell(k, y)?.fy).find(Boolean) || String(y);
                const ratio = (num: string, den: string, y: number) => { const a = cell(num, y), b = cell(den, y); return a && b && b.value ? (a.value / Math.abs(b.value)) * 100 : null; };
                return <>
                  {years.length > 0 ? (
                    <Box sx={{ overflowX: 'auto' }}>
                      <Box component="table" sx={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.6,
                        '& th, & td': { padding: '4px 6px', borderBottom: `1px solid ${tokens.line}`, textAlign: 'right', whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' },
                        '& th:first-of-type, & td:first-of-type': { textAlign: 'left', whiteSpace: 'normal' },
                        '& thead th': { fontSize: 10, letterSpacing: '.06em', textTransform: 'uppercase', color: tokens.muted, fontWeight: 700 } }}>
                        <thead><tr><th>₹ Cr unless noted</th>{years.map((y) => <th key={y}>{fyLabel(y)}</th>)}</tr></thead>
                        <tbody>
                          {SNAPSHOT_ROWS.filter(([k]) => series[k]?.points.length).map(([k, lab]) => (
                            <tr key={k}><td style={{ color: INK }}>{lab}{k.startsWith('employees') ? <span style={{ color: tokens.muted }}> · heads</span> : ''}</td>
                              {years.map((y) => { const c = cell(k, y); return <td key={y} style={{ color: c ? (c.value < 0 ? tokens.bad : INK) : tokens.muted, fontWeight: k === 'revenue' ? 700 : 400 }}>{c ? finNum(c.value) : '—'}</td>; })}
                            </tr>))}
                          {series.revenue?.points.length ? (
                            <tr><td style={{ color: tokens.muted }}>Revenue growth</td>
                              {years.map((y) => { const a = cell('revenue', y), b = cell('revenue', y - 1); const g = a && b && b.value ? ((a.value - b.value) / Math.abs(b.value)) * 100 : null;
                                return <td key={y} style={{ color: g == null ? tokens.muted : g < 0 ? tokens.bad : tokens.ok }}>{g == null ? '—' : pct(g)}</td>; })}</tr>) : null}
                          {series.ebitda?.points.length && series.revenue?.points.length ? (
                            <tr><td style={{ color: tokens.muted }}>EBITDA margin</td>
                              {years.map((y) => { const m = ratio('ebitda', 'revenue', y); return <td key={y} style={{ color: m == null ? tokens.muted : m < 0 ? tokens.bad : INK }}>{m == null ? '—' : `${m.toFixed(1)}%`}</td>; })}</tr>) : null}
                          {series.net_profit?.points.length && series.revenue?.points.length ? (
                            <tr><td style={{ color: tokens.muted }}>PAT margin</td>
                              {years.map((y) => { const m = ratio('net_profit', 'revenue', y); return <td key={y} style={{ color: m == null ? tokens.muted : m < 0 ? tokens.bad : INK }}>{m == null ? '—' : `${m.toFixed(1)}%`}</td>; })}</tr>) : null}
                          {series.bs_liability?.points.length && series.bs_equity?.points.length ? (
                            <tr><td style={{ color: tokens.muted }}>Liabilities / net worth</td>
                              {years.map((y) => { const a = cell('bs_liability', y), b = cell('bs_equity', y); const r = a && b && b.value ? a.value / Math.abs(b.value) : null;
                                return <td key={y} style={{ color: r == null ? tokens.muted : r > 3 ? tokens.bad : r > 1.5 ? tokens.warn : INK }}>{r == null ? '—' : `${r.toFixed(2)}×`}</td>; })}</tr>) : null}
                        </tbody>
                      </Box>
                    </Box>
                  ) : <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Tracxn knows the company but has no filed series yet.</Typography>}
                  <Box className="no-print" sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 0.8 }}>
                    <Typography sx={{ fontSize: 10, color: tokens.muted }}>
                      as filed · Tracxn{trx?.legal_entity?.name ? ` · ${trx.legal_entity.name}` : ''}
                      {trx?.fetched_at ? ` · fetched ${fmtDay(trx.fetched_at)}` : ''} · INR shown in crore</Typography>
                    <Box sx={{ flex: 1 }} />
                    <Tooltip title="Re-fetches every series from Tracxn (spends API calls); otherwise the register's cache answers.">
                      <Box component="span"><Button size="small" variant="text" onClick={refreshTrx} disabled={trxBusy}
                        sx={{ fontSize: 10.5, py: 0, minWidth: 0 }}>{trxBusy ? <CircularProgress size={12} /> : 'Refresh'}</Button></Box>
                    </Tooltip>
                  </Box>
                </>;
              })()}
              {finState === 'busy' && <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>{trx ? 'Fetching filings from Tracxn…' : 'Checking the register’s cache…'}</Typography>}
              {finState === 'notfetched' && (
                <Box className="no-print" sx={{ display: 'flex', flexDirection: 'column', gap: 0.8 }}>
                  <Typography sx={{ fontSize: 11.8, color: INK, lineHeight: 1.55 }}>
                    Filed financials, board and shareholding are available from Tracxn for CIN <b>{p.anchor.cin}</b>,
                    but have not been fetched yet. Tracxn charges per call, so nothing is fetched until you ask;
                    once fetched, the answer is kept and re-fetched only on Refresh.
                  </Typography>
                  <Box><Button size="small" variant="outlined" onClick={() => fetchTrx(false)} disabled={trxBusy}
                    startIcon={<DownloadIcon />}>Fetch filings from Tracxn · ~{trx?.endpoints || 15} API calls</Button></Box>
                </Box>)}
              {finState === 'nocin' && (
                <Typography sx={{ fontSize: 11.8, color: INK, lineHeight: 1.55 }}>
                  Filings, board and shareholding come from Tracxn, looked up by the company’s <b>CIN</b>. There is no CIN on record for {p.anchor.name} yet, so nothing was requested. Add it under <b>Company details</b> on the lead, or <b>Profile</b> on the client master, and this card fills itself.
                </Typography>)}
              {finState === 'unconfigured' && (
                <Typography sx={{ fontSize: 11.8, color: INK, lineHeight: 1.55 }}>The Tracxn market feed is not connected on this server (TRACXN_ACCESS_TOKEN is not set). CIN on record: {p.anchor.cin}.</Typography>)}
              {finState === 'nofilings' && (
                <Typography sx={{ fontSize: 11.8, color: INK, lineHeight: 1.55 }}>
                  {/reached|refused/.test(trx?.note || '') ? `${trx?.note} Nothing is cached from a failed lookup — the next open asks again.`
                    : <>Tracxn has no legal entity for CIN <b>{p.anchor.cin}</b>. Check the CIN on record; a corrected one is looked up again on the next open.</>}
                </Typography>)}
              {finState === 'error' && <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>{trxErr}</Typography>}
              {finState !== 'live' && hasFin && (
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.6, mt: 1 }}>
                  <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted }}>AS CAPTURED IN THE PROSPECT UNIVERSE</Typography>
                  <FallbackBars rows={[['Revenue', fin!.revenue_cr, FIN_SERIES[0][2]], ['EBITDA', fin!.ebitda_cr, FIN_SERIES[1][2]], ['PAT', fin!.net_profit_cr, FIN_SERIES[2][2]]]} />
                  <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>single year, as captured{fin!.founded_year ? ` · founded ${fin!.founded_year}` : ''}</Typography>
                </Box>)}
              {missing.some((m) => /financ|audit|itr|cma|provisional/i.test(m.label || m.section || '')) && (
                <Typography sx={{ fontSize: 10.8, color: tokens.muted, mt: 1 }}>
                  Audited financials still to request: {missing.filter((m) => /financ|audit|itr|cma|provisional/i.test(m.label || m.section || '')).map((m) => m.label).join(', ')}.</Typography>)}
            </Card>
          </Box>

          {/* ---- post-disbursement: what a disbursed line lives under --------- */}
          {pdz && (pdz.covenants.length + pdz.ews.length + pdz.sanction_terms.length + pdz.cpcs.length) > 0 && (
            <Card stripe={openEws.length ? sevColor(openEws[0].severity) : STAGE_COLOR.done}>
              <Eyebrow right={<Typography sx={{ fontSize: 10.5, color: tokens.muted }}>sanction terms · CP/CS · covenants · early warnings</Typography>}>
                Post-disbursement</Eyebrow>
              <Box sx={{ display: 'grid', gap: 1.6, gridTemplateColumns: { xs: 'minmax(0,1fr)', md: 'minmax(0,1fr) minmax(0,1fr)' } }}>
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>
                  {pdz.sanction_terms.map((t, i) => (
                    <Box key={i}>
                      <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted }}>SANCTION TERMS{p.lending.length > 1 && t.tracker_no ? ` · ${lendName(p.lending.find((r) => r.tracker_no === t.tracker_no) || p.lending[0])}` : ''}</Typography>
                      <Typography sx={{ fontSize: 12.4, color: INK, lineHeight: 1.6 }}>
                        {[t.amount_cr != null ? fmtCr(t.amount_cr) : null,
                          t.rate_pct != null ? `${t.rate_pct}% ${t.rate_kind?.toLowerCase() || ''}${t.spread_pct != null ? ` + ${t.spread_pct}%` : ''}` : null,
                          t.tenor_months != null ? `${t.tenor_months} months` : null,
                          t.emi_amount != null ? `${t.schedule_kind || 'EMI'} ₹${Math.round(t.emi_amount).toLocaleString('en-IN')}` : null,
                          t.repayment_start ? `repayments from ${fmtDate(t.repayment_start)}` : null,
                          t.moratorium_months ? `${t.moratorium_months}-month moratorium` : null,
                          t.penal_rate_pct != null ? `penal ${t.penal_rate_pct}%` : null].filter(Boolean).join(' · ')}
                      </Typography>
                    </Box>))}
                  {pdz.cpcs.map((c, i) => (
                    <Box key={i}>
                      <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted }}>CP / CS CHECKLIST · v{c.version} · {c.status}</Typography>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 0.4 }}>
                        <Box sx={{ flex: 1, height: 7, bgcolor: '#EEF1F3', borderRadius: 4 }}>
                          <Box sx={{ width: `${c.items_total ? (c.items_done / c.items_total) * 100 : 0}%`, height: 7, borderRadius: 4, bgcolor: c.required_pending.length ? tokens.warn : STAGE_COLOR.done }} />
                        </Box>
                        <Typography sx={{ fontSize: 11.4, fontWeight: 700, color: INK, whiteSpace: 'nowrap' }}>{c.items_done} / {c.items_total} done</Typography>
                      </Box>
                      {c.required_pending.length > 0 && <Typography sx={{ fontSize: 11.4, color: tokens.warn, mt: 0.3 }}>Pending (required): {c.required_pending.join(' · ')}</Typography>}
                      {(c.prepared_by || c.approved_by) && <Typography sx={{ fontSize: 10.8, color: tokens.muted }}>{[c.prepared_by ? `prepared by ${c.prepared_by}` : null, c.approved_by ? `approved by ${c.approved_by}` : null].filter(Boolean).join(' · ')}</Typography>}
                    </Box>))}
                  {!pdz.sanction_terms.length && !pdz.cpcs.length && <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>No sanction terms or CP/CS checklist recorded on the lines.</Typography>}
                </Box>
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>
                  {pdz.ews.length > 0 && <>
                    <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted }}>EARLY-WARNING CASES · {openEws.length} open</Typography>
                    {pdz.ews.map((e, i) => (
                      <Box key={i} sx={{ borderLeft: `3px solid ${e.status === 'Closed' ? '#C9D2D7' : sevColor(e.severity)}`, pl: 1 }}>
                        <Typography sx={{ fontSize: 12.2, color: INK }}><b>{e.title}</b> · {e.severity} · {e.status}{e.disposition ? ` (${e.disposition})` : ''}</Typography>
                        <Typography sx={{ fontSize: 11, color: tokens.muted }}>{[e.source, e.opened_at ? `opened ${fmtDate(e.opened_at)}` : null, e.assigned_to ? `with ${e.assigned_to}` : null, e.closed_at ? `closed ${fmtDate(e.closed_at)}` : null].filter(Boolean).join(' · ')}</Typography>
                        {e.summary && <Typography sx={{ fontSize: 11.4, color: INK, whiteSpace: 'pre-wrap' }}>{e.summary}</Typography>}
                      </Box>))}
                  </>}
                  {pdz.covenants.length > 0 && <>
                    <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted, mt: pdz.ews.length ? 0.6 : 0 }}>COVENANTS · {pdz.covenants.length}</Typography>
                    {pdz.covenants.map((c, i) => (
                      <Typography key={i} sx={{ fontSize: 12, color: INK }}>
                        <b>{c.name}</b>{c.metric ? ` · ${c.metric} ${c.operator || ''} ${c.threshold ?? ''}` : ''}{c.frequency ? ` · ${c.frequency.toLowerCase()}` : ''}
                        {c.first_due_on ? ` · ${c.first_due_on >= today ? 'next due' : 'first due'} ${fmtDate(c.first_due_on)}` : ''}
                        <span style={{ color: sevColor(c.breach_severity) }}> · breach → {c.breach_severity}</span>
                      </Typography>))}
                  </>}
                  {!pdz.ews.length && !pdz.covenants.length && <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>No covenants or early-warning cases on this company.</Typography>}
                </Box>
              </Box>
            </Card>)}

          {/* ---- promoter group: one credit risk across sister companies ------ */}
          {grp && (
            <Card stripe="#6D5FD3">
              <Eyebrow right={<Typography sx={{ fontSize: 10.5, color: tokens.muted }}>same promoters · click a company to open its brief</Typography>}>
                Promoter group · {grp.code}</Eyebrow>
              {(() => {
                const askAll = (p.stats.exposure_ask_cr || 0) + grp.members.reduce((a, m) => a + (m.ask_cr || 0), 0);
                const bookedAll = (p.stats.booked_cr || 0) + grp.members.reduce((a, m) => a + (m.booked_cr || 0), 0);
                const ewsAll = openEws.length + grp.members.reduce((a, m) => a + m.ews_open, 0);
                return <Typography sx={{ fontSize: 13.4, fontWeight: 800, color: INK, mb: 0.8 }}>
                  {grp.members.length + 1} companies · {fmtCr(bookedAll)} booked · {fmtCr(askAll)} in market
                  {ewsAll ? <span style={{ color: tokens.bad }}> · {ewsAll} open early warning{ewsAll > 1 ? 's' : ''}</span> : ''}
                </Typography>;
              })()}
              <Box sx={{ overflowX: 'auto' }}>
                <Box component="table" sx={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.8,
                  '& th, & td': { padding: '4px 6px', borderBottom: `1px solid ${tokens.line}`, textAlign: 'right', whiteSpace: 'nowrap' },
                  '& th:first-of-type, & td:first-of-type': { textAlign: 'left' },
                  '& thead th': { fontSize: 10, letterSpacing: '.06em', textTransform: 'uppercase', color: tokens.muted, fontWeight: 700 } }}>
                  <thead><tr><th>Company</th><th>Sector</th><th>In market</th><th>Booked</th><th>Early warnings</th></tr></thead>
                  <tbody>
                    <tr><td style={{ fontWeight: 700, color: INK }}>{p.anchor.name} <span style={{ color: tokens.muted, fontWeight: 400 }}>(this brief)</span></td><td>{p.anchor.sector || '—'}</td><td>{p.stats.exposure_ask_cr != null ? fmtCr(p.stats.exposure_ask_cr) : '—'}</td><td>{p.stats.booked_cr != null ? fmtCr(p.stats.booked_cr) : '—'}</td><td style={{ color: openEws.length ? tokens.bad : tokens.muted }}>{openEws.length || '—'}</td></tr>
                    {grp.members.map((m) => (
                      <tr key={m.entity_id}>
                        <td><Typography component="span" onClick={() => setTarget({ entityId: m.entity_id, company: m.name })}
                          sx={{ fontSize: 11.8, fontWeight: 700, color: tokens.tealHi, cursor: 'pointer', '&:hover': { textDecoration: 'underline' } }}>{m.name}</Typography></td>
                        <td>{m.sector || '—'}</td><td>{m.ask_cr != null ? fmtCr(m.ask_cr) : '—'}</td><td>{m.booked_cr != null ? fmtCr(m.booked_cr) : '—'}</td>
                        <td style={{ color: m.ews_open ? tokens.bad : tokens.muted }}>{m.ews_open || '—'}</td>
                      </tr>))}
                  </tbody>
                </Box>
              </Box>
            </Card>)}

          {/* ---- depth tabs ---------------------------------------------- */}
          <Card sx={{ p: 0 }}>
            <Box className="c360-tabs" sx={{ display: 'flex', gap: 0.2, borderBottom: `1px solid ${tokens.line}`, px: 1, overflowX: 'auto' }}>
              <TabBtn id="engagements" label={`Engagements · ${p.leads.filter((l) => !l.converted).length + p.lending.length + p.syndication.length + p.asset_monetisation.length}`} />
              <TabBtn id="people" label={`People · ${(trx?.board?.length || 0) + p.contacts.length}`} />
              <TabBtn id="documents" label={`Documents · ${p.stats.documents}`} />
              <TabBtn id="news" label={`News · ${news ? news.length : '…'}`} />
              <TabBtn id="risk" label={grade ? `Risk report · ${grade.score}` : 'Risk report'} />
            </Box>
            <Box sx={{ p: '12px 14px', display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>

              <Panel id="engagements" title="Engagements">
                {p.leads.map((l) => (
                  <Box key={l.lead_no || Math.random()} sx={{ display: 'flex', gap: 1.1 }}>
                    <Box sx={{ width: 8, height: 8, mt: '5px', borderRadius: 99, bgcolor: l.converted ? STAGE_COLOR.dead : tokens.tealHi, flexShrink: 0 }} />
                    <Box>
                      <Typography sx={{ fontSize: 12.8, color: INK }}><b>{l.lead_no}</b> — {['Lead', l.converted ? 'converted' : l.status, l.temperature, l.rm || 'unassigned'].filter(Boolean).join(' · ')}</Typography>
                      <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>{[l.created_at ? `opened ${fmtDate(l.created_at)}` : null, l.source, fmtDay(l.last_interaction_date) !== '—' ? `last ${fmtDay(l.last_interaction_date)}` : null, l.next_action || l.notes].filter(Boolean).join(' · ')}</Typography>
                    </Box>
                  </Box>))}
                {p.lending.map((r) => (
                  <Box key={r.tracker_no || 'l'} sx={{ display: 'flex', flexDirection: 'column', gap: 0.6 }}>
                    <Typography sx={{ fontSize: 12.8, color: INK }}><b>{lendName(r)}</b> — {r.stage || '—'} · {fmtCr(r.amount_cr)}{r.pending_with ? ` · pending with ${r.pending_with}` : ''}
                      <span style={{ color: tokens.muted }}>{[r.rm, r.analyst, r.created_at ? `since ${fmtDate(r.created_at)}` : null].filter(Boolean).map((x) => ` · ${x}`).join('')}</span></Typography>
                    <Ladder ladder={r.ladder} current={r.stage || null} />
                  </Box>))}
                {p.syndication.map((r) => (
                  <Box key={r.tracker_no || 's'}>
                    <Typography sx={{ fontSize: 12.8, color: INK }}><b>{synName(r)}</b> — {r.status || '—'}{r.amount_cr != null ? ` · ${fmtCr(r.amount_cr)} ask` : ''}<span style={{ color: tokens.muted }}>{r.created_at ? ` · since ${fmtDate(r.created_at)}` : ''}</span></Typography>
                    {(r.lenders || []).map((x) => <Typography key={x.name} sx={{ fontSize: 11.4, color: tokens.muted, pl: 1.5 }}>{x.name} — {x.status || '—'}{x.amount_cr != null ? ` · ${fmtCr(x.amount_cr)}` : ''}{x.last_reply ? ` · ${x.last_reply}` : ''}</Typography>)}
                  </Box>))}
                {p.asset_monetisation.map((r) => (
                  <Typography key={r.tracker_no || 'a'} sx={{ fontSize: 12.8, color: INK }}><b>{amName(r)}</b> — {r.status || '—'}{r.indicative_value_cr != null ? ` · ${fmtCr(r.indicative_value_cr)}` : ''}{r.investor ? ` · ${r.investor}` : ''}</Typography>))}
                <Box sx={{ mt: 0.6 }}>
                  <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted, mb: 0.4 }}>INTERACTIONS & VOCX · {p.interactions.length}</Typography>
                  <Box sx={{ maxHeight: 260, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 0.8, pr: 0.5 }}>
                    {p.interactions.map((i, n) => (
                      <Box key={n} sx={{ flexShrink: 0 }}>
                        <Typography sx={{ fontSize: 12.4, color: INK }}>{i.type} · {fmtDate(i.occurred_at)}{i.by ? ` · ${i.by}` : ''}{i.lender ? ` · ${i.lender}` : ''}</Typography>
                        {(i.summary || i.notes) && <Typography sx={{ fontSize: 11.4, color: tokens.muted, whiteSpace: 'pre-wrap' }}>{i.summary || i.notes}</Typography>}
                      </Box>))}
                    {!p.interactions.length && <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Nothing logged yet.</Typography>}
                  </Box>
                </Box>
              </Panel>

              <Panel id="people" title="People">
                {(trx?.board || []).length > 0 && <>
                  <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted }}>BOARD · Tracxn</Typography>
                  {(trx!.board || []).map((m) => <Typography key={m.name} sx={{ fontSize: 12.2, color: INK }}><b>{m.name}</b>{m.designation ? ` · ${m.designation}` : ''}{m.since ? ` · since ${String(m.since).slice(0, 4)}` : ''}</Typography>)}
                </>}
                {(trx?.shareholders || []).length > 0 && <>
                  <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted, mt: 0.6 }}>SHAREHOLDING · Tracxn</Typography>
                  {(trx!.shareholders || []).slice(0, 8).map((h) => (
                    <Box key={h.name} sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      <Typography sx={{ fontSize: 11.6, color: INK, width: '42%', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{h.name}</Typography>
                      <Box sx={{ flex: 1, height: 6, bgcolor: '#E7ECEF', borderRadius: 3 }}><Box sx={{ width: `${Math.min(100, Math.max(0, h.pct))}%`, height: 6, bgcolor: CHART_TEAL, borderRadius: 3 }} /></Box>
                      <Typography sx={{ fontSize: 11, fontWeight: 700, color: INK, width: 44, textAlign: 'right' }}>{h.pct.toFixed(1)}%</Typography>
                    </Box>))}
                </>}
                <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted, mt: 0.6 }}>CONTACTS · PRISM</Typography>
                {p.contacts.map((c) => <Typography key={c.name} sx={{ fontSize: 12.2, color: INK }}><b>{c.name}</b>{c.designation ? ` · ${c.designation}` : ''}{c.phone ? ` · ${c.phone}` : ''}<span style={{ color: tokens.muted, fontSize: 10.8 }}> ({c.source})</span></Typography>)}
                {!p.contacts.length && <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>No contacts on record — add one on the lead.</Typography>}
                {p.prospect && ((p.prospect.emails || []).length > 0 || (p.prospect.phones || []).length > 0) && (
                  <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>Prospect universe: {[...(p.prospect.emails || []), ...(p.prospect.phones || [])].join(' · ')}</Typography>)}
                {!(trx?.board || []).length && finState !== 'live' && <Typography sx={{ fontSize: 11.2, color: tokens.muted }}>Board and shareholding arrive with the Tracxn filings{finState === 'nocin' ? ' once a CIN is on record' : ''}.</Typography>}
              </Panel>

              <Panel id="documents" title="Documents">
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                  <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>{p.stats.documents} on the Data Register</Typography>
                  {p.checklist && p.checklist.required_total > 0 && <Typography sx={{ fontSize: 11.4, color: missing.length ? tokens.warn : tokens.ok }}>· required {p.checklist.required_on_file}/{p.checklist.required_total}</Typography>}
                  <Box sx={{ flex: 1 }} />
                  {p.documents.length > 0 && p.anchor.entity_id && (
                    <Button className="no-print" size="small" variant="outlined" startIcon={<DownloadIcon />} disabled={dlBusy} sx={{ fontSize: 11, py: 0.2 }}
                      onClick={() => { setDlErr(''); setDlBusy(true);
                        void documentsService.downloadAll(p.anchor.entity_id!, p.anchor.name)
                          .then((r) => { if (!r.ok || r.error) setDlErr(r.error || ''); }).finally(() => setDlBusy(false)); }}>
                      {dlBusy ? 'Packing…' : `Download all ${p.stats.documents} as .zip`}</Button>)}
                </Box>
                {dlErr && <Typography sx={{ fontSize: 11.2, color: tokens.bad }}>{dlErr}</Typography>}
                {missing.length > 0 && (
                  <Alert severity="warning" sx={{ fontSize: 11.4, py: 0 }}>
                    <b>To request ({missing.length}):</b> {missing.map((m) => `${m.label}${m.section ? ` (${m.section})` : ''}`).join(' · ')}</Alert>)}
                {(() => {
                  const bySec = new Map<string, typeof p.documents>();
                  p.documents.forEach((d) => { const k = d.section || 'Other'; bySec.set(k, [...(bySec.get(k) || []), d]); });
                  return [...bySec.entries()].map(([sec, ds]) => (
                    <Box key={sec}>
                      <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted, mb: 0.4 }}>{sec.toUpperCase()} · {ds.length}</Typography>
                      <Box sx={{ display: 'flex', gap: 0.7, flexWrap: 'wrap' }}>
                        {ds.map((d) => (
                          <Chip key={d.id || d.title + (d.uploaded_at || '')} size="small" label={d.filename || d.title} clickable={!!d.id}
                            icon={d.id ? <DownloadIcon sx={{ fontSize: 13 }} /> : undefined}
                            title={[d.doc_type, d.uploaded_by, fmtDate(d.uploaded_at)].filter(Boolean).join(' · ') + (d.id ? ' · click to download' : '')}
                            onClick={d.id ? () => { setDlErr(''); void documentsService.download({ id: d.id!, name: d.filename || d.title, size: 0, type: '', when: '', by: '', label: d.title })
                              .then((r) => { if (!r.ok) setDlErr(r.error || 'Download failed.'); }); } : undefined}
                            sx={{ height: 22, fontSize: 11, bgcolor: '#EEF4F3', maxWidth: 280, color: tokens.tealHi, fontWeight: 600, '& .MuiChip-icon': { color: tokens.tealHi } }} />))}
                      </Box>
                    </Box>));
                })()}
                {!p.documents.length && <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Nothing on the Data Register yet.</Typography>}
              </Panel>

              <Panel id="news" title="News">
                {sevCounts.filter((x) => x.n > 0).length > 1 && (
                  <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap' }}>
                    {sevCounts.filter((x) => x.n > 0).map(({ s, n }) => (
                      <Chip key={s} size="small" clickable label={`${SEV_LABEL[s].toLowerCase()} · ${n}`} onClick={() => setNewsSev(newsSev === s ? null : s)}
                        sx={{ height: 20, fontSize: 10.2, fontWeight: 700, color: newsSev === s ? '#fff' : SEV_BG[s], bgcolor: newsSev === s ? SEV_BG[s] : 'transparent', border: `1.5px solid ${SEV_BG[s]}` }} />))}
                  </Box>)}
                <Box sx={{ maxHeight: 340, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 1, pr: 0.5 }}>
                  {(newsSev ? graded.filter((g) => g.sev === newsSev) : graded).map(({ a, sev }) => (
                    <Box key={a.url} sx={{ borderLeft: `3px solid ${SEV_BG[sev]}`, pl: 1, flexShrink: 0 }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8 }}>
                        <Box component="span" sx={{ fontSize: 9.5, fontWeight: 800, borderRadius: 999, px: '8px', py: '1px', color: '#fff', bgcolor: SEV_BG[sev], flexShrink: 0 }}>{SEV_LABEL[sev]}</Box>
                        <Typography component="a" href={a.url} target="_blank" rel="noreferrer" sx={{ fontSize: 12.6, color: INK, textDecoration: 'none', '&:hover': { color: tokens.tealHi } }}>{a.headline}</Typography>
                      </Box>
                      <Typography sx={{ fontSize: 10.8, color: tokens.muted }}>{[a.source, a.when].filter(Boolean).join(' · ')}</Typography>
                    </Box>))}
                </Box>
                {news !== null && !news.length && !newsErr && <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>No mentions in the last 30 days (PULSE radar).</Typography>}
                {news === null && !newsErr && <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Searching the radar…</Typography>}
                {newsErr && <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>News radar did not answer: {newsErr}</Typography>}
              </Panel>

              <Panel id="risk" title="Risk report">{grade ? <RiskGradeDetails grade={grade} onRegrade={gradeBusy ? undefined : () => runGrade(true)} /> : (
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2 }}>
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted, flex: 1 }}>
                    Grades this client RED / AMBER / GREEN from its Register footprint, indexed documents and 30-day news, using the ATLAS client rubric. A desk aid, not a credit decision.
                    {gradeErr && <span style={{ color: tokens.bad }}> {gradeErr}</span>}
                  </Typography>
                  <Button size="small" variant="outlined" disabled={gradeBusy || (news === null && !newsErr)} onClick={() => runGrade(false)}>
                    {gradeBusy ? <CircularProgress size={15} /> : 'Grade risk'}</Button>
                </Box>)}</Panel>

            </Box>
          </Card>

          <Typography sx={{ fontSize: 10.8, color: tokens.muted, pt: 0.4 }}>
            Sources: Register · field notes · news radar · documents{finState === 'live' ? ' · Tracxn filings' : ''}{grade ? ' · risk rubric (AI-assisted)' : ''} — generated {genStamp}
            {p.restricted.length ? ` · not visible to your role: ${p.restricted.join(', ')}` : ''}
          </Typography>
        </>}
      </Box>
      <Dialog open={!!bigChart} onClose={() => setBigChart(null)} maxWidth="md" fullWidth
        PaperProps={{ sx: { p: 2, borderRadius: '12px' } }}>
        {bigChart && charts.filter((c) => c.title === bigChart).map((c) => (
          <BarChart key={c.title} big title={`${p?.anchor.name || ''} — ${c.title}`} unit={series.revenue?.unit || '₹ Cr'} series={c.series} years={chartYears} />))}
        <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 1 }}>
          <Button size="small" variant="outlined" onClick={() => setBigChart(null)}>Close</Button></Box>
      </Dialog>
    </Dialog>
  );
}
