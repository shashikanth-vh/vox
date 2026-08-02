import { useState } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Typography, Alert } from '@mui/material';
import { FieldGrid, TextFld, SelectFld } from '../../components/common/Field';
import { dealsService } from '../../services/dealsService';
import { clientsService } from '../../services/clientsService';
import { useAuth } from '../../auth/AuthContext';

// Forms spec (Add product): Product MANDATORY, Amount MANDATORY with NO default
// (v2.1 removed the default of 2 to prevent silent data pollution).
export default function AddProductDialog({ code, onClose, onDone }: { code: string | null; onClose: () => void; onDone: () => void }) {
  const { user } = useAuth();
  const [product, setProduct] = useState<'Lending' | 'Platform Deals' | 'Asset Monetisation'>('Lending');
  const [amt, setAmt] = useState<string>('');
  const [err, setErr] = useState('');
  if (!code) return null;

  const add = () => {
    const n = Number(amt);
    if (!amt.trim() || !(n > 0)) { setErr('Amount is required (₹ Cr, greater than 0).'); return; }
    dealsService.addProduct(code, product, n, user.full); onDone(); onClose();
  };

  return (
    <Dialog open={!!code} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>Add product — {clientsService.get(code).name}</DialogTitle>
      <DialogContent dividers>
        <FieldGrid>
          <SelectFld label="Product" required value={product} onChange={(v) => setProduct(v)} options={['Lending', 'Platform Deals', 'Asset Monetisation']} />
          <TextFld label="Amount / value (₹ Cr)" required value={amt} onChange={(v) => setAmt(String(v))} placeholder="e.g. 25" />
        </FieldGrid>
        <Typography sx={{ fontSize: 11.6, color: 'text.secondary', mt: 1 }}>Creates the row with a starting status; refine everything in the profile drawer.</Typography>
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined">Cancel</Button>
        <Button onClick={add} variant="contained">Add</Button>
      </DialogActions>
    </Dialog>
  );
}
