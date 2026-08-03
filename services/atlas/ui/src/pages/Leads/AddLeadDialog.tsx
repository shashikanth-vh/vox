import { useState, useEffect } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Alert, IconButton } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld, SelectFld } from '../../components/common/Field';
import { referenceService } from '../../services/referenceService';
import { leadsService } from '../../services/leadsService';
import { db } from '../../api/atlasStore';
import { normName } from '../../utils/format';
import { useAuth } from '../../auth/AuthContext';

const ADAPT_SECT = ['Industrial Water', 'Water Treatment / WASH', 'Climate Data & IoT', 'Agri / Drone'];

/**
 * The BDRM a new lead opens with: the signed-in user when they ARE a BDRM, otherwise the
 * first name on the Register's roster. Never a hard-coded name — the seeded 'Shubh' was
 * offered on deployments whose people table had never heard of him, and the lead only
 * failed much later, on conversion.
 */
function defaultRm(user: { roles: string[]; name: string }): string {
  const roster = referenceService.getRefSync('RM');
  if (user.roles.includes('BDRM') && roster.includes(user.name)) return user.name;
  return roster[0] || '';
}

export default function AddLeadDialog({ open, onClose, onSaved }: { open: boolean; onClose: () => void; onSaved: () => void }) {
  const { user } = useAuth();
  const ref = referenceService;
  const blank = { company: '', contact: '', designation: '', sector: 'Other', lens: 'Mitigation', source: 'BDRM', sourceDetail: '', rm: defaultRm(user), temp: 'Warm', phone: '', notes: '' };
  const [f, setF] = useState(blank);
  const [dupes, setDupes] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (open) { setF(blank); setDupes([]); setErr(''); setSaving(false); } }, [open]);

  const set = (k: string, v: any) => setF((p) => ({ ...p, [k]: v, ...(k === 'sector' ? { lens: ADAPT_SECT.includes(v) ? 'Adaptation' : 'Mitigation' } : {}) }));

  const checkDupes = (name: string) => {
    const q = normName(name); if (q.length < 3) { setDupes([]); return; }
    const c = Object.entries(db().clients).filter(([, v]: any) => normName(v.name).includes(q)).slice(0, 3).map(([code, v]: any) => `Existing client: ${v.name} (${code})`);
    const l = db().leads.filter((x: any) => x.status === 'Active' && normName(x.company).includes(q)).slice(0, 2).map((x: any) => `Active lead: ${x.company} (${x.id})`);
    setDupes([...c, ...l]);
  };

  // Forms spec (Add lead): Company, Sector, Source all MANDATORY; Source detail is
  // MANDATORY when Source is DSA / Referral / Event (referral-payout traceability).
  const SRC_NEEDS_DETAIL = ['DSA', 'Referral', 'Event'];
  const [err, setErr] = useState('');
  const save = async () => {
    if (!f.company.trim()) { setErr('Company is required.'); return; }
    if (!f.sector) { setErr('Sector is required.'); return; }
    if (!f.source) { setErr('Source is required.'); return; }
    if (SRC_NEEDS_DETAIL.includes(f.source) && !f.sourceDetail.trim()) {
      setErr(`Source detail is required for ${f.source} (e.g. the ${f.source.toLowerCase()} name).`); return;
    }
    setErr('');
    setSaving(true);
    // The dialog stays open on failure so the entered lead isn't lost and the API's
    // own message (which field it rejected) is readable next to the fields.
    const r = await leadsService.create(f, user.full);
    setSaving(false);
    if (!r.ok) { setErr(r.error || 'Could not add the lead.'); return; }
    onSaved(); onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>Add lead
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <FieldGrid>
          <TextFld label="Company" required value={f.company} onChange={(v) => { set('company', v); checkDupes(v); }} />
          <TextFld label="Contact person" value={f.contact} onChange={(v) => set('contact', v)} />
        </FieldGrid>
        {dupes.map((d, i) => <Alert key={i} severity="warning" sx={{ mt: 1, py: 0, fontSize: 12 }}>{d}</Alert>)}
        <Box sx={{ mt: 1.4 }}>
          <FieldGrid>
            <SelectFld label="Sector" required value={f.sector} onChange={(v) => set('sector', v)} options={ref.getRefSync('Sector')} />
            <SelectFld label="Climate lens" value={f.lens} onChange={(v) => set('lens', v)} options={ref.getRefSync('Lens')} />
            <SelectFld label="Source" required value={f.source} onChange={(v) => set('source', v)} options={ref.getRefSync('Source')} />
            <TextFld label="Source detail" required={SRC_NEEDS_DETAIL.includes(f.source)} value={f.sourceDetail} onChange={(v) => set('sourceDetail', v)} />
            <SelectFld label="BDRM" required value={f.rm} onChange={(v) => set('rm', v)} options={ref.getRefSync('RM')} />
            <SelectFld label="Temperature" value={f.temp} onChange={(v) => set('temp', v)} options={ref.getRefSync('Temperature')} />
            <TextFld label="Designation" value={f.designation} onChange={(v) => set('designation', v)} />
            <TextFld label="Phone" value={f.phone} onChange={(v) => set('phone', v)} />
          </FieldGrid>
          <Box sx={{ mt: 1.4 }}><TextFld label="Notes / ask" value={f.notes} onChange={(v) => set('notes', v)} multiline /></Box>
        </Box>
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={saving}>Cancel</Button>
        <Button onClick={save} variant="contained" disabled={saving}>{saving ? 'Adding…' : 'Add lead'}</Button>
      </DialogActions>
    </Dialog>
  );
}
