import { db, nowStamp } from '../api/atlasStore';
import { writeAudit } from './auditService';
import { PULSE_URL } from '../api/axiosClient';
import { authHeaders } from '../auth/session';
import { entitiesService } from './entitiesService';

// Port of v12 AUGMENT 16 — India News Radar. Every item stored here is a REAL
// article scraped live from GDELT; when a source is unreachable the scan reports
// the failure rather than inventing anything.

export type Severity = 'RED' | 'AMBER' | 'GREEN' | 'BLUE';

export interface NewsItem {
  id: string; code: string; term: string;
  headline: string; url: string; source: string; when: string;
  severity: Severity; category: string; reason?: string; mode: string;
  verdict: string | null; at: string;
}

export interface Article {
  headline: string; url: string; source: string; when: string;
  // Present only when PULSE answered: its verdict, which the screen prefers over the
  // client-side fallback so a digest and a live search never disagree about a headline.
  severity?: Severity; category?: string; reason?: string;
}

/* ---------------- state ---------------- */
export function news(): NewsItem[] {
  const d: any = db(); if (!d.news) d.news = [];
  return d.news;
}
// code -> [extra terms: owners, brands…]
export function watch(): Record<string, string[]> {
  const d: any = db(); if (!d.newswatch) d.newswatch = {};
  return d.newswatch;
}

/* ---------------- triage, client-side ----------------
   PULSE IS THE AUTHORITY. Every article it returns already carries `severity`,
   `category` and — for a context flip — the `reason`, and the screen renders what the
   server decided. That matters beyond tidiness: a scheduled digest and a live search
   must colour the same headline the same way, and only one copy of the rules can be
   right about that.

   What follows is the FALLBACK, used when the answer came from somewhere other than
   PULSE (the direct-GDELT hop, a public proxy, the offline prototype build). It mirrors
   services/pulse/app/news/triage.py — keep the two in step. */

const T_RED = ['fraud', 'frauds', 'fraudulent', 'defrauded', 'default', 'defaults', 'defaulter',
  'wilful defaulter', 'willful defaulter', 'insolvency', 'insolvent', 'bankruptcy', 'bankrupt',
  'arrest', 'arrested', 'raid', 'raids', 'raided', 'scam', 'npa', 'npas', 'money laundering',
  'cbi', 'ed probe', 'probe', 'fir', 'firs', 'embezzlement', 'embezzled', 'irregularity',
  'irregularities', 'misappropriation', 'shell company', 'shell entity', 'tax evasion',
  'forgery', 'fugitive', 'fraud case', 'loan fraud',
  'siphoning', 'siphoned', 'diversion of funds', 'round-tripping', 'round tripping',
  'fake itc', 'gst evasion', 'dggi', 'sfio', 'eow', 'lookout notice', 'lookout circular',
  'chargesheet', 'charge sheet', 'absconding', 'hawala', 'ponzi', 'forensic audit',
  'asset attachment', 'provisional attachment', 'sarfaesi', 'nclt', 'ibc', 'liquidation',
  'winding up', 'pmla', 'benami', 'circular trading', 'cross-default', 'event of default',
  'enforcement directorate'];
// The stress ladder — separate from the routine watch words because it must outrank a
// positive verb ("Promoter pledge RISES to 62 per cent" is not good news).
const T_STRESS = ['downgrade', 'downgraded', 'defaulted payment',
  'sma-0', 'sma-1', 'sma-2', 'special mention account', 'restructuring', 'restructured',
  'debt recast', 'recast', 'moratorium', 'one-time settlement', 'one time settlement', 'ots',
  'credit watch', 'rating withdrawn', 'covenant breach', 'dscr breach', 'dsra breach',
  'promoter pledge', 'share pledge', 'pledged shares', 'invocation of pledge', 'discom dues',
  'receivables from discom', 'auditor change', 'auditor resigns', 'auditor resignation',
  'new auditor appointed', 'qualified opinion', 'restatement', 'accounting irregularity',
  'cfo exit', 'cfo resigns', 'kmp resignation', 'gst notice', 'it survey', 'pcb notice',
  'almm delisting', 'roc charge', 'second charge', 'pari passu', 'inter-corporate deposit',
  'upstreaming', 'related party', 'related-party'];
