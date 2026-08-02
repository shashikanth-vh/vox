import { useState, useEffect } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, IconButton, Checkbox, FormControlLabel, Alert } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld } from '../../components/common/Field';
import { leadsService } from '../../services/leadsService';
import { writeAudit } from '../../services/auditService';
import { today } from '../../api/atlasStore';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { tokens } from '../../theme';

// Port of v12 AUGMENT 12 — openApplication() / submitApplication().
const blank = { co: '', cp: '', ph: '', em: '', se: '', amt: '', pu: '' };

export default function ApplicationDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user } = useAuth();
  const ro = !can(user.roles, 'createClient');
  const [f, setF] = useState(blank);
  const [c1, setC1] = useState(false);
  const [c2, setC2] = useState(false);
  const [err, setErr] = useState('');
  useEffect(() => { if (open) { setF(blank); setC1(false); setC2(false); setErr(''); } }, [open]);

  const set = (k: keyof typeof blank, v: string) => setF((p) => ({ ...p, [k]: v }));

  // v12's validation order — company, then email, then phone, then both consents.
  const submit = async () => {
    if (ro) return;
    const co = f.co.trim(), em = f.em.trim(), ph = f.ph.trim();
    if (!co) { setErr('Company name is required'); return; }
    if (em && !/^[\w.+-]+@[\w-]+\.[\w.]{2,}$/.test(em)) { setErr('Enter a valid email address'); return; }
    if (ph && !/^(\+91)?[6-9]\d{9}$/.test(ph.replace(/[\s-]/g, ''))) { setErr('Enter a valid 10-digit Indian mobile'); return; }
    if (!c1 || !c2) { setErr('Both consents are required to proceed'); return; }

    const r = await leadsService.create({
      company: co, sector: f.se, lens: '', source: 'Application Form', rm: user.name,
      status: 'Active', temp: 'Hot', contact: em, phone: f.ph, createdAt: today(),
      notes: 'Application: ' + f.pu + (f.amt ? ' · ask ₹' + f.amt + ' Cr' : ''),
    } as any, user.full);
    // No lead means no consent to record against it — keep the form open with the reason.
    if (!r.ok || !r.lead) { setErr(r.error || 'Could not create the lead'); return; }
    // Consent is audited separately from the lead — it is the record that matters.
    writeAudit(user.full, 'Consent recorded', r.lead.id, 'data-processing + bureau consent · ' + co);
    onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>📝 Application form — client intake
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <FieldGrid>
          <TextFld label="Company / borrower" required value={f.co} onChange={(v) => set('co', v)} placeholder="Legal name" />
          <TextFld label="Contact person" value={f.cp} onChange={(v) => set('cp', v)} />
          <TextFld label="Phone" value={f.ph} onChange={(v) => set('ph', v)} type="tel" />
          <TextFld label="Email" value={f.em} onChange={(v) => set('em', v)} type="email" />
          <TextFld label="Sector" value={f.se} onChange={(v) => set('se', v)} placeholder="e.g. Solar - General" />
          <TextFld label="Ticket size (₹ Cr)" value={f.amt} onChange={(v) => set('amt', v)} />
        </FieldGrid>
        <Box sx={{ mt: 1.4 }}><TextFld label="Purpose / requirement" value={f.pu} onChange={(v) => set('pu', v)} multiline /></Box>

        <Box sx={{ mt: 1.6, border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.5, bgcolor: '#F7FAFB' }}>
          <FormControlLabel control={<Checkbox size="small" checked={c1} onChange={(e) => setC1(e.target.checked)} />}
            label={<Typography sx={{ fontSize: 12.2 }}>I consent to Evam Finance processing this data to assess the application.</Typography>} />
          <FormControlLabel control={<Checkbox size="small" checked={c2} onChange={(e) => setC2(e.target.checked)} />}
            label={<Typography sx={{ fontSize: 12.2 }}>I authorise a credit-bureau (CIBIL) pull for this assessment.</Typography>} />
        </Box>
        <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 0.8 }}>
          Both consents are recorded in the audit trail with a timestamp. Live mode submits this as
          POST /api/register/leads with the consent flags.
        </Typography>
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
        {ro && <Alert severity="info" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>Management role is view-only.</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined">Cancel</Button>
        <Button onClick={submit} disabled={ro} variant="contained">Submit application</Button>
      </DialogActions>
    </Dialog>
  );
}
