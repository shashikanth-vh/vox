import { useRef, useState } from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, DialogActions, DialogContent,
  DialogTitle, IconButton, MenuItem, Tab, Tabs, TextField, Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { apiErr } from '../../api/http';
import { tokens } from '../../theme';
import {
  prospectsService, type ProspectImportPreview,
} from '../../services/prospectsService';

/**
 * The prospect universe, both directions, behind one door — the Ledger dialog's
 * sibling, with the Ledger's asymmetry for the same reasons:
 *
 *   EXPORT changes nothing and needs nothing: one button, and the file it writes is
 *   THE SAME SHAPE THE IMPORT READS — export, curate offline, bring it back.
 *
 *   IMPORT is the old CLI dry-run turned into a screen: upload → the server parses,
 *   scrubs and dedupes but WRITES NOTHING → the preview shows exactly what an apply
 *   would do (new / merged / duplicates / skipped / conflicts) → one confirm button.
 *   The merge policy is the safety rule: blanks fill, tags add, a person's edits are
 *   never overwritten — conflicts are listed for a human instead.
 */

const VERTICALS = ['Solar', 'ESS', 'EV', 'Bioenergy', 'Waste & Water'];

export default function ProspectsDialog({ open, onClose }: {
  open: boolean; onClose: () => void;
}) {
  const [tab, setTab] = useState<'export' | 'import'>('export');
  const [exportBusy, setExportBusy] = useState(false);
  const [exported, setExported] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<ProspectImportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [done, setDone] = useState<ProspectImportPreview | null>(null);

  const close = () => {
    if (busy || exportBusy) return;
    setFiles([]); setOverrides({}); setPreview(null); setDone(null); setErr('');
    setExported(false); setTab('export');
    onClose();
  };

  const runExport = async () => {
    setErr(''); setExportBusy(true);
    try { await prospectsService.exportXlsx(); setExported(true); }
    catch (e: any) { setErr(apiErr(e, 'export the prospects')); }
    finally { setExportBusy(false); }
  };

  const pick = (list: FileList | null) => {
    const chosen = Array.from(list || []);
    if (!chosen.length) return;
    setFiles(chosen); setPreview(null); setDone(null); setErr(''); setOverrides({});
  };

  const run = async (mode: 'preview' | 'apply') => {
    if (!files.length) { setErr('Choose the research .xlsx list(s) first'); return; }
    setErr(''); setBusy(true);
    try {
      const r = await prospectsService.importLists(files, mode, overrides);
      if (mode === 'preview') setPreview(r);
      else { setDone(r); setPreview(r); }
    } catch (e: any) { setErr(apiErr(e, mode === 'preview'
      ? 'preview the import' : 'import the prospects')); }
    finally { setBusy(false); }
  };

  const counts = preview?.counts;

  return (
    <Dialog open={open} onClose={close} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', fontSize: 16, pb: 0 }}>
        Prospects (Excel)
        <IconButton onClick={close} sx={{ ml: 'auto' }} size="small">
          <CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <Tabs value={tab} onChange={(_, v) => { setTab(v); setErr(''); }}
        sx={{ px: 2, minHeight: 38, '& .MuiTab-root': { minHeight: 38, fontSize: 12.6,
          textTransform: 'none', fontWeight: 700 } }}>
        <Tab value="export" label="Export" disabled={busy} />
        <Tab value="import" label="Import" disabled={exportBusy} />
      </Tabs>
      <DialogContent dividers>
        {tab === 'export' ? (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.2 }}>
            <Typography sx={{ fontSize: 13 }}>
              Writes the prospect universe as one workbook — <b>the same shape the
              import reads</b>, so a file exported here can be curated offline and
              brought straight back in. Status and remarks ride along for reference;
              the import never touches them.
            </Typography>
            <Typography sx={{ fontSize: 12, color: tokens.muted }}>
              Reading the book changes nothing on the register, and the download is
              audited like every other export.
            </Typography>
            {exported && <Alert severity="success" sx={{ fontSize: 12.4 }}>
              Exported — check your downloads.</Alert>}
          </Box>
        ) : (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.4 }}>
            <input ref={fileRef} type="file" multiple hidden
              accept=".xlsx,.xlsm" onChange={(e) => pick(e.target.files)} />
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
              <Button variant="outlined" size="small"
                onClick={() => fileRef.current?.click()} disabled={busy}>
                Choose files…</Button>
              <Typography sx={{ fontSize: 12.4, color: tokens.muted }}>
                {files.length ? `${files.length} file(s)` : 'the research .xlsx lists'}
              </Typography>
            </Box>

            {files.map((f) => {
              const detected = preview?.files.find((x) => x.file === f.name);
              return (
                <Box key={f.name} sx={{ display: 'flex', gap: 1, alignItems: 'center',
                  border: `1px solid ${tokens.line}`, borderRadius: 1, p: '6px 10px',
                  flexWrap: 'wrap' }}>
                  <Typography sx={{ fontSize: 12.2, fontFamily: 'monospace', flex: 1,
                    minWidth: 180 }}>{f.name}</Typography>
                  {detected?.sheet && (
                    <Typography sx={{ fontSize: 11, color: tokens.muted }}>
                      sheet “{detected.sheet}” · {detected.rows} rows</Typography>)}
                  <TextField select size="small" label="Vertical"
                    value={overrides[f.name] ?? detected?.vertical ?? ''}
                    onChange={(e) => { setOverrides({ ...overrides,
                      [f.name]: e.target.value }); setDone(null); }}
                    sx={{ width: 150 }} disabled={busy}>
                    <MenuItem value="">auto-detect</MenuItem>
                    {VERTICALS.map((v) => <MenuItem key={v} value={v}>{v}</MenuItem>)}
                  </TextField>
                </Box>);
            })}

            {counts && (
              <Box sx={{ display: 'flex', gap: 0.8, flexWrap: 'wrap' }}>
                <Chip size="small" color="success" label={`${counts.new} new`} />
                <Chip size="small" color="warning"
                  label={`${counts.merged} merged (tags added, blanks filled)`} />
                <Chip size="small" label={`${counts.in_file_duplicates} in-file duplicates`} />
                <Chip size="small" label={`${counts.skipped} skipped`} />
                {counts.conflicts > 0 && (
                  <Chip size="small" color="error"
                    label={`${counts.conflicts} conflicts (register kept)`} />)}
              </Box>)}

            {preview && preview.skipped.length > 0 && (
              <Alert severity="info" sx={{ fontSize: 11.8 }}>
                {preview.skipped.slice(0, 5).map((s, i) => (
                  <div key={i}>{s.file} row {s.row ?? '—'}: {s.reason}</div>))}
                {preview.skipped.length > 5
                  && <div>… and {preview.skipped.length - 5} more</div>}
              </Alert>)}
            {preview && preview.conflicts.length > 0 && (
              <Alert severity="warning" sx={{ fontSize: 11.8 }}>
                The import never overwrites a value someone typed in PRISM — these
                stay as the register has them:
                {preview.conflicts.slice(0, 5).map((c, i) => (
                  <div key={i}><b>{c.name}</b> [{c.field}]: register “{c.existing}”
                    · file “{c.incoming}”</div>))}
                {preview.conflicts.length > 5
                  && <div>… and {preview.conflicts.length - 5} more</div>}
              </Alert>)}

            {done && (
              <Alert severity="success" sx={{ fontSize: 12.4 }}>
                Imported — {done.created} created, {done.merged} updated. The Masters →
                Prospects grid has them now.
              </Alert>)}
          </Box>
        )}
        {err && <Alert severity="warning" sx={{ mt: 1.2, fontSize: 12.4 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 2.4, py: 1.4 }}>
        <Button onClick={close} disabled={busy || exportBusy}>Close</Button>
        {tab === 'export' ? (
          <Button variant="contained" onClick={runExport} disabled={exportBusy}
            startIcon={exportBusy ? <CircularProgress size={14} /> : undefined}>
            Download prospects</Button>
        ) : done ? null : preview ? (
          <>
            <Button onClick={() => run('preview')} disabled={busy}>Re-preview</Button>
            <Button variant="contained" onClick={() => run('apply')} disabled={busy}
              startIcon={busy ? <CircularProgress size={14} /> : undefined}>
              Import {preview.counts.new + preview.counts.merged} prospects</Button>
          </>
        ) : (
          <Button variant="contained" onClick={() => run('preview')}
            disabled={busy || !files.length}
            startIcon={busy ? <CircularProgress size={14} /> : undefined}>
            Preview</Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
