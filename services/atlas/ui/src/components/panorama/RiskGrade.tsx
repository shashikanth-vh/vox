import { useState } from 'react';
import {
  Box, Button, Chip, CircularProgress, IconButton, Popover, Tooltip, Typography,
} from '@mui/material';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import RefreshIcon from '@mui/icons-material/Refresh';
import DownloadIcon from '@mui/icons-material/Download';
import ShieldOutlinedIcon from '@mui/icons-material/ShieldOutlined';
import { tokens } from '../../theme';
import { panoramaService, type RiskBand, type RiskCoverage, type RiskFlag, type RiskGrade }
  from '../../services/panoramaService';

/**
 * Company 360 risk grade — RED / AMBER / GREEN from the ATLAS client rubric.
 *
 * The chip carries the band's colour and the score; PROVISIONAL (financials or
 * banking missing) keeps the colour but draws dashed, so a thin-data grade never
 * looks as settled as a full one. The (i) opens the report in a credit reader's
 * order: how much of the file the grade stands on, the red flags PRISM read from the
 * files itself, the key figures, the hard triggers (Unverified when their evidence was
 * not read), what to do — and the rubric's pillar working last, folded away.
 */

export const BAND_COLOR: Record<RiskBand, string> = {
  GREEN: tokens.ok, AMBER: tokens.warn, RED: tokens.bad,
};
const BAND_BG: Record<RiskBand, string> = {
  GREEN: tokens.okBg, AMBER: '#FBF3E3', RED: tokens.badBg,
};

function stamp(iso: string): string {
  return new Date(iso).toLocaleString('en-IN',
    { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export function RiskGradeChip({ grade, busy, error, canGrade, onGrade }: {
  grade: RiskGrade | null; busy: boolean; error: string; canGrade: boolean;
  onGrade: (refresh: boolean) => void;
}) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);

  if (busy) {
    return (
      <Chip size="small" variant="outlined"
        icon={<CircularProgress size={12} sx={{ ml: '6px !important' }} />}
        label="Grading… (up to a minute)"
        sx={{ height: 24, fontSize: 11, color: tokens.muted }} />
    );
  }
  if (!grade) {
    return (
      <Tooltip title={error || 'Grade this client RED / AMBER / GREEN with the ATLAS '
        + 'client rubric, from everything PRISM holds on it'}>
        <span>
          <Chip size="small" variant="outlined" clickable={canGrade}
            disabled={!canGrade} icon={<ShieldOutlinedIcon sx={{ fontSize: 14 }} />}
            label={error ? 'Grade failed — retry' : 'Grade risk'}
            onClick={() => onGrade(false)}
            sx={{ height: 24, fontSize: 11, fontWeight: 700,
              color: error ? tokens.bad : tokens.tealHi,
              borderColor: error ? tokens.bad : tokens.tealHi }} />
        </span>
      </Tooltip>
    );
  }
  const c = BAND_COLOR[grade.rating];
  return (
    <Box sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.2 }}>
      <Tooltip title={(grade.borderline ? 'Near a band line — the colour is not firm. ' : '')
        + (grade.verdict || grade.label)}>
        <Chip size="small" label={`${grade.provisional ? `${grade.rating} · PROV.` : grade.rating}`
          + ` · ${grade.borderline ? '≈' : ''}${grade.score}/100`}
          sx={{ height: 24, fontSize: 11, fontWeight: 800, letterSpacing: '.02em',
            color: grade.provisional ? c : '#fff',
            bgcolor: grade.provisional ? BAND_BG[grade.rating] : c,
            border: `1.5px ${grade.provisional ? 'dashed' : 'solid'} ${c}` }} />
      </Tooltip>
      <Tooltip title="How the AI determined this grade">
        <IconButton size="small" aria-label="How this grade was determined"
          onClick={(e) => setAnchor(e.currentTarget)} sx={{ p: '3px' }}>
          <InfoOutlinedIcon sx={{ fontSize: 17, color: c }} />
        </IconButton>
      </Tooltip>
      <Popover open={!!anchor} anchorEl={anchor} onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
        marginThreshold={8}
        PaperProps={{ sx: { width: 560, maxWidth: 'calc(100vw - 16px)', maxHeight: '78vh',
          p: { xs: '12px 12px', sm: '14px 16px' }, borderRadius: '12px', overflowX: 'hidden' } }}>
        <RiskGradeDetails grade={grade} onRegrade={canGrade
          ? () => { setAnchor(null); onGrade(true); } : undefined} />
      </Popover>
    </Box>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <Typography sx={{ fontSize: 10, fontWeight: 800, letterSpacing: '.07em',
      color: '#44535B', mt: 1.2, mb: 0.4 }}>{children}</Typography>
  );
}

