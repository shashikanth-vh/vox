import { Drawer, Box, Typography, Button, IconButton, Divider, TextField, Stack } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import AddIcon from '@mui/icons-material/Add';
import { useEffect, useState } from 'react';
import { FieldGrid, TextFld, SelectFld, DrawerSection, FieldShell } from '../../components/common/Field';
import { CodeText, LensPill, TempPill, ProductFlags } from '../../components/common/Pills';
import { referenceService } from '../../services/referenceService';
import { clientsService } from '../../services/clientsService';
import { entitiesService } from '../../services/entitiesService';
import { dealsService } from '../../services/dealsService';
import { lendingService, LEND_GREEN } from '../../services/lendingService';
import { syndicationService } from '../../services/syndicationService';
import { assetMonService, amStatusOptions } from '../../services/assetMonService';
import { interactionService } from '../../services/interactionService';
import { notesService } from '../../services/notesService';
import LogInteractionDialog from './LogInteractionDialog';
import DataRegisterDialog from './DataRegisterDialog';
import StageChangeDialog from './StageChangeDialog';
import CloseDealDialog from './CloseDealDialog';
import { canRequestLine, type StageLine } from '../../services/stageRequestService';
import { useAuth } from '../../auth/AuthContext';
import { USE_REAL_API, isRegisterId } from '../../api/http';
import { db } from '../../api/atlasStore';
import ActionsPanel from '../../components/workflow/ActionsPanel';
import { can } from '../../auth/rbac';
import InteractionRow from '../../components/common/InteractionRow';
import { tokens } from '../../theme';

const LIFE_STAGES = ['Prospect', 'Onboarded', 'Active', 'Serviced', 'Vistaar — Expansion', 'Dormant'];
// The funnel terminals. A deal that has already ended has nothing left to close, so the
// action is withdrawn rather than offered and then refused. Mirrors the register's
// ALLOWED_TRANSITIONS, where all three are final.
const CLOSED_STAGES = new Set(['Closed Won', 'Closed Lost', 'Dropped']);

// Field-lock triggers (Forms spec v2.1). Syndication amt/type lock once the deal is
// sanctioned / the mandate is executed; lending amount + sanction date lock on Sanctioned.
const SYN_SANCTIONED = ['Sanctioned', 'Disbursed'];

