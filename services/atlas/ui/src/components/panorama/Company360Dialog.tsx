import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, IconButton, TextField,
  Tooltip, Typography, useMediaQuery,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import DownloadIcon from '@mui/icons-material/Download';
import TrackChangesIcon from '@mui/icons-material/TrackChanges';
import { tokens } from '../../theme';
import { apiErr } from '../../api/http';
import { panoramaService, type DocAskResult, type IndexResult, type Panorama,
  type PanoramaLine, type TracxnFinancials, type TracxnSeries, type RiskGrade }
  from '../../services/panoramaService';
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
 * risk report. Every card is built from PRISM's own records, so it renders
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

const ASK_PRESETS: [string, string][] = [
  ['Key financials', 'What are the key financials — revenue, EBITDA, PAT, net '
    + 'worth and borrowings — with figures and the years they belong to?'],
  ['Promoters & shareholding', 'Who are the promoters and directors, and what '
    + 'is the shareholding pattern, including any pledge of shares?'],
  ['Registrations', 'List the company’s registrations and identifiers: '
    + 'CIN, PAN and GST numbers with their states and validity.'],
  ['Banking & CIBIL', 'What do the banking and CIBIL documents show — '
    + 'accounts, limits, scores, overdues and any adverse remarks?'],
  ['Compliance', 'Which compliance filings and certificates are present, and '
    + 'what is their status or validity?'],
];

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
/** DocRAG passages carry the page's own markup; read tables as rows of cells. */
function tidyAnswer(text: string): string {
  if (!/<\/?(table|tr|td|th|br|p|b|i)\b/i.test(text)) return text.replace(/\*\*/g, '');
  const cell = (c: string) => c.replace(/<br\s*\/?>/gi, ' ').replace(/<[^>]+>/g, '')
    .replace(/\s+/g, ' ').trim();
  return text
    .replace(/<table[\s\S]*?<\/table>/gi, (tbl) => [...tbl.matchAll(/<tr[\s\S]*?<\/tr>/gi)]
      .map((m) => [...m[0].matchAll(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi)].map((c) => cell(c[1])))
      .filter((r) => r.some(Boolean))
      .map((r) => r.join('   ·   ')).join('\n'))
    .replace(/<br\s*\/?>/gi, '\n').replace(/<\/p>/gi, '\n').replace(/<[^>]+>/g, '')
    .replace(/\*\*/g, '').replace(/\n{3,}/g, '\n\n').trim();
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

/** One small-multiple: thin rounded bars, newest value labelled, first/last
 *  years only; negatives hang below a zero line in the same colour. */
function MiniBars({ label, series, color }: {
  label: string; series: TracxnSeries; color: string;
}) {
  const pts = series.points.slice(-6);
  if (!pts.length) return null;
  const W = 168, H = 66, top = 12, bottom = 52;
  const hi = Math.max(0, ...pts.map((x) => x.value));
  const lo = Math.min(0, ...pts.map((x) => x.value));
  const span = hi - lo || 1;
  const y = (v: number) => top + ((hi - v) / span) * (bottom - top);
  const zero = y(0);
  const step = W / pts.length;
  const last = pts[pts.length - 1];
  return (
    <Box sx={{ minWidth: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.6 }}>
        <Box sx={{ width: 7, height: 7, borderRadius: 99, bgcolor: color, flexShrink: 0 }} />
        <Typography sx={{ fontSize: 10.2, fontWeight: 700, color: tokens.muted,
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {label}{series.unit ? ` · ${series.unit}` : ''}</Typography>
      </Box>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', display: 'block' }}>
        <line x1={0} x2={W} y1={zero} y2={zero} stroke="#E7ECEF" strokeWidth={1} />
        {pts.map((pt, i) => {
          const x = i * step + step / 2;
          const yv = y(pt.value);
          return <rect key={pt.year} x={x - 2.5} y={Math.min(yv, zero)} width={5}
            height={Math.max(2, Math.abs(yv - zero))} rx={2.5} fill={color} />;
        })}
        <text x={(pts.length - 1) * step + step / 2}
          y={Math.min(y(last.value), zero) - 3} textAnchor="middle"
          fontSize="8.8" fontWeight="700" fill={INK}>{finNum(last.value)}</text>
        {pts.map((pt, i) => (i === 0 || i === pts.length - 1) ? (
          <text key={pt.year} x={i * step + step / 2} y={H - 2} textAnchor="middle"
            fontSize="7.6" fill="#7B8A92">{pt.fy || pt.year}</text>) : null)}
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

type Ev = { at: number; date: string; label: string; color: string; big?: boolean };

function Timeline({ events }: { events: Ev[] }) {
  if (!events.length) return null;
  const W = 1000, H = 132, L = 46, R = 46, Y = 64;
  const t0 = events[0].at, t1 = events[events.length - 1].at;
  const x = (t: number) => t1 === t0 ? W / 2 : L + ((t - t0) / (t1 - t0)) * (W - L - R);
  // Keep neighbours a readable distance apart: nudge labels that would collide.
  const xs: number[] = [];
  events.forEach((e) => {
    let px = x(e.at);
    const prev = xs[xs.length - 1];
    if (prev != null && px - prev < 92) px = prev + 92;
    xs.push(px);
  });
  const overflow = xs[xs.length - 1] - (W - R);
  const scale = overflow > 0 ? (W - L - R) / (xs[xs.length - 1] - L) : 1;
  const fx = (px: number) => L + (px - L) * scale;
  const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', display: 'block' }}>
      <line x1={L - 10} x2={W - R + 10} y1={Y} y2={Y} stroke="#DCE3E6" strokeWidth={2} />
      {events.map((e, i) => {
        const cx = fx(xs[i]);
        const below = i % 2 === 0;
        const ly = below ? Y + 24 : Y - 30;
        const dy = below ? Y + 38 : Y - 16;
        return (
          <g key={i}>
            <line x1={cx} x2={cx} y1={Y} y2={below ? Y + 12 : Y - 12} stroke="#DCE3E6" />
            <circle cx={cx} cy={Y} r={e.big ? 7 : 5} fill={e.color} />
            <text x={cx} y={ly} textAnchor="middle" fontSize="9.5" fill="#7B8A92">{e.date}</text>
            <text x={cx} y={dy} textAnchor="middle" fontSize="10.2"
              fontWeight={e.big ? 700 : 500} fill={INK}>{clip(e.label, 24)}</text>
          </g>);
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

type Tab = 'engagements' | 'people' | 'documents' | 'news' | 'risk' | 'prospect';

// ---------- the dialog ----------------------------------------------------------

export default function Company360Dialog({ open, entityId, company, onClose }: {
  open: boolean; entityId?: string | null; company?: string; onClose: () => void;
}) {
  const small = useMediaQuery('(max-width:700px)');
  const [p, setP] = useState<Panorama | null>(null);
  const [err, setErr] = useState('');
  const [news, setNews] = useState<Article[] | null>(null);
  const [newsErr, setNewsErr] = useState('');
  const [tab, setTab] = useState<Tab>('engagements');
  const [newsSev, setNewsSev] = useState<Severity | null>(null);
  const [ask, setAsk] = useState('');
  const [askBusy, setAskBusy] = useState(false);
  const [askOut, setAskOut] = useState<DocAskResult | null>(null);
  const [askErr, setAskErr] = useState('');
  const [idxBusy, setIdxBusy] = useState(false);
  const [idxOut, setIdxOut] = useState<IndexResult | null>(null);
  const [idxErr, setIdxErr] = useState('');
  const [trx, setTrx] = useState<TracxnFinancials | null>(null);
  const [trxBusy, setTrxBusy] = useState(false);
  const [trxErr, setTrxErr] = useState('');
  const [grade, setGrade] = useState<RiskGrade | null>(null);
  const [gradeBusy, setGradeBusy] = useState(false);
  const [gradeErr, setGradeErr] = useState('');
  const [dlErr, setDlErr] = useState('');
  const [dlBusy, setDlBusy] = useState(false);
  const pRef = useRef<Panorama | null>(null);

  useEffect(() => {
    if (!open) return;
    setP(null); setErr(''); setNews(null); setNewsErr(''); pRef.current = null;
    setAsk(''); setAskOut(null); setAskErr('');
    setIdxBusy(false); setIdxOut(null); setIdxErr('');
    setTrx(null); setTrxBusy(false); setTrxErr('');
    setGrade(null); setGradeBusy(false); setGradeErr(''); setDlErr(''); setDlBusy(false);
    setTab('engagements'); setNewsSev(null);
    let alive = true;
    panoramaService.get({ entityId, company })
      .then((r) => {
        if (!alive) return;
        setP(r); pRef.current = r;
        // The index is housekeeping: the bridge uploads only what DocRAG lacks.
        if (r.stats.documents > 0 && r.anchor.entity_id) {
          setIdxBusy(true);
          panoramaService.indexDocuments(r.anchor.entity_id, r.anchor.name)
            .then((x) => { if (alive) setIdxOut(x); })
            .catch((e) => { if (alive) setIdxErr(apiErr(e, 'index the documents')); })
            .finally(() => { if (alive) setIdxBusy(false); });
        }
        // Filings are fetched up front — they are the numbers card, not an
        // extra — but never without a CIN: the card explains itself instead.
        if (r.anchor.cin) {
          setTrxBusy(true);
          panoramaService.financials({ entityId: r.anchor.entity_id, cin: r.anchor.cin })
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
  }, [open, entityId, company]);

  const refreshTrx = () => {
    if (!p || trxBusy || !p.anchor.cin) return;
    setTrxBusy(true); setTrxErr('');
    panoramaService.financials({ entityId: p.anchor.entity_id, cin: p.anchor.cin, refresh: true })
      .then(setTrx).catch((e) => setTrxErr(apiErr(e, 'refresh the market feed')))
      .finally(() => setTrxBusy(false));
  };
  const runAsk = async (preset?: string) => {
    const q = (preset ?? ask).trim();
    if (!p || !q) return;
    if (preset) setAsk(preset);
    setAskBusy(true); setAskErr(''); setAskOut(null);
    try { setAskOut(await panoramaService.askDocuments(p.anchor.name, q)); }
    catch (e: any) { setAskErr(String(e?.message || e)); }
    finally { setAskBusy(false); }
  };
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
  const runIndex = async () => {
    if (!p?.anchor.entity_id) return;
    setIdxBusy(true); setIdxErr(''); setIdxOut(null);
    try { setIdxOut(await panoramaService.indexDocuments(p.anchor.entity_id, p.anchor.name)); setAskOut(null); }
    catch (e: any) { setIdxErr(apiErr(e, 'index the documents')); }
    finally { setIdxBusy(false); }
  };

  // ---------- derived reading ----------------------------------------------
  const finState: 'live' | 'nocin' | 'busy' | 'unconfigured' | 'nofilings' | 'error' | 'idle'
    = trx?.resolved ? 'live'
    : !p?.anchor.cin ? 'nocin'
    : trxBusy && !trx ? 'busy'
    : trxErr && /TRACXN_ACCESS_TOKEN/.test(trxErr) ? 'unconfigured'
    : trxErr ? 'error'
    : trx && !trx.resolved ? 'nofilings' : 'idle';
  const series = trx?.series || {};
  const last = (k: string) => { const pts = series[k]?.points; return pts?.length ? pts[pts.length - 1] : null; };
  const prev = (k: string) => { const pts = series[k]?.points; return pts && pts.length > 1 ? pts[pts.length - 2] : null; };
  const revL = last('revenue'), revP = prev('revenue'), patL = last('net_profit');
  const growth = revL && revP && revP.value ? ((revL.value - revP.value) / Math.abs(revP.value)) * 100 : null;
  const eqL = last('bs_equity'), liabL = last('bs_liability');
  const leverage = eqL && liabL && eqL.value ? liabL.value / eqL.value : null;
  const fin = p?.prospect;
  const hasFin = !!fin && (fin.revenue_cr != null || fin.ebitda_cr != null || fin.net_profit_cr != null);
  const idxReady = idxOut ? (idxOut.ready ?? idxOut.indexed.length) : 0;
  const idxStatus = idxBusy ? 'indexing for Q&A…' : idxErr ? 'Q&A indexing failed'
    : idxOut ? `${idxReady} ready for Q&A${idxOut.skipped.length ? ` · ${idxOut.skipped.length} skipped` : ''}` : '';
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

  const events = useMemo<Ev[]>(() => {
    if (!p) return [];
    const out: Ev[] = [];
    const add = (iso: string | null | undefined, label: string, color: string, big = false) => {
      if (!iso) return;
      const t = Date.parse(iso);
      if (Number.isNaN(t)) return;
      out.push({ at: t, date: fmtDate(iso), label, color, big });
    };
    add(trx?.legal_entity?.incorporated, 'Incorporated', '#7B8A92');
    p.leads.forEach((l) => add(l.created_at, `Lead ${l.lead_no || ''} opened`, CHART_TEAL));
    p.lending.forEach((r) => {
      add(r.created_at, `Lending ${r.tracker_no || ''} started`, CHART_TEAL);
      add(r.sanction_date, `Sanctioned ${fmtCr(r.amount_cr)}`, STAGE_COLOR.done, true);
      if (r.stage === 'Disbursed') add(r.stage_updated_at, `Disbursed ${fmtCr(r.disbursed_amount ?? r.amount_cr)}`, STAGE_COLOR.done, true);
      else if (r.stage === 'Rejected') add(r.stage_updated_at, 'Lending rejected', STAGE_COLOR.dead, true);
      else if (r.stage_updated_at && r.stage) add(r.stage_updated_at, `Lending · ${r.stage}`, STAGE_COLOR.hold);
    });
    p.syndication.forEach((r) => {
      add(r.created_at, `Syndication ${r.tracker_no || ''} · ${fmtCr(r.amount_cr)}`, CHART_TEAL);
      (r.lenders || []).filter((x) => x.response_date).slice(0, 4)
        .forEach((x) => add(x.response_date, `${x.name}: ${x.status || 'replied'}`,
          lenderBucket(x.status) === 'dead' ? STAGE_COLOR.dead
            : lenderBucket(x.status) === 'advanced' ? STAGE_COLOR.done : CHART_TEAL));
    });
    p.asset_monetisation.forEach((r) => add(r.created_at, `Asset monetisation ${r.tracker_no || ''}`, CHART_TEAL));
    p.interactions.slice(0, 5).forEach((i) => add(i.occurred_at, `${i.type}${i.by ? ` · ${i.by}` : ''}`, '#1F6FA8'));
    const byDay = new Map<string, number>();
    p.documents.forEach((d) => { const k = (d.uploaded_at || '').slice(0, 10); if (k) byDay.set(k, (byDay.get(k) || 0) + 1); });
    [...byDay.entries()].sort((a, b) => b[0].localeCompare(a[0])).slice(0, 3)
      .forEach(([k, n]) => add(k, `${n} document${n > 1 ? 's' : ''} uploaded`, '#6D5FD3'));
    graded.slice(0, 3).forEach(({ a, sev }) => add(a.when, `News: ${a.headline}`, SEV_BG[sev]));
    if (grade) add(grade.generated_at, `Graded ${grade.label} · ${grade.score}`, BAND_COLOR[grade.rating], true);
    out.sort((a, b) => a.at - b.at);
    if (out.length <= 14) return out;
    return [out[0], ...out.slice(-13)];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p, trx, grade, news]);

  const missing = p?.checklist?.missing || [];
  const lastTouchDays = daysAgo(p?.stats.last_touch);
  const genStamp = useMemo(() => p ? new Date(p.generated_at).toLocaleString('en-IN',
    { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '', [p]);

  // ---------- render ----------------------------------------------------------
  const TabBtn = ({ id, label }: { id: Tab; label: string }) => (
    <Box component="button" onClick={() => setTab(id)}
      sx={{ background: 'none', border: 0, borderBottom: `2px solid ${tab === id ? tokens.tealHi : 'transparent'}`,
        p: '8px 12px', font: 'inherit', fontSize: 12.5, fontWeight: 700, cursor: 'pointer',
        color: tab === id ? tokens.tealHi : tokens.muted, whiteSpace: 'nowrap' }}>{label}</Box>
  );

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth fullScreen={small}
      PaperProps={{ className: 'c360-print', sx: { bgcolor: '#F4F6F7',
        borderRadius: small ? 0 : '14px', maxHeight: small ? '100vh' : '94vh' } }}>
      <style>{`@media print {
        body * { visibility: hidden; }
        .c360-print, .c360-print * { visibility: visible; }
        .c360-print { position: absolute; inset: 0; max-height: none !important; }
        .no-print { display: none !important; }
      }`}</style>

      {/* ---- header ------------------------------------------------------ */}
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.4, p: '13px 20px',
        bgcolor: '#fff', borderBottom: `1px solid ${tokens.line}` }}>
        <Box sx={{ minWidth: 0 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            <Typography sx={{ fontSize: 18, fontWeight: 800, color: INK }}>
              {p?.anchor.name || company || '…'}</Typography>
            {p?.anchor.sector && <Chip size="small" variant="outlined" color="primary"
              label={p.anchor.sector} sx={{ height: 21, fontSize: 10.8 }} />}
            {p?.anchor.state && <Chip size="small" variant="outlined"
              label={p.anchor.state} sx={{ height: 21, fontSize: 10.8 }} />}
          </Box>
          <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
            {[p?.anchor.cin,
              trx?.legal_entity?.incorporated ? `incorporated ${fmtDate(trx.legal_entity.incorporated)}` : null,
              p?.since ? `with PRISM since ${fmtDate(p.since)}` : null,
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
          <IconButton size="small" onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
        </Box>
      </Box>

      <Box sx={{ p: { xs: '12px 12px 16px', sm: '14px 20px 16px' }, overflowY: 'auto',
        overflowX: 'hidden', display: 'flex', flexDirection: 'column', gap: 1.4, minWidth: 0 }}>
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
                {p.lending.map((r) => <Bullet key={r.tracker_no || 'l'}
                  dot={r.stage === 'Disbursed' ? STAGE_COLOR.done : r.stage === 'Rejected' ? STAGE_COLOR.dead : STAGE_COLOR.hold}>
                  <b>Lending {r.tracker_no}</b> — {r.stage || '—'} · {fmtCr(r.amount_cr)}
                  {r.pending_with ? ` · pending with ${r.pending_with}` : ''}
                  {r.stage_updated_at ? ` · since ${fmtDay(r.stage_updated_at)}` : ''}</Bullet>)}
                {p.syndication.map((r) => { const v = lenderView(r); return (
                  <Bullet key={r.tracker_no || 's'} dot={CHART_TEAL}>
                    <b>Syndication {r.tracker_no}</b> — {r.status || '—'} · {fmtCr(r.amount_cr)}
                    {v.ls.length ? ` · ${v.adv.length} advanced · ${v.dead.length} declined of ${v.ls.length}` : ''}
                  </Bullet>); })}
                {p.asset_monetisation.map((r) => <Bullet key={r.tracker_no || 'a'} dot={CHART_TEAL}>
                  <b>Asset monetisation {r.tracker_no}</b> — {r.status || '—'}
                  {r.indicative_value_cr != null ? ` · ${fmtCr(r.indicative_value_cr)}` : ''}</Bullet>)}
                {p.leads.filter((l) => !l.converted).map((l) => <Bullet key={l.lead_no || 'ld'} dot={tokens.tealHi}>
                  <b>Lead {l.lead_no}</b>{l.temperature ? ` · ${l.temperature}` : ''}{l.rm ? ` · ${l.rm}` : ''}
                  {l.next_action ? ` — ${l.next_action.slice(0, 90)}` : ''}</Bullet>)}
                {!p.lending.length && !p.syndication.length && !p.asset_monetisation.length
                  && !p.leads.filter((l) => !l.converted).length && (
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Nothing live on the register.</Typography>)}
              </Box>
            </Card>

            <Card stripe={grade ? BAND_COLOR[grade.rating] : '#B45309'}>
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
                {finState === 'live' && revL ? (
                  <Bullet dot={FIN_SERIES[0][2]}>Revenue <b>{fmtCr(revL.value)}</b> {revL.fy || revL.year}
                    {growth != null ? ` · ${pct(growth)} YoY` : ''}
                    {patL ? ` · PAT ${patL.value < 0 ? '−' : ''}${fmtCr(Math.abs(patL.value))}` : ''} <span style={{ color: tokens.muted }}>(Tracxn filings)</span></Bullet>
                ) : finState === 'nocin' ? (
                  <Bullet dot="#B45309">Filings not fetched — <b>no CIN on record</b>; add it on the lead or client master.</Bullet>
                ) : finState === 'unconfigured' ? (
                  <Bullet dot="#9AA8AF">Market feed not connected on this server.</Bullet>
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
                  <Bullet key={r.tracker_no || 's'} dot={v.dead.length / v.ls.length >= 0.5 ? tokens.bad : tokens.warn}>
                    Lenders on {r.tracker_no}: {v.adv.length} advanced, {v.prog.length} in progress,
                    {' '}{v.dead.length} declined ({Math.round((v.dead.length / v.ls.length) * 100)}%)
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
                  const l = p.leads.find((x) => !x.converted && x.next_action);
                  const pend = p.lending.find((r) => r.pending_with && r.stage !== 'Disbursed' && r.stage !== 'Rejected');
                  if (grade?.action) return grade.action;
                  if (pend) return `Lending ${pend.tracker_no} pending with ${pend.pending_with}`;
                  if (l) return l.next_action!.slice(0, 80);
                  return 'Nothing pending on record';
                })()}
              </Typography>
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.4 }}>
                <Bullet dot={lastTouchDays != null && lastTouchDays > 14 ? tokens.warn : tokens.ok}>
                  Last touch {lastTouchDays == null ? '—' : lastTouchDays === 0 ? 'today' : `${lastTouchDays} day${lastTouchDays > 1 ? 's' : ''} ago`}
                  {p.interactions[0]?.by ? ` · ${p.interactions[0].by}` : ''}</Bullet>
                {p.leads.filter((l) => !l.converted && l.next_action).slice(0, 2).map((l) => (
                  <Bullet key={l.lead_no || 'n'} dot={tokens.tealHi}>{l.lead_no}: {l.next_action}
                    {l.next_action_date ? ` · by ${fmtDay(l.next_action_date)}` : ''}</Bullet>))}
                {p.syndication.map((r) => { const wait = (r.lenders || []).filter((x) => lenderBucket(x.status) === 'progress' && !x.response_date);
                  return wait.length ? <Bullet key={r.tracker_no || 'w'} dot={tokens.warn}>
                    {wait.length} lender{wait.length > 1 ? 's' : ''} yet to reply on {r.tracker_no}: {wait.slice(0, 3).map((x) => x.name).join(', ')}{wait.length > 3 ? '…' : ''}</Bullet> : null; })}
                {missing.length > 0 ? (
                  <Bullet dot={tokens.warn}><b>{missing.length} required document{missing.length > 1 ? 's' : ''} to request:</b> {missing.slice(0, 4).map((m) => m.label).filter(Boolean).join(', ')}{missing.length > 4 ? ` +${missing.length - 4} more` : ''}</Bullet>
                ) : p.checklist && p.checklist.required_total > 0 ? (
                  <Bullet dot={tokens.ok}>Data Register complete — {p.checklist.required_on_file}/{p.checklist.required_total} required on file</Bullet>
                ) : null}
                {grade?.conditions?.[0] && <Bullet dot="#1F6FA8"><b>Condition:</b> {grade.conditions[0]}</Bullet>}
              </Box>
            </Card>
          </Box>

          {/* ---- brief line --------------------------------------------- */}
          <Typography sx={{ fontSize: 12.6, color: INK, lineHeight: 1.6, px: 0.4 }}>{p.brief}</Typography>

          {/* ---- timeline ---------------------------------------------- */}
          {events.length > 0 && (
            <Card>
              <Eyebrow right={<Typography sx={{ fontSize: 10.5, color: tokens.muted }}>every dated fact PRISM holds, in order</Typography>}>
                Relationship timeline</Eyebrow>
              <Box sx={{ overflowX: 'auto' }}><Box sx={{ minWidth: 640 }}><Timeline events={events} /></Box></Box>
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
                      <b>Lending {r.tracker_no}</b> — {r.stage || '—'} · ask {fmtCr(r.amount_cr)}
                      {r.disbursed_amount != null ? ` · disbursed ${fmtCr(r.disbursed_amount)}` : ''}
                      {r.sanction_date ? ` · sanctioned ${fmtDay(r.sanction_date)}` : ''}
                      <span style={{ color: tokens.muted }}>{r.rm ? ` · ${r.rm}` : ''}{r.analyst ? ` / ${r.analyst}` : ''}</span>
                    </Typography>
                    <Ladder ladder={r.ladder} current={r.stage || null} />
                    {r.remarks && <Typography sx={{ fontSize: 11.2, color: tokens.muted, mt: 0.4 }}>{r.remarks.slice(0, 200)}</Typography>}
                  </Box>))}
                {p.syndication.map((r) => { const v = lenderView(r); const n = v.ls.length || 1;
                  const rows = [...v.adv, ...v.prog, ...v.dead];
                  return (
                    <Box key={r.tracker_no || 's'}>
                      <Typography sx={{ fontSize: 12.8, color: INK, mb: 0.6 }}>
                        <b>Syndication {r.tracker_no}</b> — {r.status || '—'} · ask {fmtCr(r.amount_cr)}
                        {v.offered ? ` · ${fmtCr(v.offered)} on the table` : ''}
                        <span style={{ color: tokens.muted }}>{r.rm ? ` · ${r.rm}` : ''}</span>
                      </Typography>
                      {v.ls.length > 0 && (
                        <Box sx={{ display: 'grid', gridTemplateColumns: '120px minmax(0,1fr) 34px', gap: '4px 8px', alignItems: 'center', fontSize: 11.6, mb: 0.8 }}>
                          {([['Approached', v.ls.length, CHART_TEAL], ['In progress', v.prog.length, CHART_TEAL],
                            ['Advanced', v.adv.length, STAGE_COLOR.done], ['Declined / dropped', v.dead.length, tokens.bad]] as const).map(([lab, k, col]) => (
                            <Box key={lab} sx={{ display: 'contents' }}>
                              <Typography sx={{ fontSize: 11.4, color: INK }}>{lab}</Typography>
                              <Box sx={{ height: 10, bgcolor: '#EEF4F3', borderRadius: 3, position: 'relative' }}>
                                <Box sx={{ position: 'absolute', top: 0, bottom: 0, left: 0, width: `${(k / n) * 100}%`, bgcolor: col, borderRadius: 3 }} />
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
                                    <td style={{ color: why ? INK : tokens.warn, fontSize: 11.2 }}>{why ? why.slice(0, 140) : (b === 'dead' ? 'no reason recorded — log the reply' : 'no reply recorded')}</td>
                                    <td style={{ color: tokens.muted, whiteSpace: 'nowrap' }}>{b === 'dead' ? '—' : w}</td>
                                  </tr>); })}
                            </tbody>
                          </Box>
                        </Box>)}
                    </Box>); })}
                {p.asset_monetisation.map((r) => (
                  <Typography key={r.tracker_no || 'a'} sx={{ fontSize: 12.8, color: INK }}>
                    <b>Asset monetisation {r.tracker_no}</b> — {r.status || '—'}
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
                : finState === 'busy' ? <CircularProgress size={12} /> : undefined}>
                Financial snapshot</Eyebrow>
              {finState === 'live' && (() => {
                const years = [...new Set(SNAPSHOT_ROWS.flatMap(([k]) => (series[k]?.points || []).map((x) => x.year)))].sort().slice(-4);
                const cell = (k: string, y: number) => series[k]?.points.find((x) => x.year === y) || null;
                const fyLabel = (y: number) => SNAPSHOT_ROWS.map(([k]) => cell(k, y)?.fy).find(Boolean) || String(y);
                const ratio = (num: string, den: string, y: number) => { const a = cell(num, y), b = cell(den, y); return a && b && b.value ? (a.value / Math.abs(b.value)) * 100 : null; };
                const main = FIN_SERIES.filter(([k]) => series[k]?.points.length);
                return <>
                  {main.length > 0 && (
                    <Box sx={{ display: 'grid', gap: 1.2, mb: 1, gridTemplateColumns: `repeat(${Math.min(main.length, 3)}, minmax(0,1fr))` }}>
                      {main.map(([k, lab, col]) => <MiniBars key={k} label={lab} series={series[k]} color={col} />)}
                    </Box>)}
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
              {finState === 'busy' && <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Asking the market feed for CIN {p.anchor.cin}…</Typography>}
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

          {/* ---- depth tabs ---------------------------------------------- */}
          <Card sx={{ p: 0 }}>
            <Box sx={{ display: 'flex', gap: 0.2, borderBottom: `1px solid ${tokens.line}`, px: 1, overflowX: 'auto' }}>
              <TabBtn id="engagements" label={`Engagements · ${p.leads.filter((l) => !l.converted).length + p.lending.length + p.syndication.length + p.asset_monetisation.length}`} />
              <TabBtn id="people" label={`People · ${(trx?.board?.length || 0) + p.contacts.length}`} />
              <TabBtn id="documents" label={`Documents · ${p.stats.documents}`} />
              <TabBtn id="news" label={`News · ${news ? news.length : '…'}`} />
              <TabBtn id="risk" label={grade ? `Risk report · ${grade.score}` : 'Risk report'} />
              {p.prospect && <TabBtn id="prospect" label="Prospect universe" />}
            </Box>
            <Box sx={{ p: '12px 14px', display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>

              {tab === 'engagements' && <>
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
                    <Typography sx={{ fontSize: 12.8, color: INK }}><b>Lending {r.tracker_no}</b> — {r.stage || '—'} · {fmtCr(r.amount_cr)}{r.pending_with ? ` · pending with ${r.pending_with}` : ''}
                      <span style={{ color: tokens.muted }}>{[r.rm, r.analyst, r.created_at ? `since ${fmtDate(r.created_at)}` : null].filter(Boolean).map((x) => ` · ${x}`).join('')}</span></Typography>
                    <Ladder ladder={r.ladder} current={r.stage || null} />
                  </Box>))}
                {p.syndication.map((r) => (
                  <Box key={r.tracker_no || 's'}>
                    <Typography sx={{ fontSize: 12.8, color: INK }}><b>Syndication {r.tracker_no}</b> — {r.status || '—'}{r.amount_cr != null ? ` · ${fmtCr(r.amount_cr)}` : ''}<span style={{ color: tokens.muted }}>{r.created_at ? ` · since ${fmtDate(r.created_at)}` : ''}</span></Typography>
                    {(r.lenders || []).map((x) => <Typography key={x.name} sx={{ fontSize: 11.4, color: tokens.muted, pl: 1.5 }}>{x.name} — {x.status || '—'}{x.amount_cr != null ? ` · ${fmtCr(x.amount_cr)}` : ''}{x.last_reply ? ` · ${x.last_reply.slice(0, 120)}` : ''}</Typography>)}
                  </Box>))}
                {p.asset_monetisation.map((r) => (
                  <Typography key={r.tracker_no || 'a'} sx={{ fontSize: 12.8, color: INK }}><b>Asset monetisation {r.tracker_no}</b> — {r.status || '—'}{r.indicative_value_cr != null ? ` · ${fmtCr(r.indicative_value_cr)}` : ''}{r.investor ? ` · ${r.investor}` : ''}</Typography>))}
                <Box sx={{ mt: 0.6 }}>
                  <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted, mb: 0.4 }}>INTERACTIONS & VOCX · {p.interactions.length}</Typography>
                  <Box sx={{ maxHeight: 260, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 0.8, pr: 0.5 }}>
                    {p.interactions.map((i, n) => (
                      <Box key={n} sx={{ flexShrink: 0 }}>
                        <Typography sx={{ fontSize: 12.4, color: INK }}>{i.type} · {fmtDate(i.occurred_at)}{i.by ? ` · ${i.by}` : ''}{i.lender ? ` · ${i.lender}` : ''}</Typography>
                        {(i.summary || i.notes) && <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>{(i.summary || i.notes || '').slice(0, 220)}</Typography>}
                      </Box>))}
                    {!p.interactions.length && <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Nothing logged yet.</Typography>}
                  </Box>
                </Box>
              </>}

              {tab === 'people' && <>
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
              </>}

              {tab === 'documents' && <>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                  <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>{p.stats.documents} on the Data Register{idxStatus ? ` · ${idxStatus}` : ''}</Typography>
                  {idxBusy && <CircularProgress size={10} />}
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
                <Box className="no-print" sx={{ display: 'flex', flexDirection: 'column', gap: 0.7, mt: 0.6 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.06em', color: tokens.muted }}>ASK THE DOCUMENTS</Typography>
                    <Box sx={{ flex: 1 }} />
                    {idxErr && p.anchor.entity_id && <Button size="small" variant="text" onClick={runIndex} disabled={idxBusy} sx={{ fontSize: 11, py: 0, minWidth: 0 }}>retry indexing</Button>}
                  </Box>
                  {idxOut && idxOut.skipped.length > 0 && (
                    <Alert severity="info" sx={{ fontSize: 11.2, py: 0 }}>{idxOut.skipped.length} file(s) not indexed: {idxOut.skipped.slice(0, 3).map((x) => `${x.file} — ${x.reason}`).join('; ')}{idxOut.skipped.length > 3 ? '; …' : ''}</Alert>)}
                  {idxOut?.note && (idxOut.fresh ?? 0) > 0 && <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>{idxOut.note}</Typography>}
                  {idxErr && <Typography sx={{ fontSize: 11.2, color: tokens.muted }}>{idxErr}</Typography>}
                  <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
                    {ASK_PRESETS.map(([label, q]) => <Chip key={label} size="small" clickable label={label} disabled={askBusy || idxBusy} onClick={() => runAsk(q)}
                      sx={{ height: 22, fontSize: 10.8, fontWeight: 600, color: tokens.tealHi, bgcolor: '#F0F8F6', border: `1px solid ${tokens.tealHi}` }} />)}
                  </Box>
                  <Box sx={{ display: 'flex', gap: 0.8 }}>
                    <TextField size="small" fullWidth value={ask} placeholder="…or ask anything about these files" onChange={(e) => setAsk(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') runAsk(); }} />
                    <Button size="small" variant="outlined" onClick={() => runAsk()} disabled={askBusy || idxBusy || !ask.trim()}>{askBusy ? <CircularProgress size={15} /> : 'Ask'}</Button>
                  </Box>
                  {askOut?.noDocuments && <Alert severity="info" sx={{ fontSize: 11.6, py: 0 }}>{p.stats.documents > 0 ? 'None of this company’s files could be indexed for Q&A — readable kinds are PDF, Excel, images, and zips of those.' : 'No documents on the Data Register yet — upload some, and they are indexed for Q&A the next time this opens.'}</Alert>}
                  {askOut?.answer && (
                    <Box sx={{ bgcolor: '#F6FAF9', border: '1px solid #D6E7E3', borderRadius: '10px', p: '9px 11px' }}>
                      <Typography sx={{ fontSize: 12.2, color: INK, whiteSpace: 'pre-wrap', maxHeight: 320, overflowY: 'auto' }}>{tidyAnswer(askOut.answer).slice(0, 2400)}</Typography>
                      {askOut.citations.length > 0 && <Typography sx={{ fontSize: 10.5, color: tokens.tealHi, fontWeight: 600, mt: 0.5 }}>cited · {askOut.citations.map((c) => [c.doc, c.where].filter(Boolean).join(' ')).join(' · ')}</Typography>}
                      {askOut.mode === 'extractive' && <Typography sx={{ fontSize: 10, color: tokens.muted, mt: 0.4 }}>Matching passages shown — written summaries switch on with the answer model.</Typography>}
                    </Box>)}
                  {askErr && <Typography sx={{ fontSize: 11.2, color: tokens.muted }}>Document AI did not answer: {askErr}</Typography>}
                </Box>
              </>}

              {tab === 'news' && <>
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
              </>}

              {tab === 'risk' && (grade ? <RiskGradeDetails grade={grade} onRegrade={gradeBusy ? undefined : () => runGrade(true)} /> : (
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2 }}>
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted, flex: 1 }}>
                    Grades this client RED / AMBER / GREEN from its Register footprint, indexed documents and 30-day news, using the ATLAS client rubric. A desk aid, not a credit decision.
                    {gradeErr && <span style={{ color: tokens.bad }}> {gradeErr}</span>}
                  </Typography>
                  <Button size="small" variant="outlined" disabled={gradeBusy || (news === null && !newsErr)} onClick={() => runGrade(false)}>
                    {gradeBusy ? <CircularProgress size={15} /> : 'Grade risk'}</Button>
                </Box>))}

              {tab === 'prospect' && p.prospect && <>
                <Typography sx={{ fontSize: 12.2, color: INK }}><b>{p.prospect.prospect_no}</b>{!live && !p.leads.length ? ` · ${p.prospect.status.replace(/_/g, ' ')}` : ''}{p.prospect.lead_count ? ` · ${p.prospect.lead_count} lead(s) from this row` : ''}</Typography>
                <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap' }}>
                  {(p.prospect.verticals || []).map((v) => <Chip key={v} size="small" color="primary" variant="outlined" label={v} sx={{ height: 20, fontSize: 10.6 }} />)}
                  {(p.prospect.sub_sectors || []).map((v) => <Chip key={v} size="small" variant="outlined" label={v} sx={{ height: 20, fontSize: 10.6 }} />)}
                </Box>
                {p.prospect.remarks && <Typography sx={{ fontSize: 12, color: INK }}>{p.prospect.remarks}</Typography>}
              </>}
            </Box>
          </Card>

          <Typography sx={{ fontSize: 10.8, color: tokens.muted, pt: 0.4 }}>
            Sources: Register · field notes · news radar · documents{finState === 'live' ? ' · Tracxn filings' : ''}{grade ? ' · risk rubric (AI-assisted)' : ''} — generated {genStamp}
            {p.restricted.length ? ` · not visible to your role: ${p.restricted.join(', ')}` : ''}
          </Typography>
        </>}
      </Box>
    </Dialog>
  );
}
