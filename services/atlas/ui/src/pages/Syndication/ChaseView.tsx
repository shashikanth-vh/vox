import { useState } from 'react';
import { Autocomplete, Box, Typography, Select, MenuItem, TextField, Dialog, DialogTitle, DialogContent, DialogActions, Button } from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import { syndicationService, SYN_TERM, SYN_CLOSED, lenderNext, LSTATE_COLOR, lenderLabel } from '../../services/syndicationService';
import { clientsService } from '../../services/clientsService';
import { db } from '../../api/atlasStore';
import { daysSince, fmt } from '../../utils/format';
import { useSearch } from '../../context/SearchContext';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import ExportBar, { toCsv, saveCsv } from '../../components/common/ExportBar';
import ConfirmDialog from '../../components/common/ConfirmDialog';
import { tokens } from '../../theme';

// Port of v12 vSynChase(): the attention strip + per-company chase cards with a
// density dot strip and inline lender rows (status select + log chase/reply).
interface Lender { name: string; ex?: boolean; st?: string; resp?: string; chased?: string | null; note?: string; chaseNote?: string; replyNote?: string; amt?: number | null; heldFrom?: string; }

// Chase-list status → dot colour (v12 ST_COLOR == LSTATE_COLOR).
const dotColor = (st?: string) => LSTATE_COLOR[st || 'Identified'] || '#94a3b8';

// A small pill (v12 `.pill hot/warm`).
function Pill({ tone, children }: { tone: 'red' | 'amber' | 'grey'; children: React.ReactNode }) {
  const map = { red: ['#FBE9E4', '#A93B22'], amber: ['#FBF3E1', '#8F6512'], grey: ['#EDF1F3', '#5F6E76'] } as const;
  const [bg, fg] = map[tone];
  return <Box component="span" sx={{ display: 'inline-block', borderRadius: 999, px: '8px', py: '2px', fontSize: 10.8, fontWeight: 700, bgcolor: bg, color: fg, whiteSpace: 'nowrap' }}>{children}</Box>;
}

// One line of the conversation snapshot: CHASE = the last thing we said, REPLY =
// the last thing the lender said, REMARK = the desk's own margin note. Ellipsised
// to one line; the full text rides the title tooltip.
const SNAP_TONE = {
  chase: ['#0E6E8A', '#E3F2F7'], reply: ['#0E8A68', '#E2F5EE'], remark: ['#7A6A2E', '#F5F0DC'],
} as const;
function SnapLine({ kind, when, text }: { kind: keyof typeof SNAP_TONE; when?: string; text: string }) {
  const [fg, bg] = SNAP_TONE[kind];
  return (
    <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 0.8, minWidth: 0, whiteSpace: 'nowrap' }}>
      <Box component="span" sx={{ flex: 'none', fontSize: 9.6, fontWeight: 700, letterSpacing: 0.4, borderRadius: '4px', px: 0.7, py: 0.1, color: fg, bgcolor: bg }}>{kind.toUpperCase()}</Box>
      {when && <Typography component="span" sx={{ flex: 'none', fontSize: 10.8, color: tokens.muted }}>{when}</Typography>}
      <Typography component="span" title={text} sx={{ fontSize: 11.4, color: tokens.ink, fontStyle: 'italic', overflow: 'hidden', textOverflow: 'ellipsis' }}>“{text}”</Typography>
    </Box>
  );
}
const SnapQuiet = ({ children, red }: { children: React.ReactNode; red?: boolean }) => (
  <Typography sx={{ fontSize: 11, color: red ? '#C43C2B' : tokens.muted, fontStyle: 'italic', fontWeight: red ? 600 : 400 }}>{children}</Typography>
);

// v12 `.chline .lsel`: 1px --line box, 4px radius, 12px, tight padding. Offers only
// the CURRENT status plus its legal next steps — the register enforces the same
// transition map, so a free-for-all list would just collect 422s.
const LSel = ({ value, disabled, onChange, heldFrom }: { value: string; disabled: boolean; onChange: (v: string) => void; heldFrom?: string }) => {
  const opts = [value, ...lenderNext(value, heldFrom)].filter((s, i, a) => s && a.indexOf(s) === i);
  return (
    <Select size="small" value={value} disabled={disabled || opts.length <= 1} onClick={(e) => e.stopPropagation()}
      onChange={(e) => onChange(e.target.value)}
      sx={{ fontSize: '12px', minWidth: 150, borderRadius: '4px', '& .MuiSelect-select': { py: '3px', px: '6px' }, '& fieldset': { borderColor: tokens.line } }}>
      {opts.map((s) => <MenuItem key={s} value={s} sx={{ fontSize: 12 }}>{lenderLabel(s)}</MenuItem>)}
    </Select>
  );
};

