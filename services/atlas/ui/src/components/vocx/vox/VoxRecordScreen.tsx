/**
 * Record — the blueprint's screen, verbatim markup, with every hardening the
 * field testing bought: the provider close-guard from mic-open to safe-on-server,
 * refs so the 3:00 cap cannot be missed, per-second IndexedDB persistence with
 * recovery on return, the same capture id across retries, and the in-app discard
 * sheet instead of a native dialog. Mode B (live) shows honestly as next-round.
 */

import { useEffect, useRef, useState } from 'react';
import { useAuth } from '../../../auth/AuthContext';
import { getSession } from '../../../auth/session';
import { useVocx } from '../VocxProvider';
import { voxService } from '../../../services/voxService';
import { appendTakeChunk, deleteTake, loadUnsentTake } from '../spec/takeStore';
import type { StoredTake } from '../spec/takeStore';
import { Ic } from './VoxApp';

const CAP_SECONDS = 180;          // Mode A — a post-meeting note is minutes, not a meeting
const CAP_LIVE_SECONDS = 5400;    // Mode B — the blueprint's 90-minute live meeting
const BARS = 44;

/** The certification the tick stores — permanently, even if the audio is later
 *  deleted (the consent record is write-once in the database itself). */
const CONSENT_TEXT =
  'I confirm that all attendees have been informed of the recording and have given their consent.';

type Phase = 'idle' | 'recording' | 'paused' | 'uploading' | 'error';

const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;

