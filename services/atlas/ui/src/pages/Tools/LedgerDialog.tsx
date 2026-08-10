import { useRef, useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography,
  IconButton, Alert, TextField, ToggleButtonGroup, ToggleButton, Checkbox,
  FormControlLabel, CircularProgress, Chip, Tooltip, Tabs, Tab,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { ledgerService, LedgerImportResult } from '../../services/ledgerService';
import { apiErr } from '../../api/http';
import { tokens } from '../../theme';

/**
 * The ledger, both directions, behind one door.
 *
 * Import and export used to be two tiles on Tools, which put the DIRECTION before the
 * subject — the desk says "the ledger", not "the import" — and left Import sitting in
 * the grid beside Restore, the other control that can wipe the book.
 *
 * They are deliberately not symmetric inside, because they are not symmetric in use:
 *
 *   EXPORT is frequent, needs nothing, and changes nothing. It opens first, and is one
 *   button — but a button that first SAYS what the file will contain, which the old
 *   tile never did: it fired a download and left you to open it to find out.
 *
 *   IMPORT is rare and governed. It keeps its file, its merge/replace choice, its
 *   mandatory audited reason, and the server's full account of what happened —
 *   quarantined rows, healed wording, derivations.
 */
export default function LedgerDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [tab, setTab] = useState<'export' | 'import'>('export');
  const [exportBusy, setExportBusy] = useState(false);
  const [exported, setExported] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<'merge' | 'replace'>('merge');
  const [reason, setReason] = useState('');
  const [retain, setRetain] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [result, setResult] = useState<LedgerImportResult | null>(null);
  // THIS FILE HAS BEEN IMPORTED. Leaving Import live after a result invites a second
  // run of a job that takes a while and, in Replace mode, wipes the book before
  // reloading it — and the desk has no reason to want that: the outcome is already on
  // screen. Choosing a different file (or switching mode) is a different import, and
  // clears this.
  const [done, setDone] = useState(false);

  const close = () => {
    if (busy || exportBusy) return;
    setFile(null); setReason(''); setErr(''); setResult(null); setMode('merge');
    setDone(false); setExported(false); setTab('export');
    onClose();
  };

  const runExport = async () => {
    setErr(''); setExportBusy(true);
    try {
      await ledgerService.exportLedger();
      setExported(true);
    } catch (e: any) {
      setErr(apiErr(e, 'export the ledger'));
    } finally {
      setExportBusy(false);
    }
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
      setDone(true);
    } catch (e: any) {
      // The register explains every refusal in a problem envelope ({error:{detail}}) —
      // `data.detail` is the WRONG key for it, so an audited refusal ("that operation is
      // not granted", "authorization changed since sign-in") reached the desk as the bare
      // "Request failed with status code 403", which names nothing anyone can act on.
      setErr(apiErr(e, 'import the ledger'));
    } finally {
      setBusy(false);
    }
  };

  const counts = result?.counts || {};
  const shown = Object.entries(counts).filter(([, v]) => Number(v) > 0);
  const book = result?.book;
  const cr = (v: number | undefined) =>
    `₹${Number(v || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })} Cr`;
  const spread = (d: Record<string, number> | undefined) =>
    Object.entries(d || {}).map(([k, v]) => `${v} ${k}`).join(' · ');

  // The book, in desk language — one line per area, shown after a successful import.
  const bookLines: [string, string][] = book ? [
    ['Clients on register', `${book.entities} companies · ${book.counterparties.active} of ${book.counterparties.total} lenders active in the FI master`],
    ['Leads', `${book.leads.total} (${spread(book.leads.by_status)})`],
    ['Deals', `${book.deals.total} — ${book.deals.lending} lending · ${book.deals.syndication} platform · ${book.deals.asset_mon} asset monetisation`],
    ['Lending', `${book.lending.lines} lines, ${cr(book.lending.amount_cr)} (${spread(book.lending.by_stage)})`],
    ['Platform deals', `${book.syndication.trackers} mandates at ${cr(book.syndication.ask_cr)} ask + ${book.syndication.partnership_trackers} partnerships · ${book.syndication.lenders} lender rows, ${cr(book.syndication.allocation_cr)} allocated (${spread(book.syndication.lenders_by_status)})`],
    ['Asset monetisation', `${book.asset_monetisation.mandates} mandates · ${cr(book.asset_monetisation.indicative_cr)} indicative · ${book.asset_monetisation.size_mw.toLocaleString('en-IN')} MW`],
  ] : [];

  return (
    <Dialog open={open} onClose={close} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', fontSize: 16, pb: 0 }}>
        Ledger (Excel)
        <IconButton onClick={close} sx={{ ml: 'auto' }} size="small"><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      {/* Export first: it is the frequent, safe direction, and nobody should have to
          pass the wipe-capable half to reach it. */}
      <Tabs value={tab} onChange={(_, v) => { setTab(v); setErr(''); }}
        sx={{ px: 2, minHeight: 38, '& .MuiTab-root': { minHeight: 38, fontSize: 12.6,
          textTransform: 'none', fontWeight: 700 } }}>
        <Tab value="export" label="Export" disabled={busy} />
        <Tab value="import" label="Import" disabled={exportBusy} />
      </Tabs>
      <DialogContent dividers>
        {err && <Alert severity="error" sx={{ mb: 1.2 }}>{String(err)}</Alert>}

        {tab === 'export' ? (
          <Box>
            <Typography sx={{ fontSize: 12.4, color: tokens.muted, mb: 1.2 }}>
              Writes the register out as the desk&apos;s Excel ledger — the same workbook
              shape the import reads, so a file exported here can be edited and brought
              straight back in.
            </Typography>
            {/* What you are about to get. The old Export tile fired a download and left
                you to open the file to find out what was in it. */}
            <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 1.5,
              p: '10px 12px', mb: 1.4 }}>
              <Typography sx={{ fontSize: 11, fontWeight: 700, color: tokens.muted,
                textTransform: 'uppercase', letterSpacing: '.5px', mb: 0.6 }}>
                The workbook contains
              </Typography>
              {[
                ['Clients & lenders', 'the company register and the FI master'],
                ['Leads & Deals', 'the pipeline with its stages and temperatures'],
                ['Lending', 'every line with stage, amount and dates'],
                ['Platform Deals', 'mandates, partnerships and lender allocations'],
                ['Asset Monetisation', 'mandates with indicative value and MW'],
              ].map(([label, text]) => (
                <Box key={label} sx={{ display: 'flex', gap: 1, mb: 0.35 }}>
                  <Typography sx={{ fontSize: 12, fontWeight: 700, color: tokens.navy,
                    minWidth: 132, flexShrink: 0 }}>{label}</Typography>
                  <Typography sx={{ fontSize: 12, color: tokens.ink }}>{text}</Typography>
                </Box>
              ))}
            </Box>
            <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
              Reading the book changes nothing on the register, and the download is
              audited like every other export.
            </Typography>
            {exported && (
              <Alert severity="success" sx={{ mt: 1.2 }}>
                Ledger downloaded. Check your browser&apos;s downloads if you do not see it.
              </Alert>
            )}
          </Box>
        ) : (
        <Box>
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
            onChange={(e) => { setFile(e.target.files?.[0] || null); e.target.value = '';
              setDone(false); }} />
          <ToggleButtonGroup exclusive size="small" value={mode}
            onChange={(_, v) => { if (v) { setMode(v); setDone(false); } }}>
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

        {result && (
          <Box sx={{ mt: 1.4 }}>
            <Alert severity={result.report.quarantined_count ? 'warning' : 'success'} sx={{ mb: 1 }}>
              Imported. {result.report.quarantined_count
                ? `${result.report.quarantined_count} row(s) could not land and are preserved below.`
                : 'Every row landed.'}
            </Alert>

            {/* The book after this import — desk language, not table names. */}
            {bookLines.length > 0 && (
              <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 1.5,
                p: '10px 12px', mb: 1 }}>
                <Typography sx={{ fontSize: 11, fontWeight: 700, color: tokens.muted,
                  textTransform: 'uppercase', letterSpacing: '.5px', mb: 0.6 }}>
                  Your book now holds
                </Typography>
                {bookLines.map(([label, text]) => (
                  <Box key={label} sx={{ display: 'flex', gap: 1, mb: 0.35 }}>
                    <Typography sx={{ fontSize: 12, fontWeight: 700, color: tokens.navy,
                      minWidth: 132, flexShrink: 0 }}>{label}</Typography>
                    <Typography sx={{ fontSize: 12, color: tokens.ink }}>{text}</Typography>
                  </Box>
                ))}
              </Box>
            )}

            <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap', mb: 1 }}>
              {shown.map(([k, v]) => (
                <Chip key={k} size="small" label={`${k.replace(/_/g, ' ')}: ${v}`} />
              ))}
            </Box>

            {/* Rows that could not land — shown IN FULL, because the file is the only
                other place their content exists (e.g. lead rows with no company name). */}
            {result.report.quarantined_count > 0 && (
              <Box sx={{ maxHeight: 220, overflow: 'auto', border: `1px solid ${tokens.line}`,
                borderRadius: 1, p: 1 }}>
                <Typography sx={{ fontSize: 11, fontWeight: 700, color: tokens.muted,
                  textTransform: 'uppercase', letterSpacing: '.5px', mb: 0.5 }}>
                  Rows that need your attention (fix in Excel, re-import in merge mode)
                </Typography>
                {result.report.quarantined.map((q: any, i: number) => (
                  <Box key={i} sx={{ mb: 0.7 }}>
                    <Typography sx={{ fontSize: 11.8, fontWeight: 600, color: tokens.ink }}>
                      {q.sheet}{q.company ? ` · ${q.company}` : ''} — {q.reason}
                    </Typography>
                    {q.value != null && (
                      <Typography sx={{ fontSize: 11.2, color: tokens.muted,
                        whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                        {String(q.value)}
                      </Typography>
                    )}
                    {q.missing?.length ? (
                      <Typography sx={{ fontSize: 11.2, color: tokens.muted }}>
                        missing: {q.missing.join(', ')}
                      </Typography>
                    ) : null}
                  </Box>
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
        </Box>
        )}
      </DialogContent>
      <DialogActions>
        {/* Once the action has run, CLOSE is what is left to do — it carries the
            emphasis, and the spent action steps back rather than disappearing, so the
            reason it is spent stays readable. */}
        <Button onClick={close} disabled={busy || exportBusy}
          variant={(tab === 'import' ? done : exported) ? 'contained' : 'text'}>
          Close
        </Button>
        {tab === 'export' ? (
          <Button variant={exported ? 'outlined' : 'contained'} onClick={runExport}
            disabled={exportBusy}
            startIcon={exportBusy ? <CircularProgress size={14} /> : undefined}>
            {exportBusy ? 'Preparing…' : exported ? 'Download again' : 'Download ledger'}
          </Button>
        ) : (
          <Tooltip title={done
            ? 'This file is already imported — choose another file, or switch the mode, to import again.'
            : ''}>
            <Box component="span">
              <Button variant={done ? 'outlined' : 'contained'} onClick={run}
                disabled={busy || done}
                startIcon={busy ? <CircularProgress size={14} /> : undefined}>
                {busy ? 'Importing…' : done ? 'Imported' : 'Import'}
              </Button>
            </Box>
          </Tooltip>
        )}
      </DialogActions>
    </Dialog>
  );
}
