import { useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, IconButton, TextField,
  Tooltip, Typography, useMediaQuery,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import DownloadIcon from '@mui/icons-material/Download';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import ChevronRightIcon from '@mui/icons-material/ChevronRight';
import TrackChangesIcon from '@mui/icons-material/TrackChanges';
import { tokens } from '../../theme';
import { apiErr } from '../../api/http';
import { panoramaService, type DocAskResult, type IndexResult, type Panorama,
  type TracxnFinancials, type TracxnSeries, type RiskGrade }
  from '../../services/panoramaService';
import { BAND_COLOR, RiskGradeChip, RiskGradeDetails } from './RiskGrade';
import { classify, fetchTerm, SEV_LABEL, type Article, type Severity }
  from '../../services/newsService';

/**
 * Company 360 — one company's whole story, in one place (the approved mockup).
 *
 * Summary first, expand on need: five stat tiles and the BRIEF read everything;
 * each section below is a one-line row that opens on click. The register's
 * panorama endpoint supplies every PRISM section RBAC'd server-side; news comes
 * from PULSE (the radar's own search); the ask-box queries DocRAG restricted to
 * this company's indexed files. The risk grade (on request) is the ATLAS client
 * rubric run by the orchestrator on the CAM's engine — see RiskGrade.tsx.
 * Financials come from the Tracxn market feed, CIN-anchored and cached
 * register-side. Download = the browser's print-to-PDF over a print
 * stylesheet — the footer stamps sources and generation time.
 */

const CHART_TEAL = '#0D9488';

// The dataviz palette for the financial small multiples — each series keeps ONE
// color everywhere it appears, values live on different scales so each series
// gets its own tiny chart rather than sharing an axis.
const FIN_SERIES: [key: string, label: string, color: string][] = [
  ['revenue', 'Revenue', '#0D9488'],
  ['ebitda', 'EBITDA', '#B45309'],
  ['net_profit', 'PAT', '#6D5FD3'],
];
const FIN_CONTEXT: [key: string, label: string][] = [
  ['valuation', 'Valuation'],
  ['employees', 'Employees'],
  ['employees_labour', 'Employees (labour filings)'],
  ['bs_assets', 'Total assets'],
  ['cf_operating', 'Operating cash flow'],
];

function finNum(v: number): string {
  return Math.abs(v) >= 1000 ? Math.round(v).toLocaleString('en-IN')
    : v.toLocaleString('en-IN', { maximumFractionDigits: 1 });
}

/** One small-multiple: thin rounded bars, the newest value labelled in ink,
 *  first/last years only. Negative values (a loss-making PAT) hang below a
 *  zero baseline in the same series color. */
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
        <Box sx={{ width: 7, height: 7, borderRadius: 99, bgcolor: color,
          flexShrink: 0 }} />
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
          fontSize="8.8" fontWeight="700" fill="#17252B">{finNum(last.value)}</text>
        {pts.map((pt, i) => (i === 0 || i === pts.length - 1) ? (
          <text key={pt.year} x={i * step + step / 2} y={H - 2} textAnchor="middle"
            fontSize="7.6" fill="#7B8A92">{pt.year}</text>) : null)}
      </svg>
    </Box>
  );
}

// PULSE's own verdict colors, verbatim from the radar — two PRISM surfaces must
// never disagree about the same headline.
const SEV_BG: Record<Severity, string> = {
  RED: tokens.bad, AMBER: tokens.warn, GREEN: tokens.ok, BLUE: '#1F6FA8',
};

function SevPill({ s }: { s: Severity }) {
  return (
    <Box component="span" sx={{ fontSize: 9.5, fontWeight: 800, borderRadius: 999,
      px: '8px', py: '1px', color: '#fff', bgcolor: SEV_BG[s], flexShrink: 0 }}>
      {SEV_LABEL[s]}</Box>
  );
}

// One-tap questions for the ask-box — the needs the desk actually has when a
// file lands, phrased so retrieval finds the right pages. Free text stays for
// everything else.
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

function fmtCr(v: number | null | undefined): string {
  return v == null ? '—' : `₹${Number(v).toLocaleString('en-IN',
    { maximumFractionDigits: 1 })} Cr`;
}
function fmtDay(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso.slice(0, 10)
    : d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });
}

/** A thin proportional bar — the composition of a number at a glance
 *  (in flight vs booked vs on hold; good vs bad news). Empty segments vanish. */
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

// Product-line stage colors, the same three everywhere a line is drawn.
const STAGE_COLOR = { flight: CHART_TEAL, done: '#1B7A45', hold: '#B45309' };

function Tile({ label, value, sub, bar }: {
  label: string; value: string; sub?: string;
  bar?: { value: number; color: string }[];
}) {
  return (
    <Box sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: '11px',
      p: '10px 13px', display: 'flex', flexDirection: 'column', gap: 0.3, minWidth: 0 }}>
      <Typography sx={{ fontSize: 10, fontWeight: 700, letterSpacing: '.06em',
        color: tokens.muted }}>{label}</Typography>
      <Typography sx={{ fontSize: 19, fontWeight: 800, color: '#17252B',
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{value}</Typography>
      {bar && <Box sx={{ my: '2px' }}><StackBar segs={bar} /></Box>}
      {sub && <Typography sx={{ fontSize: 10.8, color: tokens.muted, whiteSpace: 'nowrap',
        overflow: 'hidden', textOverflow: 'ellipsis' }}>{sub}</Typography>}
    </Box>
  );
}

/** The single-year figures the prospect universe captured, as bars on one
 *  scale — enough to see the shape (thin margin, loss) without a feed. */
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
          <Typography sx={{ fontSize: 11.4, fontWeight: 800, color: '#17252B',
            width: 84, textAlign: 'right', flexShrink: 0 }}>
            {v < 0 ? '−' : ''}{fmtCr(Math.abs(v))}</Typography>
        </Box>))}
    </Box>
  );
}