const T_AMBER = ['litigation', 'lawsuit', 'court', 'penalty', 'penalties', 'fined', 'fine',
  'delay', 'delays', 'delayed', 'layoff', 'layoffs', 'strike', 'dispute', 'disputes',
  'show cause', 'showcause', 'investigation', 'investigated', 'shortfall', 'recall',
  'recalled', 'resignation', 'resigns', 'resigned', 'warning', 'summons'];
// Policy: fires with NO company name. A state tariff order re-prices a portfolio.
const T_BLUE = ['tariff order', 'tariff revision', 'true-up', 'serc', 'cerc', 'regulator order',
  'regulatory order', 'open access', 'demand charge', 'fixed charge', 'sub-metering',
  'cross-subsidy surcharge', 'ists waiver', 'connectivity regulation', 'rpo',
  'renewable purchase obligation', 'payment security mechanism', 'late payment surcharge',
  'almm', 'dcr', 'domestic content requirement', 'pli scheme', 'pli disbursement',
  'anti-dumping duty', 'safeguard duty', 'net metering', 'gross metering',
  'mnre notification', 'mop notification', 'gazette notification', 'viability gap funding',
  'vgf', 'must-run status', 'curtailment compensation', 'deviation settlement',
  'dsm regulation', 'green hydrogen incentive'];
const T_GOOD = ['wins', 'win', 'won', 'bags', 'bagged', 'awarded', 'awards', 'award',
  'ppa signed', 'signs ppa', 'mou', 'commissions', 'commissioned', 'cod achieved',
  'inaugurates', 'inaugurated', 'inauguration', 'launches', 'launched', 'expansion',
  'expands', 'profit', 'profits', 'record', 'milestone', 'partnership', 'partners',
  'tie-up', 'ipo', 'listing', 'growth', 'surges', 'jumps', 'rises', 'upgrade', 'upgraded',
  'credit upgrade', 'accretive acquisition', 'wins order', 'wins contract', 'bags order'];
// Good-looking news that a live borrower doing it makes a question, with the reason.
const T_CONTEXT: [string, string[], string][] = [
  ['fresh_debt', ['raises debt', 'fresh debt', 'raises funding', 'raised funding', 'secures loan',
    'secures funding', 'new loan', 'bridge loan', 'top-up loan', 'refinance', 'rollover',
    'raises capital', 'funding round', 'debt raise', 'funding', 'funded', 'raises', 'raised',
    'investment', 'invests'],
    'Borrower levering up elsewhere dilutes our cover — could be desperation funding or ever-greening.'],
  ['settlement', ['one-time settlement', 'one time settlement', 'ots', 'settles with lender',
    'settlement with bank', 'debt settled'],
    'An OTS means a lender took a haircut — a default event, not a win.'],
  ['pledge', ['pledges shares', 'share pledge', 'promoter pledge', 'pledged stake'],
    'Promoter share-pledging for cash is a classic early-distress signal.'],
  ['big_order', ['wins huge order', 'bags order worth', 'wins order worth', 'largest order',
    'record order'],
    'Over-trading: orders beyond funding capacity start working-capital blowups.'],
  ['stake_sale', ['stake sale', 'sells stake', 'strategic investor', 'promoter stake',
    'divests stake', 'equity infusion'],
    'Could be growth — or a distress sale / promoter exit. Context decides.'],
  ['auditor_change', ['auditor change', 'new auditor appointed', 'auditor appointed',
    'changes auditor', 'auditor resigns'],
    'Auditor churn often precedes a qualified opinion or fraud discovery.'],
];
// A negator this close BEFORE a keyword means the headline clears the name, not accuses it.
const T_NEG = ['cleared of', 'clears', 'cleared', 'absolves', 'absolved', 'denies', 'denied',
  'deny', 'refutes', 'refuted', 'rejects', 'no default', 'not defaulted', 'acquitted',
  'acquittal', 'exonerated', 'dismisses', 'dismissed', 'quashes', 'quashed', 'clean chit'];
// keyword -> phrases that mean it is the WRONG sense. "Charging station" is not a charge.
const T_SENSE: Record<string, string[]> = {
  charge: ['charging station', 'ev charg', 'fast charg', 'battery charg', 'free of charge',
    'in charge of', 'took charge'],
  charges: ['charging station', 'ev charg'],
  default: ['by default', 'default setting', 'default option', 'default mode'],
  fine: ['doing fine', 'works fine', 'fine print'],
  strike: ['strike price', 'strikes a deal'],
  recast: ['recasts its board', 'recasts board', 'recasts team'],
};
const NEG_WINDOW = 70;

