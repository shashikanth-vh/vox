import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, TextField, Typography } from '@mui/material';
import MicIcon from '@mui/icons-material/Mic';
import StopIcon from '@mui/icons-material/Stop';
import { useRecorder, MAX_SECONDS } from './useRecorder';
import { vocxService, currentPosition, type VocxPreview } from '../../services/vocxService';
import { currentRm } from './rm';
import { useVocx } from './VocxProvider';
import ReportCard from './ReportCard';
import { vx, pill, pillPrimary, input as inputSx } from './vocxStyles';

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

  // Tell the rest of VocX the microphone is live, so the panel refuses to close and the
  // launcher shows it. `finishing` counts: the clip is encoded but not yet handed off,
  // and unmounting there loses it just as surely.
  const { setRecording } = useVocx();
  const live = rec.state === 'recording' || rec.state === 'requesting'
    || rec.state === 'finishing';
  useEffect(() => {
    setRecording(live);
    return () => setRecording(false);
  }, [live, setRecording]);

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

      <Typography sx={{ fontSize: 34, fontWeight: 700, mb: 0.4, mt: 1, letterSpacing: '-.01em' }}>
        Tap. Speak. Done.
      </Typography>
      <Typography sx={{ fontSize: 15.5, color: vx.grn2, fontWeight: 600, mb: 2.4,
                        maxWidth: 400, mx: 'auto' }}>
        Don't just take notes. Record it.
      </Typography>

      {/* The mic. Recording is MOTION, not a colour swap: the button keeps the
          surface's own green the whole time, and the state is carried by animation —
          an expanding cyan ripple for as long as the take runs, the level ring tracking
          the live input (so a dead microphone is visible before the user has spoken for
          three minutes into nothing), and the icon itself breathing. An alarm colour on
          the fill made a normal, wanted state look like something going wrong.
          Reduced-motion users get a static cyan ring instead — with the animation gone,
          colour is the one channel left to say the mic is open. */}
      <Box sx={{ position: 'relative', width: 200, height: 200, mx: 'auto', mb: 2 }}>
        <Box aria-hidden sx={{
          position: 'absolute', inset: 0, borderRadius: '50%',
          border: `2px solid ${recording ? vx.live : vx.line}`,
          transform: `scale(${1 + (recording ? rec.level * 0.28 : 0)})`,
          transition: 'transform 90ms linear, border-color 200ms',
        }} />
        {recording && (
          <Box aria-hidden sx={{
            position: 'absolute', inset: 26, borderRadius: '50%',
            '@keyframes vocxRipple': {
              '0%': { boxShadow: `0 0 0 0 ${vx.liveSoft}` },
              '70%': { boxShadow: '0 0 0 26px rgba(34,211,238,0)' },
              '100%': { boxShadow: '0 0 0 0 rgba(34,211,238,0)' },
            },
            animation: 'vocxRipple 1.6s ease-out infinite',
            '@media (prefers-reduced-motion: reduce)': {
              animation: 'none',
              boxShadow: `0 0 0 5px ${vx.liveSoft}`,
            },
          }} />
        )}
        <Box
          component="button"
          type="button"
          onClick={() => (recording ? rec.stop() : rec.start())}
          disabled={busy || !rm || rec.state === 'requesting' || rec.state === 'finishing'}
          aria-label={recording ? 'Stop recording' : 'Start recording'}
          aria-pressed={recording}
          sx={{
            position: 'absolute', inset: 26, borderRadius: '50%', border: 0,
            cursor: busy || !rm ? 'not-allowed' : 'pointer',
            bgcolor: vx.grn,
            color: vx.onGrn,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            opacity: busy || !rm ? 0.5 : 1,
            '&:focus-visible': { outline: `3px solid ${vx.grn}`, outlineOffset: 3 },
            '@keyframes vocxBreathe': {
              '0%, 100%': { transform: 'scale(1)' },
              '50%': { transform: 'scale(1.08)' },
            },
            '& > svg': recording ? {
              animation: 'vocxBreathe 1.6s ease-in-out infinite',
              '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
            } : {},
          }}
        >
          {recording ? <StopIcon sx={{ fontSize: 64 }} /> : <MicIcon sx={{ fontSize: 68 }} />}
        </Box>
      </Box>

      <Typography sx={{ fontSize: 30, fontWeight: 700, color: recording ? vx.live : vx.grn, minHeight: 38,
        fontVariantNumeric: 'tabular-nums' }}>
        {recording ? clock(rec.seconds) : busy ? '' : 'Tap the mic and start speaking'}
      </Typography>
      <Typography sx={{ fontSize: 15, color: vx.mut, minHeight: 22 }}>
        {status
          || (recording
            ? `Tap again to stop · ${clock(Math.max(0, remaining))} left`
            : rec.state === 'requesting' ? 'Waiting for the microphone…' : '')}
      </Typography>

      {recording && (
        <Button size="small" onClick={rec.cancel} sx={{ textTransform: 'none', mt: 0.5,
          color: vx.mut }}>Cancel</Button>
      )}

      {/* What PRISM does with a spoken note — shown while idle, gone once a take is
          running (the timer owns that moment). Not marketing: each step is literally
          the next screen the user will see. */}
      {!recording && !busy && (
        <Box sx={{ display: 'flex', gap: 0.8, justifyContent: 'center', mt: 2.2,
                   flexWrap: 'wrap', px: 1 }}>
          {[
            ['Capture', `raw intel in EN, HI or Hinglish — up to ${MAX_SECONDS / 60} minutes`],
            ['Structure', 'PRISM maps the client, key insights and actions'],
            ['Execute', 'file, sync and close — on your approval'],
          ].map(([title, sub]) => (
            <Box key={title} sx={{ flex: '1 1 118px', maxWidth: 148, textAlign: 'left',
                                   bgcolor: vx.card, border: `1px solid ${vx.line}`,
                                   borderRadius: '12px', p: 1.1 }}>
              <Typography sx={{ fontSize: 12.5, fontWeight: 800, color: vx.grn,
                                lineHeight: 1.25 }}>{title}</Typography>
              <Typography sx={{ fontSize: 10.5, color: vx.mut, mt: 0.4,
                                lineHeight: 1.4 }}>{sub}</Typography>
            </Box>
          ))}
        </Box>
      )}

      {!recording && !busy && (
        <Typography sx={{ fontSize: 11, color: vx.mut, letterSpacing: '.08em', mt: 1.6,
                          textTransform: 'uppercase' }}>
          Zero friction · Maximum momentum
        </Typography>
      )}

      {!recording && (
        <Box sx={{ mt: 1.5 }}>
          {!showTyped ? (
            <Button onClick={() => setShowTyped(true)} disabled={busy || !rm}
              sx={{ textTransform: 'none', fontSize: 15, color: vx.grn2 }}>Type instead</Button>
          ) : (
            <Box sx={{ textAlign: 'left' }}>
              <TextField
                multiline minRows={3} fullWidth size="small" autoFocus value={typed}
                onChange={(e) => setTyped(e.target.value)}
                placeholder="Met the EcoSoch team about the term loan…"
                inputProps={{ 'aria-label': 'Type the note' }}
                sx={inputSx}
              />
              <Box sx={{ display: 'flex', gap: 0.8, mt: 0.8 }}>
                <Button onClick={analyseTyped}
                  disabled={busy || !typed.trim()} sx={pillPrimary}>
                  {busy ? 'Reading…' : 'Analyse'}
                </Button>
                <Button onClick={() => { setShowTyped(false); setTyped(''); }}
                  sx={pill}>Cancel</Button>
              </Box>
            </Box>
          )}
        </Box>
      )}
    </Box>
  );
}
