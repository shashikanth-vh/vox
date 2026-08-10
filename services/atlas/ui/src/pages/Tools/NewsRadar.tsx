import { useState, useRef, useMemo } from 'react';
import { Box, Button, Chip, Typography, Paper, TextField, Accordion, AccordionSummary, AccordionDetails, Alert, Link } from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import { useSearch } from '../../context/SearchContext';
import { CodeText } from '../../components/common/Pills';
import { db } from '../../api/atlasStore';
import { useAuth } from '../../auth/AuthContext';
import { tokens } from '../../theme';
import { newsService, news, watch, classify, fetchTerm, lastFailure, contextReason, isLiveBorrower, tradingName, SEV_LABEL, type Severity, type NewsItem } from '../../services/newsService';
import { EmailNewsDialog, EmailAllFirmsDialog } from './EmailDialogs';
import SchedulesDialog from './SchedulesDialog';

export interface AdhocState {
  term: string; running: boolean; items: { headline: string; url: string; source: string; when: string; severity: Severity }[];
  err: string; dfrom: string; dto: string;
}
const BLANK_ADHOC: AdhocState = { term: '', running: false, items: [], err: '', dfrom: '', dto: '' };

const SEV_BG: Record<Severity, string> = {
  RED: tokens.bad, AMBER: tokens.warn, GREEN: tokens.ok,
  // POLICY is its own colour, not a shade of warning: a tariff order is not a problem
  // with the borrower, it is a change in the ground they stand on.
  BLUE: '#1F6FA8',
};

function SevPill({ s }: { s: Severity }) {
  return (
    <Box component="span" sx={{ flex: '0 0 auto', fontSize: 10, fontWeight: 800, borderRadius: '99px',
      px: '9px', py: '2px', color: '#fff', bgcolor: SEV_BG[s] }}>{SEV_LABEL[s]}</Box>
  );
}

// One headline row — click anywhere opens the source article.
function NewsLine({ severity, headline, url, meta, onOpen, actions, flag }: {
  severity: Severity; headline: string; url?: string; meta: string;
  onOpen: () => void; actions?: React.ReactNode; flag?: string;
}) {
  return (
    <Box onClick={onOpen} title="Open the source article"
      sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, py: 0.8, px: 1, cursor: 'pointer',
        borderTop: `1px solid ${tokens.line}`, borderLeft: `3px solid ${SEV_BG[severity]}`,
        '&:hover': { bgcolor: '#F7FAFB' } }}>
      <SevPill s={severity} />
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography sx={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, wordBreak: 'break-word' }}>
          {url ? <Link href={url} target="_blank" rel="noopener" underline="hover"
            onClick={(e) => e.stopPropagation()} sx={{ color: tokens.navy }}>{headline} ↗</Link> : headline}
        </Typography>
        <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>{meta}</Typography>
        {/* The context flip, explained where it is read. Good-looking news on a firm we
            have lent to is a question, and the answer to "why is this flagged?" has to
            be one hover away or the flag is just noise. */}
        {flag && (
          <Typography title={flag} sx={{ fontSize: 11.3, fontWeight: 600, color: tokens.warn, mt: 0.2 }}>
            ⚠ good-news check — live borrower: {flag}
          </Typography>
        )}
      </Box>
      {actions && <Box onClick={(e) => e.stopPropagation()} sx={{ display: 'flex', gap: 0.5 }}>{actions}</Box>}
    </Box>
  );
}

