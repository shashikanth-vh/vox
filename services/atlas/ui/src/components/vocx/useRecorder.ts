import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Microphone capture, as a hook.
 *
 * Recording holds two OS-level resources — a MediaStream and an audio context — and a
 * page that forgets to release them leaves the browser's recording indicator lit long
 * after the user has moved on. Owning them here means the cleanup is guaranteed by
 * unmount rather than by every caller remembering.
 *
 * The cap is a real budget, not a UI preference. A capture is transcribed SYNCHRONOUSLY
 * on CPU, so the clip's length sets how long every hop between here and Whisper must be
 * willing to wait — the edge and the gateway both carry a matching long window for the
 * capture path alone. Stopping AT the limit keeps the audio recorded so far; a clip that
 * simply ran on would be refused after the user had finished speaking.
 *
 * TWO minutes, not three. Decode time scales with the clip, and three minutes of speech
 * sat close enough to the wait every hop allows that a slow deployment could turn a
 * finished recording into a timeout. Two leaves real headroom — and a field note that
 * needs longer than two minutes is two notes.
 */

export const MAX_SECONDS = 120;

export type RecorderState = 'idle' | 'requesting' | 'recording' | 'finishing';

export interface Recorder {
  state: RecorderState;
  seconds: number;
  /** 0..1, for the level ring. Smoothed — a raw RMS reads as jitter. */
  level: number;
  error: string;
  supported: boolean;
  start: () => Promise<void>;
  stop: () => void;
  /** Drop a recording in progress without producing a clip. */
  cancel: () => void;
}

/** Codecs in the order we prefer them; the browser picks the first it supports. */
const CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
];

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === 'undefined') return undefined;
  return CANDIDATES.find((t) => {
    try { return MediaRecorder.isTypeSupported(t); } catch { return false; }
  });
}

/**
 * `onClip` receives the finished recording. It is NOT called when the user cancels, and
 * not called for an empty clip — a zero-byte capture would reach VocX as "empty audio
 * body" and read to the user as a failure they did not cause.
 */
export function useRecorder(onClip: (blob: Blob, seconds: number) => void): Recorder {
  const [state, setState] = useState<RecorderState>('idle');
  const [seconds, setSeconds] = useState(0);
  const [level, setLevel] = useState(0);
  const [error, setError] = useState('');

  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const rafRef = useRef<number | null>(null);
  const tickRef = useRef<number | null>(null);
  const cancelledRef = useRef(false);
  const secondsRef = useRef(0);
  const onClipRef = useRef(onClip);
  onClipRef.current = onClip;

  const supported =
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof MediaRecorder !== 'undefined';

  const release = useCallback(() => {
    if (rafRef.current !== null) { cancelAnimationFrame(rafRef.current); rafRef.current = null; }
    if (tickRef.current !== null) { clearInterval(tickRef.current); tickRef.current = null; }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    // close() rejects if the context is already closed — a torn-down page is not an error.
    audioCtxRef.current?.close().catch(() => {});
    audioCtxRef.current = null;
    setLevel(0);
  }, []);

  useEffect(() => () => { try { recRef.current?.stop(); } catch { /* already stopped */ } release(); },
    [release]);

  const meter = useCallback((stream: MediaStream) => {
    try {
      const Ctx: typeof AudioContext =
        (window as any).AudioContext || (window as any).webkitAudioContext;
      if (!Ctx) return;                       // no meter; recording is unaffected
      const ctx = new Ctx();
      audioCtxRef.current = ctx;
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const buf = new Uint8Array(analyser.frequencyBinCount);
      let smoothed = 0;
      const read = () => {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (const v of buf) { const d = (v - 128) / 128; sum += d * d; }
        const rms = Math.sqrt(sum / buf.length);
        smoothed = smoothed * 0.75 + Math.min(1, rms * 3) * 0.25;
        setLevel(smoothed);
        rafRef.current = requestAnimationFrame(read);
      };
      rafRef.current = requestAnimationFrame(read);
    } catch { /* the meter is decoration; never fail a capture for it */ }
  }, []);

  const start = useCallback(async () => {
    if (!supported) {
      setError('This browser cannot record audio. Type the note instead.');
      return;
    }
    if (recRef.current?.state === 'recording') return;
    setError(''); setSeconds(0); secondsRef.current = 0;
    cancelledRef.current = false;
    setState('requesting');
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (e: any) {
      setState('idle');
      // Name the actual obstacle: "permission denied" and "no microphone attached" need
      // different things from the user, and a generic failure tells them neither.
      const name = String(e?.name || '');
      setError(
        name === 'NotAllowedError' || name === 'SecurityError'
          ? 'Microphone blocked. Allow it for this site in the browser address bar, then tap again.'
          : name === 'NotFoundError' || name === 'DevicesNotFoundError'
            ? 'No microphone found. Plug one in, or type the note instead.'
            : name === 'NotReadableError'
              ? 'The microphone is in use by another application.'
              : `Microphone unavailable${e?.message ? `: ${e.message}` : '.'}`,
      );
      return;
    }
    streamRef.current = stream;
    const mimeType = pickMimeType();
    let rec: MediaRecorder;
    try {
      rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    } catch {
      release(); setState('idle');
      setError('This browser cannot encode audio for VocX. Type the note instead.');
      return;
    }
    recRef.current = rec;
    chunksRef.current = [];
    rec.ondataavailable = (e) => { if (e.data?.size) chunksRef.current.push(e.data); };
    rec.onstop = () => {
      const took = secondsRef.current;
      const blob = new Blob(chunksRef.current, { type: rec.mimeType || mimeType || 'audio/webm' });
      chunksRef.current = [];
      release();
      recRef.current = null;
      if (cancelledRef.current) { setState('idle'); return; }
      if (!blob.size) {
        setState('idle');
        setError('Nothing was recorded. Check the microphone and try again.');
        return;
      }
      setState('finishing');
      onClipRef.current(blob, took);
      setState('idle');
    };
    rec.onerror = () => {
      release(); recRef.current = null; setState('idle');
      setError('Recording stopped unexpectedly.');
    };
    rec.start();
    setState('recording');
    meter(stream);
    tickRef.current = window.setInterval(() => {
      secondsRef.current += 1;
      setSeconds(secondsRef.current);
      // At the cap, stop cleanly — the clip so far is kept, not discarded.
      if (secondsRef.current >= MAX_SECONDS) { try { rec.stop(); } catch { /* raced */ } }
    }, 1000);
  }, [meter, release, supported]);

  const stop = useCallback(() => {
    if (recRef.current?.state === 'recording') { try { recRef.current.stop(); } catch { /* raced */ } }
  }, []);

  const cancel = useCallback(() => {
    cancelledRef.current = true;
    if (recRef.current?.state === 'recording') { try { recRef.current.stop(); } catch { /* raced */ } }
    else { release(); setState('idle'); }
  }, [release]);

  return { state, seconds, level, error, supported, start, stop, cancel };
}
