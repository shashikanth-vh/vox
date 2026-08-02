import { useState } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, IconButton, Alert } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld } from '../../components/common/Field';
import { db, today } from '../../api/atlasStore';
import { leadsService } from '../../services/leadsService';
import { notesService } from '../../services/notesService';
import { writeAudit } from '../../services/auditService';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { tokens } from '../../theme';

// Port of v12 AUGMENT 12 — mailParse(). Pure client-side regex extraction, no network.
interface Parsed { company: string; amt: string; lender: string; stage: string; email: string; phone: string; knownCode: string }

function mailParse(t: string): Parsed {
  const low = t.toLowerCase();
  const amt = (t.match(/(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(cr|crore)?/i)
    || t.match(/([\d,]+(?:\.\d+)?)\s*(?:cr|crore)/i) || [])[1] || '';
  const email = (t.match(/[\w.+-]+@[\w-]+\.[\w.]+/) || [])[0] || '';
  const phone = (t.match(/(?:\+91[\s-]?)?[6-9]\d{9}/) || [])[0] || '';
  const lender = (db().lenders || []).map((l: any) => l.name)
    .find((n: string) => n && low.indexOf(n.toLowerCase()) > -1) || '';
  const stage = low.indexOf('sanction') > -1 ? 'Sanctioned'
    : low.indexOf('term sheet') > -1 ? 'IP Received'
      : (low.indexOf('information memorandum') > -1 || /\bim\b/.test(low)) ? 'IM Circulated' : '';
  const known = Object.entries(db().clients || {}).find(([, v]: any) =>
    low.indexOf(String(v.name || '').toLowerCase().split(' ')[0]) > -1 && String(v.name || '').length > 3);
  // Not a known client? Fall back to a "M/s …" / "from …" pattern for the name.
  const co = known ? (known[1] as any).name
    : ((t.match(/(?:m\/s\.?|from)\s+([A-Z][A-Za-z& ]{3,40})/i) || [])[1] || '').trim();
  return {
    company: co, amt: amt.replace(/,/g, ''), lender, stage, email, phone,
    knownCode: known ? known[0] : '',
  };
}

export default function MailIntakeDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user } = useAuth();
  const ro = !can(user.roles, 'addLead');
  const [text, setText] = useState('');
  const [p, setP] = useState<Parsed | null>(null);
  const [err, setErr] = useState('');

  const close = () => { setText(''); setP(null); setErr(''); onClose(); };
  const scrape = () => {
    if (!text.trim()) { setErr('Paste the email first'); return; }
    setErr(''); setP(mailParse(text));
  };
  const set = (k: keyof Parsed, v: string) => setP((prev) => (prev ? { ...prev, [k]: v } : prev));

  const toLead = async () => {
    if (ro || !p) return;
    const co = p.company.trim();
    if (!co) { setErr('Company name needed'); return; }
    const r = await leadsService.create({
      company: co, sector: '', lens: '', source: 'Mail intake', rm: user.name,
      status: 'Active', temp: 'Warm', contact: p.email, phone: p.phone, createdAt: today(),
      notes: 'Scraped from email' + (p.lender ? ' · lender: ' + p.lender : '') + (p.amt ? ' · ask ₹' + p.amt + ' Cr' : ''),
    } as any, user.full);
    // Keep the parsed email on screen when the API refuses it — re-parsing is manual work.
    if (!r.ok || !r.lead) { setErr(r.error || 'Could not create the lead'); return; }
    writeAudit(user.full, 'Mail parsed', r.lead.id, 'lead created from email — ' + co);
    close();
  };

  const toUpdate = () => {
    if (ro || !p || !p.knownCode) return;
    const note = 'From email: ' + (p.stage ? 'stage signal ' + p.stage + '. ' : '')
      + (p.lender ? 'Lender ' + p.lender + '. ' : '') + (p.amt ? '₹' + p.amt + ' Cr discussed.' : '');
    notesService.add(p.knownCode, note, user.full);
    writeAudit(user.full, 'Mail parsed', p.knownCode, 'email logged as update');
    close();
  };

  return (
    <Dialog open={open} onClose={close} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>📧 Mail intake — paste, we scrape
        <IconButton onClick={close} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 12.4, color: tokens.muted, mb: 1 }}>
          Paste a client / lender email. Company, amount, lender and stage signals are extracted into a
          draft you confirm — nothing is saved without your click (consent-first). Live mode does the same
          via the VOX/PULSE adapters.
        </Typography>
        <TextFld label="" value={text} onChange={setText} multiline minRows={6}
          placeholder="Paste the email text here…" />
        {err && <Alert severity="warning" sx={{ mt: 1, py: 0, fontSize: 12 }}>{err}</Alert>}
        {p && (
          <Box sx={{ mt: 1.6, border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.5 }}>
            <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.muted, fontWeight: 700, mb: 1 }}>
              Extracted draft — edit anything
            </Typography>
            <FieldGrid>
              <TextFld label="Company" value={p.company} onChange={(v) => set('company', v)} />
              <TextFld label="Amount (₹ Cr)" value={p.amt} onChange={(v) => set('amt', v)} />
              <TextFld label="Lender mentioned" value={p.lender} onChange={(v) => set('lender', v)} />
              <TextFld label="Stage signal" value={p.stage} onChange={(v) => set('stage', v)} />
              <TextFld label="Contact email" value={p.email} onChange={(v) => set('email', v)} />
              <TextFld label="Phone" value={p.phone} onChange={(v) => set('phone', v)} />
            </FieldGrid>
            <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 0.8 }}>
              Saving records the source as “Mail intake” in the audit trail.
            </Typography>
            {ro && <Typography sx={{ fontSize: 11.5, color: tokens.bad, mt: 0.5 }}>Management role is view-only.</Typography>}
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={close} variant="outlined">Close</Button>
        {p && p.knownCode && (
          <Button onClick={toUpdate} disabled={ro} variant="outlined">
            Log as update on {(db().clients[p.knownCode] || {}).name || p.knownCode}
          </Button>
        )}
        {p
          ? <Button onClick={toLead} disabled={ro} variant="contained">Create lead from this</Button>
          : <Button onClick={scrape} variant="contained">Scrape it</Button>}
      </DialogActions>
    </Dialog>
  );
}
