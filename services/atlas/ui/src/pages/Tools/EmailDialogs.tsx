import { useState, useEffect } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Typography, IconButton, Checkbox, FormControlLabel, Alert } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { TextFld } from '../../components/common/Field';
import { pulseService } from '../../services/pulseService';
import { news, SEV_LABEL } from '../../services/newsService';
import { db } from '../../api/atlasStore';
import { useAuth } from '../../auth/AuthContext';
import { tokens } from '../../theme';
import type { AdhocState } from './NewsRadar';

/* Port of v12 AUGMENT 17 openEmailNews() and AUGMENT 18 openEmailAllFirms().
   Both post to the PULSE backend, which is stubbed — see pulseService. */

export function EmailNewsDialog({ open, onClose, adhoc }: { open: boolean; onClose: () => void; adhoc: AdhocState }) {
  const { user } = useAuth();
  const [to, setTo] = useState('');
  const [subj, setSubj] = useState('');
  const [msg, setMsg] = useState('');
  const [sending, setSending] = useState(false);
  useEffect(() => { if (open) { setTo(''); setSubj('ATLAS news — ' + adhoc.term); setMsg(''); } }, [open, adhoc.term]);

  const send = async () => {
    if (!to.trim()) { setMsg('Add at least one recipient'); return; }
    setSending(true);
    const r = await pulseService.emailNews(
      { q: adhoc.term, from: adhoc.dfrom, to: adhoc.dto, recipients: to.trim(), subject: subj }, user.full);
    setSending(false);
    if (r.ok) onClose(); else setMsg(r.error || 'Send failed');
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>📧 Email these results
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 12.4, color: tokens.muted, mb: 1.2 }}>
          “{adhoc.term}” · {adhoc.items.length} articles
          {adhoc.dfrom || adhoc.dto ? ` · ${adhoc.dfrom || '…'} to ${adhoc.dto || '…'}` : ' · last 3 months'}
        </Typography>
        <TextFld label="Recipients (comma separated)" value={to} onChange={setTo} multiline />
        <Typography sx={{ mt: 1.4 }} />
        <TextFld label="Subject" value={subj} onChange={setSubj} />
        {msg && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{msg}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined">Cancel</Button>
        <Button onClick={send} disabled={sending} variant="contained">{sending ? 'Sending…' : 'Send'}</Button>
      </DialogActions>
    </Dialog>
  );
}

export function EmailAllFirmsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user } = useAuth();
  const all = news();
  const reds = all.filter((n) => n.severity === 'RED').length;
  const ambs = all.filter((n) => n.severity === 'AMBER').length;
  const [to, setTo] = useState('');
  const [subj, setSubj] = useState('ATLAS — all-firms news digest');
  const [adv, setAdv] = useState(false);
  const [msg, setMsg] = useState('');
  const [sending, setSending] = useState(false);
  useEffect(() => { if (open) { setTo(''); setMsg(''); } }, [open]);

  const send = async () => {
    if (!to.trim()) { setMsg('Add at least one recipient'); return; }
    // v12 groups client-side by firm and remaps the severity vocabulary for the mailer.
    const by: Record<string, any[]> = {};
    all.forEach((n) => {
      const name = db().clients?.[n.code]?.name || n.code || '—';
      (by[name] = by[name] || []).push({
        title: n.headline, url: n.url, source: n.source, when: n.when, via: '',
        severity: SEV_LABEL[n.severity],
      });
    });
    const groups = Object.keys(by).map((k) => ({ term: k, articles: by[k] }));
    setSending(true);
    const r = await pulseService.emailDigest({ recipients: to.trim(), subject: subj, groups, adverse_only: adv }, user.full);
    setSending(false);
    if (r.ok) onClose(); else setMsg(r.error || 'Send failed');
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>📧 Email firms’ news
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 12.4, color: tokens.muted, mb: 1.2 }}>
          {all.length} items on file · <b style={{ color: tokens.bad }}>{reds}</b> ugly · <b style={{ color: tokens.warn }}>{ambs}</b> bad
        </Typography>
        <TextFld label="Recipients (comma separated)" value={to} onChange={setTo} multiline />
        <Typography sx={{ mt: 1.4 }} />
        <TextFld label="Subject" value={subj} onChange={setSubj} />
        <FormControlLabel sx={{ mt: 0.5 }} control={<Checkbox size="small" checked={adv} onChange={(e) => setAdv(e.target.checked)} />}
          label={<Typography sx={{ fontSize: 12.2 }}>Adverse items only (ugly + bad)</Typography>} />
        {msg && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{msg}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined">Cancel</Button>
        <Button onClick={send} disabled={sending} variant="contained">{sending ? 'Sending…' : 'Send digest'}</Button>
      </DialogActions>
    </Dialog>
  );
}