export default function CompanyDrawer({ code, onClose, onChanged, onAddProduct }: {
  // onAddProduct is optional — callers that have nowhere to route the flow (FI Master)
  // open the drawer without it, and the button hides.
  code: string | null; onClose: () => void; onChanged: () => void; onAddProduct?: (code: string) => void;
}) {
  const { user } = useAuth();
  // Section-level RBAC: write follows the vertical (Operations matrix). A Credit Head
  // can edit Lending but not the Syndication section, a BDRM edits the profile but not
  // the product lines, etc.
  const roProfile = !can(user.roles, 'editDealProfile');
  const roOwn = !can(user.roles, 'editDealOwnership');
  const roSyn = !can(user.roles, 'editSyn');
  const roLend = !can(user.roles, 'editLending');
  const roAM = !can(user.roles, 'editAM');
  const canNote = can(user.roles, 'addNote');
  const canInteract = can(user.roles, 'logInteraction');
  const canProduct = can(user.roles, 'addProduct');
  // Requesters (can't directly edit a line) raise a stage-change request instead —
  // scoped PER LINE: a desk asks about its own line only (an AM RM never requests a
  // lending stage move; review feedback).
  const canRequest = can(user.roles, 'requestStageChange');
  // Closing a deal is a stage-change AUTHORITY action, never an RM convenience — the
  // register gates /close on approve_stage_change, so the button is offered to exactly
  // the roles whose click will succeed.
  const canClose = can(user.roles, 'approveRequest');
  const canRequestFor = (line: StageLine) => canRequest && canRequestLine(user.roles, line);
  // Assigning the Deal Analyst is Credit Head's action (Admin/Mgmt override) — not the
  // BD Head who owns the rest of the Ownership section.
  const canAssign = can(user.roles, 'assignAnalystLending');
  // Post-lock override: only Admin / Management may edit a locked (sanctioned/executed)
  // field; sanction-date edits after the 24h stamp are Admin-only (Forms spec).
  const adminOverride = user.roles.includes('Admin') || user.roles.includes('Management');
  const isAdmin = user.roles.includes('Admin');
  const todayISO = new Date().toISOString().slice(0, 10);
  const ref = referenceService;
  const [, force] = useState(0);
  const [logOpen, setLogOpen] = useState(false);
  // The interactions panel tells the WHOLE story — entity-phase plus the lead-phase
  // discussion that won the mandate — refetched when the log dialog closes.
  const [ints, setInts] = useState<ReturnType<typeof interactionService.for>>([]);
  const [regOpen, setRegOpen] = useState(false);
  const [stageReq, setStageReq] = useState<{ line: StageLine; refId: string; current: string } | null>(null);
  const [closeOpen, setCloseOpen] = useState(false);
  const [noteText, setNoteText] = useState('');
  const bump = () => { force((n) => n + 1); onChanged(); };
  // The drawer reads the company and its product lines out of the shared store, which is
  // filled by whichever grid the user happened to visit. Opened from Deals that store can
  // be cold, so the drawer would render the group code where the company name belongs and
  // show no product lines at all. Warm what is missing, once, on open.
  useEffect(() => {
    if (!code || !USE_REAL_API) return;
    const page = { pageIndex: 0, pageSize: 200, globalFilter: '', sorting: [], columnFilters: [] };
    const jobs: Promise<any>[] = [];
    if (!db().clients[code]) jobs.push(clientsService.list(page as any));
    // `.some(isRegisterId)` and not `.length`: a stale optimistic row would otherwise
    // count as loaded, and the drawer would go on editing a row the register never had.
    const loaded = (rows: { id: string }[]) => rows.some((r) => isRegisterId(r.id));
    // The Ownership section reads the deal from this same store — and the Deals grid's
    // live pages never land in it (only the dashboard's full hydrate does). A company
    // converted a minute ago has its deal in the register but not in the store, and the
    // drawer would claim "no Deals row yet" right above a live Lending line.
    if (!db().deals.some((dd: any) => dd.code === code && isRegisterId(String(dd.apiId || '')))) jobs.push(dealsService.hydrateAll());
    if (!loaded(lendingService.byCode(code))) jobs.push(lendingService.list(page as any));
    if (!loaded(assetMonService.byCode(code))) jobs.push(assetMonService.list(page as any));
    if (!loaded(syndicationService.byCode(code))) jobs.push(syndicationService.hydrate());
    if (jobs.length) Promise.allSettled(jobs).then(() => force((n) => n + 1));
  }, [code]);
  // The entity id every document/workbench dialog uses comes from the client cache —
  // which can hold a STALE id (a previous database's, a deleted row's) and then every
  // upload answers "Entity … not found" on a perfectly healthy company. Re-resolve it
  // from the register by Group Code on open and heal the cache when it drifted.
  useEffect(() => {
    if (!code || !USE_REAL_API) return;
    let alive = true;
    void entitiesService.byCode(code).then((row) => {
      if (!alive || !row?.entityId) return;
      const cur = (db().clients[code] || {}) as any;
      if (cur.entityId !== row.entityId) {
        db().clients[code] = { ...cur, ...row };
        force((n) => n + 1);
      }
    }).catch(() => {});
    return () => { alive = false; };
  }, [code]);
  useEffect(() => {
    if (!code) return;
    let alive = true;
    void (async () => {
      // Resolve the entity id OURSELVES rather than peeking at the client cache: on a
      // fresh page load the cache has not healed yet, so the peek came back empty, the
      // timeline fell back to the (empty) local store, and never retried — a drawer
      // showing Interactions (0) over a register full of them.
      let eid = ((db().clients[code] || {}) as any).entityId as string | undefined;
      if (!eid && USE_REAL_API) {
        try { eid = (await entitiesService.byCode(code))?.entityId; } catch { /* fallback below */ }
      }
      const l = await interactionService.forCompany(eid, code);
      if (alive) setInts(l);
    })();
    return () => { alive = false; };
  }, [code, logOpen]);
  if (!code) return null;

  const c = clientsService.get(code);
  const d = dealsService.find(code);
  const lend = lendingService.byCode(code);
  const syn = syndicationService.byCode(code);
  const am = assetMonService.byCode(code);
  const refType = syn.length ? 'Platform Deals' : lend.length ? 'Lending' : am.length ? 'AssetMon' : 'General';
  const notes = notesService.for(code);
  const aud = notesService.auditFor(code);

  const updC = (k: any, v: any) => { clientsService.update(code, { [k]: v }, user.full); bump(); };
  const updD = (k: any, v: any) => { dealsService.update(code, k, v, user.full); bump(); };
  const updS = (id: string, k: any, v: any) => { syndicationService.update(id, k, v, user.full); bump(); };
  const updL = (id: string, k: any, v: any) => { lendingService.update(id, k, v, user.full); bump(); };
  const updA = (id: string, k: any, v: any) => { assetMonService.update(id, k, v, user.full); bump(); };
  const addNote = () => { if (!noteText.trim()) return; notesService.add(code, noteText.trim(), user.full); setNoteText(''); bump(); };

  return (
    <Drawer anchor="right" open={!!code} onClose={onClose}
      PaperProps={{ sx: { width: 580, maxWidth: '100vw', height: '100%', display: 'flex', flexDirection: 'column' } }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2, p: '9px 16px', bgcolor: '#F1F3F5', borderBottom: 1, borderColor: 'divider', flexShrink: 0 }}>
        <Typography sx={{ fontSize: 15.6, fontWeight: 700, flex: 1 }}>{c.name}</Typography>
        <CodeText code={code} /><LensPill lens={c.lens} />{d && d.temp && <TempPill temp={d.temp} />}
        <IconButton onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', p: 2 }}>
        <DrawerSection title="Profile">
          <FieldGrid>
            <SelectFld label="Segment / sector" required value={c.sector} disabled={roProfile} onChange={(v) => updC('sector', v)} options={ref.getRefSync('Sector')} />
            <SelectFld label="Climate lens" value={c.lens} disabled={roProfile} onChange={(v) => updC('lens', v)} options={ref.getRefSync('Lens')} />
            <TextFld label="State" required value={c.state} disabled={roProfile} onChange={(v) => updC('state', v)} />
            <TextFld label="Type of industry" value={c.toi} disabled={roProfile} onChange={(v) => updC('toi', v)} />
            <SelectFld label="Lifecycle (Vistaar journey)" value={c.lifecycle || 'Prospect'} disabled={roProfile} onChange={(v) => updC('lifecycle', v)} options={LIFE_STAGES} />
          </FieldGrid>
        </DrawerSection>

        {d ? (
          <DrawerSection title="Ownership & sourcing">
            <FieldGrid>
              <SelectFld label="RM" required value={d.rm} disabled={roOwn} onChange={(v) => updD('rm', v)} options={ref.getRefSync('RM')} labels={ref.getRefLabels('RM')} blank />
              <SelectFld label="Deal Analyst" value={d.an} disabled={!canAssign} onChange={(v) => updD('an', v)} options={ref.getRefSync('Analyst')} labels={ref.getRefLabels('Analyst')} blank />
              <SelectFld label="Temperature" value={d.temp} disabled={roOwn} onChange={(v) => updD('temp', v)} options={ref.getRefSync('Temperature')} blank />
              {/* Source + Source detail are locked-from-lead (Forms spec) — always disabled. */}
              <SelectFld label="Source" value={d.source} disabled onChange={(v) => updD('source', v)} options={ref.getRefSync('Source')} blank />
              <TextFld label="Source detail" value={d.sourceDetail} disabled onChange={(v) => updD('sourceDetail', v)} />
              {/* Products sits in the grid on the same row as Source detail; the label
                  reuses FieldShell so it matches every other field's label style. */}
              <FieldShell label="Products">
                <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', minHeight: 34 }}>
                  <ProductFlags lend={d.lend} syn={d.syn} am={d.am} full />
                </Box>
              </FieldShell>
            </FieldGrid>
          </DrawerSection>
        ) : (
          <DrawerSection title="Ownership"><Typography sx={{ fontSize: 12, color: tokens.muted }}>In Client Master only — no Deals row yet. Use + Add product to start one.</Typography></DrawerSection>
        )}

        {syn.map((r, i) => (
          <DrawerSection key={r.id} title={`Platform Deals${syn.length > 1 ? ' · ask ' + (i + 1) : ''} — ${r.status}`}
            action={canRequestFor('Syndication') && roSyn ? <Button size="small" variant="outlined" onClick={() => setStageReq({ line: 'Syndication', refId: r.id, current: r.status })}>⟳ Request stage change</Button> : undefined}>
            <FieldGrid>
              <SelectFld label="Status of proposal" required value={r.status} disabled={roSyn} onChange={(v) => updS(r.id, 'status', v)} options={ref.getRefSync('Status of Proposal')} />
              {/* Amt is mandatory to move past Deal Sourced and LOCKS on Sanctioned (Admin/Mgmt only after). */}
              <TextFld label="Amt (₹ Cr)" required type="number" value={r.amt} disabled={roSyn || (SYN_SANCTIONED.includes(r.status) && !adminOverride)} onChange={(v) => updS(r.id, 'amt', v)} />
              {/* Syndication type LOCKS once the mandate is Executed. */}
              {/* The register's category is 'Syndication Type' (v19 renamed the label only). */}
              <SelectFld label="Platform Deals type" required value={r.synType} disabled={roSyn || (r.mstat3 === 'Executed' && !adminOverride)} onChange={(v) => updS(r.id, 'synType', v)} options={ref.getRefSync('Syndication Type')} blank />
              {/* Mandate status sits right after Platform Deals type. It uses the full
                  Mandate Status workflow list. Not required. Locks once Executed. */}
              <SelectFld label="Mandate status"
                value={r.mstat3} disabled={roSyn || (r.mstat3 === 'Executed' && !adminOverride)} onChange={(v) => updS(r.id, 'mstat3', v)} options={ref.getRefSync('Mandate Status')} blank />
              <TextFld label="Facility type" value={r.fac} disabled={roSyn} onChange={(v) => updS(r.id, 'fac', v)} />
              <SelectFld label="Tenor" value={r.tenor} disabled={roSyn} onChange={(v) => updS(r.id, 'tenor', v)} options={ref.getRefSync('Tenor')} blank />
              <SelectFld label="IM in place" value={r.im} disabled={roSyn} onChange={(v) => updS(r.id, 'im', v)} options={ref.getRefSync('IM in Place')} blank />
              <TextFld label="Lender coordinator" value={r.lc} disabled={roSyn} onChange={(v) => updS(r.id, 'lc', v)} />
              <SelectFld label="Priority" value={r.pri} disabled={roSyn} onChange={(v) => updS(r.id, 'pri', v)} options={ref.getRefSync('Priority')} />
              <SelectFld label="Pending with" value={r.pendingWith} disabled={roSyn} onChange={(v) => updS(r.id, 'pendingWith', v)} options={ref.getRefSync('Pending With')} blank />
            </FieldGrid>
            {/* What the workflow plane says this user may do next on this
                line — served, not guessed. */}
            <ActionsPanel subjectType="Syndication" subjectId={r.id} />
            <Stack spacing={1} sx={{ mt: 1 }}>
              <TextFld label="Potential lenders (after checking with client)" value={r.pot} disabled={roSyn} onChange={(v) => updS(r.id, 'pot', v)} multiline />
              <TextFld label="Sanctioned lenders" required={r.status === 'Sanctioned'} value={r.sancL} disabled={roSyn} onChange={(v) => updS(r.id, 'sancL', v)} />
              <TextFld label="IP received (lenders)" value={r.ipL} disabled={roSyn} onChange={(v) => updS(r.id, 'ipL', v)} />
              <TextFld label="Existing lenders" value={r.exist} disabled={roSyn} onChange={(v) => updS(r.id, 'exist', v)} />
              <TextFld label="Pricing expectation" value={r.price} disabled={roSyn} onChange={(v) => updS(r.id, 'price', v)} />
              <TextFld label="Latest remarks" value={r.remarks} disabled={roSyn} onChange={(v) => updS(r.id, 'remarks', v)} multiline />
            </Stack>
          </DrawerSection>
        ))}

        {lend.map((r) => (
          <DrawerSection key={r.id} title={`Lending — ${r.stage}`}
            action={canRequestFor('Lending') && roLend ? <Button size="small" variant="outlined" onClick={() => setStageReq({ line: 'Lending', refId: r.id, current: r.stage })}>⟳ Request stage change</Button> : undefined}>
            <FieldGrid>
              {/* Amount is mandatory past Data Awaited and LOCKS on Sanctioned (Admin/Mgmt only after). */}
              <TextFld label="Amount (₹ Cr)" required type="number" value={r.amt} disabled={roLend || (LEND_GREEN.includes(r.stage) && !adminOverride)} onChange={(v) => updL(r.id, 'amt', v)} />
              <SelectFld label="Stage" required value={r.stage} disabled={roLend} onChange={(v) => { void lendingService.updateStage(r.id, v, user.full).then((res) => { if (!res.ok) alert(res.error); bump(); }); }} options={ref.getRefSync('Lending Stage')} />
              <SelectFld label="Deal Analyst" value={r.an} disabled={roLend} onChange={(v) => updL(r.id, 'an', v)} options={ref.getRefSync('Analyst')} labels={ref.getRefLabels('Analyst')} blank />
              {/* BDRM is read-only here — edited on Company Ownership (Forms spec). */}
              <SelectFld label="RM" value={r.rm} disabled onChange={(v) => updL(r.id, 'rm', v)} options={ref.getRefSync('RM')} labels={ref.getRefLabels('RM')} blank />
              <SelectFld label="Pending with" value={r.pendingWith} disabled={roLend} onChange={(v) => updL(r.id, 'pendingWith', v)} options={ref.getRefSync('Pending With')} blank />
              {/* Sanction date is mandatory at Sanctioned and locks 24h after stamping (Admin-only after). */}
              {LEND_GREEN.includes(r.stage) &&
                <TextFld label="Sanction date" required type="date" value={r.sanc || ''} disabled={roLend || (!isAdmin && !!r.sanc && r.sanc !== todayISO)} onChange={(v) => updL(r.id, 'sanc', v)} />}
              <TextFld label="Stage updated" value={r.updated} disabled onChange={() => {}} />
            </FieldGrid>
            {/* What the workflow plane says this user may do next on this
                line — served, not guessed. */}
            <ActionsPanel subjectType="Lending" subjectId={r.id} code={code} entityId={(c as any)?.entityId} />
            {/* T1, T2, … — Advaya's disbursement evidence. Rendered once the line can
                carry tranches; the register writes them only from Advaya confirmations. */}
            {isRegisterId(r.id) && ['Ready for Disbursement', 'Disbursed', 'Closed'].includes(r.stage) && (
              <TranchesBlock lendingId={r.id} />
            )}
            {/* The LMS statement — the account opens itself on the first confirmed
                tranche, so this appears exactly when there is money to account for. */}
            {isRegisterId(r.id) && ['Disbursed', 'Closed'].includes(r.stage) && (
              <LoanAccountBlock lendingId={r.id} />
            )}
            <Box sx={{ mt: 1 }}><TextFld label="Remarks" value={r.remarks} disabled={roLend} onChange={(v) => updL(r.id, 'remarks', v)} multiline /></Box>
          </DrawerSection>
        ))}

        {/* No stage-change REQUEST lane for AM (desk review decision): the AM book is
            a plain update surface — editAM roles move the status directly. */}
        {am.map((r) => (
          <DrawerSection key={r.id} title={`Asset Monetisation — ${r.status}`}>
            <FieldGrid>
              <TextFld label="State" value={r.state} disabled={roAM} onChange={(v) => updA(r.id, 'state', v)} />
              <TextFld label="Indicative value (₹ Cr)" required type="number" value={r.val} disabled={roAM} onChange={(v) => updA(r.id, 'val', v)} />
              <TextFld label="Size (MW)" type="number" value={r.mw} disabled={roAM} onChange={(v) => updA(r.id, 'mw', v)} />
              <TextFld label="Nature" value={r.nature} disabled={roAM} onChange={(v) => updA(r.id, 'nature', v)} />
              <TextFld label="Deal type" value={r.dtype} disabled={roAM} onChange={(v) => updA(r.id, 'dtype', v)} />
              <TextFld label="Investor type" value={r.itype} disabled={roAM} onChange={(v) => updA(r.id, 'itype', v)} />
              {/* Legal moves only (forward one, back one, Dropped; Closed from
                  SPA / Documentation). The AM book is a plain update surface —
                  no workflow, no approval (desk review decision). */}
              <SelectFld label="Status" required value={r.status} disabled={roAM || amStatusOptions(r.status).length <= 1} onChange={(v) => updA(r.id, 'status', v)} options={amStatusOptions(r.status)} />
              <TextFld label="Date teaser shared" type="date" value={r.teaser || ''} disabled={roAM} onChange={(v) => updA(r.id, 'teaser', v)} />
            </FieldGrid>
            <Stack spacing={1} sx={{ mt: 1 }}>
              <TextFld label="Investor(s)" value={r.inv} disabled={roAM} onChange={(v) => updA(r.id, 'inv', v)} />
              <TextFld label="Notes" value={r.notes} disabled={roAM} onChange={(v) => updA(r.id, 'notes', v)} multiline />
            </Stack>
          </DrawerSection>
        ))}

        <DrawerSection title="About the company">
          <TextField fullWidth multiline minRows={3} value={c.about ?? ''} disabled={roProfile} onChange={(e) => updC('about', e.target.value)} />
        </DrawerSection>

        <DrawerSection title="Updates & notes">
          {notes.length ? notes.map((n, i) => (
            <Box key={i} sx={{ py: 0.75, borderBottom: `1px solid ${tokens.line}` }}>
              <Typography sx={{ fontSize: 10.8, color: tokens.muted }}>{n.by} · {n.when}</Typography>
              <Typography sx={{ fontSize: 12.5, whiteSpace: 'pre-wrap' }}>{n.text}</Typography>
            </Box>
          )) : <Typography sx={{ fontSize: 12, color: tokens.muted }}>No updates yet.</Typography>}
          {canNote && (
            <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
              <TextField fullWidth size="small" placeholder="Add an update — status, call summary, lender feedback…" value={noteText} onChange={(e) => setNoteText(e.target.value)} />
              <Button variant="outlined" onClick={addNote}>Add</Button>
            </Stack>
          )}
        </DrawerSection>

        <DrawerSection title="Recent audit">
          {aud.length ? aud.map((a, i) => (
            <Box key={i} sx={{ py: 0.6, borderBottom: `1px solid ${tokens.line}` }}>
              <Typography sx={{ fontSize: 10.8, color: tokens.muted }}>{a.t} · {a.by}</Typography>
              <Typography sx={{ fontSize: 12.3 }}>{a.act}{a.detail ? `: ${a.detail}` : ''}</Typography>
            </Box>
          )) : <Typography sx={{ fontSize: 12, color: tokens.muted }}>No changes logged.</Typography>}
        </DrawerSection>

        <DrawerSection title={`Interactions (${ints.length})`}
          action={canInteract ? <Button size="small" variant="contained" startIcon={<AddIcon />} onClick={() => setLogOpen(true)}>Log interaction</Button> : undefined}>
          <Box sx={{ borderLeft: `2px solid ${tokens.line}`, pl: 1.5 }}>
            {ints.length ? ints.slice(0, 8).map((i) => (
              <InteractionRow key={i.interactionId} i={i} />
            )) : <Typography sx={{ fontSize: 12, color: tokens.muted }}>No interactions logged yet. Click <b>Log interaction</b> to add one.</Typography>}
          </Box>
        </DrawerSection>
      </Box>

      <Divider />
      <Box sx={{ p: 2, display: 'flex', gap: 1, flexWrap: 'wrap', flexShrink: 0 }}>
        <Button variant="outlined" onClick={() => setRegOpen(true)}>📁 Data Register</Button>
        {canProduct && onAddProduct && <Button startIcon={<AddIcon />} variant="outlined" onClick={() => onAddProduct(code)}>Add product</Button>}
        {/* Only once the company HAS a deal to close, and only for the authority that may.
            A deal already at a funnel terminal has nothing left to close. */}
        {canClose && (d as any)?.apiId && !CLOSED_STAGES.has(String((d as any)?.stage || '')) && (
          <Button variant="outlined" color="warning" onClick={() => setCloseOpen(true)}>Close deal</Button>
        )}
        <Box sx={{ flex: 1 }} />
        <Button variant="outlined" onClick={onClose}>Done</Button>
      </Box>

      <LogInteractionDialog code={code} refType={refType} entityId={((db().clients[code] || {}) as any).entityId} open={logOpen} onClose={() => setLogOpen(false)} onDone={bump} />
      <DataRegisterDialog code={code} open={regOpen} onClose={() => setRegOpen(false)} />
      <CloseDealDialog open={closeOpen} code={code} apiId={(d as any)?.apiId}
        currentStage={(d as any)?.stage} onClose={() => setCloseOpen(false)}
        onDone={() => { bump(); onChanged(); }} />
      <StageChangeDialog open={!!stageReq} code={code} presetLine={stageReq?.line} refId={stageReq?.refId} currentStage={stageReq?.current} onClose={() => setStageReq(null)} onDone={bump} />
    </Drawer>
  );
}

