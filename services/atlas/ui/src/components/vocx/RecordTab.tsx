import { useCallback, useState } from 'react';
import { Alert, Box, Button, TextField, Typography } from '@mui/material';
import MicIcon from '@mui/icons-material/Mic';
import StopIcon from '@mui/icons-material/Stop';
import { useRecorder, MAX_SECONDS } from './useRecorder';
import { vocxService, currentPosition, type VocxPreview } from '../../services/vocxService';
import { currentRm } from './rm';
import ReportCard from './ReportCard';
import { tokens } from '../../theme';

/** m:ss */
const clock = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

export default function RecordTab({ onFiled }: { onFiled: () => void }) {
  const rm = currentRm();
  const [preview, setPreview] = useState<VocxPreview | null>(null);
  const [typed, setTyped] = useState('');
  const [showTyped, setShowTyped] = useState(false);
  const [status, setStatus] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const send = useCallback(async (blob: Blob) => {
    setErr(''); setBusy(true);
    setStatus('Transcribing — any language, filed in English…');
    // Location rides along when the browser will give it, and is simply absent when it
    // will not. A capture must never wait on, or fail because of, geolocation.
    const gps = await currentPosition();
    const r = await vocxService.captureAudio(blob, rm, gps);
    setBusy(false); setStatus('');
    if (!r.ok) { setErr(r.error); return; }
    setPreview(r.data);
  }, [rm]);

  const rec = useRecorder(send);

  const analyseTyped = async () => {
    const t = typed.trim();
    if (!t) return;
    setErr(''); setBusy(true); setStatus('Reading the note…');
    const gps = await currentPosition();
    const r = await vocxService.captureTyped(t, rm, gps);
    setBusy(false); setStatus('');
    if (!r.ok) { setErr(r.error); return; }
    setPreview(r.data); setTyped('');
  };

  if (preview) {
    return (
      <ReportCard
        preview={preview}
        onFiled={(msg) => { setPreview(null); setStatus(msg); onFiled(); }}
        onDiscarded={() => setPreview(null)}
      />
    );
  }

  const recording = rec.state === 'recording';
  const remaining = MAX_SECONDS - rec.seconds;

  return (
    <Box sx={{ p: 2, textAlign: 'center' }}>
      {!rm && (
        <Alert severity="warning" sx={{ mb: 1.2, py: 0, fontSize: 12, textAlign: 'left' }}>
          Sign in before capturing — a note has to be filed under a person.
        </Alert>
      )}
      {(err || rec.error) && (
        <Alert severity="warning" sx={{ mb: 1.2, py: 0, fontSize: 12, textAlign: 'left' }}
          onClose={() => setErr('')}>{err || rec.error}</Alert>
      )}

      <Typography sx={{ fontSize: 19, fontWeight: 800, mb: 0.3 }}>Tap. Speak. Done.</Typography>
      <Typography sx={{ fontSize: 12, color: 'rgba(232,238,242,.6)', mb: 2 }}>
        Up to {MAX_SECONDS / 60} minutes. Any language — EN, HI, Hinglish. VocX handles
        transcription, structure and filing.
      </Typography>

      {/* The mic. The ring tracks the live input level, so a dead microphone is visible
          before the user has spoken for three minutes into nothing. */}
      <Box sx={{ position: 'relative', width: 132, height: 132, mx: 'auto', mb: 1.2 }}>
        <Box aria-hidden sx={{
          position: 'absolute', inset: 0, borderRadius: '50%',
          border: `2px solid ${recording ? tokens.tealHi : 'rgba(45,214,163,.25)'}`,
          transform: `scale(${1 + (recording ? rec.level * 0.28 : 0)})`,
          transition: 'transform 90ms linear, border-color 200ms',
        }} />
        <Box
          component="button"
          type="button"
          onClick={() => (recording ? rec.stop() : rec.start())}
          disabled={busy || !rm || rec.state === 'requesting' || rec.state === 'finishing'}
          aria-label={recording ? 'Stop recording' : 'Start recording'}
          aria-pressed={recording}
          sx={{
            position: 'absolute', inset: 14, borderRadius: '50%', border: 0,
            cursor: busy || !rm ? 'not-allowed' : 'pointer',
            bgcolor: recording ? '#E0554E' : tokens.tealHi,
            color: recording ? '#fff' : '#04241B',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            opacity: busy || !rm ? 0.5 : 1,
            transition: 'background-color 200ms',
            '&:focus-visible': { outline: `3px solid ${tokens.tealHi}`, outlineOffset: 3 },
          }}
        >
          {recording ? <StopIcon sx={{ fontSize: 44 }} /> : <MicIcon sx={{ fontSize: 46 }} />}
        </Box>
      </Box>

      <Typography sx={{ fontSize: 13, fontWeight: 700, color: recording ? '#E0554E' : 'inherit',
        fontVariantNumeric: 'tabular-nums' }}>
        {recording ? clock(rec.seconds) : busy ? '' : 'Tap the mic and start speaking'}
      </Typography>
      <Typography sx={{ fontSize: 11.5, color: 'rgba(232,238,242,.55)', minHeight: 18 }}>
        {status
          || (recording
            ? `Tap again to stop · ${clock(Math.max(0, remaining))} left`
            : rec.state === 'requesting' ? 'Waiting for the microphone…' : '')}
      </Typography>

      {recording && (
        <Button size="small" onClick={rec.cancel} sx={{ textTransform: 'none', mt: 0.5,
          color: 'rgba(232,238,242,.6)' }}>Cancel</Button>
      )}

      {!recording && (
        <Box sx={{ mt: 1.5 }}>
          {!showTyped ? (
            <Button size="small" onClick={() => setShowTyped(true)} disabled={busy || !rm}
              sx={{ textTransform: 'none', color: tokens.tealHi }}>Type instead</Button>
          ) : (
            <Box sx={{ textAlign: 'left' }}>
              <TextField
                multiline minRows={3} fullWidth size="small" autoFocus value={typed}
                onChange={(e) => setTyped(e.target.value)}
                placeholder="Met the EcoSoch team about the term loan…"
                inputProps={{ 'aria-label': 'Type the note' }}
                sx={{
                  '& .MuiInputBase-root': { bgcolor: 'rgba(255,255,255,.04)', color: '#E8EEF2', fontSize: 12.5 },
                  '& fieldset': { borderColor: tokens.line },
                }}
              />
              <Box sx={{ display: 'flex', gap: 0.8, mt: 0.8 }}>
                <Button size="small" variant="contained" onClick={analyseTyped}
                  disabled={busy || !typed.trim()} sx={{ textTransform: 'none' }}>
                  {busy ? 'Reading…' : 'Analyse'}
                </Button>
                <Button size="small" onClick={() => { setShowTyped(false); setTyped(''); }}
                  sx={{ textTransform: 'none', color: 'rgba(232,238,242,.6)' }}>Cancel</Button>
              </Box>
            </Box>
          )}
        </Box>
      )}
    </Box>
  );
}
