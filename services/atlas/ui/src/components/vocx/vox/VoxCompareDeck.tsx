/**
 * Two readings of one transcript, side by side on a swipe: the Default model's
 * report and the Regional model's, each a full card, the reviewer swipes between
 * them and approves the one they trust. A fact the two readings disagree on
 * (a number, a date, a choice, a list of names) is striped amber with a one-line
 * "the other says"; prose the two merely WORD differently (summary, remarks,
 * discussion points) is marked quietly, never striped — a reviewer should not
 * have to adjudicate seventeen paraphrases to pick a report. The deck opens on
 * the differences; "All fields" shows the whole reading. Nothing is filed until
 * the reviewer approves.
 */

import { useEffect, useRef, useState } from 'react';
import { ENGINE_UI, needsYou } from '../../../services/voxService';
import type { VoxConversation, VoxEngineStats, VoxRegistry, VoxReport } from '../../../services/voxService';

const ACRONYMS = new Set(['ppa', 'epc', 'spv', 'ipp', 'ev', 'bess', 'lc', 'nbfc']);
const label = (v: string) => v.split('_').map((w, i) =>
  ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase()
    : i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w).join(' ');

/** One cell's value as the reader sees it; '' when the model had nothing. */
export const readingText = (v: any): string => {
  if (v === null || v === undefined || v === '') return '';
  if (Array.isArray(v)) {
    return v.map((x) => (typeof x === 'string' ? x
      : (x?.action ?? x?.label ?? (x?.lender ? `${x.lender}: ${x.status || x.reply || ''}` : ''))))
      .map((x) => String(x).trim()).filter(Boolean).join('; ');
  }
  if (typeof v === 'object') return Object.entries(v).map(([k, x]) => `${label(k)}: ${x}`).join('; ');
  if (typeof v === 'boolean') return v ? 'Yes' : 'No';
  return String(v);
};

const dotCls = (conf?: string) =>
  conf === 'high' ? 'hi' : conf === 'medium' ? 'md' : conf === 'low' ? 'lo' : 'na';

export type Verdict = 'same' | 'worded' | 'differs';
type Line = { path: string; label: string; text: string; conf?: string; other: string; verdict: Verdict };

const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').replace(/\s+/g, ' ').trim();
const num = (s: string): number | null => {
  const m = s.replace(/,/g, '').match(/-?\d+(?:\.\d+)?/);
  return m ? parseFloat(m[0]) : null;
};
/** Prose fields: the two models will never phrase these alike, so a difference
 *  there is "worded differently", not a disagreement to adjudicate. */
const PROSE = new Set(['meeting_summary', 'key_discussion_points', 'opportunity_assessment',
  'competitive_intelligence', 'data_quality_flags', 'remarks', 'notes', 'present_requirement',
  'offer_notes', 'next_steps', 'opportunity_score_override_reason']);

/** How two readings of one field compare, by the kind of field it is. */
export function compareField(def: any, mine: any, theirs: any): Verdict {
  const a = readingText(mine), b = readingText(theirs);
  if (norm(a) === norm(b)) return 'same';
  if (!a || !b) return PROSE.has(def.key) ? 'worded' : 'differs';
  const ctl = def.control || 'text';
  if (ctl === 'number') {
    const x = num(a), y = num(b);
    return x !== null && y !== null && Math.abs(x - y) < 1e-9 ? 'same' : 'differs';
  }
  if (ctl === 'date' || ctl === 'dropdown') return 'differs';
  if (ctl === 'chips') {
    const sa = new Set(norm(a).split(' ')), sb = new Set(norm(b).split(' '));
    return [...sa].every((t) => sb.has(t)) && [...sb].every((t) => sa.has(t)) ? 'same' : 'differs';
  }
  if (ctl === 'list' || Array.isArray(mine) || Array.isArray(theirs)) {
    if (PROSE.has(def.key)) return 'worded';
    // a list of names (attendees, lenders): the same count with the same leading
    // words is one list worded two ways; a different count is a real difference
    const la = (Array.isArray(mine) ? mine : [mine]).map((x) => norm(readingText(x)).split(' ')[0]).filter(Boolean);
    const lb = (Array.isArray(theirs) ? theirs : [theirs]).map((x) => norm(readingText(x)).split(' ')[0]).filter(Boolean);
    if (la.length !== lb.length) return 'differs';
    return la.every((t) => lb.includes(t)) ? 'worded' : 'differs';
  }
  if (ctl === 'action_items' || ctl === 'lender_updates') return 'worded';
  if (PROSE.has(def.key) || ctl === 'textarea') return 'worded';
  // free text: the same number inside both (a quantum, a size) is one fact worded twice
  const x = num(a), y = num(b);
  if (x !== null && y !== null && Math.abs(x - y) < 1e-9) return 'worded';
  return a.length > 60 || b.length > 60 ? 'worded' : 'differs';
}

