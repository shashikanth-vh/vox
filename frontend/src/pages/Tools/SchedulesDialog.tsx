import { useState, useEffect, useCallback } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, IconButton, Checkbox, FormControlLabel, Alert, Paper } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld, SelectFld } from '../../components/common/Field';
import { pulseService, type Schedule } from '../../services/pulseService';
import { newsService } from '../../services/newsService';
import { db } from '../../api/atlasStore';
import { useAuth } from '../../auth/AuthContext';
import { tokens } from '../../theme';

// Port of v12 AUGMENT 17 — openSchedules() / loadSchedules() / createSchedule().
const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const HOURS = Array.from({ length: 24 }, (_, i) => String(i));

export default function SchedulesDialog({ open, onClose, prefillAll }: { open: boolean; onClose: () => void; prefillAll?: boolean }) {
  const { user } = useAuth();
  const firms = Object.keys(db().clients || {}).length;

  const [rows, setRows] = useState<Schedule[]>([]);
  const [smtp, setSmtp] = useState(false);
  const [banner, setBanner] = useState('');
  const [q, setQ] = useState('');
  const [to, setTo] = useState('');
  const [cad, setCad] = useState<'daily' | 'weekly'>('daily');
  const [dow, setDow] = useState('Tue');
  const [hour, setHour] = useState('8');
  const [win, setWin] = useState('7');
  const [subj, setSubj] = useState('ATLAS news digest');
  const [all, setAll] = useState(false);
  const [adv, setAdv] = useState(false);

  // v12's scAllFirms(): fill the query with every firm term, lock it, force adverse-only.
  const applyAllFirms = useCallback((on: boolean) => {
    setAll(on);
    if (on) { setQ(newsService.allFirmTerms().join(', ')); setAdv(true); }
  }, []);

  const load = useCallback(async () => {
    const r = await pulseService.listSchedules();
    setRows(r.data?.schedules ?? []);
    setSmtp(!!r.data?.smtp);
    setBanner(r.ok ? '' : (r.error || ''));
  }, []);

  useEffect(() => {
    if (!open) return;
    setQ(''); setTo(''); setCad('daily'); setDow('Tue'); setHour('8'); setWin('7');
    setSubj('ATLAS news digest'); setAll(false); setAdv(false);
    load();
    if (prefillAll) applyAllFirms(true);
  }, [open, prefillAll, load, applyAllFirms]);

  const create = async () => {
    if (!q.trim()) { setBanner('Add at least one search term'); return; }
    if (!to.trim()) { setBanner('Add at least one recipient'); return; }
    const r = await pulseService.createSchedule({
      q: q.trim(), recipients: to.trim(), cadence: cad, weekday: DOW.indexOf(dow),
      hour: Number(hour) || 8, window_days: Number(win) || 7, adverse_only: adv,
      scope: all ? 'all-firms' : 'terms', subject: subj,
    }, user.full);
    if (r.ok) load(); else setBanner(r.error || 'Could not create the schedule');
  };

  const act = async (fn: Promise<{ ok: boolean; error?: string }>) => {
    const r = await fn;
    if (r.ok) load(); else setBanner(r.error || 'Action failed');
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>⏰ Scheduled news digests
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Alert severity={smtp ? 'success' : 'warning'} sx={{ py: 0, fontSize: 12, mb: 1.2 }}>
          {smtp ? 'Email is configured on the server.' : 'Email is not configured.'}
          {smtp && <Button sx={{ ml: 1 }} onClick={() => act(pulseService.sendTestEmail(to.trim()))}>Send test email</Button>}
        </Alert>
        {banner && <Alert severity="info" sx={{ py: 0, fontSize: 12, mb: 1.2 }}>{banner}</Alert>}

        <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.5, mb: 1.4 }}>
          <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.muted, fontWeight: 700, mb: 1 }}>
            New schedule
          </Typography>
          <FormControlLabel control={<Checkbox size="small" checked={all} onChange={(e) => applyAllFirms(e.target.checked)} />}
            label={<Typography sx={{ fontSize: 12.2 }}>Cover all firms on the register ({firms} firms + their watch terms)</Typography>} />
          <Box sx={{ mt: 1 }}><TextFld label="Search terms (comma separated)" value={q} onChange={setQ} disabled={all} multiline /></Box>
          <Box sx={{ mt: 1.4 }}><TextFld label="Recipients (comma separated)" value={to} onChange={setTo} /></Box>
          <Box sx={{ mt: 1.4 }}>
            <FieldGrid cols={4}>
              <SelectFld label="Cadence" value={cad} onChange={(v) => setCad(v)} options={['daily', 'weekly']} />
              <SelectFld label="Day" value={dow} onChange={setDow} options={DOW} disabled={cad !== 'weekly'} />
              <SelectFld label="Hour" value={hour} onChange={setHour} options={HOURS} />
              <TextFld label="Window (days)" value={win} onChange={setWin} />
            </FieldGrid>
          </Box>
          <Box sx={{ mt: 1.4 }}><TextFld label="Subject" value={subj} onChange={setSubj} /></Box>
          <FormControlLabel control={<Checkbox size="small" checked={adv} onChange={(e) => setAdv(e.target.checked)} />}
            label={<Typography sx={{ fontSize: 12.2 }}>Adverse items only</Typography>} />
          <Box><Button variant="contained" onClick={create}>Create schedule</Button></Box>
        </Box>

        <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.muted, fontWeight: 700, mb: 1 }}>
          Existing schedules
        </Typography>
        {rows.length ? rows.map((s) => (
          <Paper key={s.id} variant="outlined" sx={{ borderColor: tokens.line, p: 1.2, mb: 0.8, display: 'flex', alignItems: 'center', gap: 1 }}>
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: 12.8, fontWeight: 600 }}>
                {s.scope === 'all-firms' ? '🏢 All firms' : s.q.slice(0, 60) + (s.q.length > 60 ? '…' : '')}
              </Typography>
              <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                {s.cadence === 'weekly' ? `Weekly · ${DOW[s.weekday] || '—'}` : 'Daily'} at {s.hour}:00 ·
                {' '}{s.window_days}d window · {s.recipients}
                {s.adverse_only && <b style={{ color: tokens.bad }}> · ADVERSE ONLY</b>}
              </Typography>
            </Box>
            <Button onClick={() => act(pulseService.runSchedule(s.id))}>Run now</Button>
            <Button color="error" onClick={() => act(pulseService.deleteSchedule(s.id))}>Delete</Button>
          </Paper>
        )) : <Typography sx={{ fontSize: 12.4, color: tokens.muted }}>No schedules yet.</Typography>}
      </DialogContent>
      <DialogActions><Button onClick={onClose} variant="outlined">Close</Button></DialogActions>
    </Dialog>
  );
}
