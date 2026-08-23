/**
 * The report/review screen (spec screens 6–9): honest processing steps while the
 * pipeline runs server-side, then the confidence-scored review — needs-you strip,
 * use-case chips, the registry-driven blocks, company resolve (never merge
 * silently, with the lead picker when a company runs several), and the gated
 * Approve that only becomes one tap once every flagged field is confirmed.
 */

import { Alert, Box, Button, Chip, CircularProgress, MenuItem, TextField,
  Typography } from '@mui/material';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import vocxClient from '../../../api/vocxClient';
import { api } from '../../../api/http';
import { useAuth } from '../../../auth/AuthContext';
import { needsYou, voxService } from '../../../services/voxService';
import type { VoxCell, VoxConversation, VoxRegistry, VoxReport } from '../../../services/voxService';
import { banner, card, chip, microHeading, pill, pillGhost, pillPrimary, vx } from '../vocxStyles';
import RegistryReport from './RegistryReport';

const STEPS = ['Uploaded', 'Transcribed', 'Writing the report', 'Matching company', 'Ready'];
const STAGE_INDEX: Record<string, number> = {
  uploaded: 1, transcribed: 2, structured: 3, matched: 4, ready: 5 };

function ProcessingSteps({ row }: { row: VoxConversation }) {
  const done = STAGE_INDEX[row.processing_stage || ''] ?? (row.status === 'processing' ? 1 : 0);
  return (
    <Box sx={card}>
      {STEPS.map((s, i) => (
        <Box key={s} sx={{ display: 'flex', alignItems: 'center', gap: 1.4, py: 0.8 }}>
          {i < done ? (
            <Box sx={{ width: 24, height: 24, borderRadius: '50%', bgcolor: '#14322A',
              color: vx.grn2, display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 13 }}>✓</Box>
          ) : i === done ? (
            <CircularProgress size={20} sx={{ color: vx.grn }} />
          ) : (
            <Box sx={{ width: 22, height: 22, borderRadius: '50%', bgcolor: vx.card2,
              border: `1px solid ${vx.line}` }} />
          )}
          <Typography sx={{ fontSize: 14.5, fontWeight: i === done ? 700 : 400,
            color: i <= done ? vx.ink : vx.mut }}>{s}{i === done ? '…' : ''}</Typography>
        </Box>
      ))}
    </Box>
  );
}

interface Candidate { name: string; code?: string; entity_id?: string; score?: number }

