import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, CircularProgress, Dialog, DialogActions, DialogContent,
  DialogTitle, IconButton, TextField, Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import type { TrancheItem } from '../../services/lmsService';
import { fmt } from '../../utils/format';
import { tokens } from '../../theme';

/**
 * REVIEW a pending tranche booking before deciding — what money, whose recording,
 * and what CP/CS conditions were open at that moment (the recording's own frozen
 * disclosure), with expired ones flagged. Approve opens/grows the loan account in
 * the register's transaction; Reject needs the reason and returns the recording to
 * its maker. Four-eyes is enforced server-side either way.
 */

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      <Typography sx={{ fontSize: 10.4, textTransform: 'uppercase', letterSpacing: '.5px',
        color: tokens.muted, fontWeight: 700 }}>{label}</Typography>
      <Typography sx={{ fontSize: 12.8, wordBreak: 'break-word' }}>{value || '—'}</Typography>
    </Box>
  );
}

export default function BookingReviewDialog({ t, busy, onClose, onDecide }: {
  t: TrancheItem | null;
  busy: boolean;
  onClose: () => void;
  onDecide: (t: TrancheItem, action: 'approve' | 'reject', note?: string) => void;
}) {
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  useEffect(() => { setNote(''); setErr(''); }, [t?.id]);

  if (!t) return null;
  const today = new Date().toISOString().slice(0, 10);
  const conds = t.conditions_open ?? [];

  const reject = () => {
    if (!note.trim()) {
      setErr('A rejection needs the reason — the recorder corrects from your words.');
      return;
    }
    onDecide(t, 'reject', note.trim());
  };

  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>
        Review — booking approval
        <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>
          Approval opens/grows the loan account and moves the money onto the book.
        </Typography>
        <IconButton onClick={onClose} disabled={busy}
          sx={{ position: 'absolute', right: 8, top: 8 }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px 14px' }}>
          <Fact label="Borrower" value={t.borrower || t.lending_id.slice(0, 8)} />
          <Fact label="Amount" value={`₹ ${fmt(t.amount)} Cr`} />
          <Fact label="UTR / reference" value={t.tranche_ref} />
          <Fact label="Value date" value={t.disbursed_on || '—'} />
          <Fact label="Recorded by" value={t.recorded_by || '—'} />
          <Fact label="Line stage" value={t.stage || '—'} />
        </Box>

        <Typography sx={{ fontSize: 12.5, fontWeight: 600, mt: 1.6, mb: 0.5 }}>
          Conditions open when recorded ({conds.length})
        </Typography>
        {conds.length === 0 && (
          <Typography sx={{ fontSize: 12, color: tokens.muted }}>
            None — every CP/CS condition was settled at recording time.
          </Typography>
        )}
        {conds.map((c) => {
          const overdue = !!c.expiry_date && c.expiry_date < today;
          return (
            <Box key={c.key} sx={{ display: 'flex', gap: 1, alignItems: 'baseline',
              py: 0.3, borderBottom: `1px dashed ${tokens.line}`,
              '&:last-of-type': { borderBottom: 'none' } }}>
              <Typography sx={{ fontSize: 10.5, fontWeight: 700, px: 0.7, borderRadius: 1,
                bgcolor: c.status === 'Deferred as CS' ? '#E8EDF9' : '#EEF1F3',
                color: c.status === 'Deferred as CS' ? '#2A4B8D' : '#5F6E76' }}>
                {c.status === 'Deferred as CS' ? 'CP · deferred' : c.condition_type}
              </Typography>
              <Typography sx={{ fontSize: 12.3, flex: 1 }}>{c.label}</Typography>
              {c.expiry_date && (
                <Typography sx={{ fontSize: 11.5, color: overdue ? '#7C4A3E' : tokens.muted }}>
                  due {c.expiry_date}
                </Typography>
              )}
              {overdue && (
                <Typography sx={{ fontSize: 10.5, fontWeight: 700, px: 0.7, borderRadius: 1,
                  bgcolor: '#FDE8E4', color: '#7C4A3E' }}>Overdue</Typography>
              )}
            </Box>
          );
        })}

        <Box sx={{ mt: 1.4 }}>
          <TextField fullWidth size="small" multiline minRows={2} value={note}
            label="Note — required for Reject, optional on Approve"
            onChange={(e) => { setNote(e.target.value); setErr(''); }} />
        </Box>
        {err && <Alert severity="warning" sx={{ mt: 1, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        <Box sx={{ flex: 1 }} />
        <Button onClick={reject} color="error" disabled={busy}>Reject</Button>
        <Button onClick={() => onDecide(t, 'approve', note.trim() || undefined)}
          variant="contained" disabled={busy}
          startIcon={busy ? <CircularProgress size={13} color="inherit" /> : undefined}>
          {busy ? 'Booking…' : 'Approve & book'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
