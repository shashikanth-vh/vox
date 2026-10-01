/**
 * Two readings of one transcript, side by side on a swipe: the Default model's
 * report and the Regional model's, each a full card, the reviewer swipes between
 * them and approves the one they trust. Fields where the two readings differ
 * carry a stripe and a one-line "the other says", so the choice is made without
 * swiping back and forth. Nothing is filed until the reviewer approves.
 */

import { useEffect, useRef, useState } from 'react';
import { ENGINE_UI } from '../../../services/voxService';
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

type Line = { path: string; label: string; text: string; conf?: string; other: string; differs: boolean };

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
        differs: text.trim().toLowerCase() !== otherText.trim().toLowerCase() });
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

function ReadingCard({ engine, registry, mine, other, otherEngine }: {
  engine: string; registry: VoxRegistry; mine: VoxReport; other: VoxReport | null; otherEngine: string;
}) {
  const lines = readingLines(registry, mine, other);
  const differs = lines.filter((l) => l.differs).length;
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
        <div>
          <div style={{ fontSize: 15, fontWeight: 700 }}>{ui.label}</div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>{ui.vendor}</div>
          <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4 }}>
            {[confs.high ? `${confs.high} high` : '', confs.medium ? `${confs.medium} medium` : '',
              confs.low ? `${confs.low} low` : ''].filter(Boolean).join(' · ') || 'no confidence marks'}
            {lanes ? ` · ${lanes}` : ''}
          </div>
        </div>
        <span className="chip" style={differs ? { color: 'var(--warn)', borderColor: 'rgba(245,181,73,0.4)' } : {}}>
          {differs ? `${differs} differ` : 'same as other'}</span>
      </div>
      {lines.map((l) => (
        <div key={l.path} style={{ padding: '9px 0 9px 10px', borderBottom: '1px solid var(--line)',
          borderLeft: `3px solid ${l.differs ? 'var(--warn)' : 'transparent'}`, marginLeft: -10 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted)', letterSpacing: '.05em',
            textTransform: 'uppercase', display: 'flex', gap: 8, alignItems: 'center' }}>
            {l.label}
            {l.conf && l.conf !== 'n/a' && <span className={`conf-dot ${dotCls(l.conf)}`} title={l.conf} />}
            {l.differs && <span className="chip" style={{ color: 'var(--warn)', borderColor: 'rgba(245,181,73,0.4)' }}>differs</span>}
          </div>
          <div style={{ fontSize: 13.5, marginTop: 2, whiteSpace: 'pre-wrap' }}>
            {l.text || <span style={{ color: 'var(--muted)' }}>— not captured</span>}
          </div>
          {l.differs && (
            <div style={{ marginTop: 6, fontSize: 12, color: 'var(--muted)', background: 'var(--bg-3)',
              borderRadius: 8, padding: '6px 8px' }}>
              <b style={{ color: 'var(--text)', fontWeight: 600 }}>{otherUi.label} says:</b>{' '}
              {l.other || <i>nothing</i>}
            </div>
          )}
        </div>
      ))}
    </section>
  );
}

export default function VoxCompareDeck({ row, registry, stats, busy, onPick, onBack }: {
  row: VoxConversation; registry: VoxRegistry; stats: VoxEngineStats | null; busy: boolean;
  onPick: (engine: string, thenApprove: boolean) => void; onBack: () => void;
}) {
  const primary = row.engine || 'default';
  const alt = row.engine_alt || (primary === 'default' ? 'regional' : 'default');
  const cards: { engine: string; report: VoxReport; other: VoxReport | null; otherEngine: string }[] = [
    { engine: primary, report: row.structured_report as VoxReport, other: row.structured_report_alt as VoxReport, otherEngine: alt },
    { engine: alt, report: row.structured_report_alt as VoxReport, other: row.structured_report as VoxReport, otherEngine: primary },
  ];
  // Default on the left whichever engine was primary, so the swipe always reads
  // the same way: Default ← → Regional.
  cards.sort((a) => (a.engine === 'default' ? -1 : 1));
  const deckRef = useRef<HTMLDivElement | null>(null);
  const [idx, setIdx] = useState(0);
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
  const differs = lines.filter((l) => l.differs).length;
  const agree = lines.length - differs;
  const current = cards[idx] || cards[0];
  const ui = ENGINE_UI[current.engine] || { label: current.engine, vendor: '' };
  const total = stats?.dual_runs_approved || 0;

  return (
    <div className="app-body no-tabs" style={{ padding: '12px 16px 160px' }}>
      <button className="review-back" onClick={onBack}>‹ All conversations</button>
      <div className="status-pill ready">Two readings · pick one</div>
      <div style={{ fontSize: 13, lineHeight: 1.45, marginBottom: 10 }}>
        Both models read the same transcript. They agree on <b>{agree}</b> field{agree === 1 ? '' : 's'}
        {differs ? <> and differ on <b style={{ color: 'var(--warn)' }}>{differs}</b>, striped on each card</> : ''}.
        Swipe to compare, then approve the one you trust.
      </div>
      {total > 0 && stats && (
        <div style={{ fontSize: 11.5, color: 'var(--muted)', marginBottom: 10 }}>
          So far the firm has approved {ENGINE_UI.default.label} {stats.approved.default} time{stats.approved.default === 1 ? '' : 's'} and
          {' '}{ENGINE_UI.regional.label} {stats.approved.regional} time{stats.approved.regional === 1 ? '' : 's'} when both read a take.
        </div>
      )}

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
            other={c.other} otherEngine={c.otherEngine} />))}
      </div>
      <div style={{ textAlign: 'center', fontSize: 12, color: 'var(--muted)', marginTop: 10 }}>
        {idx === 0 ? `Swipe left to see the ${ENGINE_UI[cards[1].engine].label} reading →`
          : `← Swipe right to go back to the ${ENGINE_UI[cards[0].engine].label} reading`}
      </div>

      <div className="review-action-bar" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}>
        <button className="approve-pill" disabled={busy} onClick={() => onPick(current.engine, true)}>
          <span>Approve the {ui.label} reading</span>
        </button>
        <button className="btn btn-ghost" style={{ width: '100%', padding: '8px' }} disabled={busy}
          onClick={() => onPick(current.engine, false)}>
          Use this reading, review the fields first
        </button>
      </div>
    </div>
  );
}
