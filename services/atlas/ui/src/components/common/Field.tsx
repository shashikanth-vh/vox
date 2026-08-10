import { TextField, MenuItem, Box, Typography, Select, Checkbox, ListItemText, Chip, Stack } from '@mui/material';
import { tokens } from '../../theme';

export function FieldGrid({ cols = 2, children }: { cols?: number; children: React.ReactNode }) {
  return <Box sx={{ display: 'grid', gridTemplateColumns: `repeat(${cols}, 1fr)`, gap: '10px 14px' }}>{children}</Box>;
}

interface Base { label: string; value: any; onChange: (v: any) => void; disabled?: boolean; required?: boolean; }

// v17 `.fld label` — a standalone label ABOVE the control (not MUI's floating label,
// which renders as a <legend> inside the notched outline).
const LABEL_SX = {
  display: 'block', fontSize: 10.4, textTransform: 'uppercase', letterSpacing: '.5px',
  color: tokens.muted, fontWeight: 700, mb: '3px', lineHeight: 1.35,
} as const;

// v17 `.fld input/select/textarea` sizing. The MUI control carries NO label, so no
// <legend> is drawn — the outline is a plain 1px box, matching the template.
const CONTROL_SX = {
  '& .MuiOutlinedInput-root': { borderRadius: '7px', backgroundColor: '#fff', fontSize: 12.8 },
  '& .MuiOutlinedInput-input': { padding: '7px 9px' },
  '& .MuiInputBase-inputMultiline': { padding: 0, minHeight: 38 },
} as const;

// Exported so read-only cells (e.g. Products pills in the Deals drawer) can reuse the
// exact same label style + spacing as the editable fields and sit inside a FieldGrid.
export function FieldShell({ label, required, children }: { label: string; required?: boolean; children: React.ReactNode }) {
  return (
    <Box>
      <Typography component="label" sx={LABEL_SX}>
        {label}{required && <Box component="span" sx={{ color: tokens.bad, ml: '2px' }}>*</Box>}
      </Typography>
      {children}
    </Box>
  );
}

export function TextFld({ label, value, onChange, disabled, required, type = 'text', multiline, placeholder, minRows }: Base & { type?: string; multiline?: boolean; placeholder?: string; minRows?: number }) {
  return (
    <FieldShell label={label} required={required}>
      <TextField value={value ?? ''} disabled={disabled} type={type} multiline={multiline}
        minRows={multiline ? (minRows ?? 2) : undefined} placeholder={placeholder}
        onChange={(e) => onChange(type === 'number' ? Number(e.target.value) || 0 : e.target.value)}
        fullWidth size="small" sx={CONTROL_SX} />
    </FieldShell>
  );
}

export function SelectFld({ label, value, onChange, options, disabled, required, blank, labels }:
  Base & { options: string[]; blank?: boolean; labels?: Record<string, string> }) {
  /* WHAT IS STORED AND WHAT IS READ ARE NOT THE SAME STRING.
     The person lists store a short handle ("AT") and read as a full name ("Arun
     Tiwari"). Rendering only the stored value gave the desk a column of initials with
     the occasional full name where a handle happened to be one — unreadable, and
     impossible to tell two colleagues apart in. The handle stays in the record and
     stays visible beside the name, so what gets written is never a mystery either. */
  const show = (o: string) => (labels?.[o] ? `${labels[o]} · ${o}` : o);
  return (
    <FieldShell label={label} required={required}>
      <TextField select value={value ?? ''} disabled={disabled} onChange={(e) => onChange(e.target.value)}
        fullWidth size="small" sx={CONTROL_SX}>
        {blank && <MenuItem value="">—</MenuItem>}
        {options.map((o) => <MenuItem key={o} value={o}>{show(o)}</MenuItem>)}
      </TextField>
    </FieldShell>
  );
}

// Multi-select (chips) — used for role stacking on the Employee record. Value is a
// string[]; options render with checkboxes.
export function MultiSelectFld({ label, value, onChange, options, disabled, required }: { label: string; value: string[]; onChange: (v: string[]) => void; options: string[]; disabled?: boolean; required?: boolean }) {
  return (
    <FieldShell label={label} required={required}>
      <Select multiple size="small" fullWidth displayEmpty disabled={disabled} value={value}
        onChange={(e) => onChange(typeof e.target.value === 'string' ? e.target.value.split(',') : (e.target.value as string[]))}
        renderValue={(sel) => (sel as string[]).length
          ? <Stack direction="row" gap={0.5} flexWrap="wrap">{(sel as string[]).map((s) => <Chip key={s} label={s} size="small" sx={{ height: 18, fontSize: 11 }} />)}</Stack>
          : <Box component="span" sx={{ color: tokens.muted, fontSize: 12.5 }}>Select…</Box>}
        sx={CONTROL_SX}>
        {options.map((o) => (
          <MenuItem key={o} value={o} sx={{ py: 0.2 }}>
            <Checkbox size="small" checked={value.includes(o)} sx={{ p: 0.5, mr: 0.5 }} />
            <ListItemText primary={o} primaryTypographyProps={{ fontSize: 12.8 }} />
          </MenuItem>
        ))}
      </Select>
    </FieldShell>
  );
}

export function DrawerSection({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.5, mb: 1.4 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
        <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.muted, fontWeight: 700 }}>{title}</Typography>
        {action && <><Box sx={{ flex: 1 }} />{action}</>}
      </Box>
      {children}
    </Box>
  );
}
