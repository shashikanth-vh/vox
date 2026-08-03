import { useMemo, useState } from 'react';
import { Alert, Box, Button, Chip, TextField, Typography } from '@mui/material';
import { vocxService, captureIdOf, type VocxPreview } from '../../services/vocxService';
import { currentRm } from './rm';
import { tokens } from '../../theme';

/**
 * Review and APPROVE one capture.
 *
 * Nothing VocX extracts is filed until someone says so — the preview call writes nothing,
 * and this card is where a human reads what the machine heard, corrects it, and commits.
 * That is the same maker/checker shape the rest of the platform uses, applied to the one
 * place where the input is a voice and the extraction is a guess.
 *
 * The transcript is editable and re-analysable on purpose: a misheard company name is
 * better fixed at the source and re-extracted than patched field by field.
 */

const label = (s: string) => s.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());

function Field({ k, v }: { k: string; v: any }) {
  if (v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length)) return null;
  const text = Array.isArray(v)
    ? v.map((x) => (typeof x === 'object' ? JSON.stringify(x) : String(x))).join(' · ')
    : typeof v === 'object' ? JSON.stringify(v) : String(v);
  return (
    <Box sx={{ display: 'flex', gap: 1, py: 0.3 }}>
      <Typography sx={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.5px',
        color: 'rgba(232,238,242,.5)', fontWeight: 700, minWidth: 96, flexShrink: 0, pt: '2px' }}>
        {label(k)}
      </Typography>
      <Typography sx={{ fontSize: 12.5, color: '#E8EEF2', wordBreak: 'break-word' }}>{text}</Typography>
    </Box>
  );
}

/** Keys that are plumbing rather than content — shown nowhere, kept in the payload. */
const HIDDEN = new Set(['_meta', 'entity_match', 'transcript', 'raw', 'segments']);

export default function PreviewCard({ preview, onFiled, onDiscarded }: {
  preview: VocxPreview;
  onFiled: (message: string) => void;
  onDiscarded: () => void;
}) {
  const rm = currentRm();
  const ext = preview.extraction || {};
  const captureId = captureIdOf(preview);
  const match = ext.entity_match || {};

  const [transcript, setTranscript] = useState<string>(String(ext.transcript || ''));
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [current, setCurrent] = useState<VocxPreview>(preview);

  const cur = current.extraction || {};
  const curMatch = cur.entity_match || {};
  const fields = useMemo(
    () => Object.entries(cur).filter(([k]) => !HIDDEN.has(k) && !k.startsWith('_')),
    [cur]);

  const reanalyse = async () => {
    const t = transcript.trim();
    if (!t) { setErr('There is nothing to re-analyse.'); return; }
    setErr(''); setBusy('Re-reading the note…');
    const r = await vocxService.captureTyped(t, rm, {}, captureId);
    setBusy('');
    if (!r.ok) { setErr(r.error); return; }
    setCurrent(r.data);
  };

  const approve = async () => {
    setErr(''); setBusy('Filing…');
    const r = await vocxService.commit({
      rm,
      extraction: cur,
      summary: current.summary,
      capture_id: captureId,
      // The transcript the reviewer approved is the one that gets filed, even if they
      // edited it without re-analysing.
      ...(transcript.trim() && transcript.trim() !== String(cur.transcript || '')
        ? { edits: { transcript: transcript.trim() } }
        : {}),
    });
    setBusy('');
    if (!r.ok) { setErr(r.error); return; }
    const results = (r.data?.writes?.results || []) as any[];
    const failed = results.filter((w) => w.status && w.status !== 'ok' && w.status !== 'skipped');
    onFiled(failed.length
      ? `Filed, but ${failed.length} write(s) did not land — see the report.`
      : 'Filed to the register.');
  };

  const discard = async () => {
    setErr(''); setBusy('Discarding…');
    if (captureId) await vocxService.remove(rm, captureId);   // a draft may not exist yet
    setBusy('');
    onDiscarded();
  };

  return (
    <Box sx={{ p: 1.4 }}>
      {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }}
        onClose={() => setErr('')}>{err}</Alert>}

      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mb: 1, flexWrap: 'wrap' }}>
        <Typography sx={{ fontSize: 14, fontWeight: 800 }}>
          {cur.company_mentioned || curMatch.proposed_company || 'Unnamed company'}
        </Typography>
        {curMatch.code && <Chip size="small" label={curMatch.code}
          sx={{ height: 19, fontSize: 10.5, bgcolor: 'rgba(45,214,163,.15)', color: tokens.tealHi }} />}
        {curMatch.code == null && (
          <Chip size="small" label="new company"
            sx={{ height: 19, fontSize: 10.5, bgcolor: 'rgba(240,180,60,.16)', color: '#F0B43C' }} />
        )}
      </Box>

      {current.summary && (
        <Typography sx={{ fontSize: 12.5, color: 'rgba(232,238,242,.85)', mb: 1 }}>
          {current.summary}
        </Typography>
      )}

      <Box sx={{ borderTop: `1px solid ${tokens.line}`, pt: 0.8, mb: 1 }}>
        {fields.map(([k, v]) => <Field key={k} k={k} v={v} />)}
      </Box>

      <Typography sx={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.6px',
        color: 'rgba(232,238,242,.5)', fontWeight: 700, mb: 0.4 }}>Transcript</Typography>
      <TextField
        multiline minRows={3} maxRows={10} fullWidth size="small" value={transcript}
        onChange={(e) => setTranscript(e.target.value)}
        inputProps={{ 'aria-label': 'Transcript' }}
        sx={{
          '& .MuiInputBase-root': { bgcolor: 'rgba(255,255,255,.04)', color: '#E8EEF2', fontSize: 12.5 },
          '& fieldset': { borderColor: tokens.line },
        }}
      />
      <Typography sx={{ fontSize: 10.5, color: 'rgba(232,238,242,.5)', mt: 0.4 }}>
        Correcting the transcript and re-reading it is better than patching a field —
        the extraction is rebuilt from what was actually said.
      </Typography>

      <Box sx={{ display: 'flex', gap: 0.8, mt: 1.2, flexWrap: 'wrap' }}>
        <Button size="small" variant="contained" disabled={!!busy} onClick={approve}
          sx={{ textTransform: 'none', fontWeight: 700 }}>
          {busy === 'Filing…' ? 'Filing…' : 'Approve & file'}
        </Button>
        <Button size="small" variant="outlined" disabled={!!busy} onClick={reanalyse}
          sx={{ textTransform: 'none' }}>Re-read</Button>
        <Box sx={{ flex: 1 }} />
        <Button size="small" color="inherit" disabled={!!busy} onClick={discard}
          sx={{ textTransform: 'none', color: 'rgba(232,238,242,.6)' }}>Discard</Button>
      </Box>
      {busy && <Typography sx={{ fontSize: 11.5, color: tokens.tealHi, mt: 0.6 }}>{busy}</Typography>}
    </Box>
  );
}
