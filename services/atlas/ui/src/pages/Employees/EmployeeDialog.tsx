import { useState, useEffect, useMemo } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, FormControlLabel, Switch, Box, Alert, IconButton, CircularProgress } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld, SelectFld, MultiSelectFld } from '../../components/common/Field';
import { employeesService } from '../../services/employeesService';
import { useAuth } from '../../auth/AuthContext';
import type { Employee } from './employee.types';

// The 10 RBAC roles (ATLAS_RBAC_v3.1). Role drives all access — it's the RBAC join key.
const ROLES = ['Admin', 'Management', 'BD Head', 'BDRM', 'Credit Head', 'Deal Analyst', 'Syn Head', 'Syn RM', 'AM Head', 'AM RM'];
// Forms spec: Reports-to is MANDATORY for ICs, optional for Heads. The leadership +
// Head-tier roles are the "heads"; the ICs (BDRM, Deal Analyst, Syn RM, AM RM) need one.
const HEAD_ROLES = ['Admin', 'Management', 'BD Head', 'Credit Head', 'Syn Head', 'AM Head'];

export default function EmployeeDialog({ emp, mode, onClose, onDone }: {
  emp: Employee | null; mode: 'add' | 'edit'; onClose: () => void; onDone: () => void;
}) {
  const { user } = useAuth();
  const [f, setF] = useState<Employee>({ name: '', full: '', role: 'BDRM' });
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  // Opening in edit mode re-reads the record from Access (GET /v1/users?q=<email>) so the
  // form edits the live record, not whatever the grid happened to be holding. The row we
  // were handed seeds the form immediately, so the fields are never blank while it loads;
  // Access then wins if it answers. A miss (unprovisioned user, mock mode) keeps the row.
  useEffect(() => {
    if (!emp) {
      setF({ name: '', full: '', role: 'BDRM', username: '', email: '', phone: '', geography: '', sectors: '', reportsTo: '', inactive: false, notes: '' });
      setErr('');
      return;
    }
    setF({ ...emp });
    setErr('');
    if (mode !== 'edit') return;
    let cancelled = false;
    setLoading(true);
    employeesService.fetchByEmail(emp.email)
      .then((fresh) => { if (fresh && !cancelled) setF(fresh); })
      .catch((e: any) => { if (!cancelled) setErr(e?.message || 'Could not load the latest record from Access — editing the cached row.'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [emp, mode]);
  const set = (k: keyof Employee, v: any) => setF((p) => ({ ...p, [k]: v }));
  const open = mode === 'add' ? true : !!emp;

  // Reports-to offers the WHOLE active roster, not the RM/Analyst ref buckets: those
  // buckets exclude Management/Admin by design, so a Head reporting to Management found
  // an empty dropdown. Self is excluded (nobody reports to themselves) and the value an
  // old record already holds stays selectable even if that person has since gone.
  const [roster, setRoster] = useState<string[]>(() => employeesService.rosterNames());
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void employeesService.hydrateRoster()
      .then(() => { if (!cancelled) setRoster(employeesService.rosterNames()); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [open]);
  const reportOptions = useMemo(() => {
    const self = new Set([f.name, f.full].map((n) => (n || '').trim().toLowerCase()).filter(Boolean));
    const names = roster.filter((n) => !self.has(n.trim().toLowerCase()));
    const held = (f.reportsTo || '').trim();
    return held && !names.includes(held) ? [held, ...names] : names;
  }, [roster, f.name, f.full, f.reportsTo]);
  // Role stacking: the Role field holds a comma-separated list. Reports-to is mandatory
  // only when NONE of the held roles is a Head-tier role.
  const roleList = (f.role || '').split(',').map((s) => s.trim()).filter(Boolean);
  const anyHead = roleList.some((r) => HEAD_ROLES.includes(r));

  // Forms spec (Employee record): Full name & Email MANDATORY; Email must be an
  // @evamfinance.com address (SSO integrity); Role MANDATORY; Reports-to MANDATORY
  // for ICs. Short name is the record key, so it's required on add too.
  const save = async () => {
    if (mode === 'add' && !f.name?.trim()) { setErr('Short name is required.'); return; }
    if (!f.full?.trim()) { setErr('Full name is required.'); return; }
    const email = (f.email || '').trim();
    if (!email) { setErr('Email is required.'); return; }
    if (!/^[\w.+-]+@evamfinance\.com$/i.test(email)) { setErr('Email must be an @evamfinance.com address.'); return; }
    if (!roleList.length) { setErr('At least one role is required.'); return; }
    if (!anyHead && !f.reportsTo?.trim()) { setErr('Reports-to is required for IC roles.'); return; }
    setErr('');
    // BOTH paths provision/edit the person's two halves (Access identity + register
    // roster) and either half can refuse (duplicate e-mail, unknown role, a
    // deactivation that cannot reach the sign-in) — so both are awaited and the
    // dialog stays open showing why.
    setBusy(true);
    try {
      if (mode === 'add') await employeesService.create(f, user.full);
      else await employeesService.update(f.name, f, user.full);
    } catch (e: any) {
      setErr(e?.message || 'Could not save this user.');
      return;
    } finally {
      setBusy(false);
    }
    onDone(); onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>{mode === 'add' ? 'Add employee' : 'Edit employee'}
        {loading && <CircularProgress size={13} sx={{ ml: 1, verticalAlign: 'middle' }} />}
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 6 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {/* Fields per v15 employee record. */}
        <FieldGrid>
          <TextFld label="Short name" required={mode === 'add'} value={f.name} disabled={mode === 'edit'} onChange={(v) => set('name', v)} />
          <TextFld label="Full name" required value={f.full} onChange={(v) => set('full', v)} />
          <TextFld label="Email" required value={f.email} onChange={(v) => set('email', v)} placeholder="name@evamfinance.com" />
          <TextFld label="Phone" value={f.phone} onChange={(v) => set('phone', v)} />
          <MultiSelectFld label="Role" required value={roleList} onChange={(arr) => set('role', arr.join(', '))} options={ROLES} />
          <SelectFld label="Reports to" required={!anyHead} value={f.reportsTo} onChange={(v) => set('reportsTo', v)} options={reportOptions} blank />
          <TextFld label="Geography covered" value={f.geography} onChange={(v) => set('geography', v)} placeholder="e.g. Karnataka, Tamil Nadu" />
          <TextFld label="Sector specialisation" value={f.sectors} onChange={(v) => set('sectors', v)} placeholder="e.g. Solar, EV" />
          <TextFld label="Started on" type="date" value={f.startedOn} onChange={(v) => set('startedOn', v)} />
          <TextFld label="Username" value={f.username} onChange={(v) => set('username', v)} />
        </FieldGrid>
        <Box sx={{ mt: 1.4 }}><TextFld label="Notes" value={f.notes} onChange={(v) => set('notes', v)} placeholder="strengths, quirks, patch history" multiline /></Box>
        <FormControlLabel control={<Switch checked={!f.inactive} onChange={(e) => set('inactive', !e.target.checked)} />} label="Active" />
        {err && <Alert severity="warning" sx={{ mt: 1, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        <Button onClick={save} variant="contained" disabled={busy || loading}>
          {busy ? 'Adding…' : mode === 'add' ? 'Add employee' : 'Save'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