function Section({ id, title, badge, summary, open, onToggle, children }: {
  id: string; title: string; badge?: React.ReactNode; summary?: string;
  open: boolean; onToggle: (id: string) => void; children?: React.ReactNode;
}) {
  return (
    <Box sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: '12px' }}>
      <Box onClick={() => onToggle(id)}
        sx={{ display: 'flex', alignItems: 'center', gap: 1.1, p: '11px 14px',
          cursor: 'pointer', minHeight: 44, boxSizing: 'border-box' }}>
        <Typography sx={{ fontSize: 11, fontWeight: 800, letterSpacing: '.07em',
          color: '#44535B', flexShrink: 0 }}>{title}</Typography>
        {badge}
        {!open && summary && (
          <Typography sx={{ fontSize: 12, color: tokens.muted, whiteSpace: 'nowrap',
            overflow: 'hidden', textOverflow: 'ellipsis', flex: 1 }}>{summary}</Typography>)}
        <Box sx={{ flex: 1 }} />
        {open ? <ExpandMoreIcon sx={{ fontSize: 17, color: tokens.muted }} />
          : <ChevronRightIcon sx={{ fontSize: 17, color: tokens.muted }} />}
      </Box>
      {open && <Box sx={{ p: '0 14px 13px', display: 'flex', flexDirection: 'column',
        gap: 1, minWidth: 0 }}>{children}</Box>}
    </Box>
  );
}

function Ladder({ ladder, current }: { ladder: string[]; current: string | null }) {
  const idx = current ? ladder.indexOf(current) : -1;
  if (idx < 0) return null;
  // Amber marks WORK IN PROGRESS. The last rung is the finish line — landing
  // there is success and wears green, or a fully-disbursed line would read as
  // something failing.
  const done = idx === ladder.length - 1;
  const mark = done ? '#1B7A45' : '#B45309';
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5, pl: '18px' }}>
      <Box sx={{ display: 'flex', gap: '5px' }}>
        {ladder.map((s, i) => (
          <Box key={s} sx={{ height: 5, flex: 1, borderRadius: '3px',
            bgcolor: i < idx ? CHART_TEAL : i === idx ? mark : '#E7ECEF' }} />))}
      </Box>
      <Box sx={{ display: 'flex' }}>
        {ladder.map((s, i) => (
          <Typography key={s} sx={{ flex: 1, fontSize: 8.6, lineHeight: 1.2,
            color: i === idx ? mark : tokens.muted,
            fontWeight: i === idx ? 700 : 400 }}>{s.replace(' Completed', '')
              .replace('Ready for Disbursement', 'Ready')}</Typography>))}
      </Box>
    </Box>
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

