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
import { useVocx } from '../VocxProvider';
import { getSession } from '../../../auth/session';
import { voxService } from '../../../services/voxService';
import { banner, card, chip, microHeading, pill, pillGhost, pillPrimary, vx } from '../vocxStyles';
import { deleteTake, loadUnsentTake, saveTake } from './takeStore';
import type { StoredTake } from './takeStore';

const CAP_SECONDS = 180; // Mode A: 3:00. Mode B (live, 90:00) arrives with Phase 2.

type Phase = 'idle' | 'recording' | 'paused' | 'uploading' | 'done' | 'error';

export default function VoxRecord({ onCaptured }: {
  onCaptured: (conversationId: string) => void;
}) {
  const { user } = useAuth();
  const { setRecording } = useVocx();
  const [phase, setPhase] = useState<Phase>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [segments, setSegments] = useState<number[]>([]); // seconds per closed segment
  /** A take a previous page-load never sent — found in IndexedDB, offered back. */
  const [recovered, setRecovered] = useState<StoredTake | null>(null);
  const [err, setErr] = useState('');
  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const captureIdRef = useRef('');
  const segStartRef = useRef(0);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const gpsRef = useRef<{ lat?: number; lng?: number }>({});
  // Refs, not closures: the interval and the auto-finish must read LIVE state — a
  // stale closure once let a take sail straight past the 3:00 cap in the field.
  const elapsedRef = useRef(0);
  const finishingRef = useRef(false);
  const finishRef = useRef<() => Promise<void>>(async () => {});

  useEffect(() => () => { if (tickRef.current) clearInterval(tickRef.current); }, []);
  useEffect(() => {
    if (phase === 'idle') void loadUnsentTake().then((t) => setRecovered(t));
  }, [phase]);
  // The close guard stops the polite exits; this warns on the impolite ones (F5,
  // tab close). The chunks are ALSO in IndexedDB, so even an ignored warning
  // loses at most the last second.
  useEffect(() => {
    if (!['recording', 'paused', 'uploading', 'error'].includes(phase)) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [phase]);

  const start = async () => {
    setErr('');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      navigator.geolocation?.getCurrentPosition(
        (p) => { gpsRef.current = { lat: p.coords.latitude, lng: p.coords.longitude }; },
        () => {}, { timeout: 4000 });
      const rec = new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (e) => {
        if (!e.data.size) return;
        chunksRef.current.push(e.data);
        // every chunk lands in IndexedDB too — a refresh loses at most a second
        saveTake({ id: captureIdRef.current, startedAt: new Date().toISOString(),
          elapsed: elapsedRef.current, mime: rec.mimeType || 'audio/webm',
          rm: user.full, chunks: [...chunksRef.current] });
      };
      rec.start(1000);
      recRef.current = rec;
      captureIdRef.current = `vox-${crypto.randomUUID()}`;
      segStartRef.current = 0;
      setSegments([]);
      setElapsed(0);
      elapsedRef.current = 0;
      finishingRef.current = false;
      setRecording(true);   // the panel now refuses to close until the take is safe
      setPhase('recording');
      startTick();
    } catch {
      setErr('The microphone is not available. Allow mic access and try again.');
      setPhase('error');
    }
  };

  const pause = () => {
    if (recRef.current?.state !== 'recording') return;
    recRef.current.pause();
    if (tickRef.current) clearInterval(tickRef.current);
    setSegments((seg) => [...seg, elapsedRef.current - segStartRef.current]);
    setPhase('paused');
  };

  const resume = () => {
    if (recRef.current?.state !== 'paused') return;
    recRef.current.resume();
    segStartRef.current = elapsedRef.current;
    setPhase('recording');
    startTick();
  };

  const startTick = () => {
    if (tickRef.current) clearInterval(tickRef.current);
    tickRef.current = setInterval(() => {
      elapsedRef.current += 1;
      setElapsed(elapsedRef.current);
      if (elapsedRef.current >= CAP_SECONDS) void finishRef.current();
    }, 1000);
  };

  const discard = () => {
    if (!window.confirm('Discard this note? Nothing has been saved.')) return;
    stopStream();
    void deleteTake(captureIdRef.current);
    chunksRef.current = [];
    setRecording(false);
    setPhase('idle'); setElapsed(0); elapsedRef.current = 0; setSegments([]);
  };

  const stopStream = () => {
    if (tickRef.current) clearInterval(tickRef.current);
    const rec = recRef.current;
    if (rec && rec.state !== 'inactive') rec.stop();
    rec?.stream.getTracks().forEach((t) => t.stop());
  };

  const finish = async () => {
    const rec = recRef.current;
    if (!rec || finishingRef.current) return;
    finishingRef.current = true;
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
        durationSeconds: elapsedRef.current,
        ...gpsRef.current,
      });
      void deleteTake(captureIdRef.current);   // safely on the server
      setRecording(false);
      setPhase('done');
      onCaptured(out.conversation_id);
    } catch (e: any) {
      // The take is still in memory — Send again REUSES the same capture id, so a
      // retry replays rather than duplicates. `recording` stays up: this tab is the
      // only copy, so the panel keeps refusing to close until it is sent or discarded.
      chunksRef.current = [blob] as any;
      finishingRef.current = false;
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
        rm: user.full, email: getSession()?.email || '',
        durationSeconds: elapsedRef.current, ...gpsRef.current,
      });
      void deleteTake(captureIdRef.current);
      setRecording(false);
      setPhase('done');
      onCaptured(out.conversation_id);
    } catch (e: any) { setErr(String(e?.message || e)); setPhase('error'); }
  };

  const sendRecovered = async (take: StoredTake) => {
    setPhase('uploading'); setErr('');
    try {
      const blob = new Blob(take.chunks, { type: take.mime || 'audio/webm' });
      const out = await voxService.capture(blob, {
        captureId: take.id, mode: 'post_meeting',
        rm: user.full, email: getSession()?.email || '',
        durationSeconds: take.elapsed, ...gpsRef.current,
      });
      await deleteTake(take.id);
      setRecovered(null);
      setPhase('done');
      onCaptured(out.conversation_id);
    } catch (e: any) { setErr(String(e?.message || e)); setPhase('idle'); }
  };

  const discardRecovered = async (take: StoredTake) => {
    if (!window.confirm('Discard the recovered take? It cannot be brought back.')) return;
    await deleteTake(take.id);
    setRecovered(null);
  };

  finishRef.current = finish;

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

      {recovered && phase === 'idle' && (
        <Box sx={{ ...card, textAlign: 'left', borderColor: '#4A3D1D' }}>
          <Typography sx={{ ...microHeading, color: vx.amberInk }}>Unsent take recovered</Typography>
          <Typography sx={{ fontSize: 13.5, mb: 1.2 }}>
            {mmss(recovered.elapsed)} recorded {new Date(recovered.startedAt).toLocaleString()} —
            the page closed before it was sent. Nothing was lost.
          </Typography>
          <Box sx={{ display: 'flex', gap: 1 }}>
            <Button sx={pillPrimary} onClick={() => void sendRecovered(recovered)}>Send it now</Button>
            <Button sx={pillGhost} onClick={() => void discardRecovered(recovered)}>Discard…</Button>
          </Box>
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
            <Box sx={{ mt: 1.5, display: 'flex', gap: 1, justifyContent: 'center' }}>
              <Button sx={pill} onClick={retryUpload}>Send the take again</Button>
              <Button sx={pillGhost} onClick={discard}>Discard…</Button>
            </Box>
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
