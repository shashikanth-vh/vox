import { useState, useEffect } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, Checkbox, FormControlLabel, IconButton, Alert } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld, SelectFld } from '../../components/common/Field';
import { CodeText } from '../../components/common/Pills';
import { referenceService } from '../../services/referenceService';
import { conversionService, mintCode } from '../../services/conversionService';
import { useAuth } from '../../auth/AuthContext';
import { tokens } from '../../theme';
import type { Lead } from './lead.types';

const ADAPT_SECT = ['Industrial Water', 'Water Treatment / WASH', 'Climate Data & IoT', 'Agri / Drone'];

// v17 product flag chip (L / S / AM) that sits before each product-box title.
function Flag({ label, color }: { label: string; color: string }) {
  return <Box component="span" sx={{ display: 'inline-block', borderRadius: '5px', px: '7px', py: '2px', fontSize: 10.4, fontWeight: 700, color: '#fff', bgcolor: color, mr: 0.8 }}>{label}</Box>;
}

export default function PushToDealsDialog({ lead, onClose, onDone }: { lead: Lead | null; onClose: () => void; onDone: () => void }) {
  const { user } = useAuth();
  const ref = referenceService;
  const [existingCode, setExistingCode] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  // v17 default: Syndication ticked, Lending/AM off.
  const [flags, setFlags] = useState({ lend: false, syn: true, am: false });
  const [cl, setCl] = useState({ sector: 'Other', lens: 'Mitigation', state: '', toi: '', about: '', an: '', temp: 'Warm', source: 'BDRM', sourceDetail: '' });
  // Forms spec v2.1: amounts have NO default (removed the 2 / 10 defaults that silently
  // polluted data). Start at 0 and hard-block the push until a real figure is entered.
  const [lend, setLend] = useState({ amt: 0, stage: 'Data Awaited' });
  const [syn, setSyn] = useState({ amt: 0, synType: 'Fee will be paid by customer', mstat3: 'Under discussion', status: 'Deal Sourced', fac: '', tenor: '', pri: 'Medium', im: 'Work not started', pot: '', exist: '', price: '' });
  const [am, setAm] = useState({ val: 0, mw: 0, dtype: '', status: 'Teaser Prepared' });

  useEffect(() => {
    if (!lead) return;
    const ex = conversionService.findExistingCode(lead.company);
    setExistingCode(ex); setCode(ex ?? mintCode(lead.company)); setErr(''); setBusy(false);
    setFlags({ lend: false, syn: true, am: false });
    setCl((p) => ({ ...p, sector: ref.getRefSync('Sector').includes(lead.sector) ? lead.sector : 'Other', lens: lead.lens || 'Mitigation', temp: lead.temp || 'Warm', source: lead.source || 'BDRM', sourceDetail: lead.sourceDetail || '' }));
  }, [lead]);

  if (!lead) return null;

  const push = async () => {
    // Forms spec: ≥1 product; State mandatory; each ticked line's amount mandatory (no default).
    if (!flags.lend && !flags.syn && !flags.am) { setErr('Pick at least one product line to push.'); return; }
    if (!cl.state.trim()) { setErr('State is required to push.'); return; }
    if (flags.lend && !(Number(lend.amt) > 0)) { setErr('Lending amount (₹ Cr) is required.'); return; }
    if (flags.syn && !(Number(syn.amt) > 0)) { setErr('Syndication ask (₹ Cr) is required.'); return; }
    if (flags.am && !(Number(am.val) > 0)) { setErr('AM indicative value (₹ Cr) is required.'); return; }
    setErr('');
    // Qualification then conversion — two workflow calls, so the dialog stays open and
    // busy until both land rather than closing on a push that may still be refused.
    setBusy(true);
    const r = await conversionService.pushLeadToDeals(lead, {
      code, existing: !!existingCode,
      client: { sector: cl.sector, lens: cl.lens, state: cl.state, about: cl.about, toi: cl.toi },
      deal: { temp: cl.temp, source: cl.source, sourceDetail: cl.sourceDetail, an: cl.an === '—' ? '' : cl.an },
      flags, lending: lend, syndication: syn, am,
    }, user.full);
    setBusy(false);
    if (!r.ok) { setErr(r.error || 'Could not push this lead to Deals.'); return; }
    onDone(); onClose();
  };

  // v17 prodbox: dimmed + non-interactive when its checkbox is off.
  const ProductBox = ({ k, flag, color, title, children }: { k: 'lend' | 'syn' | 'am'; flag: string; color: string; title: string; children: React.ReactNode }) => {
    const on = flags[k];
    return (
      <Box sx={{ border: `1px solid ${on ? color : tokens.line}`, borderRadius: 2, p: 1.3, my: 1, opacity: on ? 1 : 0.5, transition: 'opacity .15s ease' }}>
        <FormControlLabel sx={{ m: 0 }} control={<Checkbox sx={{ p: 0.5, mr: 0.5 }} checked={on} onChange={(e) => setFlags((p) => ({ ...p, [k]: e.target.checked }))} />}
          label={<Typography sx={{ fontWeight: 700, fontSize: 13, display: 'flex', alignItems: 'center' }}><Flag label={flag} color={color} />{title}</Typography>} />
        <Box sx={{ mt: 1, pointerEvents: on ? 'auto' : 'none' }}>{children}</Box>
      </Box>
    );
  };

  const sect = (t: string) => (
    <Typography sx={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.teal, fontWeight: 800, mt: 2, mb: 1 }}>{t}</Typography>
  );

  return (
    <Dialog open={!!lead} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>Push to Deals — {lead.company}
        <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Group Code: <CodeText code={code} /> {existingCode ? '(existing client — records will merge)' : '(new)'}</Typography>
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.teal, fontWeight: 800, mb: 1 }}>Client</Typography>
        <FieldGrid>
          <SelectFld label="Segment / sector" required value={cl.sector} onChange={(v) => setCl((p) => ({ ...p, sector: v, lens: ADAPT_SECT.includes(v) ? 'Adaptation' : 'Mitigation' }))} options={ref.getRefSync('Sector')} />
          <SelectFld label="Climate lens" value={cl.lens} onChange={(v) => setCl((p) => ({ ...p, lens: v }))} options={ref.getRefSync('Lens')} />
          <TextFld label="State" required value={cl.state} onChange={(v) => setCl((p) => ({ ...p, state: v }))} />
          <TextFld label="Type of industry" value={cl.toi} onChange={(v) => setCl((p) => ({ ...p, toi: v }))} placeholder="EPC / IPP / Manufacturing / Services" />
          <SelectFld label="Allot analyst" value={cl.an} onChange={(v) => setCl((p) => ({ ...p, an: v }))} options={ref.getRefSync('Analyst')} blank />
          <SelectFld label="Temperature" value={cl.temp} onChange={(v) => setCl((p) => ({ ...p, temp: v }))} options={ref.getRefSync('Temperature')} />
          {/* Source + Source detail are LOCKED from the lead (Forms spec) — pre-filled, not editable. */}
          <SelectFld label="Source" value={cl.source} disabled onChange={(v) => setCl((p) => ({ ...p, source: v }))} options={ref.getRefSync('Source')} />
          <TextFld label="Source detail" value={cl.sourceDetail} disabled onChange={(v) => setCl((p) => ({ ...p, sourceDetail: v }))} />
        </FieldGrid>
        <Box sx={{ mt: 1 }}><TextFld label="About the company" value={cl.about} onChange={(v) => setCl((p) => ({ ...p, about: v }))} placeholder="3–4 lines: what they do, scale, why it fits Evam" multiline /></Box>

        {sect('Products')}
        <ProductBox k="lend" flag="L" color={tokens.lend} title="Lending (own book)">
          <FieldGrid>
            <TextFld label="Amount ₹ Cr" required type="number" value={lend.amt || ''} onChange={(v) => setLend((p) => ({ ...p, amt: v }))} placeholder="required — no default" />
            <SelectFld label="Stage" required value={lend.stage} onChange={(v) => setLend((p) => ({ ...p, stage: v }))} options={ref.getRefSync('Lending Stage')} />
          </FieldGrid>
        </ProductBox>
        <ProductBox k="syn" flag="S" color={tokens.synd} title="Platform Deals">
          <FieldGrid>
            <TextFld label="Ask ₹ Cr" required type="number" value={syn.amt || ''} onChange={(v) => setSyn((p) => ({ ...p, amt: v }))} placeholder="required — no default" />
            <SelectFld label="Platform Deals type" required value={syn.synType} onChange={(v) => setSyn((p) => ({ ...p, synType: v }))} options={ref.getRefSync('Platform Deals Type')} />
            {syn.synType === 'Fee will be paid by customer' && <SelectFld label="Mandate status" value={syn.mstat3} onChange={(v) => setSyn((p) => ({ ...p, mstat3: v }))} options={ref.getRefSync('Mandate Status 3')} />}
            <SelectFld label="Status" required value={syn.status} onChange={(v) => setSyn((p) => ({ ...p, status: v }))} options={ref.getRefSync('Status of Proposal')} />
            <TextFld label="Facility type" value={syn.fac} onChange={(v) => setSyn((p) => ({ ...p, fac: v }))} placeholder="Term Loan / WC / Leasing…" />
            <SelectFld label="Tenor" value={syn.tenor} onChange={(v) => setSyn((p) => ({ ...p, tenor: v }))} options={ref.getRefSync('Tenor')} blank />
            <SelectFld label="Priority" value={syn.pri} onChange={(v) => setSyn((p) => ({ ...p, pri: v }))} options={ref.getRefSync('Priority')} />
            <SelectFld label="IM in place" value={syn.im} onChange={(v) => setSyn((p) => ({ ...p, im: v }))} options={ref.getRefSync('IM in Place')} />
          </FieldGrid>
          <Box sx={{ mt: 1 }}>
            <TextFld label="Potential lenders (after checking with client)" value={syn.pot} onChange={(v) => setSyn((p) => ({ ...p, pot: v }))} />
            <Box sx={{ mt: 1 }}><TextFld label="Existing lenders" value={syn.exist} onChange={(v) => setSyn((p) => ({ ...p, exist: v }))} /></Box>
            <Box sx={{ mt: 1 }}><TextFld label="Pricing expectation" value={syn.price} onChange={(v) => setSyn((p) => ({ ...p, price: v }))} placeholder="e.g. 11–12%" /></Box>
          </Box>
        </ProductBox>
        <ProductBox k="am" flag="AM" color={tokens.am} title="Asset Monetisation">
          <FieldGrid>
            <TextFld label="Indicative value ₹ Cr" required type="number" value={am.val || ''} onChange={(v) => setAm((p) => ({ ...p, val: v }))} />
            <TextFld label="Size MW" type="number" value={am.mw || ''} onChange={(v) => setAm((p) => ({ ...p, mw: v }))} />
            <TextFld label="Deal type" value={am.dtype} onChange={(v) => setAm((p) => ({ ...p, dtype: v }))} placeholder="Capital Market / Project Advisory" />
            <SelectFld label="Status" required value={am.status} onChange={(v) => setAm((p) => ({ ...p, status: v }))} options={ref.getRefSync('Asset Mon Status')} />
          </FieldGrid>
        </ProductBox>
        {err && <Alert severity="warning" sx={{ mt: 1.4, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Typography sx={{ fontSize: 11.6, color: tokens.muted, mr: 'auto' }}>One save: client + deal + product rows; the lead leaves the register.</Typography>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        <Button onClick={push} variant="contained" disabled={busy}>{busy ? 'Qualifying & converting…' : 'Push to Deals'}</Button>
      </DialogActions>
    </Dialog>
  );
}