export default function VoxReview({ conversationId, onClose, onFiled }: {
  conversationId: string;
  onClose: () => void;
  onFiled?: () => void;
}) {
  const { user } = useAuth();
  const [row, setRow] = useState<VoxConversation | null>(null);
  const [registry, setRegistry] = useState<VoxRegistry | null>(null);
  const [report, setReport] = useState<VoxReport | null>(null);
  const [dirty, setDirty] = useState<Record<string, VoxCell>>({});
  const [confirmed, setConfirmed] = useState<Set<string>>(new Set());
  const [flashPath, setFlashPath] = useState<string | null>(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [resolveQ, setResolveQ] = useState('');
  const [cands, setCands] = useState<Candidate[]>([]);
  const [leads, setLeads] = useState<any[]>([]);
  const [entityName, setEntityName] = useState('');
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await voxService.get(conversationId);
      setRow(r);
      // adopt the server report once (or after an external change) — local edits win
      setReport((prev) => prev ?? (r.structured_report as VoxReport) ?? null);
      return r;
    } catch (e: any) {
      setErr(String(e?.message || e));
      return null;
    }
  }, [conversationId]);

  useEffect(() => { void voxService.spec().then((s) => setRegistry(s.registry)); }, []);
  const startPolling = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      const r = await refresh();
      // keep watching through failures too — a server-side retry (or another
      // device) may advance the row while this panel shows the failed screen
      if (r && ['ready', 'submitted', 'failed_permanently'].includes(r.status)
          && pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    }, 2500);
  }, [refresh]);
  useEffect(() => {
    void refresh();
    startPolling();
    return () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
  }, [refresh, startPolling]);

  // Company resolve: search-then-tap against the same corpus a commit resolves to.
  useEffect(() => {
    const q = resolveQ.trim();
    if (q.length < 2) { setCands([]); return; }
    const t = setTimeout(async () => {
      try {
        const r = await vocxClient.get('/v1/suggest', { params: { q, limit: 6 } });
        const raw = r.data?.matches || r.data?.suggestions || r.data?.candidates || [];
        setCands(raw
          .filter((c: any) => (c.kind || 'client') !== 'lead')  // link the COMPANY; leads pin below
          .map((c: any) => ({
            name: c.name || c.display_name || c.company || String(c),
            code: c.code, entity_id: c.entity_id, score: c.score })));
      } catch { setCands([]); }
    }, 250);
    return () => clearTimeout(t);
  }, [resolveQ]);

  // When the company links and it runs several open leads, the recorder picks WHICH.
  useEffect(() => {
    if (!row?.entity_id) { setLeads([]); return; }
    void api.get<any>('/leads', { entity_id: row.entity_id, limit: 20 })
      .then((d) => setLeads((d.items || []).filter((l: any) => !l.converted_deal_id)))
      .catch(() => setLeads([]));
    void api.get<any>(`/entities/${row.entity_id}`)
      .then((e) => setEntityName(e.display_name || e.legal_name || e.code || ''))
      .catch(() => setEntityName(''));
  }, [row?.entity_id]);

  const onCell = (path: string, cell: VoxCell) => {
    setDirty((d) => ({ ...d, [path]: cell }));
    setReport((r) => {
      if (!r) return r;
      const [blockKey, fieldKey] = path.split('.');
      return { ...r, [blockKey]: { ...(r as any)[blockKey], [fieldKey]: cell } };
    });
  };
  const onConfirm = (path: string) => setConfirmed((c) => new Set(c).add(path));

  const strip = useMemo(() => (registry && report ? needsYou(registry, report) : [])
    .filter((n) => !confirmed.has(n.fieldPath)), [registry, report, confirmed]);

  const readOnly = row?.status === 'submitted' || !!row?.erased_at;

  const saveEdits = async (extra: Record<string, any> = {}) => {
    const edits = Object.entries(dirty).map(([field_path, new_value]) => ({ field_path, new_value }));
    if (!edits.length && !Object.keys(extra).length) return;
    const updated = await voxService.edits(conversationId, { edits, ...extra });
    setDirty({});
    setRow(updated);
    setReport(updated.structured_report as VoxReport);
  };

  const linkEntity = async (c: Candidate) => {
    setBusy(true); setErr('');
    try {
      let entityId = c.entity_id;
      if (!entityId && c.code) {
        // the typeahead speaks group codes; the link needs the register's id
        const found = await api.get<any>('/entities', { code: c.code, limit: 1 });
        entityId = found?.items?.[0]?.id;
      }
      if (!entityId) throw new Error('That candidate has no register id yet — create it as a lead first.');
      await saveEdits({ entity_id: entityId });
      setResolveQ(''); setCands([]);
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  const approve = async () => {
    setBusy(true); setErr('');
    try {
      await saveEdits();
      const approved = await voxService.approve(conversationId);
      setRow(approved);
      // File the timeline interaction through the proven idempotent touchpoint path;
      // best-effort — the conversation is already the durable record.
      if (approved.entity_id) {
        try {
          const kdp = ((report?.common?.key_discussion_points?.value as string[]) || []);
          const tp = await vocxClient.post('/v1/touchpoints', {
            subject_type: 'Entity', subject_id: approved.entity_id,
            interaction_type: 'VOX conversation',
            summary: kdp[0] || 'VOX conversation',
            key_intel: kdp.length ? { points: kdp } : undefined,
            transcript: row?.raw_transcript || undefined,
            performed_by: user.full, capture_id: `vox-conv:${conversationId}`,
          });
          const iid = tp.data?.interaction_id || tp.data?.id;
          if (iid) await voxService.edits(conversationId, { interaction_id: String(iid) });
        } catch { /* the conversation row already holds everything */ }
      }
      onFiled?.();
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  if (!row || !registry) {
    return <Box sx={{ p: 3, textAlign: 'center' }}><CircularProgress sx={{ color: vx.grn }} /></Box>;
  }

  // ------------------------------------------------ processing / failed states
  if (['queued', 'uploading', 'processing'].includes(row.status)) {
    return (
      <Box>
        <ProcessingSteps row={row} />
        <Box sx={banner()}>You can close this — processing continues on the server and
          the note appears in Memory when it is ready.</Box>
        <Button sx={pillGhost} onClick={onClose}>Close</Button>
      </Box>
    );
  }
  if (row.status === 'processing_failed' || row.status === 'failed_permanently') {
    return (
      <Box>
        <Box sx={{ ...card, borderColor: '#4A3D1D' }}>
          <Typography sx={{ ...microHeading, color: vx.amberInk }}>
            {row.status === 'failed_permanently' ? 'Permanently failed — an admin was alerted'
              : `Processing failed — retry ${row.retry_count || 0} of 5`}
          </Typography>
          <Typography sx={{ fontSize: 13.5, color: vx.ink }}>{row.processing_error}</Typography>
          <Typography sx={{ fontSize: 12.5, color: vx.mut, mt: 1 }}>
            The recording is never lost — audio and any transcript are stored; Retry reuses them.
          </Typography>
        </Box>
        {row.status === 'processing_failed' && (
          <Button sx={pillPrimary} disabled={busy} onClick={async () => {
            setBusy(true);
            try { await voxService.process(conversationId); await refresh(); startPolling(); } finally { setBusy(false); }
          }}>Retry now</Button>
        )}
        <Button sx={{ ...pillGhost, ml: 1 }} onClick={onClose}>Close</Button>
      </Box>
    );
  }

  // ---------------------------------------------------------------- the review
  const detected = report?.detected_use_cases || [];
  const allUseCases = registry.use_cases;
  const toggleUseCase = (uc: string) => {
    if (readOnly) return;
    const next = detected.includes(uc) ? detected.filter((u) => u !== uc) : [...detected, uc];
    if (!next.length) return; // a conversation carries at least one use case
    void voxService.edits(conversationId, { use_cases: next }).then((u) => {
      setRow(u); setReport(u.structured_report as VoxReport);
    }).catch((e) => setErr(String(e?.message || e)));
  };

  return (
    <Box>
      {err && <Alert severity="warning" onClose={() => setErr('')} sx={{ mb: 1 }}>{err}</Alert>}

      {/* identity: the company is the document's identity */}
      <Typography sx={{ fontSize: 20, fontWeight: 700, mb: 0.5 }}>
        {entityName || (row.entity_candidates?.[0] ?? 'Unlinked conversation')}
        {readOnly && <Chip size="small" label={row.erased_at ? 'ERASED' : 'FILED'}
          sx={{ ml: 1, bgcolor: '#14322A', color: vx.grn2, fontWeight: 700 }} />}
      </Typography>
      <Box sx={{ mb: 1.2 }}>
        {allUseCases.map((uc) => (
          <Chip key={uc} label={registry.blocks[uc]?.label || uc}
            sx={{ ...chip(detected.includes(uc)), mr: 0.7, mb: 0.7, fontSize: 12.5, px: 1.2 }}
            onClick={() => toggleUseCase(uc)} />
        ))}
      </Box>

      {/* the needs-you strip — only the flagged fields, each row jumps to its field */}
      {!readOnly && strip.length > 0 && (
        <Box sx={{ ...card, borderColor: '#4A3D1D', bgcolor: '#241F10' }}>
          <Typography sx={{ fontSize: 13.5, fontWeight: 700, color: vx.amberInk, mb: 0.7 }}>
            ⚠ {strip.length} field{strip.length > 1 ? 's' : ''} need you — tap to jump
          </Typography>
          {strip.map((n) => (
            <Box key={n.fieldPath} onClick={() => {
              setFlashPath(n.fieldPath);
              document.getElementById(`vox-${n.fieldPath}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }}
              sx={{ display: 'flex', justifyContent: 'space-between', py: 0.5, cursor: 'pointer',
                borderTop: '1px solid rgba(74,61,29,.5)', fontSize: 13.5 }}>
              <span>{n.label}</span>
              <span style={{ color: vx.amberInk }}>{n.confidence}</span>
            </Box>
          ))}
        </Box>
      )}

      {/* company resolve — never merge silently */}
      {!readOnly && !row.entity_id && (
        <Box sx={card}>
          <Typography sx={microHeading}>Link the company</Typography>
          {(row.entity_candidates || []).length > 0 && (
            <Typography sx={{ fontSize: 13, color: vx.mut, mb: 0.8 }}>
              As heard: {(row.entity_candidates || []).map((c) => `“${c}”`).join(' · ')}
            </Typography>
          )}
          <TextField fullWidth size="small" placeholder="Search the register…"
            value={resolveQ} onChange={(e) => setResolveQ(e.target.value)}
            sx={{ '& .MuiInputBase-root': { bgcolor: vx.card2, color: vx.ink, borderRadius: '10px' },
              '& fieldset': { borderColor: vx.line } }} />
          {cands.map((c) => (
            <Box key={`${c.name}-${c.entity_id}`} onClick={() => !busy && linkEntity(c)}
              sx={{ border: `1px solid ${vx.line}`, borderRadius: '10px', p: 1.1, mt: 0.8,
                cursor: 'pointer', '&:hover': { borderColor: vx.grn } }}>
              <b>{c.name}</b> {c.code && <span style={{ color: vx.mut }}>· {c.code}</span>}
            </Box>
          ))}
          <Typography sx={{ fontSize: 12.5, color: vx.mut, mt: 1 }}>
            Not there? Leave it — the conversation sits in the Queue until someone links or
            creates the lead. Nothing merges silently.
          </Typography>
        </Box>
      )}

      {/* the lead picker — a company can run several; silently guessing is forbidden */}
      {!readOnly && row.entity_id && leads.length > 1 && (
        <Box sx={card}>
          <Typography sx={microHeading}>Which lead is this about?</Typography>
          <TextField select fullWidth size="small" value={row.lead_id ?? ''}
            onChange={(e) => void voxService.edits(conversationId,
              { lead_id: e.target.value }).then(setRow).catch((er) => setErr(String(er)))}
            sx={{ '& .MuiInputBase-root': { bgcolor: vx.card2, color: vx.ink, borderRadius: '10px' },
              '& fieldset': { borderColor: vx.line } }}>
            <MenuItem value="">Company level — no specific lead</MenuItem>
            {leads.map((l) => (
              <MenuItem key={l.id} value={l.id}>
                {l.lead_no || 'lead'} · {l.temperature || '—'} · RM {l.rm || '—'}
              </MenuItem>
            ))}
          </TextField>
        </Box>
      )}

      {report && (
        <RegistryReport registry={registry} report={report} readOnly={readOnly}
          confirmed={confirmed} flashPath={flashPath} onCell={onCell} onConfirm={onConfirm} />
      )}

      {!readOnly && (
        <Box sx={{ display: 'flex', gap: 1, mt: 1.5 }}>
          <Button sx={{ ...pillPrimary, flex: 1 }} disabled={busy || strip.length > 0}
            onClick={approve}>
            {strip.length ? `Approve · ${strip.length} unchecked` : 'Approve'}
          </Button>
          <Button sx={pillGhost} disabled={busy} onClick={async () => {
            try { await saveEdits(); onClose(); } catch (e: any) { setErr(String(e?.message || e)); }
          }}>Save & close</Button>
        </Box>
      )}
      {readOnly && <Button sx={{ ...pillGhost, mt: 1.5 }} onClick={onClose}>Close</Button>}
    </Box>
  );
}