// v12 `.mini`: 11.5px, muted, 1px --line box, 8px radius, white, 5px/11px padding.
const MiniBtn = ({ onClick, children }: { onClick: () => void; children: React.ReactNode }) => (
  <Box component="button" onClick={(e) => { e.stopPropagation(); onClick(); }}
    sx={{ background: '#fff', border: `1px solid ${tokens.line}`, borderRadius: '8px', px: '11px', py: '5px', fontSize: '11.5px', cursor: 'pointer', color: tokens.muted, whiteSpace: 'nowrap' }}>
    {children}
  </Box>
);

export default function ChaseView({ onOpenCompany }: { onOpenCompany: (code: string) => void }) {
  const { search } = useSearch();
  const { user } = useAuth();
  // Chase-list actions (add lender, log chase/reply, advance lender status) are Syn Head
  // / Syn RM work per the Operations matrix — NOT Deal Analyst (who edits the syn record
  // in the drawer). logChase shares that exact role set.
  const ro = !can(user.roles, 'logChase');
  // Deleting a MANDATE is the Admin-only irreversible gate, same as every grid —
  // the other three grids had their Remove, this view sent Admins to psql.
  const admin = can(user.roles, 'deleteRow');
  const [del, setDel] = useState<any | null>(null);
  const [delErr, setDelErr] = useState<string | null>(null);
  const [, force] = useState(0);
  const [newL, setNewL] = useState<Record<string, string>>({});
  // Log chase / Log reply note capture — MUI dialog in place of window.prompt.
  const [logDlg, setLogDlg] = useState<{ kind: 'chase' | 'reply'; code: string; name: string; id: string } | null>(null);
  const [logNote, setLogNote] = useState('');
  // The two OUTCOMES carry substance the register insists on: a decline says why,
  // a sanction says how much — captured here before the status write goes out.
  const [outDlg, setOutDlg] = useState<{ code: string; name: string; st: 'Sanctioned' | 'Declined' | 'Dropped'; id: string } | null>(null);
  const [outNote, setOutNote] = useState('');
  const [outAmt, setOutAmt] = useState('');
  const bump = () => force((n) => n + 1);
  const th = db().th || { lenderSilent: 7 };

  const q = search.trim().toLowerCase();
  const match = (name: string, code: string) => !q || name.toLowerCase().includes(q) || code.toLowerCase().includes(q);

  const files = db().syn
    .filter((r: any) => !SYN_TERM.includes(r.status) && !SYN_CLOSED.includes(r.status))
    .map((r: any) => ({ r, co: clientsService.get(r.code).name, live: (r.lenders || []).filter((l: Lender) => !l.ex && l.st).length }))
    .filter((x: any) => match(x.co, x.r.code))
    .sort((a: any, b: any) => b.live - a.live);

  // Attention: silent lenders (IM out > threshold), unanswered queries, stale chases.
  const attention: any[] = [];
  files.forEach(({ r, co }: any) => (r.lenders || []).forEach((l: Lender) => {
    if (l.ex) return;
    const rd = daysSince(l.resp); const chased = daysSince(l.chased);
    if (l.st === 'IM Circulated' && rd != null && rd > th.lenderSilent) attention.push({ code: r.code, id: r.id, co, l, reason: 'silent', age: rd, sev: rd > 14 ? 'red' : 'amber' });
    else if (l.st === 'Queries Received' && (chased == null || chased > 3)) attention.push({ code: r.code, id: r.id, co, l, reason: 'queries', age: rd, sev: 'amber' });
    else if (chased != null && chased > 10 && !['Sanctioned', 'Declined'].includes(l.st || '')) attention.push({ code: r.code, id: r.id, co, l, reason: 'stale-chase', age: chased, sev: 'amber' });
  }));
  attention.sort((a, b) => (a.sev === 'red' ? 0 : 1) - (b.sev === 'red' ? 0 : 1) || b.age - a.age);

  // Every write names the exact mandate it renders from (id) — one company can run
  // several syndication mandates, and the same bank may sit on more than one.
  const setSt = (code: string, name: string, st: string, id: string) => {
    if (ro) return;
    if (st === 'Sanctioned' || st === 'Declined' || st === 'Dropped') { setOutNote(''); setOutAmt(''); setOutDlg({ code, name, st, id }); return; }
    syndicationService.setLenderStatus(code, name, st, user.full, undefined, id); bump();
  };
  const submitOut = () => {
    if (!outDlg) return;
    syndicationService.setLenderStatus(outDlg.code, outDlg.name, outDlg.st, user.full, {
      note: outNote.trim() || undefined,
      amountCr: outDlg.st === 'Sanctioned' && outAmt ? +outAmt : undefined,
    }, outDlg.id);
    setOutDlg(null); bump();
  };
  const outOk = outDlg?.st === 'Sanctioned' ? +outAmt > 0 : outNote.trim().length > 0;
  const chase = (code: string, name: string, id: string) => { if (ro) return; setLogNote(''); setLogDlg({ kind: 'chase', code, name, id }); };
  const reply = (code: string, name: string, id: string) => { if (ro) return; setLogNote(''); setLogDlg({ kind: 'reply', code, name, id }); };
  const closeLog = () => setLogDlg(null);
  const submitLog = () => {
    if (!logDlg) return;
    const note = logNote.trim();
    if (logDlg.kind === 'chase') syndicationService.logChase(logDlg.code, logDlg.name, note, user.full, logDlg.id);
    else syndicationService.logResp(logDlg.code, logDlg.name, note, user.full, logDlg.id);
    setLogDlg(null); bump();
  };
  const add = (code: string, id: string, name?: string) => {
    if (ro) return;
    const v = (name ?? newL[id] ?? '').trim(); if (!v) return;
    syndicationService.addLender(code, v, user.full, id);
    setNewL((p) => ({ ...p, [id]: '' })); bump();
  };

  const exportCsv = () => {
    const rows: (string | number)[][] = [];
    files.forEach(({ r, co }: any) => (r.lenders || []).forEach((l: Lender) => {
      rows.push([co, r.code, r.id || '', r.dealNo || '', Number(r.amt) || 0, l.name, l.ex ? 'Existing' : (l.st || ''),
        l.ex ? '' : (daysSince(l.resp) ?? ''), l.ex ? '' : (daysSince(l.chased) ?? '')]);
    }));
    saveCsv(toCsv(['Company', 'Group Code', 'Mandate', 'Deal', 'Ask Cr', 'Lender', 'Status', 'Inbound days', 'Chased days'], rows), 'atlas_chase');
  };

  if (!files.length) return <Box sx={{ p: 3, textAlign: 'center', color: tokens.muted }}>No live Platform Deals files.</Box>;

  // v12 `.chco > .chline` banner: indented, left-bordered, alternating backgrounds.
  const CHLINE = {
    display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap',
    ml: '20px', borderLeft: '2px solid #e2e8f0', pl: '12px', pr: '8px', py: '6px',
    borderRadius: '0 4px 4px 0', mt: '2px', fontSize: '12.8px',
  } as const;
  const evenBg = (i: number) => (i % 2 === 0 ? '#f4f6f8' : '#fafbfc'); // first row is even (v12 nth-child)

  return (
    <Box>
      <Box sx={{ display: 'flex', justifyContent: 'flex-end', mb: 1 }}><ExportBar onCsv={exportCsv} /></Box>
      {/* Attention strip */}
      {attention.length > 0 && (
        <Box component="details" open sx={{ bgcolor: '#fff7ed', border: '1px solid #fed7aa', borderLeft: '4px solid #f97316', borderRadius: '8px', mb: 1.6, p: '8px 12px' }}>
          <Box component="summary" sx={{ cursor: 'pointer', listStyle: 'none', display: 'flex', gap: 1.2, alignItems: 'center', fontSize: 13, '&::-webkit-details-marker': { display: 'none' } }}>
            <b>Needs attention ({attention.length})</b>
            <Typography component="span" sx={{ fontSize: 11.5, color: tokens.muted }}>Silent lenders, unanswered queries, stale chases</Typography>
          </Box>
          <Box sx={{ mt: 1, display: 'flex', flexDirection: 'column', gap: 0.5 }}>
            {attention.slice(0, 15).map((a, i) => {
              const tag = a.reason === 'silent' ? `SILENT ${a.age}d` : a.reason === 'queries' ? `QUERIES OPEN ${a.age || '—'}d` : `CHASED ${a.age}d ago`;
              return (
                <Box key={i} sx={{ display: 'flex', gap: 1.2, alignItems: 'center', p: '5px 6px', borderRadius: '5px', fontSize: 12.5, flexWrap: 'wrap', bgcolor: a.sev === 'red' ? '#fef2f2' : '#fff' }}>
                  <Pill tone={a.sev === 'red' ? 'red' : 'amber'}>{tag}</Pill>
                  <Box component="b" sx={{ color: tokens.navy, cursor: 'pointer', '&:hover': { textDecoration: 'underline' } }} onClick={() => onOpenCompany(a.code)}>{a.co}</Box>
                  <Typography component="span" sx={{ fontSize: 11.5, color: tokens.muted }}>{a.l.name}</Typography>
                  <LSel value={a.l.st || 'Identified'} heldFrom={a.l.heldFrom} disabled={ro} onChange={(v) => setSt(a.code, a.l.name, v, a.id)} />
                  <Box sx={{ flex: 1 }} />
                  <MiniBtn onClick={() => chase(a.code, a.l.name, a.id)}>Log chase</MiniBtn>
                  <MiniBtn onClick={() => reply(a.code, a.l.name, a.id)}>Log reply</MiniBtn>
                </Box>
              );
            })}
            {attention.length > 15 && <Typography sx={{ fontSize: 11.5, color: tokens.muted, py: 0.6 }}>…and {attention.length - 15} more. Scan the company list below.</Typography>}
          </Box>
        </Box>
      )}

      {/* Company cards */}
      {files.map(({ r, co }: any) => {
        const lenders: Lender[] = (r.lenders || []).slice().sort((a: Lender, b: Lender) => (a.ex ? 1 : 0) - (b.ex ? 1 : 0));
        const outreach = lenders.filter((l) => !l.ex);
        // counts summary
        const c = { s: 0, ip: 0, q: 0, im: 0, d: 0, id: 0 };
        outreach.forEach((l) => {
          if (l.st === 'Sanctioned') c.s++; else if (l.st === 'IP Received') c.ip++; else if (l.st === 'Queries Received') c.q++;
          else if (l.st === 'IM Circulated') c.im++; else if (l.st === 'Declined') c.d++; else if (l.st === 'Identified') c.id++;
        });
        const bits: React.ReactNode[] = [];
        if (c.s) bits.push(<span key="s" style={{ color: LSTATE_COLOR['Sanctioned'] }}>{c.s} sanctioned</span>);
        if (c.ip) bits.push(<span key="ip" style={{ color: LSTATE_COLOR['IP Received'] }}>{c.ip} IP</span>);
        if (c.q) bits.push(<span key="q" style={{ color: LSTATE_COLOR['Queries Received'] }}>{c.q} queries</span>);
        if (c.im) bits.push(<span key="im" style={{ color: LSTATE_COLOR['IM Circulated'] }}>{c.im} IM circ</span>);
        if (c.id) bits.push(<span key="id">{c.id} identified</span>);
        if (c.d) bits.push(<span key="d" style={{ color: LSTATE_COLOR['Declined'] }}>{c.d} declined</span>);
        const counts = bits.length ? bits.reduce((acc: React.ReactNode[], b, i) => (i ? [...acc, ' · ', b] : [b]), []) : `${outreach.length} lenders`;

        return (
          <Box component="details" key={r.id} sx={{ bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: '8px', p: '8px 12px', my: 1 }}>
            <Box component="summary" sx={{ cursor: 'pointer', listStyle: 'none', display: 'flex', gap: 1.2, alignItems: 'center', flexWrap: 'wrap', '&::-webkit-details-marker': { display: 'none' } }}>
              <ExpandMoreIcon sx={{ fontSize: 18, color: tokens.muted }} />
              <Box component="b" sx={{ color: tokens.navy, cursor: 'pointer', '&:hover': { textDecoration: 'underline' } }}
                onClick={(e) => { e.preventDefault(); e.stopPropagation(); onOpenCompany(r.code); }}>{co}</Box>
              <Typography component="span" sx={{ fontSize: 11.5, color: tokens.muted }}>{r.code}{r.id && r.id !== r.code ? ` · ${r.id}` : ''}{r.dealNo ? ` · deal ${r.dealNo}` : ''} · ₹{fmt(Number(r.amt), 1)} Cr</Typography>
              {/* density strip */}
              <Box sx={{ display: 'inline-flex', gap: '2px', alignItems: 'center' }}>
                {outreach.length ? outreach.map((l, i) => {
                  const rd = daysSince(l.resp);
                  const silent = l.st === 'IM Circulated' && rd != null && rd > th.lenderSilent;
                  return <Box key={i} title={`${l.name}: ${lenderLabel(l.st || '') || '—'}`} sx={{ width: 9, height: 9, borderRadius: '50%', bgcolor: silent ? '#dc2626' : dotColor(l.st) }} />;
                }) : <Typography component="span" sx={{ fontSize: 11.5, color: tokens.muted }}>no lenders yet</Typography>}
              </Box>
              <Box sx={{ flex: 1 }} />
              <Typography component="span" sx={{ fontSize: 11.5, color: tokens.muted }}>{counts}</Typography>
            </Box>

            {/* lender rows */}
            {lenders.length ? lenders.map((l, i) => {
              if (l.ex) return (
                <Box key={i} sx={{ ...CHLINE, bgcolor: '#f1f5f9' }}>
                  <Pill tone="grey">EXISTING</Pill>
                  <Box component="b" sx={{ color: tokens.navy }}>{l.name}</Box>
                  <Typography component="span" sx={{ fontSize: 11.6, color: tokens.muted }}>Incumbent banker (not outreach)</Typography>
                </Box>
              );
              const rd = daysSince(l.resp); const cd = daysSince(l.chased);
              const silent = l.st === 'IM Circulated' && rd != null && rd > th.lenderSilent;
              // Conversation snapshot. Dates are YYYY-MM-DD strings, so who spoke
              // last is a plain comparison; the two quiet hints only appear when
              // there are words to hang them on.
              const hasSnap = !!(l.chaseNote || l.replyNote);
              const awaiting = !!l.chaseNote && cd != null && (!l.resp || String(l.resp) < String(l.chased));
              const remark = l.note && l.note !== l.chaseNote && l.note !== l.replyNote ? l.note : '';
              return (
                <Box key={i} sx={{
                  ...CHLINE, bgcolor: silent ? '#fff7ed' : evenBg(i),
                  ...(silent ? { borderLeft: '3px solid #f59e0b', pl: '8px' } : { '&:hover': { bgcolor: '#eef2f6' } }),
                }}>
                  <Box component="b" sx={{ color: tokens.navy, minWidth: 150 }}>{l.name}</Box>
                  <LSel value={l.st || 'Identified'} heldFrom={l.heldFrom} disabled={ro} onChange={(v) => setSt(r.code, l.name, v, r.id)} />
                  {l.st === 'Sanctioned' && l.amt != null && (
                    <Typography component="span" sx={{ fontSize: 11.6, fontWeight: 700, color: LSTATE_COLOR['Sanctioned'] }}>₹{fmt(l.amt, 1)} Cr</Typography>
                  )}
                  <Box sx={{ display: 'flex', flexDirection: 'column', gap: '1px', flex: 1, minWidth: 180 }}>
                    {l.chaseNote && <SnapLine kind="chase" when={cd != null ? `${cd}d ago` : ''} text={l.chaseNote} />}
                    {l.replyNote && <SnapLine kind="reply" when={rd != null ? `${rd}d ago` : ''} text={l.replyNote} />}
                    {!hasSnap && (rd != null || cd != null) && (
                      <SnapQuiet>{rd != null ? `inbound ${rd}d` : 'no inbound'}{cd != null ? ` · chased ${cd}d` : ''}{silent ? ' · SILENT' : ''}</SnapQuiet>
                    )}
                    {awaiting && <SnapQuiet red={silent || (cd != null && cd > th.lenderSilent)}>no reply yet — {cd}d since we chased</SnapQuiet>}
                    {remark && <SnapLine kind="remark" text={remark} />}
                    {!hasSnap && !remark && rd == null && cd == null && <SnapQuiet>no outreach logged</SnapQuiet>}
                  </Box>
                  {!ro && <><MiniBtn onClick={() => chase(r.code, l.name, r.id)}>Log chase</MiniBtn><MiniBtn onClick={() => reply(r.code, l.name, r.id)}>Log reply</MiniBtn></>}
                </Box>
              );
            }) : <Box sx={{ ...CHLINE, bgcolor: '#fafbfc', color: tokens.muted, fontSize: '11.6px' }}>No lenders yet. Add one below.</Box>}

            {/* add lender (v12 `.chco > .addl`) — picks from the FI master so one bank
                is always ONE spelling across mandates; free typing still allowed for a
                lender the master does not know yet. */}
            {admin && (
              <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 0.6 }}>
                <Box component="button" onClick={() => setDel(r)}
                  sx={{ background: '#fff', border: '1px solid #E8C6BE', borderRadius: '8px', px: '11px', py: '4px', fontSize: '11px', cursor: 'pointer', color: '#A93B22' }}>
                  Delete mandate
                </Box>
              </Box>
            )}
            {!ro && (
              <Box sx={{ ...CHLINE, bgcolor: '#f8fafc' }}>
                <Autocomplete freeSolo size="small" sx={{ flex: 1, minWidth: 180 }}
                  options={(db().lenders || [])
                    .filter((f: any) => f?.name && !f.inactive
                      && !(r.lenders || []).some((l: Lender) => l.name.toLowerCase() === f.name.toLowerCase()))
                    .map((f: any) => f.name)}
                  inputValue={newL[r.id] || ''}
                  onInputChange={(_, v) => setNewL((p) => ({ ...p, [r.id]: v }))}
                  onChange={(_, v) => v && add(r.code, r.id, String(v))}
                  renderInput={(params) => (
                    <TextField {...params} placeholder="Add lender — pick from FI master or type" />
                  )} />
                <MiniBtn onClick={() => add(r.code, r.id)}>+ Add</MiniBtn>
              </Box>
            )}
          </Box>
        );
      })}

      {/* Delete mandate — Admin only; the register's refusal is shown, never swallowed */}
      <ConfirmDialog open={!!del} title="Delete mandate"
        message={`Delete ${del ? clientsService.get(del.code).name : ''}'s mandate ${del?.id || ''}? Its lender conversations go with it.`}
        onCancel={() => setDel(null)} onConfirm={() => {
          const d = del; setDel(null);
          if (!d) return;
          void syndicationService.remove(d.id, user.full).then((res) => {
            if (!res.ok) setDelErr(res.error || 'The register refused the delete.');
            bump();
          });
        }} />
      <ConfirmDialog open={!!delErr} title="Delete refused" message={delErr || ''}
        onCancel={() => setDelErr(null)} onConfirm={() => setDelErr(null)} />

      {/* Log chase / Log reply note capture (replaces window.prompt) */}
      <Dialog open={!!logDlg} onClose={closeLog} maxWidth="sm" fullWidth>
        <DialogTitle>{logDlg?.kind === 'chase' ? 'Log chase' : 'Log reply'}{logDlg ? ` — ${logDlg.name}` : ''}</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus multiline minRows={3} fullWidth sx={{ mt: 1 }}
            label={logDlg?.kind === 'chase' ? 'What did we say?' : `What did ${logDlg?.name || 'they'} say?`}
            placeholder={logDlg?.kind === 'chase' ? 'Call summary, promise received, next step' : 'Their response, blockers raised, next step'}
            value={logNote}
            onChange={(e) => setLogNote(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submitLog(); }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={closeLog}>Cancel</Button>
          <Button variant="contained" onClick={submitLog}>Save</Button>
        </DialogActions>
      </Dialog>

      {/* Sanction / decline outcome capture */}
      <Dialog open={!!outDlg} onClose={() => setOutDlg(null)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ color: outDlg ? LSTATE_COLOR[outDlg.st] : undefined }}>
          {outDlg?.st === 'Sanctioned' ? 'Sanction' : outDlg?.st === 'Dropped' ? 'Drop' : 'Decline'}{outDlg ? ` — ${outDlg.name}` : ''}
        </DialogTitle>
        <DialogContent>
          {outDlg?.st === 'Sanctioned' && (
            <TextField autoFocus fullWidth type="number" sx={{ mt: 1 }} label="Sanctioned amount (₹ Cr)"
              value={outAmt} onChange={(e) => setOutAmt(e.target.value)} inputProps={{ min: 0, step: 0.5 }} />
          )}
          <TextField fullWidth multiline minRows={2} sx={{ mt: 1.5 }}
            autoFocus={outDlg?.st !== 'Sanctioned'}
            label={outDlg?.st === 'Declined' ? 'Why did they decline? (required)'
              : outDlg?.st === 'Dropped' ? 'Why are we dropping this bank? (required)' : 'Note (optional)'}
            placeholder={outDlg?.st === 'Declined' ? 'Sector cap, pricing, exposure, credit view…'
              : outDlg?.st === 'Dropped' ? 'Client chose another bank, sanction lapsed, pricing…'
              : 'Terms, conditions, next step'}
            value={outNote} onChange={(e) => setOutNote(e.target.value)} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOutDlg(null)}>Cancel</Button>
          <Button variant="contained" disabled={!outOk} onClick={submitOut}>
            {outDlg?.st === 'Sanctioned' ? 'Confirm sanction' : outDlg?.st === 'Dropped' ? 'Confirm drop' : 'Confirm decline'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