export default function VoxRecordScreen({ onClose, onCaptured }: {
  onClose: () => void;
  onCaptured: (conversationId: string) => void;
}) {
  const { user } = useAuth();
  const { setRecording } = useVocx();
  const [phase, setPhase] = useState<Phase>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [segments, setSegments] = useState<number[]>([]);
  const [mode, setMode] = useState<'A' | 'B'>('A');
  const [modeNote, setModeNote] = useState('');
  // Mode B's one hard gate: the tick that outlives the audio.
  const [consentTick, setConsentTick] = useState(false);
  const [consentBusy, setConsentBusy] = useState(false);
  const consentIdRef = useRef<string | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [recovered, setRecovered] = useState<StoredTake | null>(null);
  const [err, setErr] = useState('');
  const [bars, setBars] = useState<number[]>(() => Array.from({ length: BARS }, () => 4));

  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const captureIdRef = useRef('');
  const segStartRef = useRef(0);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const waveRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const gpsRef = useRef<{ lat?: number; lng?: number }>({});
  const elapsedRef = useRef(0);
  const finishingRef = useRef(false);
  const finishRef = useRef<() => Promise<void>>(async () => {});
  const capRef = useRef(CAP_SECONDS);
  // ---- streaming: the server holds the take WHILE it records ----------------
  // Chunks flush every few seconds; a failed flush retries on the next beat and
  // NEVER disturbs the recording. Cross-session resume starts the next segment.
  const streamSegRef = useRef(0);
  const flushedRef = useRef(0);
  const flushingRef = useRef(false);
  const streamedOkRef = useRef(false);
  const flushTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [serverTakes, setServerTakes] = useState<{ capture_id: string; elapsed: number;
    segments: number; mode: string; consent_id: string }[]>([]);

  const flushToServer = async () => {
    if (flushingRef.current) return;
    const end = chunksRef.current.length;
    if (end <= flushedRef.current) return;
    flushingRef.current = true;
    try {
      const batch = new Blob(chunksRef.current.slice(flushedRef.current, end));
      await voxService.streamAppend(batch, {
        captureId: captureIdRef.current, seg: streamSegRef.current,
        mime: recRef.current?.mimeType || 'audio/webm',
        mode: mode === 'B' ? 'live' : 'post_meeting',
        consentId: consentIdRef.current || '',
        elapsed: elapsedRef.current, rm: user.full,
      });
      flushedRef.current = end;
      streamedOkRef.current = true;
    } catch { /* next beat retries; IndexedDB still holds everything */ }
    finally { flushingRef.current = false; }
  };
  const startFlushing = () => {
    if (flushTimerRef.current) clearInterval(flushTimerRef.current);
    flushTimerRef.current = setInterval(() => { void flushToServer(); }, 10_000);
  };
  const stopFlushing = () => {
    if (flushTimerRef.current) { clearInterval(flushTimerRef.current); flushTimerRef.current = null; }
  };

  useEffect(() => () => {
    if (tickRef.current) clearInterval(tickRef.current);
    if (waveRef.current) clearInterval(waveRef.current);
  }, []);
  useEffect(() => {
    if (phase !== 'idle') return;
    void loadUnsentTake().then(setRecovered);
    voxService.streamUnfinished().then(setServerTakes).catch(() => {});
  }, [phase]);
  useEffect(() => () => stopFlushing(), []);
  useEffect(() => {
    if (!['recording', 'paused', 'uploading', 'error'].includes(phase)) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [phase]);

  const startTick = () => {
    if (tickRef.current) clearInterval(tickRef.current);
    tickRef.current = setInterval(() => {
      elapsedRef.current += 1;
      setElapsed(elapsedRef.current);
      if (elapsedRef.current >= capRef.current) void finishRef.current();
    }, 1000);
    if (waveRef.current) clearInterval(waveRef.current);
    waveRef.current = setInterval(() => {
      setBars(Array.from({ length: BARS }, () => 4 + Math.round(Math.random() * 26)));
    }, 140);
  };
  const stopWave = () => { if (waveRef.current) { clearInterval(waveRef.current); waveRef.current = null; } };

  /** Screen 04 — the one hard gate. The tick writes a PERMANENT consent
   *  record (immutable in the database) before the recorder ever opens;
   *  the live conversation later refuses to exist without it. */
  const grantConsent = async () => {
    setConsentBusy(true); setErr('');
    try {
      const r = await voxService.consent(CONSENT_TEXT, {
        platform: navigator.platform,
        app_version: 'vox-panel',
      });
      consentIdRef.current = r.id;
      setConsentTick(false);
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally { setConsentBusy(false); }
  };

  const start = async (resume?: { capture_id: string; elapsed: number;
    segments: number; mode: string; consent_id: string }) => {
    setErr('');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      navigator.geolocation?.getCurrentPosition(
        (p) => { gpsRef.current = { lat: p.coords.latitude, lng: p.coords.longitude }; },
        () => {}, { timeout: 4000 });
      let rec: MediaRecorder;
      try {
        // 32 kbps opus is transparent for speech; the browser default (~128k)
        // would make a 90-minute live take a 65-90 MB upload.
        rec = new MediaRecorder(stream, { audioBitsPerSecond: 32_000 });
      } catch {
        rec = new MediaRecorder(stream);
      }
      chunksRef.current = [];
      const startedAt = new Date().toISOString();
      rec.ondataavailable = (e) => {
        if (!e.data.size) return;
        const idx = chunksRef.current.length;
        chunksRef.current.push(e.data);
        // append-only: each chunk hits IndexedDB exactly once (the v1 full-array
        // rewrite was quadratic — fatal for a 90-minute take)
        appendTakeChunk({ id: captureIdRef.current, startedAt,
          elapsed: elapsedRef.current, mime: rec.mimeType || 'audio/webm',
          rm: user.full }, idx, e.data);
      };
      rec.start(1000);
      recRef.current = rec;
      if (resume) {
        // continue the SAME take: next server segment, clock carried forward
        captureIdRef.current = resume.capture_id;
        streamSegRef.current = resume.segments;
        elapsedRef.current = resume.elapsed || 0;
        if (resume.mode === 'live') { setMode('B'); consentIdRef.current = resume.consent_id || consentIdRef.current; }
        capRef.current = resume.mode === 'live' ? CAP_LIVE_SECONDS : CAP_SECONDS;
      } else {
        captureIdRef.current = `vox-${crypto.randomUUID()}`;
        streamSegRef.current = 0;
        elapsedRef.current = 0;
        capRef.current = mode === 'B' ? CAP_LIVE_SECONDS : CAP_SECONDS;
      }
      flushedRef.current = 0;
      streamedOkRef.current = !!resume;
      segStartRef.current = elapsedRef.current;
      finishingRef.current = false;
      setSegments([]); setElapsed(elapsedRef.current);
      setRecording(true);
      setPhase('recording');
      startTick();
      startFlushing();
    } catch {
      setErr('The microphone is not available. Allow mic access and try again.');
      setPhase('error');
    }
  };

  const togglePause = () => {
    const rec = recRef.current;
    if (!rec) return;
    if (rec.state === 'recording') {
      rec.pause();
      if (tickRef.current) clearInterval(tickRef.current);
      stopWave();
      setSegments((s) => [...s, elapsedRef.current - segStartRef.current]);
      setPhase('paused');
      void flushToServer();
    } else if (rec.state === 'paused') {
      rec.resume();
      segStartRef.current = elapsedRef.current;
      setPhase('recording');
      startTick();
    }
  };

  const stopStream = () => {
    if (tickRef.current) clearInterval(tickRef.current);
    stopWave();
    const rec = recRef.current;
    if (rec && rec.state !== 'inactive') rec.stop();
    rec?.stream.getTracks().forEach((t) => t.stop());
  };

  const finish = async () => {
    const rec = recRef.current;
    if (!rec || finishingRef.current) return;
    finishingRef.current = true;
    setPhase('uploading');
    stopFlushing();
    const stopped = new Promise<void>((resolve) => { rec.onstop = () => resolve(); });
    stopStream();
    await stopped;
    const blob = new Blob(chunksRef.current, { type: rec.mimeType || 'audio/webm' });
    const common = {
      mode: (mode === 'B' ? 'live' : 'post_meeting') as 'live' | 'post_meeting',
      consentId: consentIdRef.current || undefined,
      rm: user.full, email: getSession()?.email || '',
      durationSeconds: elapsedRef.current, ...gpsRef.current,
    };
    // Streamed-first: the audio is already on the server chunk by chunk — the
    // last batch flushes, then finish is a tiny POST. Any failure in this path
    // falls back to the one-shot upload of the complete local blob; the server
    // segments are then redundant and are discarded best-effort.
    try {
      if (streamedOkRef.current || flushedRef.current > 0) {
        await flushToServer();
        if (flushedRef.current >= chunksRef.current.length) {
          const out = await voxService.streamFinish({
            captureId: captureIdRef.current, ...common });
          void deleteTake(captureIdRef.current);
          setRecording(false);
          onCaptured(out.conversation_id);
          return;
        }
      }
    } catch { /* fall through to the one-shot path */ }
    try {
      const out = await voxService.capture(blob, {
        captureId: captureIdRef.current, ...common });
      void voxService.streamDiscard(captureIdRef.current).catch(() => {});
      void deleteTake(captureIdRef.current);
      setRecording(false);
      onCaptured(out.conversation_id);
    } catch (e: any) {
      chunksRef.current = [blob] as any;
      finishingRef.current = false;
      setErr(String(e?.message || e));
      setPhase('error');
    }
  };
  finishRef.current = finish;

  const retryUpload = async () => {
    setPhase('uploading'); setErr('');
    try {
      const blob = chunksRef.current.length === 1 && chunksRef.current[0] instanceof Blob
        ? (chunksRef.current[0] as Blob) : new Blob(chunksRef.current, { type: 'audio/webm' });
      const out = await voxService.capture(blob, {
        captureId: captureIdRef.current,
        mode: mode === 'B' ? 'live' : 'post_meeting',
        consentId: consentIdRef.current || undefined,
        rm: user.full, email: getSession()?.email || '',
        durationSeconds: elapsedRef.current, ...gpsRef.current,
      });
      void deleteTake(captureIdRef.current);
      setRecording(false);
      onCaptured(out.conversation_id);
    } catch (e: any) { setErr(String(e?.message || e)); setPhase('error'); }
  };

  const discardConfirm = () => {
    stopStream();
    stopFlushing();
    void voxService.streamDiscard(captureIdRef.current).catch(() => {});
    void deleteTake(captureIdRef.current);
    chunksRef.current = [];
    setRecording(false);
    setDiscardOpen(false);
    setPhase('idle'); setElapsed(0); elapsedRef.current = 0; setSegments([]);
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
      onCaptured(out.conversation_id);
    } catch (e: any) { setErr(String(e?.message || e)); setPhase('idle'); }
  };

  const live = phase === 'recording' || phase === 'paused';
  const stateLabel = phase === 'recording' ? 'Recording' : phase === 'paused' ? 'Paused'
    : phase === 'uploading' ? 'Sending…' : 'Ready';

  const requestClose = () => {
    if (live) setDiscardOpen(true);
    else onClose();
  };

  return (
    <div className="app-body no-tabs" style={{ display: 'flex', flexDirection: 'column', padding: 20 }}>
      <div className="rec-head">
        <button className="rec-close" onClick={requestClose}>✕</button>
        <div style={{ fontSize: 10, color: 'var(--muted)', fontFamily: "'JetBrains Mono',monospace",
          letterSpacing: '0.16em', fontWeight: 600 }}>
          {live ? (phase === 'paused' ? 'PAUSED' : 'RECORDING') : 'NEW RECORDING'}
        </div>
        <div style={{ width: 34 }} />
      </div>

      {discardOpen && (
        <div className="sheet-scrim show" onClick={() => setDiscardOpen(false)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="sheet-handle" />
            <div className="sheet-title">Discard this recording?</div>
            <div className="sheet-body">You have audio captured. Leaving now deletes it — it isn't saved anywhere yet.</div>
            <button className="btn btn-danger" style={{ marginBottom: 8 }} onClick={discardConfirm}>Discard recording</button>
            <button className="btn btn-ghost" onClick={() => setDiscardOpen(false)}>Keep recording</button>
          </div>
        </div>
      )}

      <div className="mode-tabs">
        <div className={`mode-tab${mode === 'A' ? ' active' : ''}${live ? ' locked' : ''}`}
          onClick={() => !live && setMode('A')}>Post-meeting note</div>
        <div className={`mode-tab${mode === 'B' ? ' active' : ''}${live ? ' locked' : ''}`}
          onClick={() => {
            if (live) return;
            setMode('B');
            setModeNote('');
          }}>Live meeting</div>
      </div>
      {modeNote && (
        <div style={{ fontSize: 11.5, color: 'var(--warn)', margin: '8px 2px 0' }}>{modeNote}</div>
      )}
      {err && (
        <div style={{ fontSize: 12, color: 'var(--danger)', margin: '8px 2px 0' }}>{err}</div>
      )}

      {mode === 'B' && !consentIdRef.current && phase === 'idle' ? (
        <>
          <div className="eyebrow" style={{ textAlign: 'center', color: 'var(--warn)', margin: '18px 0 0' }}>
            Consent required
          </div>
          <div className="consent-header">
            <div className="consent-badge"><Ic i="i-mic" /></div>
            <div className="consent-title-block">
              <div className="title">Live meeting recording</div>
              <div className="sub">Up to 90 minutes · Requires connectivity</div>
            </div>
          </div>
          <div className="consent-note">
            They know this is being recorded. This tick is stored permanently,
            even if you later delete the audio.
          </div>
          <div className={`consent-check${consentTick ? ' checked' : ''}`}
            onClick={() => setConsentTick((v) => !v)}>
            <div className="check-box" />
            <div style={{ fontSize: 13.5, lineHeight: 1.5 }}>
              <strong>I confirm</strong> that all attendees have been informed of the
              recording and have given their consent.
            </div>
          </div>
          <div className="consent-hint">Tick the box to start.</div>
          <div className="consent-audit">
            <span className="label">Recording by:</span> {getSession()?.email || user.full}<br />
            <span className="label">GPS &amp; device:</span> will be written when you start
          </div>
          <button className="btn btn-danger" disabled={!consentTick || consentBusy}
            style={!consentTick ? { opacity: 0.45, cursor: 'default' } : undefined}
            onClick={() => void grantConsent()}>
            Continue to recorder
          </button>
        </>
      ) : (
      <>
      {recovered && phase === 'idle' && (
        <div className="card" style={{ marginTop: 14, borderColor: 'rgba(245,181,73,0.5)' }}>
          <div className="card-eyebrow" style={{ color: 'var(--warn)' }}>Unsent take recovered</div>
          <div style={{ fontSize: 13, color: 'var(--text-2)', margin: '6px 0 12px' }}>
            {mmss(recovered.elapsed)} recorded {new Date(recovered.startedAt).toLocaleString()} —
            the page closed before it was sent. Nothing was lost.
          </div>
          <button className="btn btn-primary btn-sm" style={{ marginBottom: 8 }}
            onClick={() => void sendRecovered(recovered)}>Send it now</button>
          <button className="btn btn-ghost btn-sm" onClick={() => {
            void deleteTake(recovered.id);
            void voxService.streamDiscard(recovered.id).catch(() => {});
            setRecovered(null);
            setServerTakes((t) => t.filter((x) => x.capture_id !== recovered.id));
          }}>Discard</button>
        </div>
      )}

      {/* A take streamed to the SERVER — resumable from any device. The local
          IndexedDB card wins when both refer to the same take (it also holds the
          last unflushed seconds); this card covers every other scenario. */}
      {phase === 'idle' && serverTakes.filter((t) => t.capture_id !== recovered?.id)
        .slice(0, 1).map((t) => (
        <div key={t.capture_id} className="card"
          style={{ marginTop: 14, borderColor: 'rgba(46,212,166,0.5)' }}>
          <div className="card-eyebrow" style={{ color: 'var(--accent)' }}>
            Recording in progress — saved on the server</div>
          <div style={{ fontSize: 13, color: 'var(--text-2)', margin: '6px 0 10px' }}>
            {mmss(t.elapsed)} across {t.segments} segment{t.segments === 1 ? '' : 's'}
            {t.mode === 'live' ? ' · live meeting' : ''} — safe even if the browser or
            device changed. Listen, continue, or finish it now.
          </div>
          {Array.from({ length: t.segments }, (_, i) => (
            <audio key={i} controls preload="none" style={{ width: '100%', marginBottom: 8, height: 34 }}
              src={voxService.streamAudioUrl(t.capture_id, i)} />
          ))}
          <button className="btn btn-primary btn-sm" style={{ marginBottom: 8 }}
            onClick={() => void start(t)}>Continue recording</button>
          <button className="btn btn-ghost btn-sm" style={{ marginBottom: 8 }} onClick={() => {
            setPhase('uploading');
            voxService.streamFinish({
              captureId: t.capture_id, mode: t.mode,
              durationSeconds: t.elapsed, consentId: t.consent_id || undefined,
              rm: user.full, email: getSession()?.email || '',
            }).then((out) => { setRecording(false); onCaptured(out.conversation_id); })
              .catch((e) => { setErr(String(e?.message || e)); setPhase('idle'); });
          }}>Finish &amp; process now</button>
          <button className="btn btn-ghost btn-sm" onClick={() => {
            if (!window.confirm('Discard this recording from the server? This cannot be undone.')) return;
            void voxService.streamDiscard(t.capture_id).catch(() => {});
            setServerTakes((x) => x.filter((y) => y.capture_id !== t.capture_id));
          }}>Discard</button>
        </div>
      ))}

      <div className="rec-stage">
        <div className={`rec-state-label ${live ? 'live' : 'idle'}`}>{stateLabel}</div>
        <div className={`rec-timer ${live ? 'live' : 'idle'}`}>{mmss(elapsed)}</div>
        <div className="rec-timer-cap">
          {(() => {
            const cap = mmss(mode === 'B' ? CAP_LIVE_SECONDS : CAP_SECONDS);
            return live
              ? `SEGMENT ${segments.length + (phase === 'recording' ? 1 : 0) || 1} · ${cap} MAX`
              : `TAP RECORD TO START · ${cap} MAX`;
          })()}
        </div>
        <div className={`waveform ${live && phase === 'recording' ? 'live' : 'idle'}`}>
          {bars.map((h, i) => (
            <div key={i} className="wave-bar"
              style={{ height: phase === 'recording' ? h : 4 }} />
          ))}
        </div>
        {segments.length > 0 && (
          <div className="rec-segments">
            {segments.map((s, i) => <span key={i} className="chip">seg {i + 1} · {mmss(s)}</span>)}
          </div>
        )}
        <div className="rec-meta" style={{ visibility: live ? 'visible' : 'hidden' }}>
          <span><span className="dot-live">●</span> {phase === 'paused' ? 'Paused' : 'Recording'}</span>
          <span>{gpsRef.current.lat ? `GPS ${gpsRef.current.lat.toFixed(2)}, ${gpsRef.current.lng?.toFixed(2)}` : 'GPS —'}</span>
        </div>

        {(phase === 'idle' || phase === 'error') && (
          <div className="rec-controls-idle" style={{ display: 'flex' }}>
            <button className="rec-btn-record" onClick={() => (phase === 'error' && chunksRef.current.length
              ? void retryUpload() : void start())} aria-label="Start recording">
              <div className="circle"><div className="dot" /></div>
              <div className="lbl">{phase === 'error' && chunksRef.current.length ? 'Send again' : 'Record'}</div>
            </button>
          </div>
        )}
        {live && (
          <div className="rec-controls-live" style={{ display: 'flex' }}>
            <div className="rec-live-row">
              <button className="rec-ctrl-lg" onClick={togglePause}
                aria-label={phase === 'paused' ? 'Resume recording' : 'Pause recording'}>
                <span style={{ fontSize: 24 }}>{phase === 'paused' ? '▶' : '❙❙'}</span>
                <div className="mini-lbl">{phase === 'paused' ? 'Resume' : 'Pause'}</div>
              </button>
            </div>
            <button className="rec-finish-btn" onClick={() => void finish()}>
              <Ic i="i-check" /> Finish &amp; process
            </button>
            <div className="rec-finish-note">Pause and resume as many times as you need. Finish when the conversation is done.</div>
          </div>
        )}
        {phase === 'uploading' && (
          <div className="rec-finish-note" style={{ marginTop: 18 }}>
            Sending… processing continues on the server even if you close this.
          </div>
        )}
      </div>
      </>
      )}
    </div>
  );
}
