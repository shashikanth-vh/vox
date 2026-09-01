import { useState, useRef } from 'react';
import { Box, Button, MenuItem, Select, TextField, Typography, Tooltip, Snackbar, Alert, Popover, useMediaQuery } from '@mui/material';
import ExportBar from '../../components/common/ExportBar';
import {
  syndicationService, SYN_TERM, SYN_CLOSED, lenderNext, lenderLabel,
  MATRIX_LABELS, MATRIX_COLORS, MATRIX_PRESETS, ST2DOT,
} from '../../services/syndicationService';
import { clientsService } from '../../services/clientsService';
import { db } from '../../api/atlasStore';
import { daysSince, fmt } from '../../utils/format';
import { useSearch } from '../../context/SearchContext';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { tokens } from '../../theme';

// Same phone breakpoint the navbar, bottom nav and card tables use.
const MOBILE_QUERY = '(max-width:760px)';

type Scope = 'Live' | 'Closed' | 'All';
interface MF { states: number[]; dwell: number | ''; preset: string; noout: boolean; }

export default function MatrixView({ onOpenCompany }: { onOpenCompany: (code: string) => void }) {
  const { search } = useSearch();
  const { user } = useAuth();
  const ro = !can(user.roles, 'advanceMatrix');
  const [scope, setScope] = useState<Scope>('Live');
  const [mf, setMf] = useState<MF>({ states: [], dwell: '', preset: '', noout: false });
  const [, force] = useState(0);
  const [msg, setMsg] = useState('');
  const drag = useRef<string | null>(null);
  // In-context company search (the navbar search also applies; this one lives next
  // to the grid) and the column collapse. Review decision: the full lender master is
  // the DEFAULT view — narrowing to the engaged lenders is the user's explicit click,
  // never automatic (an auto-collapsed grid hid the market and read as data loss).
  const [q, setQ] = useState('');
  const [inPlayOnly, setInPlayOnly] = useState(false);
  // The cell popover: read-only roles get the story (status, dwell, note, history);
  // advanceMatrix roles also get the LEGAL next steps — Sanctioned captures the
  // allocation (₹ Cr), Declined the reason, exactly what the register demands.
  const [pop, setPop] = useState<{ c: string; id: string; l: string; el: HTMLElement } | null>(null);
  const [target, setTarget] = useState('');
  const [note, setNote] = useState('');
  const [amount, setAmount] = useState('');
  // Status-free remark editing (the manual tracker's Remarks column); null = closed.
  const [remark, setRemark] = useState<string | null>(null);
  // A phone can hold four dot columns of this matrix at best, so below the breakpoint
  // each company becomes a card whose ENGAGED lenders are tappable status chips (the
  // same popover behind them). "+ N more" per card reaches the untouched rest of the
  // lender master — the full market stays one tap away, never the default clutter.
  const isMobile = useMediaQuery(MOBILE_QUERY);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const match = (name: string, code: string) => {
    const g = search.trim().toLowerCase(); const lq = q.trim().toLowerCase();
    const hit = (t: string) => name.toLowerCase().includes(t) || code.toLowerCase().includes(t);
    return (!g || hit(g)) && (!lq || hit(lq));
  };

  const order = syndicationService.lenderOrder();
  // ONE ROW PER MANDATE — the chase list's own granularity. A company running two
  // syndications shows two adjacent rows, so every dot belongs to exactly one
  // mandate, the scope toggle hides a finished campaign without hiding the live
  // one, and a bank approached on both mandates keeps two independent cells.
  // Cells derive from the chase-list lender statuses (ST2DOT) so the views never
  // drift; a dot click writes back through the lender status of ITS OWN mandate.
  const cellObj = (r: any, l: string) => {
    const e = (r.lenders || []).find((x: any) => x.name === l && !x.ex && x.st);
    return e ? { s: ST2DOT[e.st] || 1, st: e.st, since: e.since } : null;
  };
  const cellS = (r: any, l: string) => cellObj(r, l)?.s ?? 0;
  const cellDays = (r: any, l: string) => { const o = cellObj(r, l); return o?.since ? daysSince(o.since) : null; };
  const live = (r: any) => !SYN_TERM.includes(r.status) && !SYN_CLOSED.includes(r.status);
  const closed = (r: any) => SYN_CLOSED.includes(r.status);
  // The row label's second line: the mandate's own number (when it differs from the
  // company code), its ask and its status — what tells sibling rows apart.
  const subOf = (r: any) => `${r.id && r.id !== r.code ? r.id + ' · ' : ''}₹${fmt(Number(r.amt) || 0, 1)} Cr${r.status ? ' · ' + r.status : ''}`;

  const anyFilter = () => mf.noout || mf.states.length > 0 || (mf.dwell !== '' && mf.dwell != null);
  const cellHit = (r: any, l: string) => {
    const o = cellObj(r, l);
    // State 0 = Un-Assigned: the bank is on the grid but not in play on this
    // mandate. It matches only the explicit Un-Assigned chip (dwell is meaningless
    // for a cell with no clock running).
    if (!o) return mf.states.includes(0);
    if (mf.states.length && !mf.states.includes(o.s)) return false;
    if (mf.dwell !== '' && mf.dwell != null && (daysSince(o.since) ?? 0) < +mf.dwell) return false;
    return mf.states.length > 0 || mf.dwell !== '';
  };

  // mandate list
  let rows = (db().syn as any[]).filter((r) => match(clientsService.get(r.code).name, r.code));
  if (scope === 'Live') rows = rows.filter(live);
  if (scope === 'Closed') rows = rows.filter(closed);

  // state counts within scope (before state filtering); index 0 counts the
  // Un-Assigned cells — FI-master banks not yet in play on that mandate.
  const cnt = Array(11).fill(0);
  rows.forEach((r) => order.forEach((l) => { const s = cellS(r, l); if (s) cnt[s]++; else cnt[0]++; }));

  if (mf.noout) rows = rows.filter((r) => live(r) && order.every((l) => !cellS(r, l)));
  else if (anyFilter()) rows = rows.filter((r) => order.some((l) => cellHit(r, l)));
  // Money order, with a company's mandates kept ADJACENT (grouped by the company's
  // combined ask) so a repeat client reads as one block of rows.
  const totals: Record<string, number> = {};
  rows.forEach((r) => { totals[r.code] = (totals[r.code] || 0) + (Number(r.amt) || 0); });
  rows.sort((a, b) => (totals[b.code] - totals[a.code])
    || String(a.code).localeCompare(String(b.code))
    || (Number(b.amt) || 0) - (Number(a.amt) || 0));

  // Visible columns: the banks in play across the visible mandates. Falls back to
  // the whole market when nothing is in play yet (a fresh book needs banks to
  // click), and the Un-Assigned chip force-expands — it highlights exactly the
  // cells the collapse would hide.
  const inPlay = order.filter((l) => rows.some((r) => cellObj(r, l)));
  const cols = inPlayOnly && !mf.states.includes(0) && inPlay.length ? inPlay : order;

  const dimOn = anyFilter() && !mf.noout;

  // Any role can OPEN a cell — the popover is the management story ("what exactly is
  // happening with this bank"): status, dwell, note, sanctioned amount, history.
  // advanceMatrix roles additionally see the legal next steps (mirroring the
  // register's transition map), with Sanctioned capturing the allocation and
  // Declined the reason — the server rejects both without their substance.
  const openPop = (e: React.MouseEvent<HTMLElement>, r: any, l: string) => {
    setTarget(''); setNote(''); setAmount(''); setRemark(null);
    setPop({ c: r.code, id: r.id, l, el: e.currentTarget });
  };
  const closePop = () => { setPop(null); setTarget(''); setNote(''); setAmount(''); setRemark(null); };
  const commit = (st: string) => {
    if (!pop) return;
    const row = syndicationService.lenderRow(pop.c, pop.l, pop.id);
    if (!row) syndicationService.addLender(pop.c, pop.l, user.full, pop.id);
    else syndicationService.setLenderStatus(pop.c, pop.l, st, user.full, {
      note: note.trim() || undefined,
      amountCr: st === 'Sanctioned' && amount ? +amount : undefined,
    }, pop.id);
    setMsg(`${pop.l} → ${lenderLabel(st)}${st === 'Sanctioned' && amount ? ` · ₹${amount} Cr` : ''}`);
    closePop(); force((n) => n + 1);
  };
  // A next-step click: outcomes pause for their substance, plain moves apply at once.
  const pick = (st: string) => {
    if (st === 'Sanctioned' || st === 'Declined' || st === 'Dropped') { setTarget(st); return; }
    commit(st);
  };
  const canConfirm = target === 'Sanctioned' ? +amount > 0 : note.trim().length > 0;
  const togState = (s: number) => setMf((p) => ({ ...p, preset: '', noout: false, states: p.states.includes(s) ? p.states.filter((x) => x !== s) : [...p.states, s] }));
  const setPreset = (id: string) => {
    const pr = MATRIX_PRESETS.find((x) => x.id === id)!;
    if (mf.preset === id) { setMf({ states: [], dwell: '', preset: '', noout: false }); return; }
    setMf({ states: pr.states.slice(), dwell: pr.dwell as any, preset: id, noout: !!(pr as any).noout });
    setScope(pr.scope);
  };
  // Drag-reorder works by NAME (the visible columns may be a filtered subset, so
  // visual indices no longer address the master order).
  const drop = (name: string) => {
    if (!drag.current || drag.current === name) return;
    const o = syndicationService.lenderOrder();
    const from = o.indexOf(drag.current); const to = o.indexOf(name);
    if (from > -1 && to > -1) syndicationService.reorderLenders(from, to);
    drag.current = null; force((n) => n + 1);
  };

  const exportCsv = () => {
    const out = [['Company', 'Group Code', 'Mandate', 'Mandate status', 'Lender', 'State', 'Since', 'Days', 'Ask Cr']];
    rows.forEach((r) => cols.forEach((l) => {
      const o = cellObj(r, l); if (!o) return;
      if (anyFilter() && !mf.noout && !cellHit(r, l)) return;
      out.push([clientsService.get(r.code).name, r.code, r.id || '', r.status || '', l,
        MATRIX_LABELS[o.s], o.since || '', String(daysSince(o.since) ?? ''), String(Number(r.amt) || 0)]);
    }));
    const csv = out.map((x) => x.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\n');
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' })); a.download = 'atlas_matrix.csv'; a.click();
  };

  const dotStyle = (s: number, dim: boolean): React.CSSProperties => ({
    width: 17, height: 17, borderRadius: '50%', display: 'inline-block', cursor: 'pointer',
    border: `1.6px solid ${s ? MATRIX_COLORS[s] : '#C9D2D6'}`, background: s ? MATRIX_COLORS[s] : '#fff',
    opacity: dim ? 0.14 : 1, transition: 'transform .1s',
  });

  // One bank on one mandate as a phone chip — dot colour + name + dwell, the same
  // popover (story + next steps) anchored to the tap.
  const lenderChip = (r: any, l: string) => {
    const s = cellS(r, l); const d = cellDays(r, l);
    const dim = dimOn && !!s && !cellHit(r, l);
    return (
      <Box key={l} onClick={(e) => openPop(e, r, l)}
        sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.6, cursor: 'pointer',
          border: `1.4px solid ${s ? MATRIX_COLORS[s] : '#C9D2D6'}`,
          bgcolor: s ? `${MATRIX_COLORS[s]}1A` : '#fff', borderRadius: 999,
          px: 1.1, py: 0.5, fontSize: 11.8, opacity: dim ? 0.25 : 1 }}>
        <Box sx={{ width: 10, height: 10, borderRadius: '50%', flex: 'none',
          bgcolor: s ? MATRIX_COLORS[s] : '#fff',
          border: `1.3px solid ${s ? MATRIX_COLORS[s] : '#C9D2D6'}` }} />
        {l}{d != null && s ? <span style={{ color: tokens.muted }}>· {d}d</span> : null}
      </Box>
    );
  };

  return (
    <Box>
      {/* scope + hint */}
      <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mb: 1, flexWrap: 'wrap' }}>
        {(['Live', 'Closed', 'All'] as Scope[]).map((f) => (
          <Button key={f} onClick={() => setScope(f)} size="small"
            variant={scope === f ? 'contained' : 'outlined'} sx={{ borderRadius: 999, minWidth: 0, px: 1.5, py: 0.2 }}>{f}</Button>
        ))}
        <TextField size="small" value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="Find company or code…"
          sx={{ width: 195, '& .MuiInputBase-input': { py: 0.55, fontSize: 12.4 } }} />
        <Button onClick={() => setInPlayOnly((v) => !v)} size="small"
          variant={inPlayOnly ? 'contained' : 'outlined'}
          title={inPlayOnly ? 'Showing only the lenders engaged on these mandates — click for the full lender master'
            : 'Showing the full lender master — click to narrow to the engaged lenders'}
          sx={{ borderRadius: 999, minWidth: 0, px: 1.5, py: 0.2, textTransform: 'none' }}>
          {inPlayOnly ? `Engaged lenders (${inPlay.length})` : `All lenders (${order.length})`}
        </Button>
        <Typography sx={{ fontSize: 11.6, color: tokens.muted, ml: 1 }}>{ro ? 'Click a dot for the full story (mirrors the Chase List). ' : 'Click a dot for the story and the next steps (writes to the Chase List). '}Drag lender columns to reorder · click a company for the profile</Typography>
      </Box>

      {/* filter bar */}
      <Box sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.2, mb: 1.2, display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
        {[1, 7, 0, 2, 3, 4, 5, 10, 6, 9, 8].map((s) => (
          <Box key={s} onClick={() => togState(s)}
            sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.8, border: `1.4px solid ${mf.states.includes(s) ? tokens.navy : tokens.line}`,
              bgcolor: mf.states.includes(s) ? '#F2F6F5' : '#fff', borderRadius: 999, px: 1.2, py: 0.5, fontSize: 12, cursor: 'pointer',
              color: mf.states.includes(s) ? tokens.ink : tokens.muted, fontWeight: mf.states.includes(s) ? 600 : 400 }}>
            <Box sx={{ width: 13, height: 13, borderRadius: '50%',
              bgcolor: s ? MATRIX_COLORS[s] : '#fff',
              border: `1.4px solid ${s ? MATRIX_COLORS[s] : '#C9D2D6'}` }} />
            {MATRIX_LABELS[s]} <b style={{ fontVariantNumeric: 'tabular-nums' }}>{cnt[s]}</b>
          </Box>
        ))}
        <Typography sx={{ fontSize: 11.6, color: tokens.muted, ml: 0.5 }}>in state</Typography>
        <Select size="small" displayEmpty value={String(mf.dwell)} onChange={(e) => setMf((p) => ({ ...p, preset: '', dwell: e.target.value === '' ? '' : +e.target.value }))} sx={{ fontSize: 12 }}>
          {[['', 'any time'], ['5', '≥ 5 days'], ['7', '≥ 7 days'], ['10', '≥ 10 days'], ['14', '≥ 14 days'], ['30', '≥ 30 days']].map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
        </Select>
        <Box sx={{ flex: 1 }} />
        {MATRIX_PRESETS.map((p) => (
          <Button key={p.id} onClick={() => setPreset(p.id)} size="small"
            variant={mf.preset === p.id ? 'contained' : 'outlined'}
            sx={{ borderRadius: 2, px: 1.2, py: 0.2, fontSize: 11.8, borderStyle: mf.preset === p.id ? 'solid' : 'dashed' }}>{p.label}</Button>
        ))}
        {anyFilter() && <Button size="small" variant="outlined" onClick={() => setMf({ states: [], dwell: '', preset: '', noout: false })}>Clear</Button>}
        <ExportBar onCsv={exportCsv} />
      </Box>

      {/* matrix — a dot grid needs desktop width; on a phone each company is a card of
          lender chips instead (same data, same popover, nothing lost) */}
      {isMobile ? (
      <Box>
        {rows.map((r) => {
          const cl = clientsService.get(r.code);
          const engaged = order.filter((l) => cellObj(r, l));
          // The Un-Assigned filter chip means "show me the untouched cells" — it
          // force-expands every card, exactly as it force-expands the grid's columns.
          const showAll = expanded[r.id] || mf.states.includes(0);
          const shown = showAll ? order : engaged;
          const rest = order.length - engaged.length;
          return (
            <Box key={r.id || r.apiId} sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.3, mb: 1 }}>
              <Box onClick={() => onOpenCompany(r.code)} sx={{ cursor: 'pointer' }}>
                <Typography sx={{ fontWeight: 700, fontSize: 13.4, lineHeight: 1.25 }}>{cl.name}</Typography>
                <Typography sx={{ fontSize: 11, color: tokens.muted, mt: 0.2 }}>{subOf(r)}</Typography>
              </Box>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.7, mt: engaged.length || showAll ? 1 : 0.6 }}>
                {shown.map((l) => lenderChip(r, l))}
                {!showAll && rest > 0 && (
                  <Box onClick={() => setExpanded((p) => ({ ...p, [r.id]: true }))}
                    sx={{ display: 'inline-flex', alignItems: 'center', border: `1.4px dashed ${tokens.line}`,
                      borderRadius: 999, px: 1.1, py: 0.5, fontSize: 11.8, color: tokens.muted, cursor: 'pointer' }}>
                    {engaged.length ? `+ ${rest} more` : `No outreach yet — pick from ${rest} lenders`}
                  </Box>
                )}
                {expanded[r.id] && !mf.states.includes(0) && (
                  <Box onClick={() => setExpanded((p) => ({ ...p, [r.id]: false }))}
                    sx={{ display: 'inline-flex', alignItems: 'center', border: `1.4px dashed ${tokens.line}`,
                      borderRadius: 999, px: 1.1, py: 0.5, fontSize: 11.8, color: tokens.muted, cursor: 'pointer' }}>
                    Show engaged only
                  </Box>
                )}
              </Box>
            </Box>
          );
        })}
        {!rows.length && <Typography sx={{ p: 3, color: tokens.muted, fontSize: 12.6 }}>No mandates match this view.</Typography>}
        <Typography sx={{ fontSize: 11.5, color: tokens.muted, p: '4px 4px 8px' }}>
          {rows.length} mandates{mf.noout ? ' live with zero lender outreach' : ''}
        </Typography>
      </Box>
      ) : (
      <Box sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: 2, overflow: 'auto', maxHeight: 'calc(100vh - 250px)' }}>
        <table style={{ borderCollapse: 'collapse', width: 'auto', fontSize: 12.6 }}>
          <thead>
            <tr>
              <th style={{ position: 'sticky', left: 0, top: 0, zIndex: 8, background: '#F7F9FA', minWidth: 210, maxWidth: 250, textAlign: 'left', padding: '8px 9px', borderRight: `1px solid ${tokens.line}`, borderBottom: `1px solid ${tokens.line}` }}>Company · {rows.length} mandates</th>
              {cols.map((l) => (
                <th key={l} draggable={!ro} title={`${l} — drag to reorder`}
                  onDragStart={() => (drag.current = l)}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={() => drop(l)}
                  style={{ height: 118, verticalAlign: 'bottom', padding: '6px 3px', cursor: ro ? 'default' : 'grab', minWidth: 36, position: 'sticky', top: 0, background: '#F7F9FA', borderBottom: `1px solid ${tokens.line}` }}>
                  <span style={{ writingMode: 'vertical-rl', transform: 'rotate(180deg)', fontSize: 10.2, display: 'inline-block', maxHeight: 108, overflow: 'hidden' }}>{l}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const cl = clientsService.get(r.code);
              return (
                <tr key={r.id || r.apiId}>
                  <td style={{ position: 'sticky', left: 0, zIndex: 6, background: '#fff', minWidth: 210, maxWidth: 250, padding: '5px 9px', borderRight: `1px solid ${tokens.line}`, borderBottom: `1px solid #EFF2F4`, whiteSpace: 'normal' }}>
                    <b style={{ cursor: 'pointer' }} onClick={() => onOpenCompany(r.code)}>{cl.name}</b>
                    <div style={{ fontSize: 10.8, color: tokens.muted }}>{subOf(r)}</div>
                  </td>
                  {cols.map((l) => {
                    const s = cellS(r, l); const d = cellDays(r, l);
                    const dim = dimOn && !!s && !cellHit(r, l);
                    return (
                      <td key={l} style={{ textAlign: 'center', padding: '5px 3px', borderBottom: `1px solid #EFF2F4` }}>
                        <Tooltip title={`${l} · ${lenderLabel(cellObj(r, l)?.st || '') || MATRIX_LABELS[s]}${d != null && s ? ' · ' + d + 'd' : ''} · click for details`} arrow>
                          <span style={dotStyle(s, dim)} onClick={(e) => openPop(e, r, l)}
                            onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.transform = 'scale(1.22)')}
                            onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.transform = 'none')} />
                        </Tooltip>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
            {!rows.length && <tr><td style={{ padding: 26, color: tokens.muted }}>No mandates match this view.</td></tr>}
          </tbody>
        </table>
        <Typography sx={{ fontSize: 11.5, color: tokens.muted, p: '7px 11px' }}>
          {rows.length} mandates{mf.noout ? ' live with zero lender outreach' : ''}
        </Typography>
      </Box>
      )}
      {/* Cell popover — the story of one bank on one mandate, and (advanceMatrix) the moves */}
      <Popover open={!!pop} anchorEl={pop?.el ?? null} onClose={closePop}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
        transformOrigin={{ vertical: 'top', horizontal: 'center' }}
        slotProps={{ paper: { sx: { p: 1.6, width: 300, borderRadius: 2 } } }}>
        {pop && (() => {
          const row = syndicationService.lenderRow(pop.c, pop.l, pop.id);
          const st = row?.st || '';
          const s = st ? (ST2DOT[st] || 1) : 0;
          const d = row?.since ? daysSince(row.since) : null;
          const nexts = ro ? [] : lenderNext(st, row?.heldFrom);
          // History rows arrive in two shapes: the register appends {from,to,at,by},
          // local echoes push {st,t,by} — render either, newest first.
          const hist = (row?.h || []).slice(-4).reverse().map((x: any) => ({
            what: x.to != null
              ? `${lenderLabel(x.from || '') || '—'} → ${lenderLabel(x.to)}`
              : (lenderLabel(x.st || '') || '—'),
            when: (x.at || x.t || '').slice(0, 10), who: x.by || '',
          }));
          return (
            <Box>
              <Typography sx={{ fontWeight: 700, fontSize: 13.2 }}>{pop.l}</Typography>
              <Typography sx={{ fontSize: 11.4, color: tokens.muted, mb: 1 }}>
                {clientsService.get(pop.c).name}{pop.id && pop.id !== pop.c ? ` · ${pop.id}` : ''}
              </Typography>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mb: 0.6 }}>
                <Box sx={{ width: 12, height: 12, borderRadius: '50%', bgcolor: s ? MATRIX_COLORS[s] : '#fff', border: `1.4px solid ${s ? MATRIX_COLORS[s] : '#C9D2D6'}` }} />
                <Typography sx={{ fontSize: 12.4, fontWeight: 600 }}>{lenderLabel(st) || 'Un-Assigned'}</Typography>
                {d != null && st && <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>· {d}d in state</Typography>}
              </Box>
              {row?.amt != null && (
                <Typography sx={{ fontSize: 12, color: MATRIX_COLORS[5], fontWeight: 600, mb: 0.4 }}>Approved ₹{fmt(row.amt, 1)} Cr</Typography>
              )}
              {/* Conversation snapshot — the same two lines the chase list shows. */}
              {row?.chaseNote && (
                <Typography sx={{ fontSize: 11.4, mb: 0.3 }}>
                  <b style={{ color: '#0E6E8A', fontSize: 9.8, letterSpacing: 0.4 }}>CHASE</b>
                  {row.chased ? <span style={{ color: tokens.muted }}> {daysSince(row.chased)}d ago</span> : null}
                  <i style={{ color: tokens.ink }}> “{row.chaseNote}”</i>
                </Typography>
              )}
              {row?.replyNote && (
                <Typography sx={{ fontSize: 11.4, mb: 0.3 }}>
                  <b style={{ color: '#0E8A68', fontSize: 9.8, letterSpacing: 0.4 }}>REPLY</b>
                  {row.resp ? <span style={{ color: tokens.muted }}> {daysSince(row.resp)}d ago</span> : null}
                  <i style={{ color: tokens.ink }}> “{row.replyNote}”</i>
                </Typography>
              )}
              {row?.note && row.note !== row.chaseNote && row.note !== row.replyNote
                && <Typography sx={{ fontSize: 11.6, color: tokens.muted, mb: 0.4, whiteSpace: 'pre-wrap' }}>{row.note}</Typography>}
              {hist.length > 0 && (
                <Box sx={{ borderTop: `1px solid ${tokens.line}`, mt: 0.8, pt: 0.8 }}>
                  {hist.map((x, i) => (
                    <Typography key={i} sx={{ fontSize: 11, color: tokens.muted, lineHeight: 1.7 }}>
                      {x.what}{x.when ? ` · ${x.when}` : ''}{x.who ? ` · ${x.who}` : ''}
                    </Typography>
                  ))}
                </Box>
              )}
              {/* The manual tracker's Remarks column: editable at ANY stage, status
                  untouched — "Reply awaited", "No update", "call with promoters". */}
              {!ro && row && (remark == null ? (
                <Button size="small" sx={{ mt: 0.4, px: 0.5, fontSize: 11.4, textTransform: 'none' }}
                  onClick={() => setRemark(row.note || '')}>
                  {row.note ? 'Edit remark' : 'Add remark'}
                </Button>
              ) : (
                <Box sx={{ mt: 0.8 }}>
                  <TextField size="small" fullWidth multiline minRows={2} label="Remark" autoFocus
                    value={remark} onChange={(e) => setRemark(e.target.value)} />
                  <Box sx={{ display: 'flex', gap: 0.8, mt: 0.8, justifyContent: 'flex-end' }}>
                    <Button size="small" onClick={() => setRemark(null)}>Cancel</Button>
                    <Button size="small" variant="contained" onClick={() => {
                      syndicationService.setLenderNote(pop.c, pop.l, remark.trim(), user.full, pop.id);
                      setRemark(null); force((n) => n + 1);
                    }}>Save remark</Button>
                  </Box>
                </Box>
              ))}
              {!ro && !row && (
                <Button fullWidth size="small" variant="contained" sx={{ mt: 1 }} onClick={() => commit('Identified')}>
                  Identify this lender
                </Button>
              )}
              {!ro && row && !target && nexts.length > 0 && (
                <Box sx={{ borderTop: `1px solid ${tokens.line}`, mt: 0.8, pt: 1, display: 'flex', flexWrap: 'wrap', gap: 0.7 }}>
                  {nexts.map((n) => (
                    <Button key={n} size="small" variant="outlined" onClick={() => pick(n)}
                      sx={{ borderRadius: 999, px: 1.3, py: 0.2, fontSize: 11.6, textTransform: 'none',
                        color: MATRIX_COLORS[ST2DOT[n] || 1], borderColor: MATRIX_COLORS[ST2DOT[n] || 1] }}>
                      {lenderLabel(n)}
                    </Button>
                  ))}
                </Box>
              )}
              {!ro && target && (
                <Box sx={{ borderTop: `1px solid ${tokens.line}`, mt: 0.8, pt: 1 }}>
                  <Typography sx={{ fontSize: 12, fontWeight: 600, mb: 0.8, color: MATRIX_COLORS[ST2DOT[target] || 1] }}>
                    {target === 'Sanctioned' ? 'Sanction — record the allocation'
                      : target === 'Dropped' ? 'Drop — record why we walked away' : 'Decline — record the reason'}
                  </Typography>
                  {target === 'Sanctioned' && (
                    <TextField size="small" fullWidth type="number" label="Sanctioned amount (₹ Cr)" value={amount}
                      onChange={(e) => setAmount(e.target.value)} sx={{ mb: 0.8 }} autoFocus
                      inputProps={{ min: 0, step: 0.5 }} />
                  )}
                  <TextField size="small" fullWidth multiline minRows={2} value={note}
                    label={target === 'Declined' ? 'Why did they decline? (required)'
                      : target === 'Dropped' ? 'Why are we dropping this bank? (required)' : 'Note (optional)'}
                    onChange={(e) => setNote(e.target.value)} autoFocus={target !== 'Sanctioned'} />
                  <Box sx={{ display: 'flex', gap: 0.8, mt: 1, justifyContent: 'flex-end' }}>
                    <Button size="small" onClick={() => setTarget('')}>Back</Button>
                    <Button size="small" variant="contained" disabled={!canConfirm} onClick={() => commit(target)}
                      sx={{ bgcolor: MATRIX_COLORS[ST2DOT[target] || 1] }}>
                      {target === 'Sanctioned' ? 'Confirm sanction'
                        : target === 'Dropped' ? 'Confirm drop' : 'Confirm decline'}
                    </Button>
                  </Box>
                </Box>
              )}
              {!ro && row && !target && !nexts.length && (
                <Typography sx={{ fontSize: 11.4, color: tokens.muted, mt: 0.8 }}>Terminal state — no further moves.</Typography>
              )}
            </Box>
          );
        })()}
      </Popover>
      <Snackbar open={!!msg} autoHideDuration={2600} onClose={() => setMsg('')}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="info" onClose={() => setMsg('')} sx={{ fontSize: 12.4 }}>{msg}</Alert>
      </Snackbar>
    </Box>
  );
}
