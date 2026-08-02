import { useEffect, useState } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, Alert } from '@mui/material';
import { FieldGrid, TextFld, SelectFld } from '../../components/common/Field';
import { interactionService, type LeadRef } from '../../services/interactionService';
import { db } from '../../api/atlasStore';
import { useAuth } from '../../auth/AuthContext';
import { tokens } from '../../theme';

export default function LogInteractionDialog({ code, refType, lead, open, onClose, onDone }: {
  code: string; refType?: string;
  /** Present when logging against a lead — routes to POST /v1/leads/{id}/interactions. */
  lead?: LeadRef;
  open: boolean; onClose: () => void; onDone: () => void;
}) {
  const { user } = useAuth();
  const people: string[] = (db().people || []).map((p: any) => p.name);
  const today = new Date().toISOString().slice(0, 10);
  const [f, setF] = useState({ occurredAt: today, person: user.name, interactionType: '', notes: '', nextAction: '', nextActionDate: '' });
  const [err, setErr] = useState('');
  const [saving, setSaving] = useState(false);
  const set = (k: string, v: any) => setF((p) => ({ ...p, [k]: v }));

  // The dialog stays mounted between openings, so a fresh one starts blank rather than
  // showing the last interaction that was logged.
  useEffect(() => {
    if (open) {
      setF({ occurredAt: today, person: user.name, interactionType: '', notes: '', nextAction: '', nextActionDate: '' });
      setErr(''); setSaving(false);
    }
  }, [open, code]);

  const missing = !f.interactionType || !f.notes.trim();
  const save = async () => {
    if (missing) return;
    const rec = { ...f, refId: code, refType: refType || 'General', nextActionDate: f.nextActionDate || null };
    // A lead's ledger lives behind the API; everything else is still the local store.
    if (lead) {
      setSaving(true);
      const r = await interactionService.logForLead(lead, rec, user.full);
      setSaving(false);
      if (!r.ok) { setErr(r.error || 'Could not log the interaction.'); return; }
    } else {
      interactionService.log(rec, user.full);
    }
    setErr('');
    onDone(); onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>Log a new interaction</DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 11.5, color: tokens.muted, mb: 1.2 }}>Backdate freely. Records are append-only.</Typography>
        <FieldGrid>
          <TextFld label="Date it happened" required type="date" value={f.occurredAt} onChange={(v) => set('occurredAt', v)} />
          <SelectFld label="Person" required value={f.person} onChange={(v) => set('person', v)} options={people} />
        </FieldGrid>
        <Box sx={{ mt: 1.2 }}>
          <SelectFld label="Interaction type" required value={f.interactionType} onChange={(v) => set('interactionType', v)} options={interactionService.types()} blank />
        </Box>
        <Box sx={{ mt: 1.2 }}>
          <TextFld label="Notes" required value={f.notes} onChange={(v) => set('notes', v)} multiline />
        </Box>
        <Box sx={{ mt: 1.2 }}>
          <FieldGrid>
            <TextFld label="Next action (optional)" value={f.nextAction} onChange={(v) => set('nextAction', v)} />
            <TextFld label="Due by" type="date" value={f.nextActionDate} onChange={(v) => set('nextActionDate', v)} />
          </FieldGrid>
        </Box>
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={saving}>Cancel</Button>
        <Button onClick={save} variant="contained" disabled={missing || saving}>{saving ? 'Saving…' : 'Save interaction'}</Button>
      </DialogActions>
    </Dialog>
  );
}
