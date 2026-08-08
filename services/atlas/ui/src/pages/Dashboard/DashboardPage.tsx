import { useMemo, useState } from 'react';
import { Box, Paper, Typography, Table, TableHead, TableRow, TableCell, TableBody, Button, Chip, ToggleButtonGroup, ToggleButton } from '@mui/material';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip as RTooltip, Legend, CartesianGrid } from 'recharts';
import { computeDashboard } from './compute';
import { hydrateBook } from '../../services/bookHydration';
import type { BarRow, DrillRow, LensRow, VelRow, BankRow } from './compute';
import { useAuth } from '../../auth/AuthContext';
import { viewAccess } from '../../auth/rbac';
import ExportBar from '../../components/common/ExportBar';
import { fmt } from '../../utils/format';
import { tokens } from '../../theme';

const today = () => new Date().toISOString().slice(0, 10);
const sum = (a: DrillRow[]) => a.reduce((s, x) => s + x.amt, 0);

// ---------- small building blocks ----------
function SectionTitle({ children }: { children: React.ReactNode }) {
  return <Typography sx={{ fontSize: 11, letterSpacing: '.6px', textTransform: 'uppercase', color: '#475569', fontWeight: 700, mt: 2, mb: 0.75 }}>{children}</Typography>;
}
// v17 `.v4-subttl` — teal, uppercase, bold card subtitle.
function CardTitle({ children }: { children: React.ReactNode }) {
  return <Typography sx={{ fontSize: 11, fontWeight: 800, letterSpacing: '.6px', textTransform: 'uppercase', color: tokens.teal, mb: 1 }}>{children}</Typography>;
}
function Card({ children, sx }: { children: React.ReactNode; sx?: object }) {
  return <Paper variant="outlined" sx={{ borderColor: tokens.line, borderRadius: '12px', p: 1.5, mb: 0.75, ...sx }}>{children}</Paper>;
}
function HeroTile({ accent, value, label, sub }: { accent: string; value: string; label: string; sub?: string }) {
  return (
    <Paper variant="outlined" sx={{ borderColor: tokens.line, borderRadius: '13px', p: '14px 15px 12px', position: 'relative', overflow: 'hidden' }}>
      <Box sx={{ position: 'absolute', top: 0, left: 0, right: 0, height: 3, background: accent }} />
      <Typography sx={{ fontSize: 23, fontWeight: 800, letterSpacing: '-.4px', color: tokens.navy, fontVariantNumeric: 'tabular-nums' }}>{value}</Typography>
      <Typography sx={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.6px', color: tokens.muted, mt: 0.4 }}>{label}</Typography>
      {sub && <Typography sx={{ fontSize: 11, color: tokens.muted, mt: 0.2 }}>{sub}</Typography>}
    </Paper>
  );
}
function Bars({ rows, color = tokens.navy }: { rows: BarRow[]; color?: string }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  if (!rows.length) return <Typography sx={{ fontSize: 12, color: tokens.muted, p: 1 }}>No data.</Typography>;
  return (
    <>
      {rows.map((r) => (
        <Box key={r.label} sx={{ display: 'flex', alignItems: 'center', gap: 1.25, py: 0.5 }}>
          <Box sx={{ flex: '0 0 180px', fontSize: 12, color: '#334155', [`@media (max-width:760px)`]: { flexBasis: 120 } }}>{r.label}</Box>
          <Box sx={{ flex: 1, bgcolor: '#f1f5f9', height: 14, borderRadius: '99px', overflow: 'hidden' }}>
            <Box sx={{ height: '100%', width: `${(r.value / max) * 100}%`, bgcolor: color }} />
          </Box>
          <Box sx={{ flex: '0 0 180px', textAlign: 'right', fontSize: 11.5, color: tokens.muted, [`@media (max-width:760px)`]: { flexBasis: 90 } }}>
            {r.value}{r.note ? ` ${r.note}` : ''}
          </Box>
        </Box>
      ))}
    </>
  );
}
function DataTable({ head, rows, boldFirst = true }: { head: string[]; rows: (string | number)[][]; boldFirst?: boolean }) {
  return (
    <Box sx={{ overflowX: 'auto', '&::-webkit-scrollbar': { display: 'none' }, scrollbarWidth: 'none', msOverflowStyle: 'none' }}>
      <Table size="small">
        <TableHead>
          <TableRow>{head.map((h, i) => (
            <TableCell key={h} align={i === 0 ? 'left' : 'right'} sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.4px', color: '#475569', fontWeight: 700, bgcolor: '#F4F7F8', whiteSpace: 'nowrap' }}>{h}</TableCell>
          ))}</TableRow>
        </TableHead>
        <TableBody>
          {rows.map((r, i) => (
            <TableRow key={i} hover>
              {r.map((c, j) => <TableCell key={j} align={j === 0 ? 'left' : 'right'} sx={{ fontSize: 12.4, whiteSpace: 'nowrap' }}>{j === 0 && boldFirst ? <b>{c}</b> : c}</TableCell>)}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Box>
  );
}

function VelocityChart({ rows }: { rows: VelRow[] }) {
  const d = rows.map((r) => ({ stage: r.stage, Median: Math.round((r.median || 0) * 10) / 10, P75: Math.round((r.p75 || 0) * 10) / 10 }));
  // Days axis in 0.2 steps (.2, .4, .6, .8, 1 …), formatted without a leading zero.
  const maxV = Math.max(0, ...d.map((x) => Math.max(x.Median, x.P75)));
  const top = Math.max(1, Math.ceil(maxV / 0.2) * 0.2);
  const ticks: number[] = [];
  for (let t = 0; t <= top + 1e-9; t += 0.2) ticks.push(Math.round(t * 10) / 10);
  const fmtDay = (v: number) => (v === 0 ? '0' : String(v).replace(/^0/, ''));
  return (
    <ResponsiveContainer width="100%" height={Math.max(190, rows.length * 46)}>
      <BarChart data={d} layout="vertical" margin={{ left: 6, right: 12, top: 4, bottom: 4 }}>
        {/* horizontal + vertical gridlines behind the bars (v17 look). */}
        <CartesianGrid strokeDasharray="3 3" stroke="#E5E9EC" horizontal vertical />
        <XAxis type="number" domain={[0, top]} ticks={ticks} allowDecimals tick={{ fontSize: 11 }} tickFormatter={fmtDay} />
        <YAxis type="category" dataKey="stage" width={158} tick={{ fontSize: 10.5 }} />
        <RTooltip formatter={(v: any) => `${v}d`} />
        <Legend verticalAlign="bottom" wrapperStyle={{ fontSize: 11 }} />
        <Bar dataKey="Median" fill={tokens.teal} radius={[0, 3, 3, 0]} />
        <Bar dataKey="P75" fill="#94a3b8" radius={[0, 3, 3, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function BankChart({ rows }: { rows: BankRow[] }) {
  const d = rows.map((b) => ({ name: b.name, Pursued: b.pursued, Approved: b.sanc, Declined: b.decl }));
  // v17: vertical columns — banks on the X axis, counts on the Y axis.
  return (
    <ResponsiveContainer width="100%" height={320}>
      <BarChart data={d} margin={{ left: 6, right: 12, top: 4, bottom: 72 }}>
        <XAxis dataKey="name" interval={0} angle={-45} textAnchor="end" height={72} tick={{ fontSize: 9 }} />
        <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
        <RTooltip />
        <Legend verticalAlign="top" wrapperStyle={{ fontSize: 11 }} />
        <Bar dataKey="Pursued" fill={tokens.navy} radius={[3, 3, 0, 0]} />
        <Bar dataKey="Approved" fill="#2E7D4F" radius={[3, 3, 0, 0]} />
        <Bar dataKey="Declined" fill="#B3432B" radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---------- closed / pipeline tiles with a shared, full-width drilldown ----------
// A row of KPI tiles; clicking one opens its deal table BELOW the whole row (full
// page width). Only one table is open per section — clicking another swaps it.
const THREE = { display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 1.5, '@media (max-width:760px)': { gridTemplateColumns: '1fr' } } as const;

function DrillSection({ tiles, valColor }: { tiles: { label: string; rows: DrillRow[] }[]; valColor: string }) {
  const [open, setOpen] = useState<number | null>(null);
  return (
    <>
      <Box sx={THREE}>
        {tiles.map((t, i) => (
          <Paper key={t.label} variant="outlined" onClick={() => setOpen(open === i ? null : i)}
            sx={{ borderColor: open === i ? tokens.teal : tokens.line, borderRadius: '12px', p: 1.75, cursor: 'pointer',
              '&:hover': { boxShadow: '0 6px 12px rgba(0,0,0,.06)' } }}>
            <Typography sx={{ fontSize: 12, color: tokens.muted }}>{t.label}</Typography>
            <Box sx={{ mt: 0.5, display: 'flex', alignItems: 'baseline', gap: 1 }}>
              <Typography sx={{ fontSize: 22, fontWeight: 600 }}>₹{fmt(sum(t.rows), 1)} Cr</Typography>
              <Typography sx={{ fontSize: 12, fontWeight: 500, color: valColor }}>({t.rows.length} deals)</Typography>
            </Box>
          </Paper>
        ))}
      </Box>
      {open != null && (
        <Card sx={{ mt: 0.75 }}>
          <CardTitle>{tiles[open].label}</CardTitle>
          <DataTable boldFirst={false} head={['Company', 'Sector', 'Amount']}
            rows={tiles[open].rows.map((r) => [r.name, r.sector, `₹${fmt(r.amt, 1)} Cr`])} />
        </Card>
      )}
    </>
  );
}

function LensStacks({ rows }: { rows: LensRow[] }) {
  const max = Math.max(1, ...rows.map((r) => r.total));
  const seg = (w: number, bg: string, title: string) => <Box title={title} sx={{ width: `${w}%`, height: '100%', bgcolor: bg }} />;
  return (
    <>
      {rows.map((x) => (
        <Box key={x.lens} sx={{ display: 'flex', alignItems: 'center', gap: 1.5, py: 0.75 }}>
          <Box sx={{ flex: '0 0 180px', fontSize: 13 }}><b>{x.lens}</b> <Typography component="span" sx={{ fontSize: 11.5, color: tokens.muted }}>{x.total} live</Typography></Box>
          <Box sx={{ flex: 1, display: 'flex', bgcolor: '#f1f5f9', height: 22, borderRadius: '99px', overflow: 'hidden' }}>
            {seg((x.lending / max) * 100, '#1B2A4A', `Lending: ${x.lending}`)}
            {seg((x.syn / max) * 100, '#7c3aed', `Platform Deals: ${x.syn}`)}
            {seg((x.am / max) * 100, '#d97706', `Asset Mon: ${x.am}`)}
          </Box>
          <Box sx={{ flex: '0 0 140px', textAlign: 'right', fontSize: 12, color: '#475569', fontVariantNumeric: 'tabular-nums' }}>{x.lending} · {x.syn} · {x.am}</Box>
        </Box>
      ))}
      <Box sx={{ mt: 1.25, pt: 1.25, borderTop: `1px solid ${tokens.line}`, display: 'flex', gap: 2, fontSize: 12, color: tokens.muted, flexWrap: 'wrap' }}>
        {[['Lending', '#1B2A4A'], ['Platform Deals', '#7c3aed'], ['Asset Mon', '#d97706']].map(([l, c]) => (
          <Box key={l} component="span"><Box component="span" sx={{ display: 'inline-block', width: 10, height: 10, borderRadius: '2px', bgcolor: c, mr: 0.75, verticalAlign: 'middle' }} />{l}</Box>
        ))}
      </Box>
    </>
  );
}

export default function DashboardPage() {
  const { user } = useAuth();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const person = params.get('person') || undefined;
  // Admin + Management get the cross-vertical ('full') dashboard; Head-tier roles get
  // the scoped ('inv') slice for their own vertical (Views v2.1 dashboard scope).
  const [view, setView] = useState<'inv' | 'full'>(viewAccess(user.roles, 'dash') === 'full' ? 'full' : 'inv');
  const [chartMode, setChartMode] = useState<'table' | 'chart'>('table');

  // The whole book first (every resource, every page) — a dashboard computed from
  // whichever grid pages this session had cached showed different numbers to every
  // user. staleTime matches the hydrator's TTL.
  const { data } = useQuery({ queryKey: ['dashboard-v11', person], staleTime: 30_000,
    queryFn: async () => { await hydrateBook(); return computeDashboard(person); } });
  const investor = view === 'inv';

  const twoCol = { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1.5, '@media (max-width:760px)': { gridTemplateColumns: '1fr' } } as const;
  const three = { display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 1.5, '@media (max-width:760px)': { gridTemplateColumns: '1fr' } } as const;

  const H = data?.hero;
  // v16 hero: three tiles — closed book, active pipeline, clients on register.
  const heroTiles = useMemo(() => H ? [
    { accent: `linear-gradient(90deg,${tokens.teal},${tokens.tealHi})`, value: `₹${fmt(H.closedAmt, 0)} Cr`, label: 'Closed book', sub: `${H.closedN} transactions` },
    { accent: `linear-gradient(90deg,${tokens.navy},#3A5273)`, value: `₹${fmt(H.pipeAmt, 0)} Cr`, label: 'Active pipeline', sub: `${H.pipeN} live deals` },
    { accent: `linear-gradient(90deg,#17567F,${tokens.lend})`, value: `${H.clients}`, label: 'Clients on register' },
  ] : [], [H]);

  // v16 sectioned CSV export of the dashboard.
  const exportCsv = () => {
    if (!data) return;
    const q = (v: any) => `"${String(v ?? '').replace(/"/g, '""')}"`;
    const out: string[] = [`EVAM — Dashboard,${today()}`, ''];
    const sec = (title: string, head: string[], rows: (string | number)[][]) => {
      out.push(title, head.map(q).join(','), ...rows.map((r) => r.map(q).join(',')), '');
    };
    sec('HERO', ['Metric', 'Value'], [['Closed book (Cr)', data.hero.closedAmt], ['Closed transactions', data.hero.closedN], ['Active pipeline (Cr)', data.hero.pipeAmt], ['Live deals', data.hero.pipeN], ['Clients', data.hero.clients]]);
    sec('PLATFORM DEALS FUNNEL', ['Status', 'Rows', 'Value Cr'], data.funnel.map((s) => [s.status, s.rows, s.amt]));
    sec('LENDING FUNNEL', ['Stage', 'Rows', 'Value Cr'], data.lendingFunnel.map((s) => [s.status, s.rows, s.amt]));
    sec('RM ORIGINATION', ['RM', 'Sourced', 'Active leads', 'Hot', 'Converted', 'Pipeline', 'Closed'], data.rm.map((e) => [e.name, e.sourced, e.activeLeads, e.hotLeads, e.converted, e.pipeline, e.closed]));
    sec('ANALYST THROUGHPUT', ['Analyst', 'Active lend', 'Active syn', 'Active AM', 'Closed', 'Rejected'], data.analyst.map((e) => [e.name, e.activeLend, e.activeSyn, e.activeAM, e.closed, e.rejected]));
    sec('BANK ENGAGEMENT', ['Bank', 'Pursued', 'Live', 'Approved', 'Declined'], data.bank.map((b) => [b.name, b.pursued, b.live, b.sanc, b.decl]));
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([out.join('\n')], { type: 'text/csv' }));
    a.download = 'atlas_dashboard.csv'; a.click();
  };

  if (!data) return <Typography sx={{ p: 2, color: 'text.secondary' }}>Loading dashboard…</Typography>;

  return (
    <Box>
      {/* header */}
      <Box
        sx={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-end",
          mb: 1.5,
          flexWrap: "wrap",
          gap: 1,
        }}
      >
        <Box>
          <Typography sx={{ fontSize: 20, fontWeight: 700, m: 0 }}>
            Dashboard
          </Typography>
          <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
            {today()} · Live from Register
            {person ? ` · Filtered: ${person}` : ""}
          </Typography>
        </Box>
        <Chip
          label={`Role: ${user.role}`}
          variant="outlined"
          sx={{ borderColor: tokens.line }}
        />
      </Box>

      {person && (
        <Paper
          variant="outlined"
          sx={{
            borderColor: "#93c5fd",
            borderLeft: "4px solid #2563eb",
            bgcolor: "#eff6ff",
            borderRadius: 2,
            p: "10px 14px",
            mb: 1.5,
            display: "flex",
            alignItems: "center",
            gap: 1,
            flexWrap: "wrap",
          }}
        >
          <Typography sx={{ fontSize: 13 }}>
            <b>Showing {person}’s book only.</b>{" "}
            <Typography
              component="span"
              sx={{ fontSize: 11.5, color: tokens.muted }}
            >
              Filter applied via Employees tab.
            </Typography>
          </Typography>
          <Box sx={{ flex: 1 }} />
          <Button
            size="small"
            variant="outlined"
            onClick={() => nav("/dashboard")}
          >
            Clear filter
          </Button>
        </Paper>
      )}

      {/* hero band */}
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
          gap: 1.25,
          mb: 1.5,
          "@media (max-width:760px)": { gridTemplateColumns: "repeat(2, 1fr)" },
        }}
      >
        {heroTiles.map((t) => (
          <HeroTile key={t.label} {...t} />
        ))}
      </Box>

      {/* view controls */}
      <Paper
        variant="outlined"
        sx={{
          borderColor: tokens.line,
          borderRadius: 2.5,
          p: "8px 11px",
          mb: 1.5,
          display: "flex",
          alignItems: "center",
          gap: 1,
          flexWrap: "wrap",
        }}
      >
        <ToggleButtonGroup
          exclusive
          size="small"
          value={view}
          onChange={(_, v) => v && setView(v)}
        >
          <ToggleButton value="inv">🎯 Investor view</ToggleButton>
          <ToggleButton value="full">🛠 Admin (full)</ToggleButton>
        </ToggleButtonGroup>
        <Box sx={{ flex: 1 }} />
        <ExportBar onCsv={exportCsv} />
      </Paper>

      {/* closed deals */}
      <SectionTitle>Closed Deals</SectionTitle>
      <DrillSection valColor={tokens.ok} tiles={[
        { label: 'Lending — Closed', rows: data.closed.lend },
        { label: 'Platform Deals — Closed', rows: data.closed.syn },
        { label: 'Asset Monetisation — Closed', rows: data.closed.am },
        { label: 'Lending — Sanctioned Amount', rows: data.sanctioned },
      ]} />

      {/* pipeline */}
      <SectionTitle>Pipeline</SectionTitle>
      <DrillSection valColor={tokens.warn} tiles={[
        { label: 'Lending — Pipeline', rows: data.pipe.lend },
        { label: 'Platform Deals — Pipeline', rows: data.pipe.syn },
        { label: 'Asset Monetisation — Pipeline', rows: data.pipe.am },
      ]} />

      <SectionTitle>Business line composition by climate lens</SectionTitle>
      <Card>
        {data.lens.length ? (
          <LensStacks rows={data.lens} />
        ) : (
          <Typography sx={{ fontSize: 12, color: tokens.muted }}>
            No lens data.
          </Typography>
        )}
      </Card>

      {/* v17 investor lens excludes the M&A breakup. */}
      {!investor && <>
        <SectionTitle>Mitigation &amp; Adaptation breakup</SectionTitle>
        <Card>
          <Bars color={tokens.teal} rows={data.ma} />
        </Card>
      </>}

      <SectionTitle>Sector split</SectionTitle>
      <Box sx={twoCol}>
        <Card>
          <CardTitle>Deals closed by sector</CardTitle>
          <Bars color={tokens.teal} rows={data.sectorClosed} />
        </Card>
        <Card>
          <CardTitle>Active pipeline by sector</CardTitle>
          <Bars rows={data.sector} />
        </Card>
      </Box>

      <SectionTitle>Geography split</SectionTitle>
      <Box sx={twoCol}>
        <Card>
          <CardTitle>Deals closed by geography</CardTitle>
          <Bars color={tokens.teal} rows={data.geoClosed} />
        </Card>
        <Card>
          <CardTitle>Active pipeline by geography</CardTitle>
          <Bars rows={data.geoPipe} />
        </Card>
      </Box>

      <SectionTitle>
        Platform Deals funnel: most advanced stage reached
      </SectionTitle>
      <Card>
        <DataTable
          boldFirst={false}
          head={["Status", "Rows", "Value (₹ Cr)"]}
          rows={data.funnel.map((s) => [s.status, s.rows, `₹${fmt(s.amt, 1)}`])}
        />
      </Card>

      {/* v17 investor lens excludes the lending funnel. */}
      {!investor && <>
        <SectionTitle>Lending funnel: most advanced stage reached</SectionTitle>
        <Card>
          <DataTable
            boldFirst={false}
            head={["Stage", "Rows", "Value (₹ Cr)"]}
            rows={data.lendingFunnel.map((s) => [
              s.status,
              s.rows,
              `₹${fmt(s.amt, 1)}`,
            ])}
          />
        </Card>
      </>}

      {!investor && (
        <>
          <Box
            sx={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              mt: 2,
              mb: 0.75,
              flexWrap: "wrap",
              gap: 1,
            }}
          >
            <Typography
              sx={{
                fontSize: 11,
                letterSpacing: ".6px",
                textTransform: "uppercase",
                color: "#475569",
                fontWeight: 700,
              }}
            >
              Deal velocity by stage
            </Typography>
            <ToggleButtonGroup
              exclusive
              size="small"
              value={chartMode}
              onChange={(_, v) => v && setChartMode(v)}
            >
              <ToggleButton value="table">Table</ToggleButton>
              <ToggleButton value="chart">Chart</ToggleButton>
            </ToggleButtonGroup>
          </Box>
          <Box sx={twoCol}>
            <Card>
              <DataTable
                boldFirst={false}
                head={["Transition", "N", "Median", "P75"]}
                rows={data.velocity.map((v) => [
                  v.stage,
                  v.n,
                  v.median != null ? `${v.median.toFixed(1)}d` : "—",
                  v.p75 != null ? `${v.p75.toFixed(1)}d` : "—",
                ])}
              />
            </Card>
            <Card>
              {chartMode === "chart" ? (
                <VelocityChart rows={data.velocity} />
              ) : (
                <Bars
                  color={tokens.teal}
                  rows={data.velocity.map((v) => ({
                    label: v.stage,
                    value: Math.round(v.median || 0),
                    note: "d median",
                  }))}
                />
              )}
            </Card>
          </Box>

          <SectionTitle>Lead touches by recency window</SectionTitle>
          <Card>
            <Bars color={tokens.teal} rows={data.leadTouch} />
          </Card>

          <SectionTitle>Lead sourcing across channels</SectionTitle>
          <Card>
            <Bars color={tokens.am} rows={data.sourcing} />
          </Card>

          <SectionTitle>RM origination activity</SectionTitle>
          <Card>
            <DataTable
              head={[
                "RM",
                "Sourced",
                "Active leads",
                "Converted",
                "Pipeline",
                "Closed",
              ]}
              rows={data.rm.map((e) => [
                e.name,
                e.sourced,
                `${e.activeLeads} (${e.hotLeads} hot)`,
                e.converted,
                e.pipeline,
                e.closed,
              ])}
            />
          </Card>

          <SectionTitle>Analyst throughput</SectionTitle>
          <Card>
            <DataTable
              head={[
                "Analyst",
                "Active lend",
                "Active syn",
                "Active AM",
                "Closed",
                "Rejected",
              ]}
              rows={data.analyst.map((e) => [
                e.name,
                e.activeLend,
                e.activeSyn,
                e.activeAM,
                e.closed,
                e.rejected,
              ])}
            />
          </Card>

          <SectionTitle>Bank engagement</SectionTitle>
          <Box sx={twoCol}>
            <Card>
              <DataTable
                head={["Bank", "Pursued", "Live", "Approved", "Declined"]}
                rows={data.bank.map((b) => [
                  b.name,
                  b.pursued,
                  b.live,
                  b.sanc,
                  b.decl,
                ])}
              />
            </Card>
            <Card>
              {chartMode === "chart" ? (
                <BankChart rows={data.bank} />
              ) : (
                <Bars
                  color={tokens.navy}
                  rows={data.bank.map((b) => ({
                    label: b.name,
                    value: b.pursued,
                    note: `pursued · ${b.sanc} approved`,
                  }))}
                />
              )}
            </Card>
          </Box>
        </>
      )}
    </Box>
  );
}