// Whole words and whole phrases, tolerant of punctuation: the press writes both
// "one-time settlement" and "one time settlement".
const wordRe = (arr: string[]) =>
  new RegExp('\\b(' + arr.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    .replace(/[\s-]+/g, '[\\s-]+')).join('|') + ')\\b', 'i');
const RE_RED = wordRe(T_RED), RE_STRESS = wordRe(T_STRESS), RE_AMBER = wordRe(T_AMBER);
const RE_BLUE = wordRe(T_BLUE), RE_GOOD = wordRe(T_GOOD), RE_NEG = wordRe(T_NEG);
const RE_CTX: [string, RegExp, string][] = T_CONTEXT.map(([n, ws, why]) => [n, wordRe(ws), why]);

const blockedWords = (h: string): Set<string> => {
  const low = h.toLowerCase();
  return new Set(Object.keys(T_SENSE).filter((k) => T_SENSE[k].some((p) => low.includes(p))));
};

// The first match that is neither the wrong word sense nor negated. Every match is
// checked, not just the first: "EV charging firm arrested over fraud" has a blocked
// "charge" and a real "fraud", and stopping early would drop the alert.
function realHit(h: string, re: RegExp, blocked: Set<string>): boolean {
  const rx = new RegExp(re.source, 'gi');
  let m: RegExpExecArray | null;
  while ((m = rx.exec(h)) !== null) {
    if (blocked.has(m[0].toLowerCase())) continue;
    if (RE_NEG.test(h.slice(Math.max(0, m.index - NEG_WINDOW), m.index))) continue;
    return true;
  }
  return false;
}

/** Is our money already out to this firm? A lending line that is not Rejected or On Hold.
 *  Mirrors the exposure the server is told about via `exposure=live`. */
export function isLiveBorrower(code: string): boolean {
  const rows: any[] = (db() as any).lending || [];
  return rows.some((r) => r?.code === code
    && String(r?.stage || '').trim() !== ''
    && !['rejected', 'on hold'].includes(String(r.stage).toLowerCase()));
}

/** Why a context phrase tenses credit up — for the flag's tooltip. */
export function contextReason(h: string): string {
  const hit = RE_CTX.find(([, re]) => re.test(String(h || '')));
  return hit ? hit[2] : '';
}

/* Precedence: hard-adverse, the context flip (live names only, so the flag carries its
   reason), the stress ladder, a genuine win, the routine watch words, then policy. */
export function classify(h: string, live = false): [Severity, string] {
  h = String(h || '');
  if (!h.trim()) return ['GREEN', 'neutral'];
  const blocked = blockedWords(h);
  if (realHit(h, RE_RED, blocked)) return ['RED', 'adverse'];
  const ctx = RE_CTX.find(([, re]) => re.test(h));
  if (ctx && live) return ['AMBER', 'context-review'];
  if (realHit(h, RE_STRESS, blocked)) return ['AMBER', 'stress'];
  if (RE_GOOD.test(h)) return ['GREEN', 'positive'];
  if (realHit(h, RE_AMBER, blocked)) return ['AMBER', 'watch'];
  if (RE_AMBER.test(h)) return ['GREEN', 'neutral'];   // matched, but negated / wrong sense
  if (RE_BLUE.test(h)) return ['BLUE', 'policy'];
  if (ctx) return ['GREEN', 'positive'];               // a name we are chasing: a win
  return ['GREEN', 'neutral'];
}

/** PULSE speaks the desk's labels; the screen's palette is keyed by colour. */
export const SEV_OF: Record<string, Severity> = {
  UGLY: 'RED', BAD: 'AMBER', GOOD: 'GREEN', POLICY: 'BLUE',
};

export const SEV_LABEL: Record<Severity, string> = { GREEN: 'GOOD', AMBER: 'BAD', RED: 'UGLY', BLUE: 'POLICY' };

/* ---------------- adapters ----------------
   PULSE is the source of record: it searches Google News + GDELT + Bing server-side,
   merges and de-duplicates them, and classifies each headline. The browser cannot do
   that itself — none of those upstreams send CORS headers.

   The order below is deliberate:
     0. PULSE (/pulse/v1/news/search) — three sources, and the desk's search terms stay
        inside PRISM;
     1. GDELT directly — works only where CORS happens to allow it;
     2. public CORS proxies — LAST RESORT. These route the names of companies we are
        looking at through a third party nobody vetted, so they exist only to keep the
        prototype/static build usable and are skipped the moment PULSE answers.
   The FIRST endpoint that returns real articles wins. Nothing is fabricated.
   config: window.NEWS_PROXY = 'https://your-proxy/?url=' overrides the list. */
