import { Dialog, DialogTitle, DialogContent, DialogContentText, DialogActions, Button } from '@mui/material';

export default function ConfirmDialog({ open, title, message, onConfirm, onCancel, confirmLabel = 'Delete', confirmColor = 'error' }: {
  open: boolean; title: string; message: string; onConfirm: () => void; onCancel: () => void;
  /** The confirming button's words and colour — 'Delete' in red unless told otherwise. */
  confirmLabel?: string; confirmColor?: 'error' | 'primary' | 'warning';
}) {
  return (
    <Dialog open={open} onClose={onCancel} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>{title}</DialogTitle>
      <DialogContent><DialogContentText sx={{ fontSize: 13 }}>{message}</DialogContentText></DialogContent>
      <DialogActions>
        <Button onClick={onCancel} variant="outlined">Cancel</Button>
        <Button onClick={onConfirm} color={confirmColor} variant="contained">{confirmLabel}</Button>
      </DialogActions>
    </Dialog>
  );
}
