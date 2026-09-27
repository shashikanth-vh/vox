import { useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, IconButton, TextField,
  Tooltip, Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import DownloadIcon from '@mui/icons-material/Download';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import ChevronRightIcon from '@mui/icons-material/ChevronRight';
import TrackChangesIcon from '@mui/icons-material/TrackChanges';
import { tokens } from '../../theme';
import { apiErr } from '../../api/http';
import { panoramaService, type DocAskResult, type IndexResult, type Panorama }
  from '../../services/panoramaService';
import { fetchTerm, type Article } from '../../services/newsService';

/**
 * Company 360 — one company's whole story, in one place (the approved mockup).
 *
 * Summary first, expand on need: five stat tiles and the BRIEF read everything;
 * each section below is a one-line row that opens on click. The register's
 * panorama endpoint supplies every PRISM section RBAC'd server-side; news comes
 * from PULSE (the radar's own search); the ask-box queries DocRAG restricted to
 * this company's indexed files. Financials and risk grade render as wire-ready
 * cards until their feeds connect. Download = the browser's print-to-PDF over a
 * print stylesheet — the footer stamps sources and generation time.
 */

const CHART_TEAL = '#0D9488';

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

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Box sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: '11px',
      p: '10px 13px', display: 'flex', flexDirection: 'column', gap: 0.3, minWidth: 0 }}>
      <Typography sx={{ fontSize: 10, fontWeight: 700, letterSpacing: '.06em',
        color: tokens.muted }}>{label}</Typography>
      <Typography sx={{ fontSize: 19, fontWeight: 800, color: '#17252B',
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{value}</Typography>
      {sub && <Typography sx={{ fontSize: 10.8, color: tokens.muted, whiteSpace: 'nowrap',
        overflow: 'hidden', textOverflow: 'ellipsis' }}>{sub}</Typography>}
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
        gap: 1 }}>{children}</Box>}
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
  const [ask, setAsk] = useState('');
  const [askBusy, setAskBusy] = useState(false);
  const [askOut, setAskOut] = useState<DocAskResult | null>(null);
  const [askErr, setAskErr] = useState('');
  const [idxBusy, setIdxBusy] = useState(false);
  const [idxOut, setIdxOut] = useState<IndexResult | null>(null);
  const [idxErr, setIdxErr] = useState('');

  useEffect(() => {
    if (!open) return;
    setP(null); setErr(''); setNews(null); setNewsErr('');
    setAsk(''); setAskOut(null); setAskErr('');
    setIdxBusy(false); setIdxOut(null); setIdxErr('');
    setOpenSections({ engagements: true });
    let alive = true;
    panoramaService.get({ entityId, company })
      .then((r) => { if (!alive) return; setP(r);
        // Date-bounded so the "30 days" tile means 30 days, not "whatever came back".
        const from = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);
        fetchTerm(r.anchor.name, from)
          .then((arts) => { if (alive) setNews(arts.slice(0, 6)); })
          .catch((e) => { if (alive) setNewsErr(String(e?.message || e)); });
      })
      .catch((e) => { if (alive) setErr(apiErr(e, 'load the company 360')); });
    return () => { alive = false; };
  }, [open, entityId, company]);

  const toggle = (id: string) =>
    setOpenSections((s) => ({ ...s, [id]: !s[id] }));

  const runAsk = async (preset?: string) => {
    const q = (preset ?? ask).trim();
    if (!p || !q) return;
    if (preset) setAsk(preset);
    setAskBusy(true); setAskErr(''); setAskOut(null);
    try { setAskOut(await panoramaService.askDocuments(p.anchor.name, q)); }
    catch (e: any) { setAskErr(String(e?.message || e)); }
    finally { setAskBusy(false); }
  };

  const runIndex = async () => {
    if (!p?.anchor.entity_id) return;
    setIdxBusy(true); setIdxErr(''); setIdxOut(null);
    try {
      const r = await panoramaService.indexDocuments(p.anchor.entity_id, p.anchor.name);
      setIdxOut(r); setAskOut(null);
    } catch (e: any) { setIdxErr(apiErr(e, 'index the documents')); }
    finally { setIdxBusy(false); }
  };

  const fin = p?.prospect;
  const hasFin = !!fin && (fin.revenue_cr != null || fin.ebitda_cr != null
    || fin.net_profit_cr != null);
  const genStamp = useMemo(() => p
    ? new Date(p.generated_at).toLocaleString('en-IN',
      { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
    : '', [p]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth
      PaperProps={{ className: 'c360-print', sx: { bgcolor: '#F4F6F7',
        borderRadius: '14px', maxHeight: '94vh' } }}>
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
          <Chip size="small" variant="outlined" label="Risk grade — soon"
            sx={{ height: 24, fontSize: 11, color: tokens.muted, borderStyle: 'dashed' }} />
          <Button size="small" variant="contained" startIcon={<DownloadIcon />}
            onClick={() => window.print()}>Download</Button>
          <IconButton size="small" onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
        </Box>
      </Box>

      <Box sx={{ p: '14px 20px 16px', overflowY: 'auto', display: 'flex',
        flexDirection: 'column', gap: 1.4 }}>
        {err && <Alert severity="warning" sx={{ fontSize: 12.4 }}>{err}</Alert>}
        {!p && !err && (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
            <CircularProgress size={26} /></Box>)}

        {p && <>
          <Box sx={{ display: 'grid', gap: 1.2,
            gridTemplateColumns: { xs: 'repeat(2, 1fr)', sm: 'repeat(5, 1fr)' } }}>
            <Tile label="OPEN LEADS" value={String(p.stats.open_leads)}
              sub={p.stats.leads_converted
                ? `${p.stats.leads_converted} became deal(s)` : undefined} />
            <Tile label="LIVE DEALS" value={String(p.stats.live_deals)}
              sub={p.lending[0]?.stage || p.syndication[0]?.status || undefined} />
            <Tile label="EXPOSURE ASK" value={p.stats.exposure_ask_cr != null
              ? fmtCr(p.stats.exposure_ask_cr) : '—'} sub="across live lines" />
            <Tile label="LAST TOUCH" value={fmtDay(p.stats.last_touch)}
              sub={p.interactions[0]?.by || undefined} />
            <Tile label="NEWS · 30 DAYS" value={news ? String(news.length) : '…'}
              sub="via PULSE radar" />
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
            <Typography sx={{ fontSize: 13, color: '#17252B', lineHeight: 1.6 }}>
              {p.brief}</Typography>
          </Box>

          <Box sx={{ display: 'grid', gap: 1.4, alignItems: 'start',
            gridTemplateColumns: { xs: '1fr', md: '1fr 1fr' } }}>
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.2 }}>
              <Section id="engagements" title="ENGAGEMENTS"
                open={!!openSections.engagements} onToggle={toggle}
                summary={`${p.stats.open_leads} open lead(s) · ${p.stats.live_deals} live deal(s)`}>
                {p.leads.filter((l) => !l.converted).map((l) => (
                  <Box key={l.lead_no || Math.random()} sx={{ display: 'flex', gap: 1.1 }}>
                    <Box sx={{ width: 8, height: 8, mt: '5px', borderRadius: 99,
                      bgcolor: tokens.tealHi, flexShrink: 0 }} />
                    <Box>
                      <Typography sx={{ fontSize: 12.8, color: '#17252B' }}>
                        <b>{l.lead_no}</b> — Lead, {l.temperature || '—'} · {l.rm || 'unassigned'}
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
                {p.interactions.map((i, n) => (
                  <Box key={n}>
                    <Typography sx={{ fontSize: 12.6, color: '#17252B' }}>
                      {i.type} · {fmtDay(i.occurred_at)}
                      {i.by ? ` · ${i.by}` : ''}</Typography>
                    {(i.summary || i.notes) && (
                      <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
                        {(i.summary || i.notes || '').slice(0, 160)}</Typography>)}
                  </Box>))}
                {!p.interactions.length && <Typography sx={{ fontSize: 11.6,
                  color: tokens.muted }}>Nothing logged yet.</Typography>}
              </Section>

              <Section id="news" title="NEWS"
                badge={news ? <Chip size="small" label={`${news.length} in 30 days`}
                  sx={{ height: 19, fontSize: 10, color: tokens.tealHi,
                    border: `1px solid ${tokens.tealHi}`, bgcolor: '#F0F8F6' }} /> : undefined}
                open={!!openSections.news} onToggle={toggle}
                summary={news === null
                  ? (newsErr ? 'radar unavailable' : 'searching…')
                  : (news[0]?.headline || 'no mentions in the last 30 days')}>
                {(news || []).map((a) => (
                  <Box key={a.url}>
                    <Typography component="a" href={a.url} target="_blank" rel="noreferrer"
                      sx={{ fontSize: 12.6, color: '#17252B', textDecoration: 'none',
                        '&:hover': { color: tokens.tealHi } }}>{a.headline}</Typography>
                    <Typography sx={{ fontSize: 10.8, color: tokens.muted }}>
                      {[a.source, a.when].filter(Boolean).join(' · ')}</Typography>
                  </Box>))}
                {news !== null && !news.length && !newsErr && (
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
                    No mentions in the last 30 days.</Typography>)}
                {newsErr && <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
                  News radar did not answer: {newsErr}</Typography>}
              </Section>

              {p.prospect && (
                <Section id="prospect" title="PROSPECT UNIVERSE"
                  open={!!openSections.prospect} onToggle={toggle}
                  summary={`${p.prospect.prospect_no || ''} · ${(p.prospect.verticals || [])
                    .join(', ')} · ${p.prospect.status}`}>
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

            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.2 }}>
              <Section id="financials" title="FINANCIALS"
                badge={hasFin
                  ? <Chip size="small" label="from prospect universe" sx={{ height: 19,
                      fontSize: 10, color: tokens.tealHi,
                      border: `1px solid ${tokens.tealHi}`, bgcolor: '#F0F8F6' }} />
                  : <Chip size="small" label="market feed soon" sx={{ height: 19,
                      fontSize: 10, color: '#B45309', border: '1px solid #B45309',
                      bgcolor: '#FDF3E7' }} />}
                open={!!openSections.financials} onToggle={toggle}
                summary={hasFin ? `Revenue ${fmtCr(fin!.revenue_cr)}`
                  : 'lights up when the feed connects'}>
                {hasFin ? (
                  <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)',
                    gap: 1 }}>
                    {([['Revenue', fin!.revenue_cr], ['EBITDA', fin!.ebitda_cr],
                      ['PAT', fin!.net_profit_cr]] as const).map(([lab, v]) => (
                      <Box key={lab}>
                        <Typography sx={{ fontSize: 10.5, fontWeight: 700,
                          color: tokens.muted }}>{lab}</Typography>
                        <Typography sx={{ fontSize: 15, fontWeight: 800,
                          color: '#17252B' }}>{fmtCr(v)}</Typography>
                      </Box>))}
                    {fin!.founded_year && <Typography sx={{ gridColumn: '1 / -1',
                      fontSize: 11, color: tokens.muted }}>
                      Founded {fin!.founded_year} · multi-year trend arrives with the
                      market feed</Typography>}
                  </Box>
                ) : (
                  <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
                    Market data (multi-year revenue, EBITDA, funding) renders here when
                    the feed is connected; the key stays server-side.</Typography>)}
              </Section>

              <Section id="documents" title="DOCUMENTS"
                badge={<Typography sx={{ fontSize: 10.5, color: tokens.muted }}>
                  {p.stats.documents} on the Data Register</Typography>}
                open={!!openSections.documents} onToggle={toggle}
                summary={p.documents.slice(0, 2).map((d) => d.title).join(' · ')
                  || 'none uploaded'}>
                <Box sx={{ display: 'flex', gap: 0.7, flexWrap: 'wrap' }}>
                  {p.documents.slice(0, 6).map((d) => (
                    <Chip key={d.title + (d.uploaded_at || '')} size="small"
                      label={d.filename || d.title}
                      sx={{ height: 22, fontSize: 11, bgcolor: '#EEF4F3',
                        color: tokens.tealHi, fontWeight: 600 }} />))}
                  {p.documents.length > 6 && <Chip size="small"
                    label={`+${p.documents.length - 6} more`}
                    sx={{ height: 22, fontSize: 11 }} />}
                </Box>
                <Box className="no-print" sx={{ display: 'flex', flexDirection: 'column',
                  gap: 0.7 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography sx={{ fontSize: 10.5, fontWeight: 700,
                      color: tokens.muted }}>ASK THE INDEXED DOCUMENTS</Typography>
                    <Box sx={{ flex: 1 }} />
                    {p.stats.documents > 0 && p.anchor.entity_id && (
                      <Button size="small" variant="text" onClick={runIndex}
                        disabled={idxBusy} sx={{ fontSize: 11, py: 0 }}>
                        {idxBusy ? <CircularProgress size={13} />
                          : `Index ${p.stats.documents} register file(s) for Q&A`}
                      </Button>)}
                  </Box>
                  {idxOut && (
                    <Alert severity={idxOut.indexed.length ? 'success' : 'warning'}
                      sx={{ fontSize: 11.6, py: 0 }}>
                      Indexed {idxOut.indexed.length} of {idxOut.total_on_register}
                      {idxOut.indexed.some((x) => x.duplicate) ? ' (some already were)' : ''}
                      {idxOut.skipped.length
                        ? ` · skipped ${idxOut.skipped.length}: ${idxOut.skipped
                          .slice(0, 3).map((s) => s.reason).join('; ')}` : ''}
                      {idxOut.note ? ` — ${idxOut.note}` : ''}
                    </Alert>)}
                  {idxErr && <Typography sx={{ fontSize: 11.2, color: tokens.muted }}>
                    {idxErr}</Typography>}
                  <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
                    {ASK_PRESETS.map(([label, q]) => (
                      <Chip key={label} size="small" clickable label={label}
                        disabled={askBusy} onClick={() => runAsk(q)}
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
                      disabled={askBusy || !ask.trim()}>
                      {askBusy ? <CircularProgress size={15} /> : 'Ask'}</Button>
                  </Box>
                  {askOut?.noDocuments && (
                    <Alert severity="info" sx={{ fontSize: 11.6, py: 0 }}>
                      No files indexed for this company yet
                      {p.stats.documents > 0 && p.anchor.entity_id
                        ? ' — press “Index register file(s) for Q&A” above, then ask again.'
                        : ' — upload documents first, then ask again.'}</Alert>)}
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
                badge={<Chip size="small" label="coming soon" sx={{ height: 19,
                  fontSize: 10, color: tokens.muted, borderStyle: 'dashed' }}
                  variant="outlined" />}
                open={!!openSections.risk} onToggle={toggle}
                summary="internal grading joins here">
                <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
                  The internal risk grade renders here when the grading service
                  connects — same card, live value.</Typography>
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