function gdeltURL(term: string): string {
  return 'https://api.gdeltproject.org/api/v2/doc/doc?query='
    + encodeURIComponent('"' + term + '"')
    + '&mode=ArtList&maxrecords=12&format=json&sort=DateDesc&timespan=3m';
}

let PULSE_UP: boolean | null = null; // null=untried · true=gateway present · false=static host

function newsEndpoints(term: string, dfrom?: string, dto?: string, live?: boolean) {
  const g = gdeltURL(term), enc = encodeURIComponent(g);
  const list: { k: string; u: string }[] = [];
  const dr = (dfrom ? '&from=' + encodeURIComponent(dfrom) : '') + (dto ? '&to=' + encodeURIComponent(dto) : '')
    // Polarity depends on our relationship with the firm and the BROWSER is what knows
    // it — the book is in this store. Told this, PULSE flips good-looking news on a
    // borrower to a review flag instead of filing it as a win.
    + (live ? '&exposure=live' : '');
  // 0 — same-origin PULSE proxy, only when a PRISM gateway is present. On a plain
  // static host this 404s, so after the first miss we never ask again this session.
  const havePulse = location.protocol.indexOf('http') === 0 && PULSE_UP !== false;
  if (havePulse)
    list.push({ k: 'pulse', u: PULSE_URL + '/v1/news/search?q=' + encodeURIComponent(term) + dr });
  // WHEN PULSE IS THERE, PULSE IS THE ANSWER — right or wrong.
  //
  // The public fallbacks below were written for a static build with no server at all.
  // Left in the chain on a real deployment they did two bad things. They turned a
  // 2-second refusal into a 50-second one: PULSE's own 30s budget, then GDELT direct
  // (blocked by CORS), then three third-party proxies at 9s each — which is why a
  // failed search reads on screen as a hang rather than an error. And they put the
  // desk's search terms — the names of the companies we are looking at — through
  // corsproxy.io and allorigins.win, services nobody here vetted.
  //
  // If PULSE answered badly, that is a fault to fix in PULSE, and its diagnostics
  // endpoint says which source is down. Guessing around it hides the fault.
  if (havePulse) return list;
  list.push({ k: 'direct', u: g });
  const cfg = (window as any).NEWS_PROXY;
  if (cfg) list.push({ k: 'proxy', u: cfg + enc });
  list.push({ k: 'proxy', u: 'https://corsproxy.io/?url=' + enc });
  list.push({ k: 'proxy', u: 'https://api.allorigins.win/raw?url=' + enc });
  return list;
}

