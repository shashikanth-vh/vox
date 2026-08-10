import { db, nowStamp } from '../api/atlasStore';
import { writeAudit } from './auditService';
import { PULSE_URL } from '../api/axiosClient';
import { entitiesService } from './entitiesService';

// Port of v12 AUGMENT 16 — India News Radar. Every item stored here is a REAL
// article scraped live from GDELT; when a source is unreachable the scan reports
// the failure rather than inventing anything.

export type Severity = 'RED' | 'AMBER' | 'GREEN';

export interface NewsItem {
  id: string; code: string; term: string;
  headline: string; url: string; source: string; when: string;
  severity: Severity; category: string; mode: string;
  verdict: string | null; at: string;
}

export interface Article { headline: string; url: string; source: string; when: string; }

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

/* PULSE's triage lists, client-side (keep in sync with services/pulse).
   Whole-word matching on BOTH sides — a keyword must be its own word, so
   "fir" (FIR) never matches "first" or "firm", "raid" never matches "afraid". */
const N_RED = ['fraud', 'frauds', 'fraudulent', 'defrauded', 'default', 'defaults', 'defaulter',
  'wilful defaulter', 'insolvency', 'insolvent', 'bankruptcy', 'bankrupt', 'arrest', 'arrested',
  'raid', 'raids', 'raided', 'scam', 'npa', 'npas', 'money laundering', 'cbi', 'ed probe', 'probe',
  'fir', 'firs', 'embezzlement', 'embezzled', 'irregularity', 'irregularities', 'misappropriation',
  'shell company', 'tax evasion', 'forgery', 'fugitive', 'fraud case', 'loan fraud'];
const N_AMBER = ['litigation', 'lawsuit', 'court', 'penalty', 'penalties', 'fined', 'fine',
  'downgrade', 'downgraded', 'delay', 'delays', 'delayed', 'layoff', 'layoffs', 'strike',
  'dispute', 'disputes', 'show cause', 'showcause', 'investigation', 'investigated', 'shortfall',
  'recall', 'recalled', 'resignation', 'resigns', 'resigned', 'warning', 'summons', 'defaulted payment'];
const N_GOOD = ['wins', 'win', 'won', 'bags', 'bagged', 'secures', 'secured', 'awarded', 'awards', 'award',
  'signs', 'signed', 'order', 'orders', 'contract', 'contracts', 'epc', 'ppa', 'mou', 'commissions',
  'commissioned', 'inaugurates', 'inaugurated', 'inauguration', 'launches', 'launched', 'expansion',
  'expands', 'funding', 'funded', 'raises', 'raised', 'investment', 'invests', 'profit', 'profits',
  'record', 'milestone', 'partnership', 'partners', 'tie-up', 'acquires', 'acquisition', 'ipo',
  'listing', 'approval', 'approved', 'sanctioned', 'disbursed', 'growth', 'surges', 'jumps', 'rises',
  'upgrade', 'upgraded', 'wins order', 'wins contract', 'bags order'];

const wordRe = (arr: string[]) =>
  new RegExp('\\b(' + arr.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')\\b', 'i');
const RE_RED = wordRe(N_RED), RE_AMBER = wordRe(N_AMBER), RE_GOOD = wordRe(N_GOOD);

// Precedence: a hard-adverse (RED) word always wins; otherwise a clear positive
// reads GOOD even alongside a routine watch word; only then does AMBER apply.
export function classify(h: string): [Severity, string] {
  h = String(h || '');
  if (RE_RED.test(h)) return ['RED', 'adverse'];
  if (RE_GOOD.test(h)) return ['GREEN', 'positive'];
  if (RE_AMBER.test(h)) return ['AMBER', 'watch'];
  return ['GREEN', 'neutral'];
}

export const SEV_LABEL: Record<Severity, string> = { GREEN: 'GOOD', AMBER: 'BAD', RED: 'UGLY' };

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

function newsEndpoints(term: string, dfrom?: string, dto?: string) {
  const g = gdeltURL(term), enc = encodeURIComponent(g);
  const list: { k: string; u: string }[] = [];
  const dr = (dfrom ? '&from=' + encodeURIComponent(dfrom) : '') + (dto ? '&to=' + encodeURIComponent(dto) : '');
  // 0 — same-origin PULSE proxy, only when a PRISM gateway is present. On a plain
  // static host this 404s, so after the first miss we never ask again this session.
  if (location.protocol.indexOf('http') === 0 && PULSE_UP !== false)
    list.push({ k: 'pulse', u: PULSE_URL + '/v1/news/search?q=' + encodeURIComponent(term) + dr });
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

function tryEndpoint(u: string, ms: number, userSignal?: AbortSignal): Promise<Article[]> {
  const to = typeof AbortSignal !== 'undefined' && AbortSignal.timeout ? AbortSignal.timeout(ms || 9000) : undefined;
  let sig: AbortSignal | undefined;
  if (userSignal && typeof AbortSignal !== 'undefined' && (AbortSignal as any).any)
    sig = (AbortSignal as any).any([to, userSignal].filter(Boolean)); // timeout OR user-stop
  else sig = userSignal || to;
  return fetch(u, { signal: sig, headers: { Accept: 'application/json' } })
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
export function fetchTerm(term: string, dfrom?: string, dto?: string, userSignal?: AbortSignal): Promise<Article[]> {
  const eps = newsEndpoints(term, dfrom, dto);
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
      tryEndpoint(ep.u, ep.k === 'pulse' ? 30000 : 9000, userSignal) // local server fetches the web — give it room
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
  arts.forEach((a) => {
    if (!a.headline) return;
    if (news().some((n) => (a.url && n.url === a.url) || (n.headline === a.headline && n.code === code))) return;
    const cc = classify(a.headline);
    news().unshift({
      id: 'N' + (news().length + 1) + '-' + code, code, term,
      headline: a.headline, url: a.url, source: a.source, when: a.when,
      severity: cc[0], category: cc[1], mode, verdict: null, at: nowStamp(),
    });
    added++;
  });
  return added;
}

export interface ScanState { running: boolean; done: number; total: number; failTerms: number }

export const newsService = {
  async scanCompany(code: string, by: string, quiet?: boolean): Promise<{ added: number; fails: number }> {
    const terms = termsFor(code);
    let total = 0, fails = 0;
    for (const term of terms) {
      try { total += ingest(code, term, await fetchTerm(term), 'live'); }
      catch { fails++; } // real data only — never invent
    }
    if (!quiet) {
      writeAudit(by, 'News scan', code, `${terms.length} terms · ${total} new${fails ? ` · ${fails} source failures` : ''}`);
    }
    return { added: total, fails };
  },

  // Sequential with a small gap between firms — be a polite API citizen.
  async scanAll(by: string, onProgress: (s: { done: number; total: number; found: number }) => void) {
    // The sweep reads the CLIENT STORE, which is filled by whichever screen the user
    // happened to visit first. Land straight on Tools (a bookmark, a reload, the tab
    // the sign-in lands on) and that store is empty — so "Scan all firms" swept ZERO
    // firms and reported success. Warm it here rather than trusting the route someone
    // took to arrive.
    if (!Object.keys(db().clients || {}).length) {
      try {
        const rows = await entitiesService.list();
        rows.forEach((r: any) => {
          if (r.code) db().clients[r.code] = { ...(db().clients[r.code] || {}), ...r };
        });
      } catch { /* offline / mock mode: fall through with whatever the store has */ }
    }
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
