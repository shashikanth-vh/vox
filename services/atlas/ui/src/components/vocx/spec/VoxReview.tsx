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
import { referenceService } from '../../../services/referenceService';
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
  /** The pencil next to the title reopens the company link even when already linked. */
  const [relinking, setRelinking] = useState(false);
  /** Create-as-new-lead (the blueprint's Atlas-resolve escape hatch). */
  const [creating, setCreating] = useState(false);
  const [newLeadName, setNewLeadName] = useState('');
  const [newLeadRm, setNewLeadRm] = useState('');
  const [leadName, setLeadName] = useState('');
  const [showTranscript, setShowTranscript] = useState(false);
  const [approved, setApproved] = useState(false);
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

  // A lead-only conversation (created as a new lead) titles itself from the lead.
  useEffect(() => {
    if (!row?.lead_id) { setLeadName(''); return; }
    void api.get<any>(`/leads/${row.lead_id}`)
      .then((l) => setLeadName(l.company || '')).catch(() => setLeadName(''));
  }, [row?.lead_id]);

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
      const changingCompany = !!row?.entity_id && row.entity_id !== entityId;
      await saveEdits({ entity_id: entityId, ...(changingCompany ? { lead_id: '' } : {}) });
      setResolveQ(''); setCands([]); setRelinking(false);
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  const createLead = async () => {
    const name = newLeadName.trim();
    if (!name) { setErr('The new lead needs the company name.'); return; }
    setBusy(true); setErr('');
    try {
      const lead = await api.post<any>('/leads', {
        company: name,
        rm: newLeadRm || null,
        sector: (report?.common?.sector as any)?.value || null,
        source_name: 'VOX conversation',
      });
      await saveEdits({ lead_id: String(lead.id) });
      setCreating(false); setRelinking(false); setResolveQ(''); setCands([]);
      setLeadName(name);
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  const approve = async () => {
    if (strip.length > 0 && !window.confirm(
      `${strip.length} flagged field${strip.length > 1 ? 's are' : ' is'} still unreviewed — approve anyway?`)) {
      return;
    }
    setBusy(true); setErr('');
    try {
      await saveEdits();
      const approvedRow = await voxService.approve(conversationId);
      setRow(approvedRow);
      // File the timeline interaction through the proven idempotent touchpoint path;
      // best-effort — the conversation is already the durable record. A company that
      // exists files against the ENTITY; a brand-new one files against its LEAD, so
      // the conversation shows on the lead's timeline from day one.
      const subject = approvedRow.entity_id
        ? { subject_type: 'Entity', subject_id: approvedRow.entity_id }
        : approvedRow.lead_id
          ? { subject_type: 'Lead', subject_id: approvedRow.lead_id }
          : null;
      if (subject) {
        try {
          const kdp = ((report?.common?.key_discussion_points?.value as string[]) || []);
          const tp = await vocxClient.post('/v1/touchpoints', {
            ...subject,
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
      setApproved(true);
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

  // -------------------------------------------------------- approved: one beat
  if (approved) {
    const followUp = (report?.common?.follow_up_date as any)?.value;
    return (
      <Box sx={{ textAlign: 'center', pt: 3 }}>
        <Typography sx={{ fontSize: 46, mb: 1 }}>✅</Typography>
        <Typography sx={{ fontSize: 20, fontWeight: 700, mb: 0.5 }}>The firm now knows.</Typography>
        <Typography sx={{ fontSize: 13.5, color: vx.mut, mb: 2 }}>
          Filed to <b style={{ color: vx.ink }}>{entityName || leadName || 'the register'}</b> —
          searchable by anyone, on the company timeline.
        </Typography>
        {followUp && (
          <Box sx={{ ...card, textAlign: 'left' }}>
            <Typography sx={microHeading}>Follow-up detected</Typography>
            <Typography sx={{ fontSize: 14 }}>{followUp}</Typography>
            <Typography sx={{ fontSize: 12, color: vx.mut, mt: 0.5 }}>
              Calendar linking rides the Google-connect round — nothing is written silently.
            </Typography>
          </Box>
        )}
        <Button sx={{ ...pillGhost, mt: 1 }} onClick={onClose}>Back to Memory</Button>
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

      {/* identity, per the blueprint: status pill above, a calmer title with the
          link pencil beside it, then the sector/meta line */}
      <Box sx={{ mb: 0.4 }}>
        <Box component="span" sx={{ display: 'inline-block', px: 1.2, py: 0.3,
          borderRadius: '999px', fontSize: 10.5, letterSpacing: '.12em', fontWeight: 700,
          border: `1px solid ${readOnly ? '#1D4A35' : '#4A3D1D'}`,
          color: readOnly ? vx.grn2 : vx.amberInk }}>
          {row.erased_at ? 'ERASED' : readOnly ? 'FILED' : 'READY FOR REVIEW'}
        </Box>
      </Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mb: 0.3 }}>
        <Typography sx={{ fontSize: 20, fontWeight: 700, minWidth: 0,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {entityName || (row.entity_candidates?.[0] ?? 'Unlinked conversation')}
        </Typography>
        {!readOnly && (
          <Box component="span" role="button" aria-label="Link the company"
            onClick={() => setRelinking((v) => !v)}
            sx={{ cursor: 'pointer', color: relinking ? vx.grn : vx.mut, fontSize: 15,
              lineHeight: 1, '&:hover': { color: vx.grn2 } }}
            title={row.entity_id ? 'Change the linked company' : 'Link the company'}>✎</Box>
        )}
      </Box>
      <Typography sx={{ fontSize: 12.5, color: vx.mut, mb: 1 }}>
        {[row.sector, row.subsector,
          (report?.common?.location as any)?.value].filter(Boolean).join(' · ') || 'No sector determined'}
        {'  ·  '}
        {row.duration_seconds ? `${Math.floor((row.duration_seconds || 0) / 60)}:${String((row.duration_seconds || 0) % 60).padStart(2, '0')}` : ''}
        {row.language_detected ? ` · ${row.language_detected.toUpperCase()}` : ''}
      </Typography>
      <Box sx={{ mb: 1.2 }}>
        {allUseCases.map((uc) => (
          <Chip key={uc} label={registry.blocks[uc]?.label || uc}
            sx={{ ...chip(detected.includes(uc)), mr: 0.7, mb: 0.7, fontSize: 12.5, px: 1.2 }}
            onClick={() => toggleUseCase(uc)} />
        ))}
      </Box>

      {/* the needs-you strip, blueprint layout: count badge, the VALUE in each row,
          a severity dot, the block tag. Optional, never a gate — approving with
          unreviewed fields asks once and proceeds. */}
      {!readOnly && strip.length > 0 && (
        <Box sx={{ ...card, borderColor: '#4A3D1D', bgcolor: '#241F10' }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2, mb: 0.5 }}>
            <Box sx={{ width: 26, height: 26, borderRadius: '50%', bgcolor: vx.amber,
              color: '#1A1503', fontWeight: 800, fontSize: 13.5, display: 'flex',
              alignItems: 'center', justifyContent: 'center', flex: 'none' }}>{strip.length}</Box>
            <Box>
              <Typography sx={{ fontSize: 13.5, fontWeight: 700, color: vx.amberInk, lineHeight: 1.2 }}>
                {strip.length === 1 ? 'field' : 'fields'} to confirm
              </Typography>
              <Typography sx={{ fontSize: 11.5, color: vx.mut }}>
                Low and medium confidence · tap to fix · optional
              </Typography>
            </Box>
          </Box>
          {strip.map((n) => (
            <Box key={n.fieldPath} onClick={() => {
              setFlashPath(n.fieldPath);
              document.getElementById(`vox-${n.fieldPath}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }}
              sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.6, cursor: 'pointer',
                borderTop: '1px solid rgba(74,61,29,.5)', fontSize: 13.5 }}>
              <Box component="span" sx={{ width: 8, height: 8, borderRadius: '50%', flex: 'none',
                bgcolor: n.confidence === 'low' ? '#E15B64' : vx.amber }} />
              <Box sx={{ minWidth: 0, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis',
                whiteSpace: 'nowrap' }}>
                {n.label} — {n.valueShort}
              </Box>
              <Box component="span" sx={{ fontSize: 10, letterSpacing: '.1em',
                textTransform: 'uppercase', color: vx.mut, flex: 'none' }}>{n.blockLabel}</Box>
              <Box component="span" sx={{ color: vx.mut, flex: 'none' }}>›</Box>
            </Box>
          ))}
        </Box>
      )}

      {/* company resolve — never merge silently; the title's pencil reopens it */}
      {!readOnly && (!row.entity_id || relinking) && (
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
          {!creating ? (
            <Button sx={{ ...pill, width: '100%', mt: 1.2 }} onClick={() => {
              setCreating(true);
              setNewLeadName(resolveQ.trim() || row.entity_candidates?.[0] || '');
              setNewLeadRm(user.full);
            }}>＋ Create “{(resolveQ.trim() || row.entity_candidates?.[0] || 'this company')}” as a new lead</Button>
          ) : (
            <Box sx={{ mt: 1.2, p: 1.2, border: `1px dashed ${vx.line}`, borderRadius: '12px' }}>
              <Typography sx={{ ...microHeading }}>New lead — set the RM</Typography>
              <TextField fullWidth size="small" placeholder="Company name" value={newLeadName}
                onChange={(e) => setNewLeadName(e.target.value)}
                sx={{ mb: 1, '& .MuiInputBase-root': { bgcolor: vx.card2, color: vx.ink,
                  borderRadius: '10px' }, '& fieldset': { borderColor: vx.line } }} />
              <TextField select fullWidth size="small" value={newLeadRm}
                onChange={(e) => setNewLeadRm(e.target.value)}
                sx={{ mb: 1.2, '& .MuiInputBase-root': { bgcolor: vx.card2, color: vx.ink,
                  borderRadius: '10px' }, '& fieldset': { borderColor: vx.line },
                  '& .MuiSelect-icon': { color: vx.mut } }}>
                <MenuItem value="">RM — unassigned</MenuItem>
                {referenceService.getRefSync('RM').map((o) => (
                  <MenuItem key={o} value={o}>
                    {referenceService.getRefLabels('RM')?.[o] || o}
                  </MenuItem>
                ))}
                {user.full && !referenceService.getRefSync('RM').includes(user.full) && (
                  <MenuItem value={user.full}>{user.full}</MenuItem>)}
              </TextField>
              <Box sx={{ display: 'flex', gap: 1 }}>
                <Button sx={pillPrimary} disabled={busy} onClick={() => void createLead()}>
                  Create lead & link
                </Button>
                <Button sx={pillGhost} onClick={() => setCreating(false)}>Cancel</Button>
              </Box>
            </Box>
          )}
          <Typography sx={{ fontSize: 12.5, color: vx.mut, mt: 1 }}>
            Or leave it — the conversation sits in the Queue until someone links or
            creates the lead. Nothing merges silently.
          </Typography>
        </Box>
      )}

      {/* a lead-only conversation (new company): its lead is the identity; the RM
          set at creation is editable here for anyone with assignment authority */}
      {!readOnly && !row.entity_id && row.lead_id && (
        <Box sx={card}>
          <Typography sx={microHeading}>New lead — {leadName || 'created'}</Typography>
          <Typography sx={{ fontSize: 12.5, color: vx.mut }}>
            Created in the Leads register and linked to this conversation. It appears in
            the Leads grid now; the RM can be changed there any time.
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
          {row.lead_id && (user.roles.includes('Management') || user.roles.includes('Admin')) && (
            <TextField select fullWidth size="small" sx={{ mt: 1,
              '& .MuiInputBase-root': { bgcolor: vx.card2, color: vx.ink, borderRadius: '10px' },
              '& fieldset': { borderColor: vx.line }, '& .MuiSelect-icon': { color: vx.mut } }}
              value={leads.find((l) => l.id === row.lead_id)?.rm ?? ''}
              onChange={(e) => {
                void api.patch(`/leads/${row.lead_id}`, { rm: e.target.value || null })
                  .then(() => setLeads((ls) => ls.map((l) =>
                    (l.id === row.lead_id ? { ...l, rm: e.target.value } : l))))
                  .catch((er) => setErr(String(er?.message || er)));
              }}
              helperText="RM on this lead — assignment authority only"
              FormHelperTextProps={{ sx: { color: vx.mut } }}>
              <MenuItem value="">RM — unassigned</MenuItem>
              {referenceService.getRefSync('RM').map((o) => (
                <MenuItem key={o} value={o}>
                  {referenceService.getRefLabels('RM')?.[o] || o}
                </MenuItem>
              ))}
            </TextField>
          )}
        </Box>
      )}

      {/* Summary, assembled from the key points — display-only, honest about it */}
      {report && ((report.common?.key_discussion_points as any)?.value?.length > 0) && (
        <Box sx={card}>
          <Typography sx={microHeading}>Summary</Typography>
          <Typography sx={{ fontSize: 14, lineHeight: 1.6 }}>
            {((report.common?.key_discussion_points as any).value as string[]).slice(0, 3).join('. ')}.
          </Typography>
        </Box>
      )}

      {report && (
        <RegistryReport registry={registry} report={report} readOnly={readOnly}
          confirmed={confirmed} flashPath={flashPath} onCell={onCell} onConfirm={onConfirm} />
      )}

      {/* the verbatim transcript — evidence, shown on request, never editable */}
      {row.raw_transcript && (
        <Box sx={{ ...card, py: 1.2 }}>
          <Typography onClick={() => setShowTranscript((v) => !v)}
            sx={{ ...microHeading, mb: showTranscript ? 1 : 0, cursor: 'pointer' }}>
            {showTranscript ? 'Hide original transcript ▾' : 'Show original transcript ▸'}
          </Typography>
          {showTranscript && (
            <Typography sx={{ fontSize: 13, color: vx.mut, whiteSpace: 'pre-wrap' }}>
              {row.raw_transcript}
            </Typography>
          )}
        </Box>
      )}

      {!readOnly && (
        <Box sx={{ display: 'flex', gap: 1, mt: 1.5 }}>
          <Button disabled={busy} onClick={approve}
            sx={strip.length
              ? { ...pill, flex: 1, borderColor: '#4A3D1D', color: vx.amberInk,
                  bgcolor: '#241F10', '&:hover': { bgcolor: '#2C2614', borderColor: vx.amber } }
              : { ...pillPrimary, flex: 1 }}>
            {strip.length ? `Approve · ${strip.length} unreviewed` : 'Approve'}
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
