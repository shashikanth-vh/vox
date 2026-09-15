import { useState, useEffect } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Alert, IconButton } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld, SelectFld } from '../../components/common/Field';
import { referenceService } from '../../services/referenceService';
import { leadsService } from '../../services/leadsService';
import { db } from '../../api/atlasStore';
import { nameAlike, normName } from '../../utils/format';
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
  // Live matches against the register as the name is TYPED: existing clients
  // are selectable (the new lead attaches to them — a linked lead converts
  // into a second deal under the SAME company, never a GREENPILLREN-2), open
  // leads warn. Fuzzy, because "greenphill" must still find "Greenpill".
  const [clientHits, setClientHits] = useState<{ code: string; name: string; rm?: string; entityId?: string }[]>([]);
  const [leadHits, setLeadHits] = useState<string[]>([]);
  const [linked, setLinked] = useState<{ code: string; name: string; entityId: string } | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (open) { setF(blank); setClientHits([]); setLeadHits([]); setLinked(null); setErr(''); setSaving(false); } }, [open]);

  const set = (k: string, v: any) => setF((p) => ({ ...p, [k]: v, ...(k === 'sector' ? { lens: ADAPT_SECT.includes(v) ? 'Adaptation' : 'Mitigation' } : {}) }));

  const checkDupes = (name: string) => {
    const q = normName(name);
    if (q.length < 3) { setClientHits([]); setLeadHits([]); return; }
    // Fuzzy on the DISTINCTIVE part of the name only: raw bigrams let the
    // corporate boilerplate dominate — "adani INDUSTRIES" scored a match with
    // "Veer Raj INDUSTRIES" on the strength of one generic word.
    const distinct = (a: string) => normName(String(a).replace(
      /\b(industries|industry|enterprises?|group|holdings?|corporation|corp|company|co|infra|infrastructure|international)\b/gi, ' '));
    const qd = distinct(name);
    const alike = (a: string) => {
      const an = normName(a);
      if (an.includes(q) || q.includes(an)) return true;   // prefix typing
      const ad = distinct(a);
      return !!qd && !!ad && nameAlike(qd, ad) >= 0.62;    // one-letter slips
    };
    setClientHits(Object.entries(db().clients)
      .filter(([, v]: any) => v?.name && !v.aliasOf && alike(v.name))
      .slice(0, 3)
      .map(([code, v]: any) => ({ code, name: v.name, rm: v.rm, entityId: v.entityId })));
    setLeadHits(db().leads
      .filter((x: any) => x.status === 'Active' && !x.conv && alike(x.company))
      .slice(0, 2)
      .map((x: any) => `An active lead for this company already exists: ${x.company} (${x.id}${x.rm ? ` · RM ${x.rm}` : ''}) — adding another is allowed, but check first.`));
  };

  const linkTo = (c: { code: string; name: string; entityId?: string }) => {
    if (!c.entityId) return;
    setLinked({ code: c.code, name: c.name, entityId: c.entityId });
    set('company', c.name);           // the canonical spelling travels with the link
    setClientHits([]);
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
    const r = await leadsService.create(
      { ...f, ...(linked ? { entityId: linked.entityId } : {}) }, user.full);
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
          <TextFld label="Company" required live value={f.company}
            onChange={(v) => { set('company', v); if (linked) setLinked(null); checkDupes(v); }} />
          <TextFld label="Contact person" value={f.contact} onChange={(v) => set('contact', v)} />
        </FieldGrid>
        {linked && (
          <Alert severity="success" sx={{ mt: 1, py: 0, fontSize: 12 }}
            onClose={() => setLinked(null)}>
            This lead will be attached to <b>{linked.name}</b> ({linked.code}) — its
            conversion adds a deal under that company, never a duplicate one.
          </Alert>
        )}
        {!linked && clientHits.map((c) => (
          <Alert key={c.code} severity="info" icon={false}
            sx={{ mt: 1, py: 0, fontSize: 12, cursor: c.entityId ? 'pointer' : 'default' }}
            onClick={() => linkTo(c)}
            action={c.entityId ? <Button size="small" onClick={() => linkTo(c)}>Attach</Button> : undefined}>
            Existing client: <b>{c.name}</b> ({c.code}{c.rm ? ` · RM ${c.rm}` : ''}) — same
            company? Attach the lead to it.
          </Alert>
        ))}
        {leadHits.map((d, i) => <Alert key={i} severity="warning" sx={{ mt: 1, py: 0, fontSize: 12 }}>{d}</Alert>)}
        <Box sx={{ mt: 1.4 }}>
          <FieldGrid>
            <SelectFld label="Sector" required value={f.sector} onChange={(v) => set('sector', v)} options={ref.getRefSync('Sector')} />
            <SelectFld label="Climate lens" value={f.lens} onChange={(v) => set('lens', v)} options={ref.getRefSync('Lens')} />
            <SelectFld label="Source" required value={f.source} onChange={(v) => set('source', v)} options={ref.getRefSync('Source')} />
            <TextFld label="Source detail" required={SRC_NEEDS_DETAIL.includes(f.source)} value={f.sourceDetail} onChange={(v) => set('sourceDetail', v)} />
            <SelectFld label="BDRM" required value={f.rm} onChange={(v) => set('rm', v)} options={ref.getRefSync('RM')} labels={ref.getRefLabels('RM')} />
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
