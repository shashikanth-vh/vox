import { db, nowStamp } from '../api/atlasStore';
import { writeAudit } from './auditService';

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
   GDELT is the source, but it sends no CORS header, so we try, in order:
     0. the PRISM gateway's own PULSE proxy, if this page is served by it;
     1. GDELT directly;
     2. public CORS proxies.
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
    list.push({ k: 'pulse', u: '/api/pulse/search?q=' + encodeURIComponent(term) + dr });
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

function tryEndpoint(u: string, ms: number, userSignal?: AbortSignal): Promise<Article[]> {
  const to = typeof AbortSignal !== 'undefined' && AbortSignal.timeout ? AbortSignal.timeout(ms || 9000) : undefined;
  let sig: AbortSignal | undefined;
  if (userSignal && typeof AbortSignal !== 'undefined' && (AbortSignal as any).any)
    sig = (AbortSignal as any).any([to, userSignal].filter(Boolean)); // timeout OR user-stop
  else sig = userSignal || to;
  return fetch(u, { signal: sig, headers: { Accept: 'application/json' } })
    .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
    .then((t) => {
      let j: any;
      try { j = JSON.parse(t); } catch { throw new Error('non-JSON'); } // HTML error page → skip
      return parseArticles(j);
    });
}

// PRODUCTION RULE: returns REAL articles or throws — never fabricates.
export function fetchTerm(term: string, dfrom?: string, dto?: string, userSignal?: AbortSignal): Promise<Article[]> {
  const eps = newsEndpoints(term, dfrom, dto);
  const aborted = () => !!(userSignal && userSignal.aborted);
  return new Promise((resolve, reject) => {
    let i = 0, lastErr: any;
    (function next() {
      if (aborted()) { const a: any = new Error('stopped'); a.name = 'AbortError'; return reject(a); }
      if (i >= eps.length) return reject(lastErr || new Error('all sources failed'));
      const ep = eps[i++];
      tryEndpoint(ep.u, ep.k === 'pulse' ? 30000 : 9000, userSignal) // local server fetches the web — give it room
        .then((arts) => { if (ep.k === 'pulse') PULSE_UP = true; resolve(arts); })
        .catch((e) => {
          if (aborted()) { e.name = 'AbortError'; return reject(e); } // user stopped — don't try more
          if (ep.k === 'pulse') PULSE_UP = false; // static host — stop asking
          lastErr = e; next();
        });
    })();
  });
}

/* ---------------- the scan ---------------- */
const coName = (code: string) => (db().clients?.[code]?.name) || code || '';

export function termsFor(code: string): string[] {
  return [db().clients?.[code]?.name].concat(watch()[code] || []).filter(Boolean) as string[];
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
