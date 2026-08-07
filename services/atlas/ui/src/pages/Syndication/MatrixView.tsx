import { useState, useRef } from 'react';
import { Box, Button, MenuItem, Select, TextField, Typography, Tooltip, Snackbar, Alert, Popover } from '@mui/material';
import ExportBar from '../../components/common/ExportBar';
import {
  syndicationService, SYN_TERM, SYN_CLOSED, LENDER_NEXT, lenderLabel,
  MATRIX_LABELS, MATRIX_COLORS, MATRIX_PRESETS, ST2DOT,
} from '../../services/syndicationService';
import { clientsService } from '../../services/clientsService';
import { db } from '../../api/atlasStore';
import { daysSince, fmt } from '../../utils/format';
import { useSearch } from '../../context/SearchContext';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { tokens } from '../../theme';

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
  const drag = useRef<number | null>(null);
  // The cell popover: read-only roles get the story (status, dwell, note, history);
  // advanceMatrix roles also get the LEGAL next steps — Sanctioned captures the
  // allocation (₹ Cr), Declined the reason, exactly what the register demands.
  const [pop, setPop] = useState<{ c: string; l: string; el: HTMLElement } | null>(null);
  const [target, setTarget] = useState('');
  const [note, setNote] = useState('');
  const [amount, setAmount] = useState('');

  const match = (name: string, code: string) => {
    const q = search.trim().toLowerCase();
    return !q || name.toLowerCase().includes(q) || code.toLowerCase().includes(q);
  };

  const order = syndicationService.lenderOrder();
  // The matrix is DERIVED from the chase-list lender statuses (ST2DOT) so the two
  // views never drift. Clicking a dot is allowed for advanceMatrix roles and writes
  // back through the lender status (the source of truth) — never to a separate store.
  const mx = syndicationService.matrixFromLenders();
  const cellObj = (c: string, l: string) => mx[c]?.[l] ?? null;
  const cellS = (c: string, l: string) => cellObj(c, l)?.s ?? 0;
  const cellDays = (c: string, l: string) => { const o = cellObj(c, l); return o?.since ? daysSince(o.since) : null; };
  const synRows = (c: string) => db().syn.filter((r: any) => r.code === c);
  const live = (c: string) => synRows(c).some((r: any) => !SYN_TERM.includes(r.status));
  const closed = (c: string) => synRows(c).some((r: any) => SYN_CLOSED.includes(r.status));

  const anyFilter = () => mf.noout || mf.states.length > 0 || (mf.dwell !== '' && mf.dwell != null);
  const cellHit = (c: string, l: string) => {
    const o = cellObj(c, l); if (!o) return false;
    if (mf.states.length && !mf.states.includes(o.s)) return false;
    if (mf.dwell !== '' && mf.dwell != null && (daysSince(o.since) ?? 0) < +mf.dwell) return false;
    return mf.states.length > 0 || mf.dwell !== '';
  };

  // company list
  let codes = [...new Set(db().syn.map((r: any) => r.code))] as string[];
  codes = codes.filter((c) => match(clientsService.get(c).name, c));
  if (scope === 'Live') codes = codes.filter(live);
  if (scope === 'Closed') codes = codes.filter(closed);

  // state counts within scope (before state filtering)
  const cnt = [0, 0, 0, 0, 0, 0, 0];
  codes.forEach((c) => order.forEach((l) => { const s = cellS(c, l); if (s) cnt[s]++; }));

  if (mf.noout) codes = codes.filter((c) => live(c) && order.every((l) => !cellS(c, l)));
  else if (anyFilter()) codes = codes.filter((c) => order.some((l) => cellHit(c, l)));
  codes.sort((a, b) => syndicationService.offLive(b) - syndicationService.offLive(a));

  const dimOn = anyFilter() && !mf.noout;

  // Any role can OPEN a cell — the popover is the management story ("what exactly is
  // happening with this bank"): status, dwell, note, sanctioned amount, history.
  // advanceMatrix roles additionally see the legal next steps (mirroring the
  // register's transition map), with Sanctioned capturing the allocation and
  // Declined the reason — the server rejects both without their substance.
  const openPop = (e: React.MouseEvent<HTMLElement>, c: string, l: string) => {
    setTarget(''); setNote(''); setAmount('');
    setPop({ c, l, el: e.currentTarget });
  };
  const closePop = () => { setPop(null); setTarget(''); setNote(''); setAmount(''); };
  const commit = (st: string) => {
    if (!pop) return;
    const row = syndicationService.lenderRow(pop.c, pop.l);
    if (!row) syndicationService.addLender(pop.c, pop.l, user.full);
    else syndicationService.setLenderStatus(pop.c, pop.l, st, user.full, {
      note: note.trim() || undefined,
      amountCr: st === 'Sanctioned' && amount ? +amount : undefined,
    });
    setMsg(`${pop.l} → ${lenderLabel(st)}${st === 'Sanctioned' && amount ? ` · ₹${amount} Cr` : ''}`);
    closePop(); force((n) => n + 1);
  };
  // A next-step click: outcomes pause for their substance, plain moves apply at once.
  const pick = (st: string) => {
    if (st === 'Sanctioned' || st === 'Declined') { setTarget(st); return; }
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
  const drop = (to: number) => { if (drag.current == null || drag.current === to) return; syndicationService.reorderLenders(drag.current, to); drag.current = null; force((n) => n + 1); };

  const exportCsv = () => {
    const rows = [['Company', 'Group Code', 'Lender', 'State', 'Since', 'Days', 'Live ask Cr']];
    codes.forEach((c) => order.forEach((l) => {
      const o = cellObj(c, l); if (!o) return;
      if (anyFilter() && !mf.noout && !cellHit(c, l)) return;
      rows.push([clientsService.get(c).name, c, l, MATRIX_LABELS[o.s], o.since || '', String(daysSince(o.since) ?? ''), String(syndicationService.offLive(c))]);
    }));
    const csv = rows.map((r) => r.map((x) => `"${String(x).replace(/"/g, '""')}"`).join(',')).join('\n');
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' })); a.download = 'atlas_matrix.csv'; a.click();
  };

  const dotStyle = (s: number, dim: boolean): React.CSSProperties => ({
    width: 17, height: 17, borderRadius: '50%', display: 'inline-block', cursor: 'pointer',
    border: `1.6px solid ${s ? MATRIX_COLORS[s] : '#C9D2D6'}`, background: s ? MATRIX_COLORS[s] : '#fff',
    opacity: dim ? 0.14 : 1, transition: 'transform .1s',
  });

  return (
    <Box>
      {/* scope + hint */}
      <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mb: 1, flexWrap: 'wrap' }}>
        {(['Live', 'Closed', 'All'] as Scope[]).map((f) => (
          <Button key={f} onClick={() => setScope(f)} size="small"
            variant={scope === f ? 'contained' : 'outlined'} sx={{ borderRadius: 999, minWidth: 0, px: 1.5, py: 0.2 }}>{f}</Button>
        ))}
        <Typography sx={{ fontSize: 11.6, color: tokens.muted, ml: 1 }}>{ro ? 'Click a dot for the full story (mirrors the Chase List). ' : 'Click a dot for the story and the next steps (writes to the Chase List). '}Drag lender columns to reorder · click a company for the profile</Typography>
      </Box>

      {/* filter bar */}
      <Box sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.2, mb: 1.2, display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
        {[1, 2, 3, 4, 5, 6].map((s) => (
          <Box key={s} onClick={() => togState(s)}
            sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.8, border: `1.4px solid ${mf.states.includes(s) ? tokens.navy : tokens.line}`,
              bgcolor: mf.states.includes(s) ? '#F2F6F5' : '#fff', borderRadius: 999, px: 1.2, py: 0.5, fontSize: 12, cursor: 'pointer',
              color: mf.states.includes(s) ? tokens.ink : tokens.muted, fontWeight: mf.states.includes(s) ? 600 : 400 }}>
            <Box sx={{ width: 13, height: 13, borderRadius: '50%', bgcolor: MATRIX_COLORS[s], border: `1.4px solid ${MATRIX_COLORS[s]}` }} />
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

      {/* matrix */}
      <Box sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: 2, overflow: 'auto', maxHeight: 'calc(100vh - 250px)' }}>
        <table style={{ borderCollapse: 'collapse', width: 'auto', fontSize: 12.6 }}>
          <thead>
            <tr>
              <th style={{ position: 'sticky', left: 0, top: 0, zIndex: 8, background: '#F7F9FA', minWidth: 210, maxWidth: 250, textAlign: 'left', padding: '8px 9px', borderRight: `1px solid ${tokens.line}`, borderBottom: `1px solid ${tokens.line}` }}>Company</th>
              {order.map((l, i) => (
                <th key={l} draggable={!ro} title={`${l} — drag to reorder`}
                  onDragStart={() => (drag.current = i)}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={() => drop(i)}
                  style={{ height: 118, verticalAlign: 'bottom', padding: '6px 3px', cursor: ro ? 'default' : 'grab', minWidth: 36, position: 'sticky', top: 0, background: '#F7F9FA', borderBottom: `1px solid ${tokens.line}` }}>
                  <span style={{ writingMode: 'vertical-rl', transform: 'rotate(180deg)', fontSize: 10.2, display: 'inline-block', maxHeight: 108, overflow: 'hidden' }}>{l}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {codes.map((c) => {
              const cl = clientsService.get(c);
              const st = synRows(c).map((r: any) => r.status);
              const top = ['Disbursed', 'Sanctioned', 'IP Received', 'Queries Received', 'IM Circulated', 'IM in Prep', 'Docs Pending', 'Deal Sourced', 'On Hold'].find((s) => st.includes(s)) || st[0] || '';
              return (
                <tr key={c}>
                  <td style={{ position: 'sticky', left: 0, zIndex: 6, background: '#fff', minWidth: 210, maxWidth: 250, padding: '5px 9px', borderRight: `1px solid ${tokens.line}`, borderBottom: `1px solid #EFF2F4`, whiteSpace: 'normal' }}>
                    <b style={{ cursor: 'pointer' }} onClick={() => onOpenCompany(c)}>{cl.name}</b>
                    <div style={{ fontSize: 10.8, color: tokens.muted }}>₹{fmt(syndicationService.offLive(c), 1)} Cr · {top}</div>
                  </td>
                  {order.map((l) => {
                    const s = cellS(c, l); const d = cellDays(c, l);
                    const dim = dimOn && !!s && !cellHit(c, l);
                    return (
                      <td key={l} style={{ textAlign: 'center', padding: '5px 3px', borderBottom: `1px solid #EFF2F4` }}>
                        <Tooltip title={`${l} · ${lenderLabel(cellObj(c, l)?.st || '') || MATRIX_LABELS[s]}${d != null && s ? ' · ' + d + 'd' : ''} · click for details`} arrow>
                          <span style={dotStyle(s, dim)} onClick={(e) => openPop(e, c, l)}
                            onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.transform = 'scale(1.22)')}
                            onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.transform = 'none')} />
                        </Tooltip>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
            {!codes.length && <tr><td style={{ padding: 26, color: tokens.muted }}>No companies match this view.</td></tr>}
          </tbody>
        </table>
        <Typography sx={{ fontSize: 11.5, color: tokens.muted, p: '7px 11px' }}>
          {codes.length} companies{mf.noout ? ' with a live mandate and zero lender outreach' : ''}
        </Typography>
      </Box>
      {/* Cell popover — the story of one bank on one mandate, and (advanceMatrix) the moves */}
      <Popover open={!!pop} anchorEl={pop?.el ?? null} onClose={closePop}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
        transformOrigin={{ vertical: 'top', horizontal: 'center' }}
        slotProps={{ paper: { sx: { p: 1.6, width: 300, borderRadius: 2 } } }}>
        {pop && (() => {
          const row = syndicationService.lenderRow(pop.c, pop.l);
          const st = row?.st || '';
          const s = st ? (ST2DOT[st] || 1) : 0;
          const d = row?.since ? daysSince(row.since) : null;
          const nexts = ro ? [] : (LENDER_NEXT[st] ?? []);
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
              <Typography sx={{ fontSize: 11.4, color: tokens.muted, mb: 1 }}>{clientsService.get(pop.c).name}</Typography>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mb: 0.6 }}>
                <Box sx={{ width: 12, height: 12, borderRadius: '50%', bgcolor: s ? MATRIX_COLORS[s] : '#fff', border: `1.4px solid ${s ? MATRIX_COLORS[s] : '#C9D2D6'}` }} />
                <Typography sx={{ fontSize: 12.4, fontWeight: 600 }}>{lenderLabel(st) || 'Not in play'}</Typography>
                {d != null && st && <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>· {d}d in state</Typography>}
              </Box>
              {row?.amt != null && (
                <Typography sx={{ fontSize: 12, color: MATRIX_COLORS[5], fontWeight: 600, mb: 0.4 }}>Approved ₹{fmt(row.amt, 1)} Cr</Typography>
              )}
              {row?.note && <Typography sx={{ fontSize: 11.6, color: tokens.muted, mb: 0.4, whiteSpace: 'pre-wrap' }}>{row.note}</Typography>}
              {hist.length > 0 && (
                <Box sx={{ borderTop: `1px solid ${tokens.line}`, mt: 0.8, pt: 0.8 }}>
                  {hist.map((x, i) => (
                    <Typography key={i} sx={{ fontSize: 11, color: tokens.muted, lineHeight: 1.7 }}>
                      {x.what}{x.when ? ` · ${x.when}` : ''}{x.who ? ` · ${x.who}` : ''}
                    </Typography>
                  ))}
                </Box>
              )}
              {!ro && !row && (
                <Button fullWidth size="small" variant="contained" sx={{ mt: 1 }} onClick={() => commit('Identified')}>
                  Identify — put this bank in play
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
                    {target === 'Sanctioned' ? 'Approve — record the allocation' : 'Decline — record the reason'}
                  </Typography>
                  {target === 'Sanctioned' && (
                    <TextField size="small" fullWidth type="number" label="Approved amount (₹ Cr)" value={amount}
                      onChange={(e) => setAmount(e.target.value)} sx={{ mb: 0.8 }} autoFocus
                      inputProps={{ min: 0, step: 0.5 }} />
                  )}
                  <TextField size="small" fullWidth multiline minRows={2} value={note}
                    label={target === 'Declined' ? 'Why did they decline? (required)' : 'Note (optional)'}
                    onChange={(e) => setNote(e.target.value)} autoFocus={target === 'Declined'} />
                  <Box sx={{ display: 'flex', gap: 0.8, mt: 1, justifyContent: 'flex-end' }}>
                    <Button size="small" onClick={() => setTarget('')}>Back</Button>
                    <Button size="small" variant="contained" disabled={!canConfirm} onClick={() => commit(target)}
                      sx={{ bgcolor: MATRIX_COLORS[ST2DOT[target] || 1] }}>
                      {target === 'Sanctioned' ? 'Confirm approval' : 'Confirm decline'}
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