/** The lines of one reading, in registry order, each knowing what the other
 *  reading said for the same field. A field neither reading filled is skipped. */
export function readingLines(registry: VoxRegistry, mine: VoxReport, other: VoxReport | null): Line[] {
  const out: Line[] = [];
  const push = (blockKey: string, defs: any[]) => {
    const cells = (blockKey === 'common' ? mine.common : (mine as any)[blockKey]) || {};
    const theirs = (blockKey === 'common' ? other?.common : (other as any)?.[blockKey]) || {};
    for (const def of defs || []) {
      const cell = cells[def.key];
      const text = readingText(cell?.value);
      const otherText = readingText(theirs[def.key]?.value);
      if (!text && !otherText) continue;
      out.push({ path: `${blockKey}.${def.key}`, label: def.label || label(def.key), text,
        conf: cell?.confidence, other: otherText,
        verdict: other ? compareField(def, cell?.value, theirs[def.key]?.value) : 'same' });
    }
  };
  push('common', registry.common as any[]);
  const lanes = new Set<string>([...(mine.detected_use_cases || []), ...(other?.detected_use_cases || [])]);
  for (const uc of lanes) {
    const block = (registry.blocks || {})[uc];
    if (block?.fields?.length) push(uc, block.fields);
  }
  return out;
}

const CHIP_WARN: React.CSSProperties = { color: 'var(--warn)', borderColor: 'rgba(245,181,73,0.4)' };

function ReadingCard({ engine, registry, mine, other, otherEngine, view, needs }: {
  engine: string; registry: VoxRegistry; mine: VoxReport; other: VoxReport | null; otherEngine: string;
  view: 'differences' | 'all'; needs: number;
}) {
  const lines = readingLines(registry, mine, other);
  const differs = lines.filter((l) => l.verdict === 'differs').length;
  const worded = lines.filter((l) => l.verdict === 'worded').length;
  const agreed = lines.filter((l) => l.verdict === 'same');
  const shown = view === 'all' ? lines : lines.filter((l) => l.verdict !== 'same');
  const confs = lines.reduce((a, l) => { const k = l.conf || 'n/a'; a[k] = (a[k] || 0) + 1; return a; },
    {} as Record<string, number>);
  const ui = ENGINE_UI[engine] || { label: engine, vendor: '' };
  const otherUi = ENGINE_UI[otherEngine] || { label: otherEngine, vendor: '' };
  const lanes = (mine.detected_use_cases || []).map(label).join(' · ');
  return (
    <section style={{ flex: '0 0 100%', scrollSnapAlign: 'center', scrollSnapStop: 'always', minWidth: 0,
      background: 'var(--bg-2)', border: '1px solid var(--line)', borderRadius: 14, padding: '12px 14px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'flex-start',
        borderBottom: '1px solid var(--line)', paddingBottom: 10, marginBottom: 4 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 15, fontWeight: 700 }}>{ui.label}</div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>{ui.vendor}</div>
          <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4 }}>
            {[confs.high ? `${confs.high} high` : '', confs.medium ? `${confs.medium} medium` : '',
              confs.low ? `${confs.low} low` : ''].filter(Boolean).join(' · ') || 'no confidence marks'}
            {lanes ? ` · ${lanes}` : ''}
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-end', flexShrink: 0 }}>
          <span className="chip" style={differs ? CHIP_WARN : {}}>{differs ? `${differs} disagree` : 'no disagreement'}</span>
          <span className="chip" style={needs ? CHIP_WARN : {}}>{needs ? `${needs} to confirm` : 'nothing to confirm'}</span>
        </div>
      </div>
      {shown.length === 0 && (
        <div style={{ fontSize: 12.5, color: 'var(--muted)', padding: '10px 0' }}>
          The two readings agree on every field.
        </div>
      )}
      {shown.map((l) => (
        <div key={l.path} style={{ padding: '9px 0 9px 10px', borderBottom: '1px solid var(--line)',
          borderLeft: `3px solid ${l.verdict === 'differs' ? 'var(--warn)' : l.verdict === 'worded' ? 'var(--line-2)' : 'transparent'}`,
          marginLeft: -10 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted)', letterSpacing: '.05em',
            textTransform: 'uppercase', display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            {l.label}
            {l.conf && l.conf !== 'n/a' && <span className={`conf-dot ${dotCls(l.conf)}`} title={l.conf} />}
            {(l.conf === 'low' || l.conf === 'medium') && <span className="chip" style={CHIP_WARN}>confirm</span>}
            {l.verdict === 'differs' && <span className="chip" style={CHIP_WARN}>disagree</span>}
            {l.verdict === 'worded' && <span className="chip">worded differently</span>}
          </div>
          <div style={{ fontSize: 13.5, marginTop: 2, whiteSpace: 'pre-wrap' }}>
            {l.text || <span style={{ color: 'var(--muted)' }}>— not captured</span>}
          </div>
          {l.verdict !== 'same' && (
            <div style={{ marginTop: 6, fontSize: 12, color: 'var(--muted)', background: 'var(--bg-3)',
              borderRadius: 8, padding: '6px 8px' }}>
              <b style={{ color: 'var(--text)', fontWeight: 600 }}>{otherUi.label} says:</b>{' '}
              {l.other || <i>nothing</i>}
            </div>
          )}
        </div>
      ))}
      {view === 'differences' && agreed.length > 0 && (
        <div style={{ fontSize: 11.5, color: 'var(--muted)', padding: '10px 0 2px', lineHeight: 1.5 }}>
          <b style={{ color: 'var(--text)', fontWeight: 600 }}>Both agree on {agreed.length}:</b>{' '}
          {agreed.map((l) => l.label).join(' · ')}
          {worded ? <> · <i>{worded} worded differently</i></> : null}
        </div>
      )}
    </section>
  );
}

