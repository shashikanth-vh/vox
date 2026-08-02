import { useState, useEffect } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, IconButton, Alert, Typography } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld, SelectFld } from '../../components/common/Field';
import { referenceService } from '../../services/referenceService';
import { clientsService } from '../../services/clientsService';
import { useAuth } from '../../auth/AuthContext';
import { tokens } from '../../theme';

const blank = { name: '', sector: 'Other', lens: 'Mitigation', state: '', toi: '', about: '' };

export default function AddClientDialog({ open, onClose, onSaved }: { open: boolean; onClose: () => void; onSaved: () => void }) {
  const { user } = useAuth();
  const [f, setF] = useState(blank);
  const [err, setErr] = useState('');
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (open) { setF(blank); setErr(''); setSaving(false); } }, [open]);
  const set = (k: keyof typeof blank, v: string) => setF((p) => ({ ...p, [k]: v }));

  // Awaited: against the real API this is POST /v1/entities, and a rejection
  // (duplicate code/CIN, unknown sector) has to land in the alert below.
  const save = async () => {
    setSaving(true);
    const r = await clientsService.create(f, user.full);
    setSaving(false);
    if (!r.ok) { setErr(r.error || 'Could not add client'); return; }
    onSaved(); onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>Add client
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <FieldGrid>
          <TextFld label="Company name" required value={f.name} onChange={(v) => set('name', v)} placeholder="Full legal name" />
          <SelectFld label="Segment / sector" value={f.sector} onChange={(v) => set('sector', v)} options={referenceService.getRefSync('Sector')} />
          <SelectFld label="Climate lens" value={f.lens} onChange={(v) => set('lens', v)} options={referenceService.getRefSync('Lens')} />
          <TextFld label="State" value={f.state} onChange={(v) => set('state', v)} />
          <TextFld label="Type of industry" value={f.toi} onChange={(v) => set('toi', v)} placeholder="EPC / IPP / Manufacturing / Services" />
        </FieldGrid>
        <Box sx={{ mt: 1.4 }}>
          <TextFld label="About the company" value={f.about} onChange={(v) => set('about', v)} multiline
            placeholder="3–4 lines: what they do, scale, why it fits Evam" />
        </Box>
        <Typography sx={{ fontSize: 11.6, color: tokens.muted, mt: 1 }}>
          The Group Code is minted automatically from the name.
        </Typography>
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined">Cancel</Button>
        <Button onClick={save} variant="contained" disabled={saving}>{saving ? 'Adding…' : 'Add client'}</Button>
      </DialogActions>
    </Dialog>
  );
}
