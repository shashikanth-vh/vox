/**
 * Record — Mode A, the volume path (spec screen 4). Explicit start, explicit
 * finish, honest cap. Pause/Resume is a peer control and each resume is a new
 * segment; pause as many times as needed inside the 3:00 budget — the pipeline
 * transcribes the stitched whole, so pauses never split a note into two reports.
 *
 * The capture id is minted ONCE per take, so a flaky upload retried by the user
 * replays the same conversation instead of duplicating it.
 */

import { Alert, Box, Button, Chip, Typography } from '@mui/material';
import { useEffect, useRef, useState } from 'react';
import { useAuth } from '../../../auth/AuthContext';
import { getSession } from '../../../auth/session';
import { voxService } from '../../../services/voxService';
import { banner, chip, pill, pillGhost, vx } from '../vocxStyles';

const CAP_SECONDS = 180; // Mode A: 3:00. Mode B (live, 90:00) arrives with Phase 2.

type Phase = 'idle' | 'recording' | 'paused' | 'uploading' | 'done' | 'error';

export default function VoxRecord({ onCaptured }: {
  onCaptured: (conversationId: string) => void;
}) {
  const { user } = useAuth();
  const [phase, setPhase] = useState<Phase>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [segments, setSegments] = useState<number[]>([]); // seconds per closed segment
  const [err, setErr] = useState('');
  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const captureIdRef = useRef('');
  const segStartRef = useRef(0);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const gpsRef = useRef<{ lat?: number; lng?: number }>({});

  useEffect(() => () => { if (tickRef.current) clearInterval(tickRef.current); }, []);

  const start = async () => {
    setErr('');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      navigator.geolocation?.getCurrentPosition(
        (p) => { gpsRef.current = { lat: p.coords.latitude, lng: p.coords.longitude }; },
        () => {}, { timeout: 4000 });
      const rec = new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (e) => { if (e.data.size) chunksRef.current.push(e.data); };
      rec.start(1000);
      recRef.current = rec;
      captureIdRef.current = `vox-${crypto.randomUUID()}`;
      segStartRef.current = 0;
      setSegments([]);
      setElapsed(0);
      setPhase('recording');
      tickRef.current = setInterval(() => setElapsed((s) => {
        if (s + 1 >= CAP_SECONDS) void finish();
        return s + 1;
      }), 1000);
    } catch {
      setErr('The microphone is not available. Allow mic access and try again.');
      setPhase('error');
    }
  };

  const pause = () => {
    if (recRef.current?.state !== 'recording') return;
    recRef.current.pause();
    if (tickRef.current) clearInterval(tickRef.current);
    setSegments((seg) => [...seg, elapsed - segStartRef.current]);
    setPhase('paused');
  };

  const resume = () => {
    if (recRef.current?.state !== 'paused') return;
    recRef.current.resume();
    segStartRef.current = elapsed;
    setPhase('recording');
    tickRef.current = setInterval(() => setElapsed((s) => {
      if (s + 1 >= CAP_SECONDS) void finish();
      return s + 1;
    }), 1000);
  };

  const discard = () => {
    if (!window.confirm('Discard this note? Nothing has been saved.')) return;
    stopStream();
    setPhase('idle'); setElapsed(0); setSegments([]);
  };

  const stopStream = () => {
    if (tickRef.current) clearInterval(tickRef.current);
    const rec = recRef.current;
    if (rec && rec.state !== 'inactive') rec.stop();
    rec?.stream.getTracks().forEach((t) => t.stop());
  };

  const finish = async () => {
    const rec = recRef.current;
    if (!rec || phase === 'uploading') return;
    setPhase('uploading');
    const stopped = new Promise<void>((resolve) => { rec.onstop = () => resolve(); });
    stopStream();
    await stopped;
    const blob = new Blob(chunksRef.current, { type: rec.mimeType || 'audio/webm' });
    try {
      const out = await voxService.capture(blob, {
        captureId: captureIdRef.current,
        mode: 'post_meeting',
        rm: user.full,
        email: getSession()?.email || '',
        durationSeconds: elapsed,
        ...gpsRef.current,
      });
      setPhase('done');
      onCaptured(out.conversation_id);
    } catch (e: any) {
      // The take is still in memory — Send again REUSES the same capture id, so a
      // retry replays rather than duplicates.
      chunksRef.current = [blob] as any;
      setErr(String(e?.message || e));
      setPhase('error');
    }
  };

  const retryUpload = async () => {
    setPhase('uploading'); setErr('');
    try {
      const blob = chunksRef.current[0] instanceof Blob && chunksRef.current.length === 1
        ? (chunksRef.current[0] as Blob)
        : new Blob(chunksRef.current, { type: 'audio/webm' });
      const out = await voxService.capture(blob, {
        captureId: captureIdRef.current, mode: 'post_meeting',
        rm: user.full, email: getSession()?.email || '', durationSeconds: elapsed, ...gpsRef.current,
      });
      setPhase('done');
      onCaptured(out.conversation_id);
    } catch (e: any) { setErr(String(e?.message || e)); setPhase('error'); }
  };

  const mmss = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  const recording = phase === 'recording';
  const paused = phase === 'paused';

  return (
    <Box sx={{ textAlign: 'center', pt: 1 }}>
      {err && <Alert severity="warning" onClose={() => setErr('')} sx={{ mb: 1, textAlign: 'left' }}>{err}</Alert>}

      <Typography sx={{ fontSize: 13, color: vx.mut }}>
        Post-meeting note · cap {mmss(CAP_SECONDS)}
        {paused && <b style={{ color: vx.amberInk }}> · PAUSED</b>}
      </Typography>
      <Typography sx={{ fontSize: 46, fontWeight: 200, fontVariantNumeric: 'tabular-nums', my: 0.5 }}>
        {mmss(elapsed)} <span style={{ fontSize: 16, color: vx.mut }}>/ {mmss(CAP_SECONDS)}</span>
      </Typography>

      {(recording || paused) && (
        <Box sx={{ mb: 1 }}>
          {segments.map((s, i) => (
            <Chip key={i} label={`seg ${i + 1} · ${mmss(s)}`}
              sx={{ ...chip(true), mr: 0.7, fontSize: 12, px: 1 }} />
          ))}
          <Chip label={recording ? `seg ${segments.length + 1} …` : 'resume starts a new segment'}
            sx={{ ...chip(false, true), fontSize: 12, px: 1 }} />
        </Box>
      )}

      {/* the one big control */}
      {phase === 'idle' || phase === 'error' ? (
        <Box>
          <Box onClick={start} role="button" aria-label="Start recording" sx={{ width: 86, height: 86, borderRadius: '50%', bgcolor: vx.grn,
            m: '14px auto 8px', display: 'flex', alignItems: 'center', justifyContent: 'center',
            cursor: 'pointer', boxShadow: '0 0 0 10px rgba(52,211,153,.12)' }}>
            <Box sx={{ width: 30, height: 30, borderRadius: '50%', bgcolor: vx.onGrn }} />
          </Box>
          <Typography sx={{ color: vx.grn2, fontWeight: 600 }}>Record</Typography>
          {phase === 'error' && chunksRef.current.length > 0 && (
            <Button sx={{ ...pill, mt: 1.5 }} onClick={retryUpload}>Send the take again</Button>
          )}
        </Box>
      ) : phase === 'uploading' ? (
        <Typography sx={{ color: vx.mut, my: 3 }}>Sending… you can keep the panel open or close it —
          processing continues on the server.</Typography>
      ) : phase === 'done' ? (
        <Typography sx={{ color: vx.grn2, my: 3, fontWeight: 600 }}>Sent. Writing the report…</Typography>
      ) : (
        <Box>
          <Box onClick={paused ? resume : pause} role="button"
            aria-label={paused ? 'Resume recording' : 'Pause recording'}
            sx={{ width: 86, height: 86, borderRadius: '50%',
              bgcolor: paused ? vx.grn : '#173A2C', m: '14px auto 8px', display: 'flex',
              alignItems: 'center', justifyContent: 'center', cursor: 'pointer',
              border: `1px solid ${paused ? vx.grn : '#2C5A44'}` }}>
            {paused ? (
              <Box sx={{ width: 0, height: 0, borderLeft: `24px solid ${vx.onGrn}`,
                borderTop: '15px solid transparent', borderBottom: '15px solid transparent', ml: '6px' }} />
            ) : (
              <Box sx={{ display: 'flex', gap: '7px' }}>
                <Box sx={{ width: 9, height: 30, bgcolor: vx.ink, borderRadius: 1 }} />
                <Box sx={{ width: 9, height: 30, bgcolor: vx.ink, borderRadius: 1 }} />
              </Box>
            )}
          </Box>
          <Typography sx={{ color: vx.grn2, fontWeight: 600, mb: 1.5 }}>
            {paused ? 'Resume' : 'Pause'}
          </Typography>
          <Box sx={{ display: 'flex', gap: 1, justifyContent: 'center' }}>
            <Button sx={pill} onClick={finish}>■ Finish note</Button>
            <Button sx={pillGhost} onClick={discard}>Discard…</Button>
          </Box>
        </Box>
      )}

      <Box sx={{ ...banner(), textAlign: 'center', mt: 2.5 }}>
        Nothing records on its own. Explicit start, explicit finish — pause and resume
        as many times as you need.
      </Box>
    </Box>
  );
}
