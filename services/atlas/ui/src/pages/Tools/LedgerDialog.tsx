import { useRef, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, Alert, TextField, ToggleButtonGroup, ToggleButton, Checkbox,
  FormControlLabel, CircularProgress, Chip,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { ledgerService, LedgerImportResult } from '../../services/ledgerService';
import { tokens } from '../../theme';

// Admin-only ledger import: bring the desk's Excel (the live Dashboard ledger or a
// previous PRISM export) into the register, with the server's full account of what
// happened shown back — quarantined rows, healed wording, derivations. The reason is
// mandatory because an import is a governed exception to the interactive rules.
export default function LedgerDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<'merge' | 'replace'>('merge');
  const [reason, setReason] = useState('');
  const [retain, setRetain] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [result, setResult] = useState<LedgerImportResult | null>(null);

  const close = () => {
    if (busy) return;
    setFile(null); setReason(''); setErr(''); setResult(null); setMode('merge');
    onClose();
  };

  const run = async () => {
    if (!file) { setErr('Choose the ledger .xlsx first'); return; }
    if (!reason.trim()) { setErr('A reason is required — imports are audited'); return; }
    if (mode === 'replace'
        && !window.confirm('Replace mode wipes this tenant’s current book before '
          + 'loading the file. Continue?')) return;
    setErr(''); setBusy(true); setResult(null);
    try {
      setResult(await ledgerService.importLedger(file, mode, reason.trim(), retain));
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || 'Import failed');
    } finally {
      setBusy(false);
    }
  };

  const counts = result?.counts || {};
  const shown = Object.entries(counts).filter(([, v]) => Number(v) > 0);

  return (
    <Dialog open={open} onClose={close} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', fontSize: 16 }}>
        Import ledger (Excel)
        <IconButton onClick={close} sx={{ ml: 'auto' }} size="small"><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 12.4, color: tokens.muted, mb: 1.4 }}>
          Reads the desk&apos;s Dashboard ledger or a PRISM ledger export. Old wording
          (Rejected, Disbursed, typos) is translated to PRISM&apos;s stages automatically,
          and everything the sheets carry is preserved — the result below tells you
          exactly what happened.
        </Typography>

        <Box sx={{ display: 'flex', gap: 1.2, alignItems: 'center', mb: 1.4, flexWrap: 'wrap' }}>
          <Button variant="outlined" size="small" onClick={() => fileRef.current?.click()}>
            {file ? file.name : 'Choose .xlsx'}
          </Button>
          <input ref={fileRef} type="file" accept=".xlsx,.xlsm" style={{ display: 'none' }}
            onChange={(e) => { setFile(e.target.files?.[0] || null); e.target.value = ''; }} />
          <ToggleButtonGroup exclusive size="small" value={mode}
            onChange={(_, v) => v && setMode(v)}>
            <ToggleButton value="merge" sx={{ fontSize: 11.6, px: 1.2 }}>Merge into current book</ToggleButton>
            <ToggleButton value="replace" sx={{ fontSize: 11.6, px: 1.2 }}>Replace current book</ToggleButton>
          </ToggleButtonGroup>
        </Box>

        <TextField fullWidth size="small" label="Reason (audited)" value={reason}
          onChange={(e) => setReason(e.target.value)} sx={{ mb: 1 }}
          placeholder="e.g. moving the desk ledger into PRISM" />
        <FormControlLabel
          control={<Checkbox size="small" checked={retain} onChange={(e) => setRetain(e.target.checked)} />}
          label={<Typography sx={{ fontSize: 12.2 }}>
            Keep incomplete historical rows (flagged for reconciliation) instead of skipping them
          </Typography>} />

        {err && <Alert severity="error" sx={{ mt: 1.2 }}>{String(err)}</Alert>}

        {result && (
          <Box sx={{ mt: 1.4 }}>
            <Alert severity={result.report.quarantined_count ? 'warning' : 'success'} sx={{ mb: 1 }}>
              Imported. {result.report.quarantined_count
                ? `${result.report.quarantined_count} row(s) needed attention — see below.`
                : 'Every row landed.'}
            </Alert>
            <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap', mb: 1 }}>
              {shown.map(([k, v]) => (
                <Chip key={k} size="small" label={`${k.replace(/_/g, ' ')}: ${v}`} />
              ))}
            </Box>
            {result.report.quarantined_count > 0 && (
              <Box sx={{ maxHeight: 160, overflow: 'auto', border: `1px solid ${tokens.line}`,
                borderRadius: 1, p: 1 }}>
                {result.report.quarantined.map((q: any, i: number) => (
                  <Typography key={i} sx={{ fontSize: 11.6, color: tokens.muted }}>
                    • {q.sheet}{q.company ? ` · ${q.company}` : ''} — {q.reason}
                    {q.value ? ` (${String(q.value).slice(0, 120)})` : ''}
                  </Typography>
                ))}
              </Box>
            )}
            <Typography sx={{ fontSize: 11.4, color: tokens.muted, mt: 0.8 }}>
              {result.report.translated_count} wording translation(s) ·{' '}
              {result.report.derived_count} derivation(s) ·{' '}
              {result.report.reconciliation_count} reconciliation item(s). The full
              detail is in the audit trail (batch {String(result.checksum || '').slice(0, 12)}…).
            </Typography>
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={close} disabled={busy}>Close</Button>
        <Button variant="contained" onClick={run} disabled={busy}
          startIcon={busy ? <CircularProgress size={14} /> : undefined}>
          {busy ? 'Importing…' : 'Import'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