/**
 * T1, T2, … — the disbursement tranches Advaya confirmed, with the reconciliation the
 * register keeps (cumulative vs ceiling). Read-only here by design: tranches are written
 * ONLY from Advaya confirmations ("Record an Advaya confirmation"), never typed into a
 * grid — the actuals belong to the party that moved the money.
 */
/**
 * The LOAN ACCOUNT STATEMENT — the sheet the servicing team keeps, served by the
 * register's LMS: the account header (number, borrower, rate, tenure, EMI,
 * classification) and the ledger (Date | Particulars | Debit | Credit | Balance).
 * Interest is never typed: "Calculate" previews balance × rate% × days ÷ day-count
 * with its inputs, and only then is the row posted.
 */
function LoanAccountBlock({ lendingId }: { lendingId: string }) {
  const [data, setData] = useState<any | null>(null);
  const [upto, setUpto] = useState(() => new Date().toISOString().slice(0, 10));
  const [preview, setPreview] = useState<any | null>(null);
  const [emiDate, setEmiDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [emiAmt, setEmiAmt] = useState('');
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const load = async () => {
    const { api } = await import('../../api/http');
    try { setData(await api.get<any>(`/lending/${lendingId}/loan-account`)); }
    catch { setData(null); }
  };
  useEffect(() => { void load(); }, [lendingId]);  // eslint-disable-line react-hooks/exhaustive-deps

  if (!data) return null;
  const a = data.account;
  const entries: any[] = data.entries || [];
  const money = (v: any) => (v == null ? '' : Number(v).toLocaleString('en-IN',
    { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

  const act = async (what: () => Promise<string>) => {
    setErr(''); setMsg(''); setBusy(true);
    try { setMsg(await what()); await load(); }
    catch (e: any) {
      setErr(e?.response?.data?.error?.detail || e?.message || String(e));
    }
    setBusy(false);
  };

  const calc = () => act(async () => {
    const { api } = await import('../../api/http');
    const p = await api.get<any>(`/lending/${lendingId}/loan-account/interest-preview`,
      { upto });
    setPreview(p);
    return `Interest to ${upto}: ₹ ${money(p.interest)} (${p.formula})`;
  });
  const postInterest = () => act(async () => {
    const { api } = await import('../../api/http');
    await api.post(`/lending/${lendingId}/loan-account/accrue`, { upto });
    setPreview(null);
    return 'Interest posted to the ledger.';
  });
  const postEmi = () => act(async () => {
    const { api } = await import('../../api/http');
    await api.post(`/lending/${lendingId}/loan-account/entries`,
      { entry_date: emiDate, kind: 'EMI', amount: Number(emiAmt) });
    setEmiAmt('');
    return 'EMI receipt recorded.';
  });

  const fact = (label: string, value: any) => (
    <Box key={label}>
      <Typography sx={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '.5px', color: tokens.muted, fontWeight: 700 }}>{label}</Typography>
      <Typography sx={{ fontSize: 12.4 }}>{value ?? '—'}</Typography>
    </Box>
  );

  return (
    <Box sx={{ mt: 1.2, border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1.2 }}>
      <Typography sx={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.6px', color: tokens.muted, fontWeight: 700, mb: 0.6 }}>
        Loan account — statement
      </Typography>
      <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 0.8, mb: 1 }}>
        {fact('Loan account number', a.account_no)}
        {fact('Borrower', a.borrower)}
        {fact('Date of disbursement', a.disbursed_on)}
        {fact('Facility type', a.facility_type)}
        {fact('Loan amount', a.amount != null ? `₹ ${money(a.amount)}` : null)}
        {fact('Rate of interest', a.rate_pct != null ? `${a.rate_pct}% (${a.rate_kind})` : null)}
        {fact('Tenure', a.tenor_months != null ? `${a.tenor_months} months` : null)}
        {fact('Repayment', a.emi_amount != null
          ? `EMI ₹ ${money(a.emi_amount)}${a.repayment_start ? ` from ${a.repayment_start}` : ''}` : null)}
        {fact('Overdue position', a.overdue_position)}
        {fact('Loan status', a.status)}
      </Box>
      <Box sx={{ maxHeight: 200, overflow: 'auto', border: `1px solid ${tokens.line}`, borderRadius: 1 }}>
        <Box component="table" sx={{ width: '100%', borderCollapse: 'collapse', fontSize: 12,
          '& th, & td': { borderBottom: `1px solid ${tokens.line}`, p: '3px 8px', textAlign: 'right' },
          '& th:nth-of-type(-n+2), & td:nth-of-type(-n+2)': { textAlign: 'left' } }}>
          <thead>
            <tr><th>Date</th><th>Particulars</th><th>Debit</th><th>Credit</th><th>Balance</th></tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.entry_no}>
                <td>{e.entry_date}</td><td>{e.particulars}</td>
                <td>{money(e.debit)}</td><td>{money(e.credit)}</td>
                <td><b>{money(e.balance)}</b></td>
              </tr>
            ))}
          </tbody>
        </Box>
      </Box>
      {a.status !== 'Closed' && (
        <>
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mt: 1, flexWrap: 'wrap' }}>
            <TextField size="small" type="date" label="Interest up to" value={upto}
              onChange={(e) => { setUpto(e.target.value); setPreview(null); }}
              InputLabelProps={{ shrink: true }} sx={{ width: 160 }} />
            <Button size="small" variant="outlined" disabled={busy} onClick={() => void calc()}
              sx={{ textTransform: 'none' }}>Calculate</Button>
            {preview && (
              <Button size="small" variant="contained" disabled={busy}
                onClick={() => void postInterest()} sx={{ textTransform: 'none' }}>
                Post ₹ {money(preview.interest)} interest
              </Button>
            )}
          </Box>
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mt: 0.8, flexWrap: 'wrap' }}>
            <TextField size="small" type="date" label="EMI received on" value={emiDate}
              onChange={(e) => setEmiDate(e.target.value)}
              InputLabelProps={{ shrink: true }} sx={{ width: 160 }} />
            <TextField size="small" label="Amount" value={emiAmt}
              placeholder={a.emi_amount != null ? String(a.emi_amount) : ''}
              onChange={(e) => setEmiAmt(e.target.value)} sx={{ width: 130 }} />
            <Button size="small" variant="outlined" sx={{ textTransform: 'none' }}
              disabled={busy || !emiAmt || !(Number(emiAmt) > 0)}
              onClick={() => void postEmi()}>Record EMI receipt</Button>
          </Box>
        </>
      )}
      {msg && <Typography sx={{ fontSize: 11.8, color: 'success.main', mt: 0.6 }}>{msg}</Typography>}
      {err && <Typography sx={{ fontSize: 11.8, color: 'error.main', mt: 0.6 }}>{err}</Typography>}
    </Box>
  );
}