function Bullets({ items, color }: { items: string[]; color?: string }) {
  if (!items.length) {
    return <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>None given.</Typography>;
  }
  return (
    <Box component="ul" sx={{ m: 0, pl: 2.2 }}>
      {items.map((t, i) => (
        <Typography component="li" key={i} sx={{ fontSize: 11.8, color: color || '#17252B',
          lineHeight: 1.45 }}>{t}</Typography>))}
    </Box>
  );
}

const TRIGGER_COLOR = { Hit: tokens.bad, Clear: tokens.ok, Unknown: tokens.muted };
/** "Unknown" means the evidence was not read — say that, never imply a clear. */
const TRIGGER_WORD = { Hit: 'HIT', Clear: 'CLEAR', Unknown: 'UNVERIFIED' };
const FLAG_COLOR = { red: tokens.bad, amber: tokens.warn, info: '#1F6FA8' };
const COVER: Record<RiskCoverage['status'], { color: string; word: string }> = {
  'read': { color: tokens.ok, word: 'read' },
  'partly read': { color: tokens.warn, word: 'partly read' },
  'not read': { color: tokens.bad, word: 'not read' },
  'no file': { color: tokens.muted, word: 'no file' },
  'none found': { color: tokens.muted, word: 'none found' },
};
// Key figures, in the order a credit reader scans them.
const FIGURES: [string, string, (v: number) => string][] = [
  ['revenue', 'Revenue (₹ L)', (v) => v.toLocaleString('en-IN', { maximumFractionDigits: 0 })],
  ['growth', 'Growth', (v) => `${v > 0 ? '+' : ''}${v.toFixed(1)}%`],
  ['ebitda', 'EBITDA (₹ L)', (v) => v.toLocaleString('en-IN', { maximumFractionDigits: 0 })],
  ['ebitda_pct', 'EBITDA margin', (v) => `${(v * 100).toFixed(1)}%`],
  ['pat', 'PAT (₹ L)', (v) => v.toLocaleString('en-IN', { maximumFractionDigits: 0 })],
  ['tnw', 'Net worth (₹ L)', (v) => v.toLocaleString('en-IN', { maximumFractionDigits: 0 })],
  ['current_ratio', 'Current ratio', (v) => `${v.toFixed(2)}×`],
  ['tol_tnw', 'TOL / TNW', (v) => `${v.toFixed(2)}×`],
  ['td_ebitda', 'Debt / EBITDA', (v) => `${v.toFixed(2)}×`],
  ['dscr', 'DSCR', (v) => `${v.toFixed(2)}×`],
];

function Coverage({ rows }: { rows: RiskCoverage[] }) {
  return (
    <Box sx={{ display: 'grid', gap: 0.5,
      gridTemplateColumns: { xs: 'minmax(0,1fr)', sm: 'minmax(0,1fr) minmax(0,1fr)' } }}>
      {rows.map((r) => {
        const c = COVER[r.status] || COVER['no file'];
        const tip = [
          r.read.length ? `Read: ${r.read.join('; ')}` : '',
          ...r.unread.map((u) => `Not read: ${u.name}${u.reason ? ` — ${u.reason}` : ''}`),
          r.note || '',
        ].filter(Boolean).join('\n');
        return (
          <Tooltip key={r.area} title={<span style={{ whiteSpace: 'pre-line' }}>{tip || 'No file in the Data Register'}</span>}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.7, minWidth: 0,
              border: `1px solid ${tokens.line}`, borderLeft: `3px solid ${c.color}`,
              borderRadius: '6px', px: 0.8, py: 0.4 }}>
              <Typography sx={{ fontSize: 11.4, fontWeight: 700, flex: 1, minWidth: 0 }} noWrap>
                {r.area}</Typography>
              {r.checked_by_prism && (
                <Chip size="small" label="checked" variant="outlined"
                  sx={{ height: 16, fontSize: 9, color: tokens.tealHi, borderColor: tokens.tealHi }} />)}
              <Typography sx={{ fontSize: 10.5, fontWeight: 800, color: c.color }}>
                {c.word.toUpperCase()}</Typography>
            </Box>
          </Tooltip>);
      })}
    </Box>
  );
}

