/**
 * VocX's own visual language.
 *
 * The panel is not an ATLAS grid and should not look like one. Field intel is captured
 * one-handed, often standing up, frequently on a phone — so the type is large, the touch
 * targets are generous, and each part of the report sits in its own card rather than in a
 * dense two-column form. The palette is the green-tinted dark the VOM design uses, which
 * also does the useful job of telling the user, at a glance, that this surface belongs to
 * a different tool than the register behind it.
 *
 * Kept in one module because a design language enforced by copy-paste stops being one
 * after the third screen.
 */

export const vx = {
  bg: '#0A120E',
  card: '#101D18',
  card2: '#0D1713',
  line: '#1D332A',
  lineSoft: 'rgba(29,51,42,.7)',
  ink: '#E6F2EC',
  mut: '#8FA89C',
  grn: '#34D399',
  grn2: '#7FE0B2',
  teal: '#35B8A4',
  amber: '#C9A227',
  amberInk: '#E0C05D',
  // LIVE — the microphone is open. Deliberately cyan and not red: red reads as an error
  // in a surface whose errors are amber, and it clashed with the green-tinted dark the
  // rest of VocX is built from. Cyan is the one hue here that nothing else uses, so it
  // means exactly one thing wherever it appears.
  live: '#22D3EE',
  liveSoft: 'rgba(34,211,238,.55)',
  onGrn: '#06251A',
};

/** A section of the report — its own bordered card, as in the design. */
export const card = {
  bgcolor: vx.card,
  border: `1px solid ${vx.line}`,
  borderRadius: '16px',
  p: 2,
  mb: 1.75,
};

/** The big heading that opens a card ("Key intel", "Next steps"). */
export const heading = {
  fontSize: 19,
  fontWeight: 700,
  color: vx.ink,
  mb: 1,
};

/** The small uppercase heading used for a group inside a card. */
export const microHeading = {
  fontSize: 12.5,
  letterSpacing: '.14em',
  textTransform: 'uppercase' as const,
  color: vx.teal,
  fontWeight: 600,
  mb: 1,
};

/** The label above a field. */
export const label = {
  fontSize: 11.5,
  letterSpacing: '.12em',
  textTransform: 'uppercase' as const,
  color: vx.mut,
  mb: 0.6,
  display: 'block',
};

/** Inputs: tall, rounded, dark — comfortable for a thumb.
 *
 * `minWidth: 0` is load-bearing, not tidiness. A TextField carries an intrinsic minimum
 * width of about 180px, so `flex: 1` inside a 420px card does NOT shrink it: three of
 * them in a row (attendees) reserved 540px, the row overflowed, and the panel's scroll
 * container quietly scrolled sideways with the content sliding out past a clipped edge.
 * A field that may sit in a flex row has to be allowed to be narrow. */
export const input = {
  minWidth: 0,
  '& .MuiInputBase-root': {
    bgcolor: vx.card2,
    color: vx.ink,
    fontSize: 15,
    borderRadius: '10px',
    padding: '2px 4px',
  },
  '& .MuiInputBase-input': { padding: '10px 8px' },
  '& fieldset': { borderColor: vx.line },
  '&:hover fieldset': { borderColor: vx.line },
  '& .Mui-focused fieldset': { borderColor: `${vx.grn} !important` },
  '& .MuiInputLabel-root': { fontSize: 13, color: vx.mut },
  '& .MuiSelect-icon': { color: vx.mut },
};

/** A pill button — the design's default control. */
export const pill = {
  textTransform: 'none' as const,
  fontSize: 15,
  fontWeight: 600,
  borderRadius: '11px',
  px: 2,
  py: 1.1,
  bgcolor: '#173A2C',
  color: vx.ink,
  border: '1px solid #2C5A44',
  '&:hover': { bgcolor: '#1D4835', borderColor: '#3A7057' },
  '&.Mui-disabled': { opacity: 0.45, color: vx.mut },
};

export const pillPrimary = {
  ...pill,
  bgcolor: vx.grn,
  borderColor: vx.grn,
  color: vx.onGrn,
  fontWeight: 700,
  '&:hover': { bgcolor: vx.grn2, borderColor: vx.grn2 },
};

export const pillGhost = {
  ...pill,
  bgcolor: 'transparent',
  border: `1px dashed ${vx.line}`,
  color: vx.mut,
  '&:hover': { bgcolor: 'rgba(255,255,255,.03)', borderColor: vx.grn2, color: vx.grn2 },
};

/** Destructive, without red. It must not look like the primary action and must not
 *  look LIVE either — so it is the one neutral control on the surface. */
export const pillDanger = {
  ...pill,
  bgcolor: 'transparent',
  borderColor: '#3A4A44',
  color: vx.mut,
  '&:hover': { bgcolor: 'rgba(255,255,255,.05)', borderColor: vx.ink, color: vx.ink },
};

/** A selectable chip — temperature, log-to lane, template. */
export const chip = (on: boolean, dashed = false) => ({
  borderRadius: '999px',
  px: 1.9,
  py: 0.9,
  height: 'auto',
  fontSize: 15,
  fontWeight: on ? 600 : 400,
  cursor: 'pointer',
  userSelect: 'none' as const,
  bgcolor: on ? vx.grn : vx.card2,
  color: on ? vx.onGrn : (dashed ? vx.grn2 : vx.ink),
  border: `1px ${dashed && !on ? 'dashed' : 'solid'} ${on ? vx.grn : vx.line}`,
  '&:hover': { bgcolor: on ? vx.grn2 : '#16281F' },
  '& .MuiChip-label': { px: 0 },
});

/** The status badge (DRAFT / READY / COMMITTED). */
export const badge = (status: string) => {
  const s = status.toLowerCase();
  const tone = s === 'committed' ? { bg: '#14322A', fg: vx.grn2 }
    : s === 'ready' ? { bg: vx.grn, fg: vx.onGrn }
    : { bg: '#2A2414', fg: vx.amberInk };
  return {
    display: 'inline-block',
    px: 1.6, py: 0.55,
    borderRadius: '8px',
    fontSize: 13,
    fontWeight: 700,
    letterSpacing: '.06em',
    bgcolor: tone.bg,
    color: tone.fg,
  };
};

/** An inline banner inside the card (calendar state, commit result). */
export const banner = (warn = false) => ({
  bgcolor: warn ? '#241F10' : '#0F2A1E',
  border: `1px solid ${warn ? '#4A3D1D' : '#1D4A35'}`,
  borderRadius: '12px',
  p: 1.5,
  my: 1.2,
  fontSize: 14,
  color: warn ? vx.amberInk : vx.grn2,
});