function TranchesBlock({ lendingId }: { lendingId: string }) {
  const [data, setData] = useState<any | null>(null);
  useEffect(() => {
    let alive = true;
    void import('../../api/http').then(({ api }) =>
      api.get<any>(`/lending/${lendingId}/tranches`)
        .then((d) => { if (alive) setData(d); })
        .catch(() => { if (alive) setData(null); }));
    return () => { alive = false; };
  }, [lendingId]);
  if (!data) return null;
  const items: any[] = data.items || [];
  return (
    <Box sx={{ mt: 1.2, border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1.2 }}>
      <Typography sx={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.6px', color: tokens.muted, fontWeight: 700, mb: 0.6 }}>
        Disbursement tranches
      </Typography>
      {items.length === 0 && (
        <Typography sx={{ fontSize: 12, color: tokens.muted }}>
          None yet — each Advaya confirmation with event <b>disbursed</b> records the next tranche.
        </Typography>
      )}
      {items.map((t) => (
        <Box key={t.id} sx={{ display: 'flex', gap: 1, alignItems: 'baseline', py: 0.4, borderBottom: `1px dashed ${tokens.line}`, flexWrap: 'wrap' }}>
          <Typography sx={{ fontSize: 12, fontWeight: 700, minWidth: 26 }}>{t.tranche_no}</Typography>
          <Typography sx={{ fontSize: 12.5 }}>₹ {t.amount} Cr</Typography>
          <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
            {[t.disbursed_on, t.advaya_reference || t.tranche_ref, t.recorded_by].filter(Boolean).join(' · ')}
          </Typography>
        </Box>
      ))}
      {items.length > 0 && (
        <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 0.6 }}>
          Disbursed <b>₹ {data.total_disbursed} Cr</b>
          {data.ceiling != null && <> of ₹ {data.ceiling} Cr{data.fully_disbursed ? ' — fully disbursed' : ` · remaining ₹ ${data.remaining} Cr`}</>}
        </Typography>
      )}
    </Box>
  );
}
