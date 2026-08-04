import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Typography,
} from '@mui/material';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import type { Completeness } from './completeness';
import { vx, pill, pillPrimary } from './vocxStyles';

/**
 * The last thing between a capture and the register.
 *
 * Filing is the only irreversible step in VocX — the preview writes nothing, the draft
 * writes nothing, and then one button puts an interaction on a client's permanent
 * timeline. So this says plainly what the report is missing before that happens, split
 * the way the two kinds of gap actually differ:
 *
 *   * REQUIRED gaps make "Go back and fill" the default action. Not a hard block: an RM
 *     standing in a car park who genuinely does not know the sector should be able to get
 *     the intel filed rather than lose it — but they have to choose that, deliberately,
 *     against a button that says what they are doing.
 *   * OPTIONAL gaps are listed and waived with one click.
 *
 * A dialog that merely said "some fields are empty" would be noise. Naming them, and
 * saying which template asked for each, is what makes it worth stopping for.
 */

export default function ApproveDialog({ open, state, busy, onFill, onFile, onClose }: {
  open: boolean;
  state: Completeness;
  busy: boolean;
  /** Close and put the caret on the first gap. */
  onFill: (key: string) => void;
  onFile: () => void;
  onClose: () => void;
}) {
  const { missingRequired, missingOptional, filled, total } = state;
  const clean = !missingRequired.length && !missingOptional.length;

  const list = (gaps: typeof missingRequired, colour: string) => (
    <Box component="ul" sx={{ m: 0, pl: 2.2, mb: 1 }}>
      {gaps.map((g) => (
        <Box component="li" key={g.key} sx={{ fontSize: 15, color: colour, mb: 0.4 }}>
          <Box component="span"
            onClick={() => onFill(g.key)}
            sx={{ cursor: 'pointer', textDecoration: 'underline dotted' }}>
            {g.label}
          </Box>
          {g.from && (
            <Typography component="span" sx={{ fontSize: 13, color: vx.mut, ml: 0.6 }}>
              — {g.from}
            </Typography>
          )}
        </Box>
      ))}
    </Box>
  );

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="xs" fullWidth
      PaperProps={{ sx: { bgcolor: vx.card, color: vx.ink, borderRadius: '16px',
        border: `1px solid ${vx.line}` } }}>
      <DialogTitle sx={{ fontSize: 19, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1 }}>
        {clean ? <CheckCircleIcon sx={{ color: vx.grn, fontSize: 20 }} />
               : missingRequired.length ? <ErrorOutlineIcon sx={{ color: vx.amberInk, fontSize: 20 }} />
                                        : <InfoOutlinedIcon sx={{ color: vx.mut, fontSize: 20 }} />}
        {clean ? 'Ready to file' : 'Before you file'}
      </DialogTitle>
      <DialogContent dividers sx={{ borderColor: vx.line }}>
        <Typography sx={{ fontSize: 14, color: vx.mut, mb: 1.4 }}>
          {filled} of {total} details captured. Filing puts this interaction on the
          client's permanent timeline — it cannot be taken back.
        </Typography>

        {!!missingRequired.length && (
          <>
            <Alert severity="warning" sx={{ py: 0, fontSize: 12, mb: 0.8 }}>
              {missingRequired.length === 1 ? 'One required detail is' : `${missingRequired.length} required details are`} missing.
            </Alert>
            {list(missingRequired, vx.amberInk)}
          </>
        )}

        {!!missingOptional.length && (
          <>
            <Typography sx={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.6px',
              color: vx.mut, fontWeight: 700, mt: 1.4, mb: 0.6 }}>
              Optional, not captured
            </Typography>
            {list(missingOptional, vx.mut)}
            <Typography sx={{ fontSize: 13.5, color: vx.mut }}>
              These are worth having but nothing waits on them.
            </Typography>
          </>
        )}

        {clean && (
          <Typography sx={{ fontSize: 15 }}>
            Everything this report asks for is filled in.
          </Typography>
        )}
      </DialogContent>
      <DialogActions sx={{ gap: 0.8, p: 2, borderTop: `1px solid ${vx.line}` }}>
        <Button onClick={onClose} disabled={busy} sx={pill}>Cancel</Button>
        {!!missingRequired.length && (
          <Button disabled={busy} sx={pillPrimary}
            onClick={() => onFill(missingRequired[0].key)}>
            Go back and fill
          </Button>
        )}
        <Button
          onClick={onFile}
          disabled={busy}
          sx={missingRequired.length
            ? { ...pill, color: vx.amberInk, borderColor: '#4A3D1D' }
            : pillPrimary}
        >
          {busy ? 'Filing…'
                : missingRequired.length ? 'File without them'
                : 'File to the register'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