function Flags({ flags }: { flags: RiskFlag[] }) {
  if (!flags.length) {
    return <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
      No red or amber flags in the files PRISM could check — see what was not read above.</Typography>;
  }
  return (
    <Box sx={{ display: 'grid', gap: 0.6 }}>
      {flags.map((f, i) => (
        <Box key={i} sx={{ display: 'flex', gap: 0.8, alignItems: 'flex-start' }}>
          <Box sx={{ width: 9, height: 9, borderRadius: '50%', mt: '5px', flexShrink: 0,
            bgcolor: FLAG_COLOR[f.severity] }} />
          <Box sx={{ minWidth: 0 }}>
            <Typography sx={{ fontSize: 11.8, fontWeight: 700, color: '#17252B' }}>
              {f.title}
              {f.trigger && <Chip size="small" label="hard trigger" sx={{ height: 16, fontSize: 9,
                ml: 0.6, color: '#fff', bgcolor: tokens.bad }} />}</Typography>
            <Typography sx={{ fontSize: 11.2, color: '#44535B', lineHeight: 1.4,
              overflowWrap: 'anywhere' }}>{f.detail}
              <span style={{ color: tokens.muted }}> · {f.area} · {f.source}</span></Typography>
          </Box>
        </Box>))}
    </Box>
  );
}

function Figures({ kf }: { kf: NonNullable<NonNullable<RiskGrade['evidence']>['key_figures']> }) {
  const rows = FIGURES.filter(([k]) => kf.rows[k]);
  return (
    <Box sx={{ overflowX: 'auto' }}>
      <Box component="table" sx={{ borderCollapse: 'collapse', fontSize: 11.2, width: '100%',
        '& td, & th': { px: 0.8, py: 0.3, borderBottom: `1px solid ${tokens.line}`,
          textAlign: 'right', whiteSpace: 'nowrap' },
        '& th:first-of-type, & td:first-of-type': { textAlign: 'left' } }}>
        <thead><tr><th style={{ color: tokens.muted, fontWeight: 600 }}>{kf.source}</th>
          {kf.years.map((y) => <th key={y} style={{ fontWeight: 700 }}>{y}</th>)}</tr></thead>
        <tbody>{rows.map(([k, label, fmt]) => (
          <tr key={k}><td style={{ fontWeight: 600 }}>{label}</td>
            {kf.years.map((y) => {
              const v = kf.rows[k][y];
              return <td key={y}>{v == null ? '—' : fmt(v)}</td>;
            })}</tr>))}</tbody>
      </Box>
    </Box>
  );
}

function More({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <Box sx={{ mt: 1 }}>
      <Button size="small" onClick={() => setOpen(!open)}
        sx={{ fontSize: 10.5, fontWeight: 800, letterSpacing: '.07em', color: '#44535B',
          px: 0, minWidth: 0 }}>{open ? '▾' : '▸'} {title}</Button>
      {open && <Box sx={{ mt: 0.4 }}>{children}</Box>}
    </Box>
  );
}

function ReportButton({ grade }: { grade: RiskGrade }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  return (
    <Tooltip title={err || 'The full grading report as a Word document'}>
      <Button size="small" startIcon={busy ? <CircularProgress size={12} /> : <DownloadIcon />}
        disabled={busy} sx={{ fontSize: 11, color: err ? tokens.bad : undefined }}
        onClick={async () => {
          setBusy(true); setErr('');
          try { await panoramaService.downloadRiskReport(grade); }
          catch { setErr('The report could not be created. Check the connection and try again.'); }
          finally { setBusy(false); }
        }}>Report</Button>
    </Tooltip>
  );
}

