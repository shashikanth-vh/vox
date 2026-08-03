import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Typography,
} from '@mui/material';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import type { Completeness } from './completeness';
import { tokens } from '../../theme';

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
        <Box component="li" key={g.key} sx={{ fontSize: 12.8, color: colour, mb: 0.2 }}>
          <Box component="span"
            onClick={() => onFill(g.key)}
            sx={{ cursor: 'pointer', textDecoration: 'underline dotted' }}>
            {g.label}
          </Box>
          {g.from && (
            <Typography component="span" sx={{ fontSize: 11, color: tokens.muted, ml: 0.6 }}>
              — {g.from}
            </Typography>
          )}
        </Box>
      ))}
    </Box>
  );

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ fontSize: 15.5, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1 }}>
        {clean ? <CheckCircleIcon sx={{ color: tokens.ok, fontSize: 20 }} />
               : missingRequired.length ? <ErrorOutlineIcon sx={{ color: tokens.warn, fontSize: 20 }} />
                                        : <InfoOutlinedIcon sx={{ color: tokens.muted, fontSize: 20 }} />}
        {clean ? 'Ready to file' : 'Before you file'}
      </DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 11.5, color: tokens.muted, mb: 1.2 }}>
          {filled} of {total} details captured. Filing puts this interaction on the
          client's permanent timeline — it cannot be taken back.
        </Typography>

        {!!missingRequired.length && (
          <>
            <Alert severity="warning" sx={{ py: 0, fontSize: 12, mb: 0.8 }}>
              {missingRequired.length === 1 ? 'One required detail is' : `${missingRequired.length} required details are`} missing.
            </Alert>
            {list(missingRequired, tokens.warn)}
          </>
        )}

        {!!missingOptional.length && (
          <>
            <Typography sx={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.6px',
              color: tokens.muted, fontWeight: 700, mt: 1, mb: 0.4 }}>
              Optional, not captured
            </Typography>
            {list(missingOptional, tokens.muted)}
            <Typography sx={{ fontSize: 11, color: tokens.muted }}>
              These are worth having but nothing waits on them.
            </Typography>
          </>
        )}

        {clean && (
          <Typography sx={{ fontSize: 12.8 }}>
            Everything this report asks for is filled in.
          </Typography>
        )}
      </DialogContent>
      <DialogActions sx={{ gap: 0.5 }}>
        <Button onClick={onClose} disabled={busy} sx={{ textTransform: 'none' }}>Cancel</Button>
        {!!missingRequired.length && (
          <Button variant="contained" disabled={busy} sx={{ textTransform: 'none' }}
            onClick={() => onFill(missingRequired[0].key)}>
            Go back and fill
          </Button>
        )}
        <Button
          onClick={onFile}
          disabled={busy}
          variant={missingRequired.length ? 'outlined' : 'contained'}
          color={missingRequired.length ? 'warning' : 'primary'}
          sx={{ textTransform: 'none', fontWeight: 700 }}
        >
          {busy ? 'Filing…'
                : missingRequired.length ? 'File without them'
                : 'File to the register'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
