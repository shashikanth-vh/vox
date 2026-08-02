import { useState, useEffect } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, IconButton, Alert } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld, SelectFld } from '../../components/common/Field';
import { fiService } from '../../services/fiService';
import { useAuth } from '../../auth/AuthContext';

// Forms spec (FI record): Lender type MANDATORY (List: Lender Types).
const LENDER_TYPES = ['Bank', 'NBFC', 'DFI', 'AIF / Fund', 'Multilateral', 'Other'];
const blank = { name: '', type: '', preferredSectors: '', notes: '' };

export default function AddFIDialog({ open, onClose, onSaved }: { open: boolean; onClose: () => void; onSaved: () => void }) {
  const { user } = useAuth();
  const [f, setF] = useState(blank);
  const [err, setErr] = useState('');
  useEffect(() => { if (open) { setF(blank); setErr(''); } }, [open]);
  const set = (k: keyof typeof blank, v: string) => setF((p) => ({ ...p, [k]: v }));

  const save = () => {
    if (!f.name.trim()) { setErr('Bank / FI name is required.'); return; }
    if (!f.type) { setErr('Lender type is required.'); return; }
    const r = fiService.create(f, user.full);
    if (!r.ok) { setErr(r.error || 'Could not add FI'); return; }
    onSaved(); onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>Add bank / FI
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <FieldGrid>
          <TextFld label="Bank / FI name" required value={f.name} onChange={(v) => set('name', v)} />
          <SelectFld label="Lender type" required value={f.type} onChange={(v) => set('type', v)} options={LENDER_TYPES} blank />
        </FieldGrid>
        <Box sx={{ mt: 1.4 }}><TextFld label="Preferred sectors" value={f.preferredSectors} onChange={(v) => set('preferredSectors', v)} placeholder="e.g. Solar, EV, MSME" /></Box>
        <Box sx={{ mt: 1.4 }}><TextFld label="Notes (appetite, contacts, quirks)" value={f.notes} onChange={(v) => set('notes', v)} multiline /></Box>
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined">Cancel</Button>
        <Button onClick={save} variant="contained">Add FI</Button>
      </DialogActions>
    </Dialog>
  );
}