export default function VoxCompareDeck({ row, registry, stats, busy, transcript, onPick, onFixTranscript, onBack }: {
  row: VoxConversation; registry: VoxRegistry; stats: VoxEngineStats | null; busy: boolean;
  transcript: string;
  onPick: (engine: string, thenApprove: boolean) => void;
  onFixTranscript: (engine: string) => void;
  onBack: () => void;
}) {
  const primary = row.engine || 'default';
  const alt = row.engine_alt || (primary === 'default' ? 'regional' : 'default');
  const cards: { engine: string; report: VoxReport; other: VoxReport | null; otherEngine: string; needs: number }[] = [
    { engine: primary, report: row.structured_report as VoxReport, other: row.structured_report_alt as VoxReport, otherEngine: alt, needs: 0 },
    { engine: alt, report: row.structured_report_alt as VoxReport, other: row.structured_report as VoxReport, otherEngine: primary, needs: 0 },
  ];
  cards.forEach((c) => { c.needs = needsYou(registry, c.report).length; });
  // Default on the left whichever engine was primary, so the swipe always reads
  // the same way: Default ← → Regional.
  cards.sort((a) => (a.engine === 'default' ? -1 : 1));
  const deckRef = useRef<HTMLDivElement | null>(null);
  const [idx, setIdx] = useState(0);
  const [view, setView] = useState<'differences' | 'all'>('differences');
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  useEffect(() => {
    const deck = deckRef.current;
    if (!deck) return;
    let t: ReturnType<typeof setTimeout> | null = null;
    const onScroll = () => {
      if (t) clearTimeout(t);
      t = setTimeout(() => {
        const x = deck.scrollLeft + deck.clientWidth / 2;
        let best = 0, bd = Infinity;
        Array.from(deck.children).forEach((c, i) => {
          const el = c as HTMLElement;
          const d = Math.abs(el.offsetLeft + el.offsetWidth / 2 - x);
          if (d < bd) { bd = d; best = i; }
        });
        setIdx(best);
      }, 60);
    };
    deck.addEventListener('scroll', onScroll, { passive: true });
    return () => { deck.removeEventListener('scroll', onScroll); if (t) clearTimeout(t); };
  }, []);
  const goto = (i: number) => {
    const deck = deckRef.current; const el = deck?.children[i] as HTMLElement | undefined;
    if (deck && el) deck.scrollTo({ left: el.offsetLeft - deck.offsetLeft, behavior: 'smooth' });
  };
  const lines = readingLines(registry, cards[0].report, cards[0].other);
  const differs = lines.filter((l) => l.verdict === 'differs').length;
  const worded = lines.filter((l) => l.verdict === 'worded').length;
  const agree = lines.length - differs - worded;
  const current = cards[idx] || cards[0];
  const ui = ENGINE_UI[current.engine] || { label: current.engine, vendor: '' };
  const total = stats?.dual_runs_approved || 0;

  return (
    <div className="app-body no-tabs" style={{ padding: '12px 16px 170px' }}>
      <button className="review-back" onClick={onBack}>‹ All conversations</button>
      <div className="status-pill ready">Two readings · pick one</div>
      <div style={{ fontSize: 13, lineHeight: 1.45, marginBottom: 8 }}>
        Both models read the same transcript. They agree on <b>{agree}</b> field{agree === 1 ? '' : 's'}
        {worded ? <>, word <b>{worded}</b> differently</> : ''}
        {differs ? <> and disagree on <b style={{ color: 'var(--warn)' }}>{differs}</b>, striped on each card</> : ''}.
        Swipe to compare, then approve the one you trust — or take it into the review to confirm its fields first.
      </div>
      {total > 0 && stats && (
        <div style={{ fontSize: 11.5, color: 'var(--muted)', marginBottom: 8 }}>
          So far the firm has approved {ENGINE_UI.default.label} {stats.approved.default} time{stats.approved.default === 1 ? '' : 's'} and
          {' '}{ENGINE_UI.regional.label} {stats.approved.regional} time{stats.approved.regional === 1 ? '' : 's'} when both read a take.
        </div>
      )}
      <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
        {(['differences', 'all'] as const).map((v) => (
          <button key={v} type="button" className="chip" style={{ cursor: 'pointer', opacity: view === v ? 1 : 0.5,
            fontWeight: view === v ? 700 : 400, outline: view === v ? '1px solid currentColor' : 'none' }}
            onClick={() => setView(v)}>{v === 'differences' ? 'Differences only' : 'All fields'}</button>
        ))}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', margin: '6px 0 8px' }}>
        <button className="btn btn-ghost" style={{ width: 36, height: 36, padding: 0, borderRadius: '50%' }}
          disabled={idx === 0} onClick={() => goto(idx - 1)} aria-label="Previous reading">‹</button>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 13, fontWeight: 700 }}>{ui.label}</div>
          <div style={{ display: 'flex', gap: 6, justifyContent: 'center', marginTop: 4 }}>
            {cards.map((c, i) => (
              <span key={c.engine} style={{ display: 'block', height: 8, borderRadius: 4,
                width: i === idx ? 22 : 8, background: i === idx ? 'var(--accent)' : 'var(--line-2)',
                transition: 'width .15s' }} />))}
          </div>
        </div>
        <button className="btn btn-ghost" style={{ width: 36, height: 36, padding: 0, borderRadius: '50%' }}
          disabled={idx === cards.length - 1} onClick={() => goto(idx + 1)} aria-label="Next reading">›</button>
      </div>

      <div ref={deckRef} style={{ display: 'flex', gap: 12, overflowX: 'auto', scrollSnapType: 'x mandatory',
        scrollbarWidth: 'none', margin: '0 -16px', padding: '0 16px', WebkitOverflowScrolling: 'touch' } as any}>
        {cards.map((c) => (
          <ReadingCard key={c.engine} engine={c.engine} registry={registry} mine={c.report}
            other={c.other} otherEngine={c.otherEngine} view={view} needs={c.needs} />))}
      </div>
      <div style={{ textAlign: 'center', fontSize: 12, color: 'var(--muted)', marginTop: 10 }}>
        {idx === 0 ? `Swipe left to see the ${ENGINE_UI[cards[1].engine].label} reading →`
          : `← Swipe right to go back to the ${ENGINE_UI[cards[0].engine].label} reading`}
      </div>

      {/* The one transcript both models read. A misheard name is fixed HERE,
          before either reading is picked: both models re-read the corrected text. */}
      <div style={{ marginTop: 14, background: 'var(--bg-2)', border: '1px solid var(--line)', borderRadius: 12, padding: '10px 14px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <button type="button" className="btn btn-ghost" style={{ width: 'auto', padding: '2px 0', border: 'none', fontSize: 13, fontWeight: 600 }}
            onClick={() => setTranscriptOpen((o) => !o)}>
            {transcriptOpen ? '▾' : '▸'} Full transcript · read by both models
          </button>
          <button type="button" className="btn btn-ghost" style={{ width: 'auto', padding: '4px 10px', fontSize: 12 }}
            disabled={busy} onClick={() => onFixTranscript(current.engine)}>
            Fix the transcript &amp; re-read
          </button>
        </div>
        {transcriptOpen && (
          <div style={{ fontSize: 12.5, whiteSpace: 'pre-wrap', marginTop: 8, maxHeight: 'min(50vh, 420px)',
            overflowY: 'auto', color: 'var(--text-2)', lineHeight: 1.55 }}>{transcript || '— no transcript —'}</div>
        )}
      </div>

      <div className="review-action-bar" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}>
        <button className="approve-pill" disabled={busy} onClick={() => onPick(current.engine, true)}>
          <span>Approve the {ui.label} reading</span>
        </button>
        <button className="btn btn-ghost" style={{ width: '100%', padding: '8px' }} disabled={busy}
          onClick={() => onPick(current.engine, false)}>
          {current.needs
            ? `Use this reading · confirm its ${current.needs} flagged field${current.needs === 1 ? '' : 's'} first`
            : 'Use this reading, review the fields first'}
        </button>
      </div>
    </div>
  );
}