export default function Company360Dialog({ open, entityId, company, onClose }: {
  open: boolean; entityId?: string | null; company?: string; onClose: () => void;
}) {
  const [p, setP] = useState<Panorama | null>(null);
  const [err, setErr] = useState('');
  const [news, setNews] = useState<Article[] | null>(null);
  const [newsErr, setNewsErr] = useState('');
  const [openSections, setOpenSections] = useState<Record<string, boolean>>(
    { engagements: true });
  const [briefOpen, setBriefOpen] = useState(false);
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

  const small = useMediaQuery('(max-width:700px)');

  useEffect(() => {
    if (!open) return;
    setP(null); setErr(''); setNews(null); setNewsErr('');
    setAsk(''); setAskOut(null); setAskErr('');
    setIdxBusy(false); setIdxOut(null); setIdxErr('');
    setTrx(null); setTrxBusy(false); setTrxErr('');
    setGrade(null); setGradeBusy(false); setGradeErr('');
    setOpenSections({ engagements: true }); setBriefOpen(false); setNewsSev(null);
    let alive = true;
    panoramaService.get({ entityId, company })
      .then((r) => { if (!alive) return; setP(r);
        // The ask-box's index is housekeeping, not a decision — it runs itself.
        // The bridge only uploads what DocRAG does not already hold, so a
        // re-open costs one listing.
        if (r.stats.documents > 0 && r.anchor.entity_id) {
          setIdxBusy(true);
          panoramaService.indexDocuments(r.anchor.entity_id, r.anchor.name)
            .then((x) => { if (alive) setIdxOut(x); })
            .catch((e) => { if (alive) setIdxErr(apiErr(e, 'index the documents')); })
            .finally(() => { if (alive) setIdxBusy(false); });
        }
        // A grade computed earlier (by anyone, for this role's view) shows at once;
        // a new one is only ever computed on request — it is a model call.
        panoramaService.riskGrade(r.anchor.entity_id, r.anchor.name)
          .then((g) => { if (alive && g) setGrade(g); })
          .catch(() => { /* no saved grade to show — the button stays */ });
        // Date-bounded so the "30 days" tile means 30 days, not "whatever came back".
        const from = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);
        fetchTerm(r.anchor.name, from)
          .then((arts) => { if (alive) setNews(arts.slice(0, 15)); })
          .catch((e) => { if (alive) setNewsErr(String(e?.message || e)); });
      })
      .catch((e) => { if (alive) setErr(apiErr(e, 'load the company 360')); });
    return () => { alive = false; };
  }, [open, entityId, company]);

  const toggle = (id: string) =>
    setOpenSections((s) => ({ ...s, [id]: !s[id] }));

  // The market feed is fetched LAZILY, on the card's first expand — the register
  // caches per CIN so a re-open is free, but the dialog itself should not spend
  // a 15-endpoint resolve for a user who never opens FINANCIALS.
  useEffect(() => {
    if (!open || !p || !openSections.financials) return;
    if (trx || trxBusy || trxErr) return;
    // No CIN anywhere (master, leads, prospect row) → nothing to look up. The
    // card explains itself instead of asking a paid API a question it cannot
    // answer.
    if (!p.anchor.cin) return;
    let alive = true;
    setTrxBusy(true);
    panoramaService.financials({ entityId: p.anchor.entity_id, cin: p.anchor.cin })
      .then((r) => { if (alive) setTrx(r); })
      .catch((e) => { if (alive) setTrxErr(apiErr(e, 'reach the market feed')); })
      .finally(() => { if (alive) setTrxBusy(false); });
    return () => { alive = false; };
  }, [open, p, openSections.financials, trx, trxBusy, trxErr]);

  const refreshTrx = () => {
    if (!p || trxBusy) return;
    setTrxBusy(true); setTrxErr('');
    panoramaService.financials({ entityId: p.anchor.entity_id,
      cin: p.anchor.cin, refresh: true })
      .then(setTrx)
      .catch((e) => setTrxErr(apiErr(e, 'refresh the market feed')))
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
    // The same verdicts the News section shows: PULSE's, else the local classifier.
    const live = (p.stats.deals_in_flight + p.stats.deals_done) > 0;
    const newsIn = (news || []).map((a) => ({ headline: a.headline, source: a.source,
      when: a.when, severity: a.severity || classify(a.headline, live)[0] }));
    try {
      setGrade(await panoramaService.gradeRisk(p.anchor.entity_id, p.anchor.name,
        newsIn, refresh));
    } catch (e: any) { setGradeErr(apiErr(e, 'grade this client')); }
    finally { setGradeBusy(false); }
  };

  // Only reached from "retry" after a failed automatic pass.
  const runIndex = async () => {
    if (!p?.anchor.entity_id) return;
    setIdxBusy(true); setIdxErr(''); setIdxOut(null);
    try {
      const r = await panoramaService.indexDocuments(p.anchor.entity_id, p.anchor.name);
      setIdxOut(r); setAskOut(null);
    } catch (e: any) { setIdxErr(apiErr(e, 'index the documents')); }
    finally { setIdxBusy(false); }
  };
  const idxReady = idxOut ? (idxOut.ready ?? idxOut.indexed.length) : 0;
  const idxStatus = idxBusy ? 'indexing for Q&A…'
    : idxErr ? 'Q&A indexing failed'
    : idxOut ? `${idxReady} ready for Q&A${idxOut.skipped.length
        ? ` · ${idxOut.skipped.length} skipped` : ''}`
    : '';

  // What the FINANCIALS card is in, and why — one word the badge, summary and
  // body all agree on.
  const finState: 'live' | 'nocin' | 'busy' | 'unconfigured' | 'nofilings'
    | 'error' | 'idle' = trx?.resolved ? 'live'
    : !p?.anchor.cin ? 'nocin'
    : trxBusy && !trx ? 'busy'
    : trxErr && /TRACXN_ACCESS_TOKEN/.test(trxErr) ? 'unconfigured'
    : trxErr ? 'error'
    : trx && !trx.resolved ? 'nofilings'
    : 'idle';

  const inPrism = !!p && (p.leads.length > 0 || (p.stats.deal_count ?? 0) > 0
    || p.stats.live_deals > 0);
  const fin = p?.prospect;
  const hasFin = !!fin && (fin.revenue_cr != null || fin.ebitda_cr != null
    || fin.net_profit_cr != null);
  const genStamp = useMemo(() => p
    ? new Date(p.generated_at).toLocaleString('en-IN',
      { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
    : '', [p]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth fullScreen={small}
      PaperProps={{ className: 'c360-print', sx: { bgcolor: '#F4F6F7',
        borderRadius: small ? 0 : '14px', maxHeight: small ? '100vh' : '94vh' } }}>
      {/* Print: only the dialog, expanded, on white. */}
      <style>{`@media print {
        body * { visibility: hidden; }
        .c360-print, .c360-print * { visibility: visible; }
        .c360-print { position: absolute; inset: 0; max-height: none !important; }
      }`}</style>

      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.4, p: '13px 20px',
        bgcolor: '#fff', borderBottom: `1px solid ${tokens.line}` }}>
        <Box sx={{ minWidth: 0 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            <Typography sx={{ fontSize: 18, fontWeight: 800, color: '#17252B' }}>
              {p?.anchor.name || company || '…'}</Typography>
            {p?.anchor.sector && <Chip size="small" variant="outlined" color="primary"
              label={p.anchor.sector} sx={{ height: 21, fontSize: 10.8 }} />}
            {p?.anchor.state && <Chip size="small" variant="outlined"
              label={p.anchor.state} sx={{ height: 21, fontSize: 10.8 }} />}
          </Box>
          <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
            {[p?.anchor.cin, p?.anchor.domain,
              p ? (p.anchor.matched_by === 'name-only'
                ? 'no client master yet' : 'settled via client master') : null]
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
        overflowX: 'hidden', display: 'flex', flexDirection: 'column', gap: 1.4,
        minWidth: 0 }}>
        {err && <Alert severity="warning" sx={{ fontSize: 12.4 }}>{err}</Alert>}
        {!p && !err && (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
            <CircularProgress size={26} /></Box>)}

        {p && <>
          <Box sx={{ display: 'grid', gap: 1.2,
            gridTemplateColumns: { xs: 'repeat(2, minmax(0, 1fr))',
              sm: 'repeat(3, minmax(0, 1fr))', md: 'repeat(5, minmax(0, 1fr))' } }}>
            <Tile label="OPEN LEADS" value={String(p.stats.open_leads)}
              sub={p.stats.leads_converted
                ? `${p.stats.leads_converted} became deal(s)` : undefined} />
            <Tile
              label={p.stats.deal_count != null ? 'DEALS · PRODUCTS' : 'PRODUCTS'}
              value={p.stats.deal_count != null
                ? `${p.stats.deal_count} · ${p.stats.live_deals}`
                : String(p.stats.live_deals)}
              bar={[{ value: p.stats.deals_in_flight, color: STAGE_COLOR.flight },
                { value: p.stats.deals_done, color: STAGE_COLOR.done },
                { value: p.stats.deals_on_hold, color: STAGE_COLOR.hold }]}
              sub={[
                p.stats.deals_in_flight ? `${p.stats.deals_in_flight} in flight` : '',
                p.stats.deals_done ? `${p.stats.deals_done} disbursed` : '',
                p.stats.deals_on_hold ? `${p.stats.deals_on_hold} on hold` : '',
              ].filter(Boolean).join(' · ') || undefined} />
            <Tile label="EXPOSURE"
              bar={[{ value: p.stats.exposure_ask_cr || 0, color: STAGE_COLOR.flight },
                { value: p.stats.booked_cr || 0, color: STAGE_COLOR.done },
                { value: p.stats.on_hold_cr || 0, color: STAGE_COLOR.hold }]}
              value={p.stats.exposure_ask_cr != null ? fmtCr(p.stats.exposure_ask_cr)
                : p.stats.booked_cr != null ? fmtCr(p.stats.booked_cr)
                : p.stats.on_hold_cr != null ? fmtCr(p.stats.on_hold_cr) : '—'}
              sub={[
                p.stats.exposure_ask_cr != null ? 'in-flight ask' : '',
                p.stats.booked_cr != null
                  ? (p.stats.exposure_ask_cr != null
                    ? `${fmtCr(p.stats.booked_cr)} booked` : 'booked · disbursed') : '',
                p.stats.on_hold_cr != null
                  ? (p.stats.exposure_ask_cr != null || p.stats.booked_cr != null
                    ? `${fmtCr(p.stats.on_hold_cr)} on hold` : 'on hold') : '',
              ].filter(Boolean).join(' · ') || undefined} />
            <Tile label="LAST TOUCH" value={fmtDay(p.stats.last_touch)}
              sub={p.interactions[0]?.by || undefined} />
            <Tile label="NEWS · 30 DAYS" value={news ? String(news.length) : '…'}
              bar={news ? (() => {
                const live = (p.stats.deals_in_flight + p.stats.deals_done) > 0;
                const sevs = news.map((a) => a.severity || classify(a.headline, live)[0]);
                return (['GREEN', 'BLUE', 'AMBER', 'RED'] as Severity[]).map((s) => ({
                  value: sevs.filter((x) => x === s).length, color: SEV_BG[s] }));
              })() : undefined}
              sub={news === null ? (newsErr ? 'radar unavailable' : 'searching…')
                : 'via PULSE radar'} />
          </Box>

          <Box sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`,
            borderRadius: '12px', p: '12px 14px' }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.6 }}>
              <Typography sx={{ fontSize: 11, fontWeight: 800, letterSpacing: '.07em',
                color: '#44535B' }}>BRIEF</Typography>
              <Box sx={{ flex: 1 }} />
              <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>
                from register · field notes · news · documents</Typography>
            </Box>
            <Typography sx={{ fontSize: 13, color: '#17252B', lineHeight: 1.6,
              // Expanded, the card holds its ground and the TEXT scrolls — a
              // long field note must not shove the sections off screen.
              ...(briefOpen ? { maxHeight: 240, overflowY: 'auto', pr: 1,
                display: 'block' } : {}) }}>
              {briefOpen ? (p.brief_full || p.brief) : p.brief}
              {(p.brief_full || '') !== p.brief && (
                <Typography component="span" onClick={() => setBriefOpen((v) => !v)}
                  sx={{ fontSize: 12.5, fontWeight: 700, color: tokens.tealHi,
                    cursor: 'pointer', ml: 0.6, userSelect: 'none',
                    '&:hover': { textDecoration: 'underline' } }}>
                  {briefOpen ? 'less' : 'more'}
                </Typography>)}
            </Typography>
          </Box>

          <Box sx={{ display: 'grid', gap: 1.4, alignItems: 'start',
            gridTemplateColumns: { xs: 'minmax(0, 1fr)',
              md: 'minmax(0, 1fr) minmax(0, 1fr)' } }}>
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.2, minWidth: 0 }}>
              <Section id="engagements" title="ENGAGEMENTS"
                open={!!openSections.engagements} onToggle={toggle}
                summary={`${p.stats.open_leads} open lead(s) · ${p.stats.live_deals} live deal(s)`}>
                <Box sx={{ maxHeight: 420, overflowY: 'auto', display: 'flex',
                  flexDirection: 'column', gap: 1, pr: 0.5 }}>
                {p.leads.filter((l) => !l.converted).map((l) => (
                  <Box key={l.lead_no || Math.random()} sx={{ display: 'flex', gap: 1.1 }}>
                    <Box sx={{ width: 8, height: 8, mt: '5px', borderRadius: 99,
                      bgcolor: tokens.tealHi, flexShrink: 0 }} />
                    <Box>
                      <Typography sx={{ fontSize: 12.8, color: '#17252B' }}>
                        <b>{l.lead_no}</b> — {['Lead', l.temperature, l.rm || 'unassigned']
                          .filter(Boolean).join(' · ')}
                      </Typography>
                      <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
                        {[fmtDay(l.last_interaction_date), l.next_action || l.notes]
                          .filter(Boolean).join(' · ')}</Typography>
                    </Box>
                  </Box>))}
                {p.lending.map((r) => (
                  <Box key={r.tracker_no || 'l'} sx={{ display: 'flex',
                    flexDirection: 'column', gap: 0.7 }}>
                    <Box sx={{ display: 'flex', gap: 1.1 }}>
                      <Box sx={{ width: 8, height: 8, mt: '5px', borderRadius: 99,
                        bgcolor: r.stage === 'Rejected' ? '#9AA8AF'
                          : r.stage === 'Disbursed' ? '#1B7A45' : '#B45309',
                        flexShrink: 0 }} />
                      <Box>
                        <Typography sx={{ fontSize: 12.8, color: '#17252B' }}>
                          <b>Lending {r.tracker_no}</b> — {r.stage || '—'} · {fmtCr(r.amount_cr)}
                          {r.pending_with ? ` · pending with ${r.pending_with}` : ''}
                        </Typography>
                        <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
                          {[r.rm, r.stage_updated_at
                            ? `stage since ${fmtDay(r.stage_updated_at)}` : null]
                            .filter(Boolean).join(' · ')}</Typography>
                      </Box>
                    </Box>
                    <Ladder ladder={r.ladder} current={r.stage || null} />
                  </Box>))}
                {p.syndication.map((r) => (
                  <Box key={r.tracker_no || 's'} sx={{ display: 'flex',
                    flexDirection: 'column', gap: 0.7 }}>
                    <Typography sx={{ fontSize: 12.8, color: '#17252B', pl: '19px' }}>
                      <b>Syndication {r.tracker_no}</b> — {r.status || '—'}
                      {r.amount_cr != null ? ` · ${fmtCr(r.amount_cr)}` : ''}
                    </Typography>
                    {(r.lenders || []).slice(0, 4).map((x) => (
                      <Typography key={x.name} sx={{ fontSize: 11.4,
                        color: tokens.muted, pl: '19px' }}>
                        {x.name} — {x.status || '—'}
                        {x.last_reply ? ` · ${x.last_reply.slice(0, 60)}` : ''}
                      </Typography>))}
                  </Box>))}
                {p.asset_monetisation.map((r) => (
                  <Typography key={r.tracker_no || 'a'} sx={{ fontSize: 12.8,
                    color: '#17252B', pl: '19px' }}>
                    <b>Asset Monetisation {r.tracker_no}</b> — {r.status || '—'}
                    {r.indicative_value_cr != null
                      ? ` · ${fmtCr(r.indicative_value_cr)}` : ''}
                  </Typography>))}
                {!p.lending.length && !p.syndication.length
                  && !p.asset_monetisation.length && (
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
                    No live product lines.</Typography>)}
                </Box>
              </Section>

              <Section id="contacts" title="KEY CONTACTS"
                open={!!openSections.contacts} onToggle={toggle}
                summary={p.contacts.length
                  ? `${p.contacts[0].name}${p.contacts[0].designation
                    ? ` · ${p.contacts[0].designation}` : ''}`
                  : 'none on record'}>
                {p.contacts.map((c) => (
                  <Typography key={c.name} sx={{ fontSize: 12.6, color: '#17252B' }}>
                    <b>{c.name}</b>{c.designation ? ` · ${c.designation}` : ''}
                    {c.phone ? ` · ${c.phone}` : ''}
                    <Typography component="span" sx={{ fontSize: 10.8,
                      color: tokens.muted }}> ({c.source})</Typography>
                  </Typography>))}
                {!p.contacts.length && <Typography sx={{ fontSize: 11.6,
                  color: tokens.muted }}>No contacts on record yet.</Typography>}
              </Section>

              <Section id="interactions" title="INTERACTIONS & VOCX"
                open={!!openSections.interactions} onToggle={toggle}
                summary={p.interactions.length
                  ? `${fmtDay(p.interactions[0].occurred_at)} · ${((p.interactions[0]
                      .summary || p.interactions[0].notes || p.interactions[0].type)
                      .slice(0, 90))} · ${p.interactions.length} recent`
                  : 'none logged'}>
                <Box sx={{ maxHeight: 300, overflowY: 'auto', display: 'flex',
                  flexDirection: 'column', gap: 1, pr: 0.5 }}>
                {p.interactions.map((i, n) => (
                  <Box key={n} sx={{ flexShrink: 0 }}>
                    <Typography sx={{ fontSize: 12.6, color: '#17252B' }}>
                      {i.type} · {fmtDay(i.occurred_at)}
                      {i.by ? ` · ${i.by}` : ''}</Typography>
                    {(i.summary || i.notes) && (
                      <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
                        {(i.summary || i.notes || '').slice(0, 160)}</Typography>)}
                  </Box>))}
                </Box>
                {!p.interactions.length && <Typography sx={{ fontSize: 11.6,
                  color: tokens.muted }}>Nothing logged yet.</Typography>}
              </Section>

              <Section id="news" title="NEWS"
                badge={news ? <>
                  <Chip size="small" label={`${news.length} in 30 days`}
                    sx={{ height: 19, fontSize: 10, color: tokens.tealHi,
                      border: `1px solid ${tokens.tealHi}`, bgcolor: '#F0F8F6' }} />
                  {(() => {
                    // The desk cares first when it's bad — surface the worst
                    // verdict on the collapsed row, PULSE's colors exactly.
                    const live = (p.stats.deals_in_flight + p.stats.deals_done) > 0;
                    const sevs = news.map((a) =>
                      a.severity || classify(a.headline, live)[0]);
                    const worst: Severity | null = sevs.includes('RED') ? 'RED'
                      : sevs.includes('AMBER') ? 'AMBER' : null;
                    const mix = (['GREEN', 'BLUE', 'AMBER', 'RED'] as Severity[])
                      .map((s) => ({ value: sevs.filter((x) => x === s).length,
                        color: SEV_BG[s] }));
                    return <>
                      {worst ? <SevPill s={worst} /> : null}
                      {news.length > 0 && <Box sx={{ width: 64, flexShrink: 0 }}>
                        <StackBar segs={mix} height={5} /></Box>}
                    </>;
                  })()}
                </> : undefined}
                open={!!openSections.news} onToggle={toggle}
                summary={news === null
                  ? (newsErr ? 'radar unavailable' : 'searching…')
                  : (news[0]?.headline || 'no mentions in the last 30 days')}>
                {(() => {
                  const live = (p.stats.deals_in_flight + p.stats.deals_done) > 0;
                  const graded = (news || []).map((a) => ({ a,
                    sev: (a.severity || classify(a.headline, live)[0]) as Severity }));
                  const counts = (['GREEN', 'AMBER', 'RED', 'BLUE'] as Severity[])
                    .map((s) => [s, graded.filter((g) => g.sev === s).length] as const)
                    .filter(([, n]) => n > 0);
                  const shown = newsSev
                    ? graded.filter((g) => g.sev === newsSev) : graded;
                  return <>
                    {/* The radar's stat-filter idiom: counts you can click. */}
                    {counts.length > 1 && (
                      <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap' }}>
                        {counts.map(([s, n]) => (
                          <Chip key={s} size="small" clickable
                            label={`${SEV_LABEL[s].toLowerCase()} · ${n}`}
                            onClick={() => setNewsSev(newsSev === s ? null : s)}
                            sx={{ height: 20, fontSize: 10.2, fontWeight: 700,
                              color: newsSev === s ? '#fff' : SEV_BG[s],
                              bgcolor: newsSev === s ? SEV_BG[s] : 'transparent',
                              border: `1.5px solid ${SEV_BG[s]}` }} />))}
                      </Box>)}
                    <Box sx={{ maxHeight: 280, overflowY: 'auto', display: 'flex',
                      flexDirection: 'column', gap: 1, pr: 0.5 }}>
                      {shown.map(({ a, sev }) => (
                        <Box key={a.url} sx={{ borderLeft: `3px solid ${SEV_BG[sev]}`,
                          pl: 1, flexShrink: 0 }}>
                          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8 }}>
                            <SevPill s={sev} />
                            <Typography component="a" href={a.url} target="_blank"
                              rel="noreferrer"
                              sx={{ fontSize: 12.6, color: '#17252B',
                                textDecoration: 'none',
                                '&:hover': { color: tokens.tealHi } }}>
                              {a.headline}</Typography>
                          </Box>
                          <Typography sx={{ fontSize: 10.8, color: tokens.muted }}>
                            {[a.source, a.when].filter(Boolean).join(' · ')}</Typography>
                        </Box>))}
                    </Box>
                  </>;
                })()}
                {news !== null && !news.length && !newsErr && (
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
                    No mentions in the last 30 days.</Typography>)}
                {newsErr && <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
                  News radar did not answer: {newsErr}</Typography>}
              </Section>

              {/* The prospect row is the story only until PRISM has one of its
                  own. Once a lead or deal exists, its stored status ("uncontacted"
                  when the lead was added directly) is stale — the section stays
                  only for what it still adds: sub-sectors or the desk's remark. */}
              {p.prospect && (!inPrism || p.prospect.remarks
                || (p.prospect.sub_sectors || []).length > 0) && (
                <Section id="prospect" title="PROSPECT UNIVERSE"
                  open={!!openSections.prospect} onToggle={toggle}
                  summary={[p.prospect.prospect_no, (p.prospect.verticals || []).join(', '),
                    inPrism ? null : p.prospect.status.replace(/_/g, ' ')]
                    .filter(Boolean).join(' · ')}>
                  <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap' }}>
                    {(p.prospect.verticals || []).map((v) => <Chip key={v} size="small"
                      color="primary" variant="outlined" label={v}
                      sx={{ height: 20, fontSize: 10.6 }} />)}
                    {(p.prospect.sub_sectors || []).map((v) => <Chip key={v} size="small"
                      variant="outlined" label={v} sx={{ height: 20, fontSize: 10.6 }} />)}
                  </Box>
                  {p.prospect.remarks && <Typography sx={{ fontSize: 12,
                    color: '#17252B' }}>{p.prospect.remarks}</Typography>}
                </Section>)}
            </Box>

            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.2, minWidth: 0 }}>
              <Section id="financials" title="FINANCIALS"
                badge={finState === 'live'
                  ? <Chip size="small" label="Tracxn · live" sx={{ height: 19,
                      fontSize: 10, fontWeight: 700, color: '#fff',
                      bgcolor: CHART_TEAL }} />
                  : finState === 'nocin'
                  ? <Chip size="small" label="CIN needed" sx={{ height: 19,
                      fontSize: 10, fontWeight: 700, color: '#B45309',
                      border: '1px solid #B45309', bgcolor: '#FDF3E7' }} />
                  : finState === 'unconfigured'
                  ? <Chip size="small" variant="outlined" label="feed not connected"
                      sx={{ height: 19, fontSize: 10, color: tokens.muted,
                        borderStyle: 'dashed' }} />
                  : finState === 'nofilings'
                  ? <Chip size="small" variant="outlined" label="no filings"
                      sx={{ height: 19, fontSize: 10, color: tokens.muted }} />
                  : hasFin
                  ? <Chip size="small" label="from prospect universe" sx={{ height: 19,
                      fontSize: 10, color: tokens.tealHi,
                      border: `1px solid ${tokens.tealHi}`, bgcolor: '#F0F8F6' }} />
                  : undefined}
                open={!!openSections.financials} onToggle={toggle}
                summary={(() => {
                  const rev = trx?.series?.revenue?.points;
                  if (rev?.length) {
                    const l = rev[rev.length - 1];
                    return `Revenue ${finNum(l.value)}${trx?.series?.revenue?.unit
                      ? ` ${trx.series.revenue.unit}` : ''} (FY${l.year})`;
                  }
                  if (finState === 'nocin') {
                    return 'no CIN on record — add it on the lead or client master';
                  }
                  if (finState === 'unconfigured') return 'market feed not connected on this server';
                  if (finState === 'nofilings') return `Tracxn has no filings for CIN ${p.anchor.cin}`;
                  return hasFin ? `Revenue ${fmtCr(fin!.revenue_cr)} · prospect universe`
                    : 'filings, board & shareholding — expand to fetch';
                })()}>
                {finState === 'busy' && (
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <CircularProgress size={15} />
                    <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
                      Asking the market feed for CIN {p.anchor.cin}…</Typography>
                  </Box>)}
                {/* Every empty state says WHY, and what would fill it. */}
                {finState === 'nocin' && (
                  <Typography sx={{ fontSize: 11.8, color: '#17252B', lineHeight: 1.55 }}>
                    Filings, board and shareholding come from Tracxn, looked up by
                    the company’s <b>CIN</b>. There is no CIN on record for
                    {' '}{p.anchor.name} yet, so nothing was requested. Add it under
                    {' '}<b>Company details</b> on the lead, or <b>Profile</b> on the
                    client master (Masters ▸ Clients), and this card fills itself.
                  </Typography>)}
                {finState === 'unconfigured' && (
                  <Typography sx={{ fontSize: 11.8, color: '#17252B', lineHeight: 1.55 }}>
                    The Tracxn market feed is not connected on this server
                    (TRACXN_ACCESS_TOKEN is not set), so nothing was requested.
                    CIN on record: {p.anchor.cin}.
                  </Typography>)}
                {finState === 'nofilings' && (
                  <Typography sx={{ fontSize: 11.8, color: '#17252B', lineHeight: 1.55 }}>
                    Tracxn has no legal entity for CIN <b>{p.anchor.cin}</b>
                    {trx?.note ? ` — ${trx.note}` : ''}. Check the CIN on record;
                    a corrected one is looked up again on the next open.
                  </Typography>)}
                {finState === 'error' && (
                  <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
                    {trxErr}</Typography>)}

                {trx?.resolved && (() => {
                  const s = trx.series || {};
                  const main = FIN_SERIES.filter(([k]) => s[k]?.points.length);
                  // Context series live on wildly different scales (heads, ₹),
                  // so each keeps its own chart; first two that have data.
                  const extra = FIN_CONTEXT
                    .filter(([k]) => s[k]?.points.length).slice(0, 2);
                  return <>
                    {main.length > 0 && (
                      <Box sx={{ display: 'grid', gap: 1.2,
                        gridTemplateColumns: `repeat(${Math.min(main.length, 3)}, 1fr)` }}>
                        {main.map(([k, lab, col]) => (
                          <MiniBars key={k} label={lab} series={s[k]} color={col} />))}
                      </Box>)}
                    {extra.length > 0 && (
                      <Box sx={{ display: 'grid', gap: 1.2,
                        gridTemplateColumns: 'repeat(2, 1fr)' }}>
                        {extra.map(([k, lab]) => (
                          <MiniBars key={k} label={lab} series={s[k]}
                            color="#5B6B74" />))}
                      </Box>)}
                    {!main.length && !extra.length && (
                      <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
                        Tracxn knows the company but has no filed series yet.
                      </Typography>)}
                    {(trx.board || []).length > 0 && <>
                      <Typography sx={{ fontSize: 10, fontWeight: 800,
                        letterSpacing: '.06em', color: tokens.muted, mt: 0.4 }}>
                        BOARD</Typography>
                      {(trx.board || []).slice(0, 6).map((m) => (
                        <Typography key={m.name} sx={{ fontSize: 11.8,
                          color: '#17252B' }}>
                          <b>{m.name}</b>{m.designation ? ` · ${m.designation}` : ''}
                          {m.since ? ` · since ${String(m.since).slice(0, 4)}` : ''}
                        </Typography>))}
                    </>}
                    {(trx.shareholders || []).length > 0 && <>
                      <Typography sx={{ fontSize: 10, fontWeight: 800,
                        letterSpacing: '.06em', color: tokens.muted, mt: 0.4 }}>
                        SHAREHOLDING</Typography>
                      {(trx.shareholders || []).slice(0, 6).map((h) => (
                        <Box key={h.name} sx={{ display: 'flex',
                          alignItems: 'center', gap: 1 }}>
                          <Typography sx={{ fontSize: 11.6, color: '#17252B',
                            width: '42%', whiteSpace: 'nowrap', overflow: 'hidden',
                            textOverflow: 'ellipsis' }}>{h.name}</Typography>
                          <Box sx={{ flex: 1, height: 6, bgcolor: '#E7ECEF',
                            borderRadius: 3 }}>
                            <Box sx={{ width: `${Math.min(100, Math.max(0, h.pct))}%`,
                              height: 6, bgcolor: CHART_TEAL, borderRadius: 3 }} />
                          </Box>
                          <Typography sx={{ fontSize: 11, fontWeight: 700,
                            color: '#17252B', width: 44, textAlign: 'right' }}>
                            {h.pct.toFixed(1)}%</Typography>
                        </Box>))}
                    </>}
                    <Box className="no-print" sx={{ display: 'flex',
                      alignItems: 'center', gap: 1 }}>
                      <Typography sx={{ fontSize: 10, color: tokens.muted }}>
                        as reported · Tracxn
                        {trx.legal_entity?.name ? ` · ${trx.legal_entity.name}` : ''}
                        {trx.fetched_at ? ` · fetched ${fmtDay(trx.fetched_at)}` : ''}
                      </Typography>
                      <Box sx={{ flex: 1 }} />
                      <Tooltip title="Re-fetches every series from Tracxn (spends API calls); otherwise answers come from the register's cache.">
                        <Box component="span">
                          <Button size="small" variant="text" onClick={refreshTrx}
                            disabled={trxBusy}
                            sx={{ fontSize: 10.5, py: 0, minWidth: 0 }}>
                            {trxBusy ? <CircularProgress size={12} /> : 'Refresh'}
                          </Button>
                        </Box>
                      </Tooltip>
                    </Box>
                  </>;
                })()}

                {finState !== 'live' && hasFin && (
                  <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.6,
                    mt: finState === 'idle' || finState === 'busy' ? 0 : 0.6 }}>
                    <Typography sx={{ fontSize: 10, fontWeight: 800,
                      letterSpacing: '.06em', color: tokens.muted }}>
                      AS CAPTURED IN THE PROSPECT UNIVERSE</Typography>
                    <FallbackBars rows={[['Revenue', fin!.revenue_cr, FIN_SERIES[0][2]],
                      ['EBITDA', fin!.ebitda_cr, FIN_SERIES[1][2]],
                      ['PAT', fin!.net_profit_cr, FIN_SERIES[2][2]]]} />
                    <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>
                      single year, as captured{fin!.founded_year
                        ? ` · founded ${fin!.founded_year}` : ''}</Typography>
                  </Box>)}
                {finState === 'idle' && !hasFin && (
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
                    Fetching filings for CIN {p.anchor.cin}…</Typography>)}
              </Section>

              <Section id="documents" title="DOCUMENTS"
                badge={<Box sx={{ display: 'flex', alignItems: 'center', gap: 0.6,
                  minWidth: 0 }}>
                  <Typography sx={{ fontSize: 10.5, color: tokens.muted,
                    whiteSpace: 'nowrap' }}>
                    {p.stats.documents} on the Data Register
                    {idxStatus ? ` · ${idxStatus}` : ''}</Typography>
                  {idxBusy && <CircularProgress size={10} />}
                </Box>}
                open={!!openSections.documents} onToggle={toggle}
                summary={p.documents.slice(0, 2).map((d) => d.title).join(' · ')
                  || 'none uploaded'}>
                {/* Every file, in a box that scrolls rather than a "+19 more". */}
                <Box sx={{ display: 'flex', gap: 0.7, flexWrap: 'wrap', maxHeight: 96,
                  overflowY: 'auto', pr: 0.5 }}>
                  {p.documents.map((d) => (
                    <Chip key={d.title + (d.uploaded_at || '')} size="small"
                      label={d.filename || d.title}
                      title={[d.section, d.doc_type, d.uploaded_by].filter(Boolean).join(' · ')}
                      sx={{ height: 22, fontSize: 11, bgcolor: '#EEF4F3', maxWidth: 260,
                        color: tokens.tealHi, fontWeight: 600 }} />))}
                  {!p.documents.length && <Typography sx={{ fontSize: 11.6,
                    color: tokens.muted }}>Nothing on the Data Register yet.</Typography>}
                </Box>
                <Box className="no-print" sx={{ display: 'flex', flexDirection: 'column',
                  gap: 0.7 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography sx={{ fontSize: 10.5, fontWeight: 700,
                      color: tokens.muted }}>ASK THE DOCUMENTS</Typography>
                    <Box sx={{ flex: 1 }} />
                    {idxErr && p.anchor.entity_id && (
                      <Button size="small" variant="text" onClick={runIndex}
                        disabled={idxBusy} sx={{ fontSize: 11, py: 0, minWidth: 0 }}>
                        retry indexing</Button>)}
                  </Box>
                  {/* Only what needs saying: a skip and its reason, or a failure.
                      A clean pass is the badge's "N ready for Q&A". */}
                  {idxOut && idxOut.skipped.length > 0 && (
                    <Alert severity="info" sx={{ fontSize: 11.2, py: 0 }}>
                      {idxOut.skipped.length} file(s) not indexed: {idxOut.skipped
                        .slice(0, 3).map((x) => `${x.file} — ${x.reason}`).join('; ')}
                      {idxOut.skipped.length > 3 ? '; …' : ''}
                    </Alert>)}
                  {idxOut?.note && (idxOut.fresh ?? 0) > 0 && (
                    <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>
                      {idxOut.note}</Typography>)}
                  {idxErr && <Typography sx={{ fontSize: 11.2, color: tokens.muted }}>
                    {idxErr}</Typography>}
                  <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
                    {ASK_PRESETS.map(([label, q]) => (
                      <Chip key={label} size="small" clickable label={label}
                        disabled={askBusy || idxBusy} onClick={() => runAsk(q)}
                        sx={{ height: 22, fontSize: 10.8, fontWeight: 600,
                          color: tokens.tealHi, bgcolor: '#F0F8F6',
                          border: `1px solid ${tokens.tealHi}` }} />))}
                  </Box>
                  <Box sx={{ display: 'flex', gap: 0.8 }}>
                    <TextField size="small" fullWidth value={ask}
                      placeholder="…or ask anything about these files"
                      onChange={(e) => setAsk(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') runAsk(); }} />
                    <Button size="small" variant="outlined" onClick={() => runAsk()}
                      disabled={askBusy || idxBusy || !ask.trim()}>
                      {askBusy ? <CircularProgress size={15} /> : 'Ask'}</Button>
                  </Box>
                  {askOut?.noDocuments && (
                    <Alert severity="info" sx={{ fontSize: 11.6, py: 0 }}>
                      {p.stats.documents > 0
                        ? 'None of this company’s files could be indexed for Q&A — '
                          + 'readable kinds are PDF, Excel, images, and zips of those.'
                        : 'No documents on the Data Register yet — upload some, and '
                          + 'they are indexed for Q&A the next time this opens.'}</Alert>)}
                  {askOut?.answer && (
                    <Box sx={{ bgcolor: '#F6FAF9', border: '1px solid #D6E7E3',
                      borderRadius: '10px', p: '9px 11px' }}>
                      <Typography sx={{ fontSize: 12.2, color: '#17252B',
                        whiteSpace: 'pre-wrap' }}>{askOut.answer.slice(0, 900)}</Typography>
                      {askOut.citations.length > 0 && (
                        <Typography sx={{ fontSize: 10.5, color: tokens.tealHi,
                          fontWeight: 600, mt: 0.5 }}>
                          cited · {askOut.citations.map((c) =>
                            [c.doc, c.where].filter(Boolean).join(' ')).join(' · ')}
                        </Typography>)}
                      {askOut.mode === 'extractive' && (
                        <Typography sx={{ fontSize: 10, color: tokens.muted,
                          mt: 0.4 }}>
                          Matching passages shown — written summaries switch on
                          with the answer model.</Typography>)}
                    </Box>)}
                  {askErr && <Typography sx={{ fontSize: 11.2, color: tokens.muted }}>
                    Document AI did not answer: {askErr}</Typography>}
                </Box>
              </Section>

              <Section id="risk" title="RISK GRADE"
                badge={grade ? <Chip size="small" label={`${grade.label} · ${grade.score}/100`}
                  sx={{ height: 19, fontSize: 10, fontWeight: 800, color: '#fff',
                    bgcolor: BAND_COLOR[grade.rating] }} /> : undefined}
                open={!!openSections.risk} onToggle={toggle}
                summary={grade ? grade.verdict
                  : 'AI grade from the ATLAS client rubric — not computed yet'}>
                {grade ? <RiskGradeDetails grade={grade}
                  onRegrade={gradeBusy ? undefined : () => runGrade(true)} /> : (
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2 }}>
                    <Typography sx={{ fontSize: 11.6, color: tokens.muted, flex: 1 }}>
                      Grades this client RED / AMBER / GREEN from its Register footprint,
                      indexed documents and 30-day news, using the ATLAS client rubric.
                      {gradeErr && <span style={{ color: tokens.bad }}> {gradeErr}</span>}
                    </Typography>
                    <Button size="small" variant="outlined" disabled={gradeBusy || (news === null && !newsErr)}
                      onClick={() => runGrade(false)}>
                      {gradeBusy ? <CircularProgress size={15} /> : 'Grade risk'}</Button>
                  </Box>)}
              </Section>
            </Box>
          </Box>

          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, pt: 0.4 }}>
            <Typography sx={{ fontSize: 10.8, color: tokens.muted }}>
              Sources: Register · field notes · news radar · documents — generated {genStamp}
              {p.restricted.length
                ? ` · not visible to your role: ${p.restricted.join(', ')}` : ''}
            </Typography>
          </Box>
        </>}
      </Box>
    </Dialog>
  );
}