/** The IRG report — the full credit document behind the grade (a job of minutes). */
function FullReportButton({ grade }: { grade: RiskGrade }) {
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState('');
  const [err, setErr] = useState('');
  return (
    <Tooltip title={err || (busy ? stage : 'The IRG report: every document read the CAM way, '
      + 'the grade on it and each pillar explained, as a Word document (about 20 minutes)')}>
      <Button size="small" startIcon={busy ? <CircularProgress size={12} /> : <DownloadIcon />}
        disabled={busy} sx={{ fontSize: 11, color: err ? tokens.bad : undefined }}
        onClick={async () => {
          setBusy(true); setErr(''); setStage('Starting');
          try {
            await panoramaService.fullIrgReport(grade.entity_id ?? null, grade.company,
              (s, d, t) => setStage(t ? `${s} ${d}/${t}` : s));
          } catch (e: any) {
            setErr(e?.message || 'The IRG report could not be created.');
          } finally { setBusy(false); }
        }}>{busy ? stage || 'Working' : 'Full IRG report'}</Button>
    </Tooltip>
  );
}

export function RiskGradeDetails({ grade, onRegrade }: {
  grade: RiskGrade; onRegrade?: () => void;
}) {
  const c = BAND_COLOR[grade.rating];
  const inp = grade.inputs;
  const ev = grade.evidence;
  const reds = ev?.flags.filter((f) => f.severity === 'red').length ?? 0;
  const ambers = ev?.flags.filter((f) => f.severity === 'amber').length ?? 0;
  const hits = grade.hard_triggers.filter((t) => t.status === 'Hit').length;
  const unverified = grade.hard_triggers.filter((t) => t.status === 'Unknown').length;
  // Every file on record that the grade could not read is a document to ask for again.
  const unreadAsks = (ev?.coverage ?? []).flatMap((r) => r.unread.map((u) =>
    `${u.name} — on file but not read${u.reason ? ` (${u.reason})` : ''}; send it in a readable form`));
  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <Typography sx={{ fontSize: 15, fontWeight: 800, color: c }}>{grade.label}</Typography>
        <Typography sx={{ fontSize: 13, fontWeight: 700 }}>{grade.score}/100</Typography>
        <Tooltip title={grade.confidence_detail
          ? `60% × data coverage (${grade.confidence_detail.data_coverage}% of the rubric's `
            + `weight had data) + 40% × agreement (${grade.confidence_detail.agreement}% — `
            + `the readings spread ${grade.confidence_detail.spread} points)`
          : 'The model\'s own confidence'}>
          <Chip size="small" variant="outlined"
            label={`Confidence ${grade.confidence_pct != null
              ? `${grade.confidence_pct}%` : grade.confidence}`}
            sx={{ height: 20, fontSize: 10.5, fontWeight: 700 }} />
        </Tooltip>
        <Chip size="small" variant="outlined" label={grade.action}
          sx={{ height: 20, fontSize: 10.5, fontWeight: 700 }} />
        <Box sx={{ flex: 1 }} />
        <ReportButton grade={grade} />
        <FullReportButton grade={grade} />
        {onRegrade && (
          <Button size="small" startIcon={<RefreshIcon />} onClick={onRegrade}
            sx={{ fontSize: 11 }}>Regrade</Button>)}
      </Box>
      {grade.verdict && (
        <Typography sx={{ fontSize: 12.4, mt: 0.6, color: '#17252B' }}>{grade.verdict}</Typography>)}
      {grade.borderline && (
        <Typography sx={{ fontSize: 11.4, mt: 0.6, color: tokens.warn, fontWeight: 600 }}>
          Near a band line (45 / 70): a small change in the evidence can change the colour
          — treat it as {grade.score < 45 ? 'RED/AMBER' : grade.score < 70 ? 'AMBER, close to a line'
            : 'GREEN/AMBER'}.</Typography>)}
      {grade.unchanged && (
        <Typography sx={{ fontSize: 11.4, mt: 0.6, color: tokens.tealHi, fontWeight: 600 }}>
          No change since the last grade: the same records, files and news were read, so the
          grade stands. It is recomputed when any of them change.</Typography>)}
      {grade.runs && grade.runs.length > 1 && (
        <Typography sx={{ fontSize: 10.8, color: tokens.muted, mt: 0.4 }}>
          {grade.runs.length} independent readings ({grade.runs.map((r) => r.score).join(' · ')});
          each pillar takes the median of its scores.</Typography>)}
      {ev && (
        <Typography sx={{ fontSize: 11.6, mt: 0.8, fontWeight: 700 }}>
          <span style={{ color: tokens.bad }}>{reds} red flag{reds === 1 ? '' : 's'}</span>
          {' · '}<span style={{ color: tokens.warn }}>{ambers} amber</span>
          {' · '}<span style={{ color: hits ? tokens.bad : '#17252B' }}>{hits} hard trigger{hits === 1 ? '' : 's'} hit</span>
          {' · '}<span style={{ color: tokens.muted }}>{unverified} unverified</span></Typography>)}

      {ev && ev.coverage.length > 0 && (<>
        <Label>WHAT THE GRADE STANDS ON — evidence read, by area (hover for files)</Label>
        <Coverage rows={ev.coverage} /></>)}

      {ev && (<>
        <Label>RED FLAGS — read by PRISM from the files</Label>
        <Flags flags={ev.flags} /></>)}

      {ev?.key_figures && (<>
        <Label>KEY FIGURES</Label>
        <Figures kf={ev.key_figures} /></>)}

      <Label>HARD RED TRIGGERS</Label>
      {grade.hard_triggers.length ? grade.hard_triggers.map((t, i) => (
        <Box key={i} sx={{ display: 'flex', gap: 0.8, alignItems: 'baseline', mb: 0.3 }}>
          <Typography sx={{ fontSize: 10.5, fontWeight: 800, minWidth: 74,
            color: TRIGGER_COLOR[t.status] }}>{TRIGGER_WORD[t.status] || t.status}</Typography>
          <Typography sx={{ fontSize: 11.4 }}>{t.trigger}
            {t.evidence && <span style={{ color: tokens.muted }}> — {t.evidence}</span>}
          </Typography>
        </Box>)) : <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
          Not reported.</Typography>}

      <Box sx={{ display: 'grid', gridTemplateColumns: { xs: 'minmax(0,1fr)', sm: 'minmax(0,1fr) minmax(0,1fr)' }, columnGap: 2 }}>
        <Box><Label>TOP RISKS</Label><Bullets items={grade.risks} /></Box>
        <Box><Label>TOP MITIGANTS</Label><Bullets items={grade.mitigants} /></Box>
      </Box>

      {grade.conditions.length > 0 && (<>
        <Label>CONDITIONS PRECEDENT (IF PROCEEDING)</Label><Bullets items={grade.conditions} /></>)}

      <Label>DOCUMENTS TO REQUEST</Label>
      <Bullets items={[...grade.data_gaps, ...unreadAsks]} />

      <Label>WHAT WOULD MOVE IT</Label>
      <Typography sx={{ fontSize: 11.6 }}>▲ {grade.move_up || '—'}</Typography>
      <Typography sx={{ fontSize: 11.6 }}>▼ {grade.move_down || '—'}</Typography>

      <More title="HOW THE SCORE WAS BUILT — seven weighted pillars">
      {/* Four columns on a desk; on a phone the evidence drops under its pillar
          on a full-width row instead of being squeezed off the right edge. */}
      <Box sx={{ display: 'grid', columnGap: 1, rowGap: 0.6, alignItems: 'start', fontSize: 11.4,
        gridTemplateColumns: { xs: 'minmax(0,1fr) 44px 36px', sm: 'minmax(150px,1.3fr) 52px 44px 1.1fr' } }}>
        {['Pillar', 'Score', 'Pts', 'Evidence'].map((h) => (
          <Typography key={h} sx={{ fontSize: 10, fontWeight: 700, color: tokens.muted,
            display: h === 'Evidence' ? { xs: 'none', sm: 'block' } : 'block' }}>
            {h}</Typography>))}
        {grade.pillars.map((pl) => (
          <Box key={pl.key} sx={{ display: 'contents' }}>
            <Typography sx={{ fontSize: 11.6, fontWeight: 600, minWidth: 0 }}>
              {pl.key}. {pl.name} <span style={{ color: tokens.muted, fontWeight: 400 }}>
                ({pl.weight}%)</span>
              {!pl.available && <Chip size="small" label="no data · not scored" variant="outlined"
                sx={{ height: 16, fontSize: 9, ml: 0.5, color: tokens.warn,
                  borderColor: tokens.warn }} />}
            </Typography>
            <Typography sx={{ fontSize: 11.6 }}>{pl.available ? `${pl.score}/10` : '—'}</Typography>
            <Typography sx={{ fontSize: 11.6, fontWeight: 700 }}>{pl.available ? pl.weighted : '—'}</Typography>
            <Typography sx={{ fontSize: 11, color: '#44535B', lineHeight: 1.4, minWidth: 0,
              overflowWrap: 'anywhere', gridColumn: { xs: '1 / -1', sm: 'auto' },
              pb: { xs: 0.6, sm: 0 }, borderBottom: { xs: `1px solid ${tokens.line}`, sm: 'none' } }}>
              {pl.evidence}</Typography>
          </Box>))}
      </Box>
      <Typography sx={{ fontSize: 10.8, color: tokens.muted, mt: 0.6 }}>
        Bands: GREEN ≥ 70 · AMBER 45–69 · RED &lt; 45, scored over the pillars that
        have data. Any hard trigger → RED. No financials or banking → never GREEN
        (PROVISIONAL).</Typography>
      </More>

      {grade.adjustments.length > 0 && (
        <More title={`WHERE THE RULES CORRECTED THE MODEL (${grade.adjustments.length})`}>
          <Bullets items={grade.adjustments} color={tokens.warn} /></More>)}

      <More title={`FILES (${inp.documents?.length ?? 0}) AND OTHER INPUTS`}>
      <Typography sx={{ fontSize: 11, color: '#44535B', lineHeight: 1.5 }}>
        Register: {inp.lending_lines} lending line(s), {inp.platform_deals} platform
        deal(s), {inp.asset_monetisation} asset monetisation, {inp.interactions} interaction(s),
        {' '}{inp.register_documents} Data Register file(s)
        {inp.register_sections_hidden.length > 0
          && ` · hidden from your role: ${inp.register_sections_hidden.join(', ')}`}.
        <br />News (PULSE): {inp.news_items} headline(s) about the firm and its key people,
        last 90 days{inp.news_note ? ` — ${inp.news_note}` : ''}.
      </Typography>
      {inp.documents && inp.documents.length > 0 ? (
        <Box sx={{ mt: 0.6, display: 'grid', gap: 0.3 }}>
          {inp.documents.map((d, i) => (
            <Typography key={i} sx={{ fontSize: 11, color: d.used ? '#17252B' : tokens.muted }}>
              <b style={{ color: d.used ? tokens.ok : tokens.warn }}>{d.used ? 'READ' : 'NOT READ'}</b>
              {' '}{d.name}
              <span style={{ color: tokens.muted }}>
                {' · '}{d.section || '—'}
                {d.engines.length ? ` · read by ${d.engines.join(', ')}` : ''}
                {d.cached ? ' · cached' : ''}{d.note ? ` · ${d.note}` : ''}
                {!d.used && d.reason ? ` · ${d.reason}` : ''}</span>
            </Typography>))}
        </Box>
      ) : (
        <Typography sx={{ fontSize: 11, color: tokens.muted, mt: 0.4 }}>
          Documents: none — {inp.document_note || 'no files in the graded sections'}.</Typography>
      )}
      </More>
      <Typography sx={{ fontSize: 10.5, color: tokens.muted, mt: 0.8 }}>
        {grade.engine} · rubric {grade.prompt} ({grade.prompt_version}) · graded
        {' '}{stamp(grade.generated_at)} by {grade.generated_by}
        {grade.cached ? ' · saved result' : ''}. AI-assisted: a desk aid, not a
        credit decision.</Typography>
    </Box>
  );
}