function parseArticles(j: any): Article[] {
  const arts = (j && j.articles) || (j && j.contents && JSON.parse(j.contents).articles) || [];
  return arts.map((a: any) => ({
    headline: a.title || a.headline || '', url: a.url || '',
    source: (a.source || a.domain || '') + (a.via ? ' · ' + a.via : ''),
    when: (a.seendate || a.when || a.published_at || '').slice(0, 8),
    // PULSE labels in the desk's words (UGLY/BAD/GOOD/POLICY); GDELT and the proxies
    // send no verdict at all, and those fall through to the client-side rules.
    ...(a.severity && SEV_OF[a.severity] ? { severity: SEV_OF[a.severity] } : {}),
    ...(a.category ? { category: String(a.category) } : {}),
    ...(a.reason ? { reason: String(a.reason) } : {}),
  })).filter((a: Article) => a.headline && /^https?:\/\//i.test(a.url));
}

/* Whatever the far end called the problem. PULSE answers `{error:"…"}`, the gateway
   answers the problem envelope `{error:{detail:"…"}}`, and FastAPI's own refusals are
   `{detail:"…"}` — all three have to read back as one sentence. */
function detailOf(j: any): string {
  if (!j) return '';
  const e = j.error;
  if (typeof e === 'string') return e;
  if (e && typeof e === 'object') return String(e.detail || e.title || '');
  return typeof j.detail === 'string' ? j.detail : '';
}

/* WHO IS ASKING.
   Every PULSE call here uses raw `fetch` — for the abort signal, the mixed endpoint
   kinds and the non-JSON error pages — and `fetch` knows nothing about the axios
   request interceptor that attaches identity to every OTHER API call. So all of them
   went to the gateway anonymously and came back "HTTP 401 — Authentication required
   (Bearer token)", four hundred times per sweep. The screen reported it faithfully;
   nobody read it as "the radar is signed out" because nothing else on the page was.

   Sent to PULSE ONLY. The public CORS proxies in the fallback chain are third parties,
   and handing one of them a live bearer token would be far worse than a failed search. */
function pulseHeaders(): Record<string, string> {
  return { Accept: 'application/json', ...authHeaders() };
}

function tryEndpoint(u: string, ms: number, userSignal?: AbortSignal,
                     mine = false): Promise<Article[]> {
  const to = typeof AbortSignal !== 'undefined' && AbortSignal.timeout ? AbortSignal.timeout(ms || 9000) : undefined;
  let sig: AbortSignal | undefined;
  if (userSignal && typeof AbortSignal !== 'undefined' && (AbortSignal as any).any)
    sig = (AbortSignal as any).any([to, userSignal].filter(Boolean)); // timeout OR user-stop
  else sig = userSignal || to;
  return fetch(u, { signal: sig, headers: mine ? pulseHeaders() : { Accept: 'application/json' } })
    .then((r) => r.text().then((t) => {
      let j: any;
      try { j = JSON.parse(t); } catch { j = null; }   // HTML error page → no detail to read
      // Carry the far end's OWN words. "HTTP 403" is unanswerable; "HTTP 403 —
      // Forbidden: run_news_scan" names the permission to grant, and "HTTP 502 —
      // Upstream unavailable" says the service is down rather than the news.
      if (!r.ok) throw new Error('HTTP ' + r.status + (detailOf(j) ? ' — ' + detailOf(j) : ''));
      if (j === null) throw new Error('non-JSON response');
      // A 200 that carries an error and no articles is a REACHED-BUT-EMPTY answer:
      // PULSE saying every upstream failed. Treat it as a failure so the fallbacks
      // still get their turn and the reason reaches the screen.
      const why = detailOf(j);
      if (why && !((j.articles || []).length)) throw new Error(why);
      return parseArticles(j);
    }));
}

// The reason the LAST search failed, in the server's own words — held here so the screen
// can show it and a support call starts from a fact rather than a guess. A function
// rather than a live binding, so it reads the same however the bundle is built.
let _lastFailure = '';
export const lastFailure = (): string => _lastFailure;

// PRODUCTION RULE: returns REAL articles or throws — never fabricates.
export function fetchTerm(term: string, dfrom?: string, dto?: string, userSignal?: AbortSignal,
                          live?: boolean): Promise<Article[]> {
  const eps = newsEndpoints(term, dfrom, dto, live);
  const aborted = () => !!(userSignal && userSignal.aborted);
  return new Promise((resolve, reject) => {
    let i = 0, lastErr: any, pulseErr = '';
    (function next() {
      if (aborted()) { const a: any = new Error('stopped'); a.name = 'AbortError'; return reject(a); }
      if (i >= eps.length) {
        // PULSE is the only endpoint whose failure is diagnosable — the public proxies
        // fail for a hundred uninteresting reasons — so its reason is the one to keep.
        _lastFailure = pulseErr || String(lastErr?.message || lastErr || 'all sources failed');
        const err: any = new Error(_lastFailure);
        err.pulse = pulseErr;
        return reject(err);
      }
      const ep = eps[i++];
      tryEndpoint(ep.u, ep.k === 'pulse' ? 30000 : 9000, userSignal, ep.k === 'pulse')
        .then((arts) => { if (ep.k === 'pulse') { PULSE_UP = true; _lastFailure = ''; } resolve(arts); })
        .catch((e) => {
          if (aborted()) { e.name = 'AbortError'; return reject(e); } // user stopped — don't try more
          if (ep.k === 'pulse') {
            pulseErr = String(e?.message || e);
            // Only a 404 means "no gateway here" (a static host). Any other answer —
            // 403, 502, an upstream outage — means PULSE exists and is worth asking
            // again; giving up on it for the session would silently downgrade every
            // later search to the public proxies.
            if (/HTTP 404/.test(pulseErr)) PULSE_UP = false;
          }
          lastErr = e; next();
        });
    })();
  });
}

/* ---------------- the scan ---------------- */
const coName = (code: string) => (db().clients?.[code]?.name) || code || '';

/* The register holds LEGAL names — "Avana Capital Private Limited". Headlines do not:
   they say "Avana Capital". Searching the legal name verbatim is why a sweep of four
   hundred firms can finish with nothing at all and look like a broken pipeline; the
   suffix is the whole reason the phrase never matches. Strip it and search the name the
   press actually prints. (Watch terms are typed by the desk and left exactly as typed.) */
const CORP_SUFFIX =
  /\s*[,.]?\s*\b(private limited|pvt\.?\s?ltd\.?|pvt\.?\s?limited|public limited|limited|ltd\.?|llp|inc\.?|incorporated|corporation|corp\.?|plc|pte\.?\s?ltd\.?|gmbh|s\.?a\.?|b\.?v\.?|&\s?co\.?|company)\b\.?\s*$/i;

export function tradingName(name: string): string {
  const original = String(name || '').trim();
  let s = original;
  // Twice, so "Acme Solar India Pvt. Ltd." sheds "Ltd." and then "Pvt." — but never so
  // far that nothing recognisable is left ("Reliance Ltd" must not become "Reliance"
  // by way of an empty string).
  for (let i = 0; i < 2 && CORP_SUFFIX.test(s); i++) s = s.replace(CORP_SUFFIX, '').trim();
  s = s.replace(/[,\s]+$/, '').trim();
  return s.length >= 3 ? s : original;
}

export function termsFor(code: string): string[] {
  const legal = db().clients?.[code]?.name;
  const terms = [legal ? tradingName(legal) : '', ...(watch()[code] || [])]
    .map((t) => String(t || '').trim()).filter(Boolean);
  return Array.from(new Set(terms));   // a watch term equal to the name is one search
}

function ingest(code: string, term: string, arts: Article[], mode: string): number {
  let added = 0;
  const live = isLiveBorrower(code);
  arts.forEach((a) => {
    if (!a.headline) return;
    if (news().some((n) => (a.url && n.url === a.url) || (n.headline === a.headline && n.code === code))) return;
    // The server's verdict wins where there is one — it knows the same rules and it is
    // the copy a scheduled digest also used.
    const cc: [Severity, string] = a.severity
      ? [a.severity, a.category || 'neutral'] : classify(a.headline, live);
    const why = a.reason || (cc[1] === 'context-review' ? contextReason(a.headline) : '');
    news().unshift({
      id: 'N' + (news().length + 1) + '-' + code, code, term,
      headline: a.headline, url: a.url, source: a.source, when: a.when,
      severity: cc[0], category: cc[1], ...(why ? { reason: why } : {}),
      mode, verdict: null, at: nowStamp(),
    });
    added++;
  });
  return added;
}

export interface ScanState { running: boolean; done: number; total: number; failTerms: number }

export interface ScanState { running: boolean; done: number; total: number; failTerms: number }

/* The state-scoped themes a policy sweep asks about. PULSE publishes them; this copy is
   only for a build with no gateway in front of it. */
const POLICY_FALLBACK = ['tariff order', 'open access charges', 'net metering policy', 'ALMM',
  'payment security mechanism DISCOM', 'anti-dumping duty solar'];
let POLICY_CACHE: string[] | null = null;

async function policyThemes(): Promise<string[]> {
  if (POLICY_CACHE) return POLICY_CACHE;
  try {
    const r = await fetch(PULSE_URL + '/v1/news/config', { headers: pulseHeaders() });
    const j = r.ok ? await r.json() : null;
    const list = Array.isArray(j?.policy_themes) ? j.policy_themes.filter(Boolean) : [];
    POLICY_CACHE = list.length ? list : POLICY_FALLBACK;
  } catch { POLICY_CACHE = POLICY_FALLBACK; }
  return POLICY_CACHE || POLICY_FALLBACK;
}

/* THE FIRMS ARE THE REGISTER'S, not the browser's.

   `db().clients` is a working cache that only ever GROWS: lead conversion adds a row,
   a ledger import adds rows, every screen that lists companies merges its page into it,
   and nothing prunes. Read it as the roster and the radar sweeps companies the register
   does not have — which is why "firms watched" climbed past the number the Clients
   table shows, and why the sweep spent time on names nobody is lending to.

   hydrateAll() REPLACES the cache with the register's own entity list — the same source,
   and the same rows, the Clients table renders. Refreshed on every sweep, so the two
   screens cannot drift apart within a session. */
async function refreshFirms(): Promise<void> {
  try {
    const { clientsService } = await import('./clientsService');
    await clientsService.hydrateAll();
  } catch { /* offline / mock mode: keep whatever the store already holds */ }
}

export const newsService = {
  async scanCompany(code: string, by: string, quiet?: boolean): Promise<{ added: number; fails: number }> {
    const terms = termsFor(code);
    const live = isLiveBorrower(code);
    let total = 0, fails = 0;
    for (const term of terms) {
      try { total += ingest(code, term, await fetchTerm(term, undefined, undefined, undefined, live), 'live'); }
      catch { fails++; } // real data only — never invent
    }
    if (!quiet) {
      writeAudit(by, 'News scan', code, `${terms.length} terms · ${total} new${fails ? ` · ${fails} source failures` : ''}`);
    }
    return { added: total, fails };
  },

  /**
   * ONE request for the whole sweep.
   *
   * This used to be a `for` loop over four hundred firms, each awaiting its own
   * fetch — and a loop that awaits cannot overlap, so no two firms were ever
   * searched at the same time. Four hundred serial crossings of the gateway, each
   * re-opening TLS to the same three news hosts, plus a deliberate 250 ms pause
   * between firms, is half an hour of spinner for work the upstreams could have
   * done in a couple of minutes.
   *
   * PULSE now takes every term at once and fans out next to the upstreams, sharing
   * one connection pool and the cache and coalescing it already had. The browser's
   * job shrinks to what only the browser can do: knowing which term belongs to which
   * firm, and which of those firms we have money out to.
   *
   * Falls back to the old firm-by-firm path when PULSE is not reachable (a static
   * build, or a deployment with no gateway), so the sweep degrades rather than dies.
   */
  async sweepAll(by: string, onProgress: (s: { done: number; total: number; found: number }) => void) {
    await refreshFirms();
    const codes = Object.keys(db().clients || {});
    // term -> the firms watching it. Two firms watching the same promoter is ONE
    // search whose result files against both.
    const owners = new Map<string, string[]>();
    const liveTerms: string[] = [];
    for (const code of codes) {
      for (const term of termsFor(code)) {
        const at = owners.get(term);
        if (at) at.push(code); else owners.set(term, [code]);
        if (isLiveBorrower(code)) liveTerms.push(term);
      }
    }
    const terms = [...owners.keys()];
    if (!terms.length) return { found: 0, failTerms: 0, firms: 0 };
    onProgress({ done: 0, total: terms.length, found: 0 });

    const r = await fetch(PULSE_URL + '/v1/news/sweep', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...pulseHeaders() },
      // A sweep genuinely takes minutes at four hundred terms; the edge budgets for it.
      signal: typeof AbortSignal !== 'undefined' && AbortSignal.timeout
        ? AbortSignal.timeout(600_000) : undefined,
      body: JSON.stringify({ terms, live_terms: Array.from(new Set(liveTerms)) }),
    });
    const j = await r.json().catch(() => null);
    if (!r.ok || !j || !Array.isArray(j.results)) {
      throw new Error('HTTP ' + r.status + (detailOf(j) ? ' — ' + detailOf(j) : ''));
    }
    let found = 0, failTerms = 0, done = 0;
    for (const row of j.results) {
      if (row.error) failTerms++;
      const arts = parseArticles(row);
      for (const code of owners.get(row.term) || []) found += ingest(code, row.term, arts, 'live');
      onProgress({ done: ++done, total: terms.length, found });
    }
    if (j.error) _lastFailure = String(j.error);
    writeAudit(by, 'News scan', 'ALL',
      `${codes.length} firms · ${terms.length} terms · ${found} new`
      + (failTerms ? ` · ${failTerms} term(s) unreachable` : ''));
    return { found, failTerms, firms: codes.length };
  },

  // Sequential with a small gap between firms — be a polite API citizen.
  async scanAll(by: string, onProgress: (s: { done: number; total: number; found: number }) => void) {
    // The sweep reads the CLIENT STORE, which is filled by whichever screen the user
    // happened to visit first. Land straight on Tools (a bookmark, a reload, the tab
    // the sign-in lands on) and that store is empty — so "Scan all firms" swept ZERO
    // firms and reported success. Warm it here rather than trusting the route someone
    // took to arrive.
    await refreshFirms();
    const codes = Object.keys(db().clients || {});
    let found = 0, failTerms = 0, done = 0;
    for (const code of codes) {
      const r = await this.scanCompany(code, by, true);
      found += r.added; failTerms += r.fails; done++;
      onProgress({ done, total: codes.length, found });
      await new Promise((res) => setTimeout(res, 250));
    }
    writeAudit(by, 'News scan', 'ALL', `${codes.length} firms · ${found} new${failTerms ? ` · ${failTerms} source failures` : ''}`);
    return { found, failTerms, firms: codes.length };
  },

  /** Can PULSE reach the news from where it runs? Names each source and its latency. */
  async diagnostics(): Promise<{ ok: boolean; summary: string; sources: any[] }> {
    const r = await fetch(PULSE_URL + '/v1/news/diagnostics', {
      headers: pulseHeaders(),
      signal: typeof AbortSignal !== 'undefined' && AbortSignal.timeout
        ? AbortSignal.timeout(45_000) : undefined,
    });
    const j = await r.json().catch(() => null);
    // 503 is a real ANSWER here — "I am up, the sources are not" — so it is read, not
    // thrown. Only an unparseable reply means PULSE itself could not be reached.
    if (!j || typeof j.summary !== 'string') {
      throw new Error('HTTP ' + r.status + (detailOf(j) ? ' — ' + detailOf(j) : ''));
    }
    return j;
  },

  /* POLICY SWEEP — the risk that never names the firm.
     A state tariff order, an ALMM revision, a change to open-access charges: none of
     these mention a borrower, so a radar that only matches company names cannot see
     them, yet they re-price whole exposures at once. The sweep asks each theme AGAINST
     THE FIRM'S STATE and files what it finds against that firm, so triage shows which
     exposure the policy move touches.

     Themes come from PULSE (/v1/news/config) so there is one list, not a copy here that
     drifts; POLICY_FALLBACK only covers a static build with no gateway. */
  async policyScan(code: string, by: string): Promise<{ added: number; fails: number; state: string }> {
    const c: any = db().clients?.[code] || {};
    const state = String(c.state || '').trim();
    if (!state) return { added: 0, fails: 0, state: '' };   // policy risk is state-scoped
    const themes = await policyThemes();
    let added = 0, fails = 0;
    for (const theme of themes) {
      const q = theme + ' ' + state;
      try { added += ingest(code, 'policy: ' + q, await fetchTerm(q), 'scan'); }
      catch { fails++; }
    }
    writeAudit(by, 'News scan', code, `policy sweep (${state}) · ${added} new${fails ? ` · ${fails} source failures` : ''}`);
    return { added, fails, state };
  },

  verdict(id: string, v: string, by: string) {
    const n = news().find((x) => x.id === id); if (!n) return;
    n.verdict = v;
    writeAudit(by, 'News verdict', n.code, `${v} — ${n.headline.slice(0, 70)}`);
  },

  open(n: { url?: string; code?: string; headline?: string }, by: string): boolean {
    if (n.url && /^https?:\/\//i.test(n.url)) {
      window.open(n.url, '_blank', 'noopener');
      writeAudit(by, 'News opened', n.code || '', (n.headline || '').slice(0, 70));
      return true;
    }
    return false;
  },

  addTerm(code: string, term: string, by: string) {
    (watch()[code] = watch()[code] || []).push(term.trim());
    writeAudit(by, 'News watch', code, 'term added: ' + term.trim());
  },
  delTerm(code: string, i: number) { (watch()[code] || []).splice(i, 1); },

  clearAll(by: string): number {
    const n = news().length;
    (db() as any).news = [];
    writeAudit(by, 'News cleared', '', `all firm news (${n} items)`);
    return n;
  },

  // Terms the all-firms digest covers: every client name plus its watch terms.
  allFirmTerms(): string[] {
    return Object.keys(db().clients || {}).flatMap((code) => termsFor(code));
  },

  // Tools-tab CSV export = the full news register (v12 pageExport override).
  exportCsv(by: string) {
    const q = (v: any) => { v = String(v == null ? '' : v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
    const head = ['id', 'code', 'company', 'term', 'severity', 'category', 'headline', 'source', 'when', 'url', 'verdict', 'mode'];
    const day = new Date().toISOString().slice(0, 10);
    const csv = 'EVAM — News Radar,' + day + '\n\n' + head.join(',') + '\n'
      + news().map((n) => [n.id, n.code, coName(n.code), n.term, n.severity,
        n.category, n.headline, n.source, n.when, n.url, n.verdict || '', n.mode].map(q).join(',')).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = 'EVAM_NewsRadar_' + day + '.csv';
    document.body.appendChild(a); a.click(); a.remove();
    writeAudit(by, 'Exported', '', 'News Radar CSV');
  },
};
