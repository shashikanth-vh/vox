import { useState, useRef, useMemo } from 'react';
import { Box, Button, Chip, Typography, Paper, TextField, Accordion, AccordionSummary, AccordionDetails, Alert, Link, LinearProgress } from '@mui/material';
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

  const [sev, setSev] = useState<Severity | ''>('');
  /* LIVE EXPOSURE is not a colour.
     Severity says how bad a story is; exposure says whose money is in it — and the
     question that actually matters ("ugly news on a name we have lent to") needs BOTH
     at once. Putting exposure in the colour row would make it look mutually exclusive
     with Ugly, which is the opposite of how it is used, so it is its own toggle and it
     combines with whatever colour is selected. */
  const [liveOnly, setLiveOnly] = useState(false);
  const [scan, setScan] = useState({ running: false, done: 0, total: 0, found: 0, failTerms: 0, why: '', firms: 0 });
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
  const greens = all.filter((n) => n.severity === 'GREEN').length;
  const codes = Object.keys(db().clients || {});
  const lives = all.filter((n) => isLiveBorrower(n.code)).length;

  const q = search.trim().toLowerCase();
  const shown = all.filter((n) =>
    (!sev || n.severity === sev)
    && (!liveOnly || isLiveBorrower(n.code))
    && (!q || [n.headline, db().clients?.[n.code]?.name, n.code, n.term, n.source]
      .some((x) => String(x || '').toLowerCase().includes(q))));

  /* The colour tiles read the AD-HOC results as well as the book. They used to filter
     only the stored news, so picking Ugly while a search was on screen silently did
     nothing to it — the one list the desk was actually looking at. */
  const adhocShown = useMemo(
    () => (sev ? adhoc.items.filter((n) => n.severity === sev) : adhoc.items),
    [adhoc.items, sev]);

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
    // total 0 until the sweep knows its term list — an honest "reading the register…"
    // beats a fake denominator taken from the firm count.
    setScan({ running: true, done: 0, total: 0, found: 0, failTerms: 0, why: '', firms: codes.length });
    const progress = (p: { done: number; total: number; found: number }) =>
      setScan((s) => ({ ...s, done: p.done, total: p.total, found: p.found }));
    let r: { found: number; failTerms: number; firms: number };
    try {
      // ONE request; PULSE fans out next to the upstreams.
      r = await newsService.sweepAll(user.full, progress);
    } catch {
      // No PULSE in front of this build (a static host, or the gateway is down): fall
      // back to the old firm-by-firm walk rather than leaving the desk with nothing.
      r = await newsService.scanAll(user.full, progress);
    }
    // A sweep that files nothing across 400 firms is either a quiet news week or a
    // broken pipe, and the count alone cannot say which — so carry the last reason.
    setScan((s) => ({ ...s, running: false, done: s.total, found: r.found,
                      firms: r.firms, failTerms: r.failTerms, why: lastFailure() }));
    bump();
  };

  /* Is it us or the network? The one question the search itself cannot answer: no
     articles reads the same for a quiet company and a container with no egress. */
  const [probe, setProbe] = useState('');
  const runProbe = async () => {
    setProbe('checking the news sources…');
    try {
      const out: any = await newsService.diagnostics();
      setProbe([
        out.summary + ' — ' + (out.sources || []).map((s: any) =>
          `${s.name}: ${s.ok ? `ok (${s.ms}ms, ${s.count} items)` : s.error}`).join(' · '),
        // A digest that never arrives and a schedule that never fires look identical
        // from here, and both are usually one unset variable. Say which.
        out.email ? 'Email — ' + out.email.detail : '',
        out.scheduler ? 'Schedules — ' + out.scheduler.detail
          + (out.scheduler.next_run ? ` · next ${String(out.scheduler.next_run).slice(0, 16).replace('T', ' ')}` : '') : '',
      ].filter(Boolean).join('  |  '));
    } catch (e: any) {
      setProbe('could not reach PULSE itself — ' + String(e?.message || e));
    }
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

  /* THE COUNT IS THE FILTER.
     Two rows said the same thing: tiles reporting "25 ugly", and chips to filter to
     Ugly. The desk reads the number, then hunts for the matching chip — two controls
     for one intention, on a bar wide enough to wrap. The tile IS the button now: it
     carries the count in its own colour and selecting it filters to that colour, so
     what you read and what you press are the same object. `items` clears back to
     everything; `firms watched` is context rather than a colour, so it stays inert. */
  const stat = (label: string, value: number | string, opts?: {
    color?: string; filter?: Severity | ''; title?: string;
  }) => {
    const pickable = opts?.filter !== undefined;
    const on = pickable && sev === opts!.filter;
    return (
      <Paper variant="outlined" key={label} title={opts?.title}
        onClick={pickable ? () => setSev(opts!.filter!) : undefined}
        sx={{ display: 'flex', alignItems: 'baseline', gap: 0.7, userSelect: 'none',
          cursor: pickable ? 'pointer' : 'default',
          // A 2px border on selection eats 1px of padding, so the tile does not jump.
          borderWidth: on ? 2 : 1, px: on ? '11px' : '12px', py: on ? '4.6px' : '5.6px',
          borderColor: on ? (opts?.color || tokens.teal) : tokens.line,
          bgcolor: on ? '#F0F8F6' : tokens.card,
          ...(pickable ? { '&:hover': { borderColor: opts?.color || tokens.tealHi } } : {}) }}>
        <Typography component="b" sx={{ fontSize: 15, fontWeight: 700, color: opts?.color }}>{value}</Typography>
        <Typography sx={{ fontSize: 12, color: tokens.muted }}>{label}</Typography>
      </Paper>
    );
  };

  return (
    <>
      {/* THE FILTER BAR SITS ABOVE EVERYTHING IT FILTERS.
          It used to live below the search box and its results, which read as though the
          colours belonged to the firm list alone — so a desk searching one company saw
          the chips underneath and never thought to use them on what it was looking at.
          Above both, one control governs the search results and the book alike. */}
      {/* ---- register KPIs + filters ---- */}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', mb: 1 }}>
        {stat('items', all.length, { filter: '', title: 'Everything on file — clears the filter' })}
        {stat('good', greens, { color: tokens.ok, filter: 'GREEN' })}
        {stat('ugly', reds, { color: tokens.bad, filter: 'RED' })}
        {stat('bad', ambs, { color: tokens.warn, filter: 'AMBER' })}
        {blues > 0 && stat('policy', blues, { color: SEV_BG.BLUE, filter: 'BLUE' })}
        {stat('firms watched', codes.length)}
        {/* A SECOND AXIS, set apart by a rule: exposure is not a colour, and it
            COMBINES with whichever colour is selected rather than replacing it.
            "Ugly + Live exposure" is the view the credit desk actually wants, and one
            exclusive row of tiles could never express it. */}
        {lives > 0 && (
          <>
            <Box sx={{ width: '1px', height: 24, bgcolor: tokens.line, mx: 0.2 }} />
            <Chip label={`💰 Live exposure (${lives})`} clickable size="small"
              variant={liveOnly ? 'filled' : 'outlined'} color={liveOnly ? 'primary' : 'default'}
              title="Only firms we already have money out to — combines with the colour"
              onClick={() => setLiveOnly((v) => !v)} />
          </>
        )}
        <Box sx={{ flex: 1 }} />
        <Button onClick={() => setSched('all')}>⏰ Schedule scan</Button>
        {/* The question a search cannot answer: no articles reads the same whether the
            company is quiet or this deployment has no way out to the internet. */}
        <Button onClick={runProbe}>🩺 Check sources</Button>
        {all.length > 0 && <Button onClick={() => setEmailAll(true)}>📧 Email firms’ news</Button>}
        {all.length > 0 && <Button color="error" onClick={clearAll}>🗑 Clear all news</Button>}
        <Button variant="contained" disabled={scan.running} onClick={scanAll}>
          {scan.running ? 'Scanning…' : '📡 Scan all firms'}
        </Button>
      </Box>

      {probe && (
        <Alert severity={/^NO source|could not reach/.test(probe) ? 'error' : 'info'}
          onClose={() => setProbe('')} sx={{ py: 0, fontSize: 12, mb: 1 }}>
          {probe}
        </Alert>
      )}
      {/* ---- ad-hoc search ---- */}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mb: 0.8 }}>
        <TextField value={qBox} onChange={(e) => setQBox(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') searchGo(); }}
          placeholder="🔎 Search any name — company, owner, partner, plant…"
          sx={{ flex: '1 1 200px', minWidth: 0 }} />
        <Button variant="contained" onClick={searchGo}
          sx={{ flex: { xs: '1 1 100%', sm: '0 0 auto' },
            ...(adhoc.running ? { background: 'linear-gradient(135deg,#B3432B,#C85A3F)' } : {}) }}>
          {adhoc.running ? '■ Stop' : 'Search news'}
        </Button>
      </Box>
      <Box sx={{ display: 'flex', gap: 1.2, alignItems: 'center', flexWrap: 'wrap', mb: 0.5 }}>
        <TextField type="date" label="From" InputLabelProps={{ shrink: true }} value={adhoc.dfrom}
          sx={{ flex: { xs: '1 1 40%', sm: '0 0 auto' } }}
          onChange={(e) => setAdhoc((a) => ({ ...a, dfrom: e.target.value }))} />
        <TextField type="date" label="To" InputLabelProps={{ shrink: true }} value={adhoc.dto}
          sx={{ flex: { xs: '1 1 40%', sm: '0 0 auto' } }}
          onChange={(e) => setAdhoc((a) => ({ ...a, dto: e.target.value }))} />
        <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>leave blank = last 3 months</Typography>
        <Box sx={{ flex: 1 }} />
        {(adhoc.term || adhoc.items.length) ? <Button onClick={searchClear}>↺ New search</Button> : null}
        <Button onClick={() => setSched('plain')}>⏰ Schedules</Button>
      </Box>

      {adhoc.term && (
        <Paper variant="outlined" sx={{ borderColor: tokens.line, p: 1.2, mb: 1.8 }}>
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap', mb: 0.5 }}>
            <Typography component="b" sx={{ fontSize: 13, fontWeight: 700 }}>Results for “{adhoc.term}”</Typography>
            <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
              {adhoc.running ? 'searching…'
                : sev && adhocShown.length !== adhoc.items.length
                  ? `${adhocShown.length} of ${adhoc.items.length} real articles`
                  : `${adhoc.items.length} real articles`}
            </Typography>
            <Box sx={{ flex: 1 }} />
            {!adhoc.running && adhoc.items.length > 0 && <Button onClick={() => setEmailNews(true)}>📧 Email these</Button>}
            {!adhoc.running && adhoc.items.length > 0 && <Button onClick={saveAsWatchTerm}>★ Save as watch term</Button>}
            <Button onClick={searchClear}>✕ Clear</Button>
          </Box>
          {adhoc.err && <Alert severity="error" sx={{ py: 0, fontSize: 12, mb: 1 }}>{adhoc.err}</Alert>}
          {adhoc.running
            ? <Typography sx={{ p: 2, textAlign: 'center', color: tokens.muted, fontSize: 12.5 }}>Scraping live news for “{adhoc.term}”…</Typography>
            : adhocShown.length
              ? adhocShown.map((n, i) => (
                <NewsLine key={i} severity={n.severity} headline={n.headline} url={n.url}
                  meta={`${n.source || ''} · ${n.when || ''}`}
                  onOpen={() => newsService.open(n, user.full)} />
              ))
              : !adhoc.err && (
                <Typography sx={{ p: 2, textAlign: 'center', color: tokens.muted, fontSize: 12.5 }}>
                  {adhoc.items.length
                    ? `None of the ${adhoc.items.length} articles for “${adhoc.term}” is ${SEV_LABEL[sev as Severity]}.`
                    : `No articles found for “${adhoc.term}”.`}
                </Typography>
              )}
        </Paper>
      )}

      {scan.running && (
        <Alert severity="info" icon={false} sx={{ py: 0.4, fontSize: 12, mb: 1 }}>
          {/* SEARCHES, not firms. The unit was wrong: the sweep runs one search per
              company name AND per watch term, so a book of 340 firms is often 400-odd
              searches — the counter appeared to overshoot its own total. */}
          {scan.total
            ? `Searching ${scan.done} / ${scan.total} terms across ${scan.firms || codes.length} firms · ${scan.found} real items so far…`
            : 'Reading the register…'}
          <LinearProgress variant={scan.total ? 'determinate' : 'indeterminate'}
            value={scan.total ? Math.round((scan.done / scan.total) * 100) : undefined}
            sx={{ mt: 0.6, height: 4, borderRadius: 99 }} />
        </Alert>
      )}
      {!scan.running && scan.failTerms > 0 && (
        <Alert severity="error" sx={{ py: 0, fontSize: 12, mb: 1 }}>
          {scan.failTerms} source request{scan.failTerms > 1 ? 's' : ''} failed in the last scan
          {scan.why ? <> — <b>{scan.why}</b></> : '. Usually connectivity or rate limiting'}. Everything shown is a
          real scraped article; nothing is invented.
        </Alert>
      )}

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
          /* COLLAPSED, and unmounted until opened.
             Every firm used to render expanded, so a book of 340 firms mounted all
             7,400 headline rows at once — tens of thousands of DOM nodes, rebuilt on
             every keystroke in the filter and every re-render. That is the freeze:
             not the network, the page. Closed by default the sweep costs 340 summary
             rows, and a firm's headlines are built when someone actually opens it.
             A filter or a search that narrows to a few firms opens them itself. */
          <Accordion key={code} disableGutters variant="outlined"
            defaultExpanded={groups.length <= 12}
            TransitionProps={{ unmountOnExit: true }}
            sx={{ borderColor: tokens.line, mb: 0.8, '&:before': { display: 'none' } }}>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              {/* On a phone this row cannot be one line: the name got squeezed to a
                  word per line while Scan/Policy ran off the right edge. The name takes
                  its own full-width line there and the meta/controls wrap beneath it. */}
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, width: '100%', pr: 1,
                flexWrap: { xs: 'wrap', sm: 'nowrap' }, rowGap: 0.4 }}>
                <Typography component="b" sx={{ fontSize: 13.2, fontWeight: 700,
                  flex: { xs: '1 1 100%', sm: '0 1 auto' }, minWidth: 0 }}>{c.name || code}</Typography>
                <CodeText code={code} />
                <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                  {items.length} item{items.length > 1 ? 's' : ''}
                </Typography>
                <Box sx={{ flex: 1, display: { xs: 'none', sm: 'block' } }} />
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
                  meta={`${n.source || ''} · ${n.when || ''} · matched “${n.term}”`}
                  flag={n.category === 'context-review'
                    ? (n.reason || contextReason(n.headline)) : undefined}
                  onOpen={() => newsService.open(n, user.full)} />
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
