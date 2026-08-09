import { Drawer, Box, Typography, Button, IconButton, Divider, Alert } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import AddIcon from '@mui/icons-material/Add';
import { useEffect, useState } from 'react';
import { FieldGrid, TextFld, SelectFld, DrawerSection } from '../../components/common/Field';
import { CodeText, TempPill } from '../../components/common/Pills';
import { referenceService } from '../../services/referenceService';
import { leadsService } from '../../services/leadsService';
import { interactionService, type Interaction } from '../../services/interactionService';
import LogInteractionDialog from '../Deals/LogInteractionDialog';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { employeesService } from '../../services/employeesService';
import InteractionRow from '../../components/common/InteractionRow';
import { tokens } from '../../theme';
import type { Lead } from './lead.types';

const ADAPT_SECT = ['Industrial Water', 'Water Treatment / WASH', 'Climate Data & IoT', 'Agri / Drone'];

export default function LeadDrawer({ lead, onClose, onChanged, onPush }: {
  lead: Lead | null; onClose: () => void; onChanged: () => void; onPush: (l: Lead) => void;
}) {
  const { user } = useAuth();
  const ro = !can(user.roles, 'editLead');
  const canInteract = can(user.roles, 'logInteraction');
  const ref = referenceService;

  // `row` is the saved lead; `edits` are the pending changes. Nothing is sent until Done,
  // so the drawer holds one PATCH's worth of edits rather than firing per keystroke.
  const [row, setRow] = useState<Lead | null>(lead);
  const [edits, setEdits] = useState<Partial<Lead>>({});
  const [ints, setInts] = useState<Interaction[]>([]);
  const [logOpen, setLogOpen] = useState(false);
  const [err, setErr] = useState('');
  const [saving, setSaving] = useState(false);

  // The grid row is only as fresh as the last list call, so the drawer re-reads the lead
  // from GET /v1/leads/{id} on open. The passed row shows immediately and is replaced
  // when the read lands, so the drawer never opens empty.
  useEffect(() => {
    setErr(''); setEdits({}); setSaving(false);
    setRow(lead); setInts([]);
    if (!lead) return;
    let alive = true;
    leadsService.get(lead).then((full) => { if (alive && full) setRow(full); });
    interactionService.forLead(lead).then((list) => { if (alive) setInts(list); });
    // Empty name lists on open = a boot-time /v1/ref (or roster) fetch that failed —
    // retry it now, so one bad moment at login does not leave every dropdown dead
    // until a full page reload.
    if (!referenceService.getRefSync('RM').length) {
      void referenceService.hydrate()
        .then(() => employeesService.hydrateRoster())
        .then(() => { if (alive) setRow((r) => (r ? { ...r } : r)); });
    }
    return () => { alive = false; };
  }, [lead?.id, lead?.apiId]);

  if (!lead || !row) return null;

  // What the fields show: the pending edit if there is one, else the saved value.
  const v = <K extends keyof Lead>(k: K): any => (k in edits ? (edits as any)[k] : (row as any)[k]);
  const set = (k: keyof Lead, val: any) => {
    setErr('');
    setEdits((p) => ({
      ...p, [k]: val,
      // Lens follows sector, exactly as it does on Add lead.
      ...(k === 'sector' ? { lens: ADAPT_SECT.includes(val) ? 'Adaptation' : 'Mitigation' } : {}),
    }));
  };

  const dirty = Object.keys(edits).length > 0;

  const reloadInteractions = () => {
    interactionService.forLead(row).then(setInts);
    // A logged interaction is a touch on the lead, so the list's "last touch" may move.
    onChanged();
  };

  // Done is the save: it PATCHes whatever changed, then closes. With nothing changed it
  // is just a close, so a read-only visit costs no request.
  const done = async () => {
    if (!dirty) { onClose(); return; }
    setSaving(true);
    const r = await leadsService.patch(row, edits, user.full);
    setSaving(false);
    if (!r.ok) { setErr(r.error || 'Could not save the lead.'); return; }
    setEdits({});
    onChanged();
    onClose();
  };

  // Pushing to Deals would leave unsaved edits behind, so they go up first.
  const push = async () => {
    if (dirty) {
      setSaving(true);
      const r = await leadsService.patch(row, edits, user.full);
      setSaving(false);
      if (!r.ok) { setErr(r.error || 'Could not save the lead.'); return; }
      setEdits({});
      onChanged();
    }
    onClose();
    onPush(row);
  };

  return (
    <Drawer anchor="right" open={!!lead} onClose={onClose} PaperProps={{ sx: { width: 560, maxWidth: '100vw' } }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2, p: '9px 16px', bgcolor: '#F1F3F5', borderBottom: 1, borderColor: 'divider' }}>
        <Typography sx={{ fontSize: 15.6, fontWeight: 700, flex: 1 }}>{v('company')}</Typography>
        <CodeText code={row.id} /><TempPill temp={v('temp')} />
        <IconButton onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
      </Box>
      <Box sx={{ flex: 1, overflow: 'auto', p: 2 }}>
        {err && <Alert severity="warning" sx={{ mb: 1.4, py: 0, fontSize: 12 }}>{err}</Alert>}
        <DrawerSection title="Lead">
          <FieldGrid>
            {/* Company is locked after creation — Admin only can override (Forms spec). */}
            <TextFld label="Company" value={v('company')} disabled={ro || !user.roles.includes('Admin')} onChange={(x) => set('company', x)} />
            <SelectFld label="Sector" required value={v('sector')} disabled={ro} onChange={(x) => set('sector', x)} options={ref.getRefSync('Sector')} />
            <SelectFld label="Climate lens" value={v('lens')} disabled={ro} onChange={(x) => set('lens', x)} options={ref.getRefSync('Lens')} />
            <SelectFld label="Source" required value={v('source')} disabled={ro} onChange={(x) => set('source', x)} options={ref.getRefSync('Source')} />
            {/* Reassigning the BDRM is Admin / Mgmt / BD Head only — a BDRM can't change
                their own ownership (Operations matrix: Reassign lead). */}
            {/* The list is LIVE from the register's people roster (/v1/ref). An empty
                roster renders a select with nothing in it and no way forward, so: the
                lead's CURRENT value is always offerable (validation happens at the
                register, which explains itself), a fresh hydrate retries a boot-time
                fetch that failed, and the empty state SAYS what to do about it. */}
            <SelectFld label="BDRM" required value={v('rm')} disabled={!can(user.roles, 'reassignLead')} onChange={(x) => set('rm', x)}
              options={[...new Set([...(v('rm') ? [v('rm')] : []), ...ref.getRefSync('RM')])]} />
            {!ref.getRefSync('RM').length && (
              <Alert severity="info" sx={{ gridColumn: '1 / -1', py: 0, fontSize: 12 }}>
                No BDRMs on the roster yet — add the person (with their e-mail) under
                Masters ▸ Employees, and this list fills immediately.
              </Alert>
            )}
            <SelectFld label="Temperature" value={v('temp')} disabled={ro} onChange={(x) => set('temp', x)} options={ref.getRefSync('Temperature')} />
            <SelectFld label="Status" value={v('status')} disabled={ro} onChange={(x) => set('status', x)} options={['Active', 'Dropped']} />
            <TextFld label="Contact" value={v('contact')} disabled={ro} onChange={(x) => set('contact', x)} />
            <TextFld label="Designation" value={v('designation') || ''} disabled={ro} onChange={(x) => set('designation', x)} />
            <TextFld label="Phone" value={v('phone')} disabled={ro} onChange={(x) => set('phone', x)} />
            <TextFld label="Last interaction" type="date" value={v('last')} disabled={ro} onChange={(x) => set('last', x)} />
          </FieldGrid>
          <Box sx={{ mt: 1, display: 'grid', gap: 1 }}>
            <TextFld label="Next action" value={v('next')} disabled={ro} onChange={(x) => set('next', x)} />
            <TextFld label="Notes / ask" value={v('notes')} disabled={ro} onChange={(x) => set('notes', x)} multiline />
          </Box>
        </DrawerSection>

        <DrawerSection title={`Interactions (${ints.length})`}
          action={canInteract ? <Button size="small" variant="contained" startIcon={<AddIcon />} onClick={() => setLogOpen(true)}>Log interaction</Button> : undefined}>
          <Box sx={{ borderLeft: `2px solid ${tokens.line}`, pl: 1.5 }}>
            {ints.length ? ints.slice(0, 8).map((i) => (
              <InteractionRow key={i.interactionId} i={i} leadBadge={false} />
            )) : <Typography sx={{ fontSize: 12, color: tokens.muted }}>No interactions logged yet. Click <b>Log interaction</b> to add one.</Typography>}
          </Box>
        </DrawerSection>
      </Box>
      <Divider />
      <Box sx={{ p: 2, display: 'flex', alignItems: 'center', gap: 1 }}>
        {!ro && v('status') === 'Active' && <Button variant="contained" onClick={push} disabled={saving}>Push to Deals →</Button>}
        <Box sx={{ flex: 1 }} />
        {dirty && <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>Unsaved changes</Typography>}
        <Button variant={dirty ? 'contained' : 'outlined'} onClick={done} disabled={saving}>
          {saving ? 'Saving…' : dirty ? 'Save & close' : 'Done'}
        </Button>
      </Box>
      <LogInteractionDialog code={row.id} refType="Lead" lead={row} open={logOpen}
        onClose={() => setLogOpen(false)} onDone={reloadInteractions} />
    </Drawer>
  );
}