export default function NewsRadar() {
  const { user } = useAuth();
  const { search } = useSearch();
  const [, force] = useState(0);
  const bump = () => force((n) => n + 1);

  // 'CTX' is a filter, not a severity: the open review flags cut across colours.
  const [sev, setSev] = useState<Severity | 'CTX' | ''>('');
  const [scan, setScan] = useState({ running: false, done: 0, total: 0, found: 0, failTerms: 0, why: '' });
  const [adhoc, setAdhoc] = useState<AdhocState>(BLANK_ADHOC);
  const [qBox, setQBox] = useState('');
  const ctrl = useRef<AbortController | null>(null);
  const stopped = useRef(false);

  const [emailNews, setEmailNews] = useState(false);
  const [emailAll, setEmailAll] = useState(false);
  const [sched, setSched] = useState<false | 'all' | 'plain'>(false);

  const all = news();
  const reds = all.filter((n) => n.severity === 'RED').length;
  const ambs = all.filter((n) => n.severity === 'AMBER').length;
  const blues = all.filter((n) => n.severity === 'BLUE').length;
  // Open review flags: good-looking news on a live borrower that nobody has judged yet.
  const ctxs = all.filter((n) => n.category === 'context-review' && !n.verdict).length;
  const codes = Object.keys(db().clients || {});

  const q = search.trim().toLowerCase();
  const shown = all.filter((n) =>
    (!sev || (sev === 'CTX' ? (n.category === 'context-review' && !n.verdict) : n.severity === sev))
    && (!q || [n.headline, db().clients?.[n.code]?.name, n.code, n.term, n.source]
      .some((x) => String(x || '').toLowerCase().includes(q))));

  // Group by firm, most-adverse first.
  const groups = useMemo(() => {
    const by: Record<string, NewsItem[]> = {};
    shown.forEach((n) => { (by[n.code] = by[n.code] || []).push(n); });
    return Object.entries(by).sort((a, b) => {
      const ra = a[1].filter((n) => n.severity === 'RED').length;
      const rb = b[1].filter((n) => n.severity === 'RED').length;
      return rb - ra || b[1].length - a[1].length;
    });
  }, [shown]);

  /* ---------------- scans ---------------- */
  const scanAll = async () => {
    if (scan.running) return;
    setScan({ running: true, done: 0, total: codes.length, found: 0, failTerms: 0, why: '' });
    const r = await newsService.scanAll(user.full, (p) =>
      setScan((s) => ({ ...s, done: p.done, total: p.total, found: p.found })));
    // A sweep that files nothing across 400 firms is either a quiet news week or a
    // broken pipe, and the count alone cannot say which — so carry the last reason.
    setScan({ running: false, done: r.firms, total: r.firms, found: r.found,
              failTerms: r.failTerms, why: lastFailure() });
    bump();
  };

  const scanOne = async (code: string) => {
    await newsService.scanCompany(code, user.full);
    bump();
  };

  // The risk that never names the firm — state-level tariff / ALMM / open-access moves.
  const [policyBusy, setPolicyBusy] = useState('');
  const policyScan = async (code: string) => {
    setPolicyBusy(code);
    try {
      const r = await newsService.policyScan(code, user.full);
      if (!r.state) window.alert('Set this firm’s State first — policy risk is state-scoped.');
    } finally { setPolicyBusy(''); bump(); }
  };

  const addTerm = (code: string) => {
    const name = db().clients?.[code]?.name || code;
    const t = window.prompt(`Watch term for ${name} — owner / promoter / brand / plant name:`, '');
    if (!t || !t.trim()) return;
    newsService.addTerm(code, t, user.full); bump();
  };
  const delTerm = (code: string, i: number) => { newsService.delTerm(code, i); bump(); };

  const setVerdict = (id: string, v: string) => { newsService.verdict(id, v, user.full); bump(); };

  const clearAll = () => {
    const n = all.length;
    if (!n) return;
    if (!window.confirm(`Clear all stored firm news (${n} items)? This starts the radar fresh — scans and verdicts are removed.`)) return;
    newsService.clearAll(user.full); bump();
  };

  /* ---------------- ad-hoc search ---------------- */
  const searchStop = () => {
    if (!adhoc.running) return;
    stopped.current = true;
    try { ctrl.current?.abort(); } catch { /* already gone */ }
    setAdhoc((a) => ({ ...a, running: false }));
  };

  const searchGo = async () => {
    if (adhoc.running) { searchStop(); return; }   // TOGGLE: 2nd click stops
    const term = qBox.trim();
    if (!term) return;
    if (adhoc.dfrom && adhoc.dto && adhoc.dfrom > adhoc.dto) {
      setAdhoc((a) => ({ ...a, err: 'From date is after To date' })); return;
    }
    stopped.current = false;
    ctrl.current = typeof AbortController !== 'undefined' ? new AbortController() : null;
    setAdhoc((a) => ({ ...a, term, running: true, items: [], err: '' }));
    try {
      const arts = await fetchTerm(term, adhoc.dfrom, adhoc.dto, ctrl.current?.signal);
      if (stopped.current) return;                 // ignore a late result after Stop
      setAdhoc((a) => ({
        ...a, running: false,
        // PULSE already judged these; only a fallback source leaves it to us.
        items: arts.map((x) => ({ ...x, severity: x.severity || classify(x.headline)[0] })),
      }));
    } catch (e: any) {
      if (stopped.current || e?.name === 'AbortError') { setAdhoc((a) => ({ ...a, running: false })); return; }
      // Say WHAT failed. The old text guessed one cause — a file:// page — and read as
      // nonsense to a desk sitting on the gateway over HTTPS, while the server had
      // already explained itself (a permission it lacks, a service that is down, an
      // upstream that would not answer). Show that, and keep the file:// hint only for
      // the case it actually describes.
      const why = String(e?.message || '').trim();
      const local = location.protocol === 'file:';
      setAdhoc((a) => ({
        ...a, running: false,
        err: 'News search failed'
          + (why ? ' — ' + why : '.')
          + (local ? ' This page was opened as a local file; browsers block cross-site news calls from file://. Run it from the PRISM gateway.' : ''),
      }));
    }
  };

  const searchClear = () => { setAdhoc(BLANK_ADHOC); setQBox(''); };

  // v12's nrSearchWatch(): attach the searched term to a firm's watch list by code.
  const saveAsWatchTerm = () => {
    const pick = window.prompt(`Save “${adhoc.term}” as a watch term. Type the Group Code of the firm to attach it to:`, '');
    if (!pick) return;
    const code = pick.trim().toUpperCase();
    if (!db().clients?.[code]) { window.alert('No firm with code ' + code); return; }
    newsService.addTerm(code, adhoc.term, user.full); bump();
  };

  const kpi = (label: string, value: number | string, color?: string) => (
    <Paper variant="outlined" sx={{ px: 1.5, py: 0.7, borderColor: tokens.line, display: 'flex', alignItems: 'baseline', gap: 0.7 }}>
      <Typography component="b" sx={{ fontSize: 15, fontWeight: 700, color }}>{value}</Typography>
      <Typography sx={{ fontSize: 12, color: tokens.muted }}>{label}</Typography>
    </Paper>
  );

  const sevChip = (s: Severity | 'CTX' | '', label: string) => (
    <Chip label={label} clickable size="small" variant={sev === s ? 'filled' : 'outlined'}
      color={sev === s ? 'primary' : 'default'} onClick={() => setSev(s)} />
  );

  return (
    <>
      {/* ---- ad-hoc search ---- */}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mb: 0.8 }}>
        <TextField value={qBox} onChange={(e) => setQBox(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') searchGo(); }}
          placeholder="🔎 Search any name — company, owner, partner, plant…"
          sx={{ flex: '1 1 200px', minWidth: 0 }} />
        <Button variant="contained" onClick={searchGo}
          sx={adhoc.running ? { background: 'linear-gradient(135deg,#B3432B,#C85A3F)' } : undefined}>
          {adhoc.running ? '■ Stop' : 'Search news'}
        </Button>
      </Box>
      <Box sx={{ display: 'flex', gap: 1.2, alignItems: 'center', flexWrap: 'wrap', mb: 0.5 }}>
        <TextField type="date" label="From" InputLabelProps={{ shrink: true }} value={adhoc.dfrom}
          onChange={(e) => setAdhoc((a) => ({ ...a, dfrom: e.target.value }))} />
        <TextField type="date" label="To" InputLabelProps={{ shrink: true }} value={adhoc.dto}
          onChange={(e) => setAdhoc((a) => ({ ...a, dto: e.target.value }))} />
        <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>leave blank = last 3 months</Typography>
        <Box sx={{ flex: 1 }} />
        {(adhoc.term || adhoc.items.length) ? <Button onClick={searchClear}>↺ New search</Button> : null}
        <Button onClick={() => setSched('plain')}>⏰ Schedules</Button>
      </Box>
      <Typography sx={{ fontSize: 11.8, color: tokens.muted, mb: 1.5 }}>
        Searches live news across the web (Google News + GDELT) for anything you type — a company, a founder,
        or a key executive; optionally within a date range. Colour-coded 🟢 good · 🟡 bad · 🔴 ugly; click to
        open the source, ★ save the term to a firm’s watch list, 📧 email the results, or ⏰ schedule a digest.
      </Typography>

      {adhoc.term && (
        <Paper variant="outlined" sx={{ borderColor: tokens.line, p: 1.2, mb: 1.8 }}>
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap', mb: 0.5 }}>
            <Typography component="b" sx={{ fontSize: 13, fontWeight: 700 }}>Results for “{adhoc.term}”</Typography>
            <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
              {adhoc.running ? 'searching…' : `${adhoc.items.length} real articles`}
            </Typography>
            <Box sx={{ flex: 1 }} />
            {!adhoc.running && adhoc.items.length > 0 && <Button onClick={() => setEmailNews(true)}>📧 Email these</Button>}
            {!adhoc.running && adhoc.items.length > 0 && <Button onClick={saveAsWatchTerm}>★ Save as watch term</Button>}
            <Button onClick={searchClear}>✕ Clear</Button>
          </Box>
          {adhoc.err && <Alert severity="error" sx={{ py: 0, fontSize: 12, mb: 1 }}>{adhoc.err}</Alert>}
          {adhoc.running
            ? <Typography sx={{ p: 2, textAlign: 'center', color: tokens.muted, fontSize: 12.5 }}>Scraping live news for “{adhoc.term}”…</Typography>
            : adhoc.items.length
              ? adhoc.items.map((n, i) => (
                <NewsLine key={i} severity={n.severity} headline={n.headline} url={n.url}
                  meta={`${n.source || ''} · ${n.when || ''}`}
                  onOpen={() => newsService.open(n, user.full)} />
              ))
              : !adhoc.err && <Typography sx={{ p: 2, textAlign: 'center', color: tokens.muted, fontSize: 12.5 }}>No articles found for “{adhoc.term}”.</Typography>}
        </Paper>
      )}

      {/* ---- register KPIs + filters ---- */}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', mb: 1 }}>
        {kpi('items', all.length)}
        {kpi('ugly', reds, tokens.bad)}
        {kpi('bad', ambs, tokens.warn)}
        {blues > 0 && kpi('policy', blues, SEV_BG.BLUE)}
        {ctxs > 0 && kpi('to review', ctxs, tokens.warn)}
        {kpi('firms watched', codes.length)}
        <Box sx={{ flex: 1 }} />
        {sevChip('', 'All')}{sevChip('GREEN', '🟢 Good')}{sevChip('AMBER', '🟡 Bad')}{sevChip('RED', '🔴 Ugly')}
        {blues > 0 && sevChip('BLUE', '🔵 Policy')}
        {ctxs > 0 && (
          <Chip label={`⚠ To review (${ctxs})`} clickable size="small"
            variant={sev === 'CTX' ? 'filled' : 'outlined'} color={sev === 'CTX' ? 'warning' : 'default'}
            onClick={() => setSev(sev === 'CTX' ? '' : 'CTX')} />
        )}
        <Button onClick={() => setSched('all')}>⏰ Schedule scan</Button>
        {all.length > 0 && <Button onClick={() => setEmailAll(true)}>📧 Email firms’ news</Button>}
        {all.length > 0 && <Button color="error" onClick={clearAll}>🗑 Clear all news</Button>}
        <Button variant="contained" disabled={scan.running} onClick={scanAll}>
          {scan.running ? 'Scanning…' : '📡 Scan all firms'}
        </Button>
      </Box>

      {scan.running && (
        <Alert severity="info" sx={{ py: 0, fontSize: 12, mb: 1 }}>
          Scanning {scan.done} / {scan.total} firms · {scan.found} real items…
        </Alert>
      )}
      {!scan.running && scan.failTerms > 0 && (
        <Alert severity="error" sx={{ py: 0, fontSize: 12, mb: 1 }}>
          {scan.failTerms} source request{scan.failTerms > 1 ? 's' : ''} failed in the last scan
          {scan.why ? <> — <b>{scan.why}</b></> : '. Usually connectivity or rate limiting'}. Everything shown is a
          real scraped article; nothing is invented.
        </Alert>
      )}
      <Typography sx={{ fontSize: 11.8, color: tokens.muted, mb: 1.2 }}>
        One click sweeps every firm on the Register — company name plus the owner/promoter terms you add per firm.
        Every item is a REAL article scraped live; if the source is unreachable the scan reports the failure —
        nothing is ever invented. Colours read 🟢 good · 🟡 bad · 🔴 ugly · 🔵 policy. Watch terms should carry every
        promoter and director: the person is named before the company is. <b>⚡ Policy</b> sweeps state-level tariff
        and regulatory moves that never mention the firm, and good-sounding news on a firm we have lent to —
        fresh debt, an OTS, a share pledge, a stake sale, an auditor change — is flagged ⚠ for review rather than
        filed as good. Click any item to open the source.
      </Typography>

      {/* ---- per-firm groups ---- */}
      {!all.length ? (
        <Paper variant="outlined" sx={{ borderColor: tokens.line, p: 3, textAlign: 'center' }}>
          <Typography sx={{ color: tokens.muted, fontSize: 12.5 }}>
            No news on file — hit <b>📡 Scan all firms</b> (requires internet; only real articles are stored).
          </Typography>
        </Paper>
      ) : !groups.length ? (
        <Paper variant="outlined" sx={{ borderColor: tokens.line, p: 3, textAlign: 'center' }}>
          <Typography sx={{ color: tokens.muted, fontSize: 12.5 }}>Nothing matches the filter.</Typography>
        </Paper>
      ) : groups.map(([code, items]) => {
        const c = db().clients?.[code] || {};
        const terms = watch()[code] || [];
        return (
          <Accordion key={code} defaultExpanded disableGutters variant="outlined"
            sx={{ borderColor: tokens.line, mb: 0.8, '&:before': { display: 'none' } }}>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, width: '100%', pr: 1 }}>
                <Typography component="b" sx={{ fontSize: 13.2, fontWeight: 700 }}>{c.name || code}</Typography>
                <CodeText code={code} />
                <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                  {items.length} item{items.length > 1 ? 's' : ''}
                </Typography>
                <Box sx={{ flex: 1 }} />
                {isLiveBorrower(code) && (
                  <Chip label="live exposure" size="small" variant="outlined"
                    title="Our money is out to this firm, so good-looking news is flagged for review rather than filed as a win."
                    sx={{ height: 20, fontSize: 10.5 }} />
                )}
                <Button onClick={(e) => { e.stopPropagation(); scanOne(code); }}>↻ Scan</Button>
                {c.state && (
                  <Button disabled={policyBusy === code}
                    title={`Policy sweep: tariff / ALMM / payment-security / net-metering × ${c.state} — these never name the firm`}
                    onClick={(e) => { e.stopPropagation(); policyScan(code); }}>
                    {policyBusy === code ? 'sweeping…' : '⚡ Policy'}
                  </Button>
                )}
              </Box>
            </AccordionSummary>
            <AccordionDetails sx={{ p: 0 }}>
              <Box sx={{ px: 1, pb: 0.8, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.6 }}>
                {/* The term actually searched, not the register's legal name — otherwise
                    this line claims a search that never went out. */}
                <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                  watching: {c.name ? tradingName(c.name) : ''}
                </Typography>
                {terms.map((t, i) => (
                  <Typography key={i} sx={{ fontSize: 11.5, color: tokens.muted }}>
                    · {t}{' '}
                    <Box component="span" onClick={() => delTerm(code, i)}
                      sx={{ cursor: 'pointer', color: tokens.bad, fontWeight: 700 }}>×</Box>
                  </Typography>
                ))}
                <Button onClick={() => addTerm(code)}>＋ owner / term</Button>
              </Box>
              {items.map((n) => (
                <NewsLine key={n.id} severity={n.severity} headline={n.headline} url={n.url}
                  meta={`${n.source || ''} · ${n.when || ''} · matched “${n.term}”${n.verdict ? ` · verdict: ${n.verdict}` : ''}`}
                  flag={n.category === 'context-review'
                    ? (n.reason || contextReason(n.headline)) : undefined}
                  onOpen={() => newsService.open(n, user.full)}
                  actions={!n.verdict ? (
                    <>
                      <Button onClick={() => setVerdict(n.id, 'confirmed')}>✓ Real</Button>
                      <Button onClick={() => setVerdict(n.id, 'dismissed')}>✕ Noise</Button>
                    </>
                  ) : undefined} />
              ))}
            </AccordionDetails>
          </Accordion>
        );
      })}

      <EmailNewsDialog open={emailNews} onClose={() => setEmailNews(false)} adhoc={adhoc} />
      <EmailAllFirmsDialog open={emailAll} onClose={() => setEmailAll(false)} />
      <SchedulesDialog open={sched !== false} prefillAll={sched === 'all'} onClose={() => setSched(false)} />
    </>
  );
}
