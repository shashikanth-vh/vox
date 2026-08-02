import { createTheme } from '@mui/material/styles';

// Palette mirrors the ATLAS template tokens.
export const tokens = {
  navy: '#1B2A4A', navy2: '#233659', teal: '#0D7377', tealHi: '#12917E',
  paper: '#F3F5F6', card: '#FFFFFF', line: '#E2E7EA', ink: '#17222B', muted: '#5F6E76',
  lend: '#1F6FA8', synd: '#7A4FC4', am: '#C47A1F',
  ok: '#2E7D4F', okBg: '#E8F4EC', warn: '#C98A1B', bad: '#B3432B', badBg: '#FBEBE7',
};

const theme = createTheme({
  palette: {
    primary: { main: tokens.teal, dark: tokens.tealHi },
    secondary: { main: tokens.navy },
    error: { main: tokens.bad },
    warning: { main: tokens.warn },
    success: { main: tokens.ok },
    background: { default: tokens.paper, paper: tokens.card },
    text: { primary: tokens.ink, secondary: tokens.muted },
    divider: tokens.line,
  },
  shape: { borderRadius: 9 },
  typography: {
    fontFamily: '"Segoe UI",-apple-system,BlinkMacSystemFont,Roboto,Helvetica,Arial,sans-serif',
    fontSize: 13.5,
  },
  components: {
    // Requirement 13: keep the UI extra compact — default to small and tighten padding/fonts.
    MuiButton: {
      defaultProps: { size: 'small' },
      styleOverrides: {
        root: { textTransform: 'none', fontWeight: 600, fontSize: 12, minHeight: 0, padding: '3px 10px' },
        sizeSmall: { padding: '3px 9px', fontSize: 11.5 },
      },
    },
    MuiTextField: { defaultProps: { size: 'small' } },
    MuiSelect: { defaultProps: { size: 'small' }, styleOverrides: { select: { fontSize: 12.5, paddingTop: 6, paddingBottom: 6 } } },
    MuiFormControl: { defaultProps: { size: 'small' } },
    // Shorter inputs/dropdowns. Vertical padding is trimmed symmetrically (so the
    // value/placeholder stay centered), and the resting outlined label is nudged to
    // match the new height so the floating label is centered too.
    MuiInputBase: { styleOverrides: { root: { fontSize: 12.5 } } },
    MuiOutlinedInput: {
      styleOverrides: {
        inputSizeSmall: { paddingLeft: 10, paddingRight: 10, paddingTop: 6, paddingBottom: 6 },
        // On focus: keep the outline thin (1px, not MUI's 2px) and add a soft glow.
        root: {
          '&.Mui-focused .MuiOutlinedInput-notchedOutline': { borderWidth: 1, borderColor: tokens.tealHi },
          '&.Mui-focused': { boxShadow: '0 0 0 3px rgba(18,145,126,.16)' },
        },
      },
    },
    MuiInputLabel: {
      styleOverrides: {
        sizeSmall: { fontSize: 12.5 },
        // Re-centre the resting (un-shrunk) outlined label in the shorter field.
        // The shrunk (top) position is left at MUI's default.
        outlined: { '&.MuiInputLabel-sizeSmall:not(.MuiInputLabel-shrink)': { transform: 'translate(10px, 7px) scale(1)' } },
      },
    },
    // Mandatory-field marker: the required asterisk shows in the brand red.
    MuiFormLabel: { styleOverrides: { asterisk: { color: tokens.bad } } },
    MuiMenuItem: { styleOverrides: { root: { fontSize: 12.5, minHeight: 26, paddingTop: 2, paddingBottom: 2 } } },
    MuiIconButton: { defaultProps: { size: 'small' }, styleOverrides: { sizeSmall: { padding: 5 } } },
    MuiChip: { defaultProps: { size: 'small' }, styleOverrides: { sizeSmall: { height: 20, fontSize: 11 }, labelSmall: { paddingLeft: 8, paddingRight: 8 } } },
    // MRT's multi-select column filter is a MUI Autocomplete portaled to <body>,
    // so it can only be styled from the theme. Match v12's field type: options at
    // the `.fld input` 12.8px, a v12 `.code`-teal accent, and a card-style popup.
    MuiAutocomplete: {
      defaultProps: { size: 'small' },
      styleOverrides: {
        input: { fontSize: 12.8, padding: '3px 6px !important' },
        option: {
          fontSize: 12.6, minHeight: 28, paddingTop: 3, paddingBottom: 3,
          '&[aria-selected="true"]': { backgroundColor: '#EAF3F1' },
          '& .MuiCheckbox-root': { padding: '2px', marginRight: 4 },
          '& .MuiSvgIcon-root': { fontSize: 17 },
        },
        listbox: { paddingTop: 2, paddingBottom: 2 },
        // v12 card: white, 1px --line border, soft shadow.
        paper: { border: '1px solid #E2E7EA', borderRadius: 8, boxShadow: '0 8px 24px rgba(0,0,0,.12)' },
        tag: { fontSize: 11, height: 18 },
      },
    },
    // Compact, light-gray header for every dialog.
    MuiDialogTitle: {
      styleOverrides: {
        root: { padding: '9px 16px', background: '#F1F3F5', borderBottom: `1px solid ${tokens.line}`, minHeight: 0 },
      },
    },
    MuiTable: { defaultProps: { size: 'small' } },
    MuiTableCell: { styleOverrides: { root: { fontSize: 12, padding: '6px 9px' }, sizeSmall: { padding: '5px 9px' } } },
    MuiToggleButton: { defaultProps: { size: 'small' }, styleOverrides: { root: { textTransform: 'none', fontSize: 11.5, fontWeight: 600, padding: '3px 9px' } } },
  },
});

export default theme;
