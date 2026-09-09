import { useState, useEffect, useCallback } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, IconButton, Checkbox, FormControlLabel, Alert, Paper } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld, SelectFld } from '../../components/common/Field';
import { pulseService, type Schedule } from '../../services/pulseService';
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
  const [storeErr, setStoreErr] = useState('');
  const [banner, setBanner] = useState('');
  // A failure shown in calm blue reads as information. Every banner here was a failure
  // and every one of them said 'info'; a test email that WORKED said nothing at all.
  const [bannerOk, setBannerOk] = useState(false);
  const [q, setQ] = useState('');
  const [to, setTo] = useState('');
  const [cad, setCad] = useState<'daily' | 'weekly'>('daily');
  const [dow, setDow] = useState('Tue');
  const [hour, setHour] = useState('8');
  const [win, setWin] = useState('7');
  const [subj, setSubj] = useState('ATLAS news digest');
  const [all, setAll] = useState(false);
  const [adv, setAdv] = useState(false);
  // Which schedule the form is editing — null means the form creates a new one.
  const [editId, setEditId] = useState<string | null>(null);
  // Which schedule row is expanded to show its full details and run history.
  const [openId, setOpenId] = useState<string | null>(null);

  const resetForm = useCallback(() => {
    setQ(''); setTo(''); setCad('daily'); setDow('Tue'); setHour('8'); setWin('7');
    setSubj('ATLAS news digest'); setAll(false); setAdv(false); setEditId(null);
  }, []);

  // "All firms" MEANS all firms: the server reads the register at send time, so no
  // term list is snapshotted into the schedule any more (v12 froze one here — the
  // wall of names that made every all-firms schedule unreadable and instantly stale).
  const applyAllFirms = useCallback((on: boolean) => {
    setAll(on);
    if (on) { setQ(''); setAdv(true); }
  }, []);

  const load = useCallback(async () => {
    const r = await pulseService.listSchedules();
    setRows(r.data?.schedules ?? []);
    setSmtp(!!r.data?.smtp);
    // store_ok is absent on an older server — only an explicit false is a failure.
    setStoreErr(r.data?.store_ok === false
      ? (r.data?.store_error || 'The schedule store is not writable.') : '');
    setBannerOk(false);
    setBanner(r.ok ? '' : (r.error || ''));
  }, []);

  useEffect(() => {
    if (!open) return;
    resetForm();
    load();
    if (prefillAll) applyAllFirms(true);
  }, [open, prefillAll, load, applyAllFirms, resetForm]);

  const startEdit = (s: Schedule) => {
    setEditId(s.id);
    setQ(s.q);
    // recipients is normalised to a string in the service; String() keeps this safe
    // even against an older cached bundle handing over the raw list.
    setTo(Array.isArray(s.recipients as unknown) ? (s.recipients as unknown as string[]).join(', ') : String(s.recipients ?? ''));
    setCad(s.cadence === 'weekly' ? 'weekly' : 'daily');
    setDow(DOW[s.weekday] || 'Tue');
    setHour(String(s.hour)); setWin(String(s.window_days));
    setSubj(s.subject || 'ATLAS news digest');
    setAll(s.scope === 'all-firms'); setAdv(!!s.adverse_only);
    setBanner('');
  };

  const save = async () => {
    // String() first: whatever shape state ends up in, a save must never die on a
    // thrown .trim() — silently doing NOTHING is the one unacceptable outcome.
    try {
      const qs = String(q ?? '').trim();
      const tos = String(to ?? '').trim();
      // An all-firms schedule reads the register at send time — typed terms optional.
      if (!qs && !all) { setBannerOk(false); setBanner('Add at least one search term'); return; }
      if (!tos) { setBannerOk(false); setBanner('Add at least one recipient'); return; }
      const payload = {
        // Not `Number(hour) || 8` — midnight is hour 0, and 0 is falsy.
        // All-firms saves clear any stored term list: the register is the list now.
        q: all ? '' : qs, recipients: tos, cadence: cad, weekday: DOW.indexOf(dow),
        hour: Number.isFinite(Number(hour)) ? Number(hour) : 8,
        window_days: Number(win) || 7, adverse_only: adv,
        scope: all ? 'all-firms' : 'terms', subject: subj,
      } as const;
      const r = editId
        ? await pulseService.updateSchedule(editId, payload, user.full)
        : await pulseService.createSchedule(payload, user.full);
      if (r.ok) { resetForm(); setBannerOk(true); setBanner(editId ? 'Schedule updated.' : ''); load(); }
      else { setBannerOk(false); setBanner(r.error || 'Could not save the schedule'); }
    } catch (e: any) {
      setBannerOk(false);
      setBanner(`Could not save the schedule: ${e?.message || e}`);
    }
  };

  // The alert at the top of the dialog scrolls out of sight behind a long term list —
  // an edit that failed there looked exactly like one that saved. The same message
  // renders beside whichever Save the user actually pressed.
  const feedback = banner ? (
    <Typography sx={{ fontSize: 11.5, alignSelf: 'center',
                      color: bannerOk ? tokens.muted : tokens.bad }}>{banner}</Typography>
  ) : null;

  const act = async (fn: Promise<{ ok: boolean; error?: string; data?: any }>,
                     okMessage?: string) => {
    const r: any = await fn;
    setBannerOk(!!r.ok);
    if (r.ok) {
      setBanner(okMessage ? (r.data?.message ? `${okMessage} ${r.data.message}` : okMessage) : '');
      load();
    } else setBanner(r.error || 'Action failed');
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>⏰ Scheduled news digests
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Alert severity={smtp ? 'success' : 'warning'} sx={{ py: 0, fontSize: 12, mb: 1.2 }}>
          {smtp ? 'Email is configured on the server.' : 'Email is not configured.'}
          {smtp && <Button sx={{ ml: 1 }} onClick={() => act(pulseService.sendTestEmail(String(to ?? '').trim()), 'Test email sent.')}>Send test email</Button>}
        </Alert>
        {storeErr && (
          <Alert severity="error" sx={{ py: 0, fontSize: 12, mb: 1.2 }}>
            Schedules cannot be saved right now: {storeErr} Anything created here will be
            lost when the service restarts — ask an admin to fix the volume first.
          </Alert>
        )}
        {banner && (
          <Alert severity={bannerOk ? 'success' : 'error'} onClose={() => setBanner('')}
            sx={{ py: 0, fontSize: 12, mb: 1.2 }}>{banner}</Alert>
        )}

        <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.5, mb: 1.4 }}>
          <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.muted, fontWeight: 700, mb: 1 }}>
            {editId ? 'Edit schedule' : 'New schedule'}
          </Typography>
          <FormControlLabel control={<Checkbox size="small" checked={all} onChange={(e) => applyAllFirms(e.target.checked)} />}
            label={<Typography sx={{ fontSize: 12.2 }}>Cover all firms on the register ({firms} firms + their watch terms)</Typography>} />
          {all && (
            <Typography sx={{ fontSize: 11.3, color: tokens.muted, ml: 3.5, mt: -0.5 }}>
              Covers every firm on the register at send time — firms added later are
              included automatically, no term list is stored. Want only a few firms?
              Untick this and type them below.
            </Typography>
          )}
          {!all && (
            <Box sx={{ mt: 1 }}><TextFld label="Search terms (comma separated)" value={q} onChange={setQ} multiline /></Box>
          )}
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
          <Box sx={{ display: 'flex', gap: 1 }}>
            <Button variant="contained" onClick={save}>{editId ? 'Save changes' : 'Create schedule'}</Button>
            {editId && <Button variant="outlined" onClick={resetForm}>Cancel edit</Button>}
            {feedback}
          </Box>
        </Box>

        <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.muted, fontWeight: 700, mb: 1 }}>
          Existing schedules
        </Typography>
        {rows.length ? rows.map((s) => (
          <Paper key={s.id} variant="outlined" sx={{ borderColor: tokens.line, p: 1.2, mb: 0.8 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              {/* The summary is the door: click anywhere on it to see the whole
                  schedule without entering edit mode. */}
              <Box sx={{ flex: 1, minWidth: 0, cursor: 'pointer' }}
                onClick={() => setOpenId(openId === s.id ? null : s.id)}>
                <Typography sx={{ fontSize: 12.8, fontWeight: 600 }}>
                  {s.scope === 'all-firms' ? '🏢 All firms' : s.q.slice(0, 60) + (s.q.length > 60 ? '…' : '')}
                  <span style={{ color: tokens.muted, fontWeight: 400 }}>{openId === s.id ? ' ▾' : ' ▸'}</span>
                </Typography>
                <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                  {s.cadence === 'weekly' ? `Weekly · ${DOW[s.weekday] || '—'}` : 'Daily'} at {s.hour}:00 ·
                  {' '}{s.window_days}d window · {s.recipients}
                  {s.adverse_only && <b style={{ color: tokens.bad }}> · ADVERSE ONLY</b>}
                </Typography>
                {(() => {
                  const last = s.history?.[s.history.length - 1];
                  if (!last) return null;
                  return (
                    <Typography sx={{ fontSize: 11, color: last.ok ? tokens.muted : tokens.bad }}>
                      Last run {new Date(last.at * 1000).toLocaleString()} — {last.ok
                        ? `sent ${last.items ?? 0} item(s) for ${last.firms ?? 0} firm(s)`
                        : `FAILED: ${last.note}`}
                    </Typography>
                  );
                })()}
              </Box>
              <Button onClick={() => startEdit(s)}>Edit</Button>
              <Button onClick={() => act(pulseService.runSchedule(s.id))}>Run now</Button>
              <Button color="error" onClick={() => act(pulseService.deleteSchedule(s.id))}>Delete</Button>
            </Box>
            {openId === s.id && (
              <Box sx={{ mt: 1, pt: 1, borderTop: `1px dashed ${tokens.line}`, fontSize: 11.6 }}>
                <Typography sx={{ fontSize: 11.6 }}><b>Subject:</b> {s.subject || 'ATLAS news digest'}</Typography>
                <Typography sx={{ fontSize: 11.6 }}><b>Recipients:</b> {s.recipients}</Typography>
                <Typography sx={{ fontSize: 11.6 }}>
                  <b>Next run:</b> {s.next_run ? new Date(s.next_run * 1000).toLocaleString() : '—'}
                </Typography>
                <Typography sx={{ fontSize: 11.6 }}>
                  <b>Covers:</b> {s.scope === 'all-firms'
                    ? `every firm on the register at send time${s.q ? ', plus the stored terms below' : ''}`
                    : 'the terms below'}
                </Typography>
                {!!s.q && (
                  <Typography sx={{ fontSize: 11.2, color: tokens.muted, wordBreak: 'break-word' }}>
                    {s.q.slice(0, 300) + (s.q.length > 300 ? ` … (${s.q.split(',').length} terms)` : '')}
                  </Typography>
                )}
                {!!s.history?.length && (
                  <Box sx={{ mt: 0.8 }}>
                    <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.muted, fontWeight: 700 }}>
                      Recent runs
                    </Typography>
                    {[...s.history].reverse().map((h, i) => (
                      <Typography key={i} sx={{ fontSize: 11.2, color: h.ok ? tokens.muted : tokens.bad }}>
                        {new Date(h.at * 1000).toLocaleString()} — {h.ok
                          ? `sent ${h.items ?? 0} item(s) for ${h.firms ?? 0} firm(s)` : h.note}
                      </Typography>
                    ))}
                  </Box>
                )}
              </Box>
            )}
          </Paper>
        )) : <Typography sx={{ fontSize: 12.4, color: tokens.muted }}>No schedules yet.</Typography>}
      </DialogContent>
      <DialogActions>
        {/* The form's own button scrolls away behind the schedule list — the footer
            carries the same action so Save is always in reach, next to Close. */}
        {feedback}
        <Button variant="contained" onClick={save}>{editId ? 'Save changes' : 'Create schedule'}</Button>
        {editId && <Button variant="outlined" onClick={resetForm}>Cancel edit</Button>}
        <Button onClick={onClose} variant="outlined">Close</Button>
      </DialogActions>
    </Dialog>
  );
}
