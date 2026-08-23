/**
 * Memory and Queue (spec screens 2, 3, 11): the firm's conversations, everyone's
 * by default — the Mine/All toggle and the use-case chips are filters, never a
 * privacy tier. The Queue holds exactly what needs a human, and each row's verb
 * matches its destination: a failed processing job is not a linking problem.
 */

import { Box, Chip, CircularProgress, TextField, Typography } from '@mui/material';
import { useEffect, useMemo, useState } from 'react';
import { voxService } from '../../../services/voxService';
import type { VoxConversation, VoxRegistry } from '../../../services/voxService';
import { banner, card, chip, microHeading, vx } from '../vocxStyles';

const UC_SHORT: Record<string, string> = {
  lending: 'Lending', syndication: 'Syndication', asset_monetisation: 'Asset mon',
  credit_diligence: 'Credit', investor_relations: 'IR', banking_relations: 'Banking',
  operations: 'Ops',
};

function Row({ c, onOpen }: { c: VoxConversation; onOpen: (id: string) => void }) {
  const when = (c.created_at || '').replace('T', ' ').slice(0, 16);
  return (
    <Box onClick={() => onOpen(c.id)}
      sx={{ display: 'flex', gap: 1.2, py: 1.1, px: 0.4, cursor: 'pointer',
        borderBottom: `1px solid ${vx.line}`, '&:hover': { bgcolor: 'rgba(255,255,255,.02)' },
        '&:last-child': { borderBottom: 'none' } }}>
      <Box sx={{ width: 38, height: 38, borderRadius: '12px', bgcolor: '#173A2C',
        color: vx.grn2, display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontWeight: 700, fontSize: 12.5, flex: 'none' }}>
        {(c.recorder_name || c.recorder_email).slice(0, 2).toUpperCase()}
      </Box>
      <Box sx={{ minWidth: 0, flex: 1 }}>
        <Typography sx={{ fontSize: 14.5, fontWeight: 600, whiteSpace: 'nowrap',
          overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {(c.entity_candidates && c.entity_candidates[0]) || c.sector || 'Conversation'}
          {(c.use_cases || []).map((uc) => (
            <Box key={uc} component="span" sx={{ fontSize: 10, letterSpacing: '.08em',
              textTransform: 'uppercase', color: vx.teal, border: '1px solid #1D4A42',
              borderRadius: '6px', px: 0.7, py: 0.1, ml: 0.7, verticalAlign: '2px' }}>
              {UC_SHORT[uc] || uc}
            </Box>
          ))}
        </Typography>
        <Typography sx={{ fontSize: 12.5, color: vx.mut }}>
          {when} · {c.recorder_name || c.recorder_email} · {c.recording_mode === 'live' ? 'live' : 'note'}
          {c.status !== 'submitted' && ` · ${c.status.replace('_', ' ')}`}
        </Typography>
      </Box>
    </Box>
  );
}

export function VoxMemoryTab({ onOpen }: { onOpen: (id: string) => void }) {
  const [items, setItems] = useState<VoxConversation[] | null>(null);
  const [mine, setMine] = useState(false);
  const [uc, setUc] = useState<string>('');
  const [q, setQ] = useState('');
  const [registry, setRegistry] = useState<VoxRegistry | null>(null);

  useEffect(() => { void voxService.spec().then((s) => setRegistry(s.registry)); }, []);
  useEffect(() => {
    const t = setTimeout(() => {
      void voxService.list({ mine, use_case: uc || undefined, q: q.trim() || undefined,
        status: 'ready,submitted,processing,processing_failed,failed_permanently', limit: 50 })
        .then((r) => setItems(r.items)).catch(() => setItems([]));
    }, q ? 300 : 0);
    return () => clearTimeout(t);
  }, [mine, uc, q]);

  return (
    <Box>
      <TextField fullWidth size="small" placeholder="Search every Evam conversation…"
        value={q} onChange={(e) => setQ(e.target.value)}
        sx={{ mb: 1.2, '& .MuiInputBase-root': { bgcolor: vx.card2, color: vx.ink,
          borderRadius: '12px' }, '& fieldset': { borderColor: vx.line } }} />
      <Box sx={{ mb: 1 }}>
        <Chip label="All" sx={{ ...chip(!mine), mr: 0.7, fontSize: 12.5, px: 1 }}
          onClick={() => setMine(false)} />
        <Chip label="Mine" sx={{ ...chip(mine), mr: 0.7, fontSize: 12.5, px: 1 }}
          onClick={() => setMine(true)} />
        {(registry?.use_cases || []).map((u) => (
          <Chip key={u} label={UC_SHORT[u] || u}
            sx={{ ...chip(uc === u), mr: 0.7, mb: 0.7, fontSize: 12.5, px: 1 }}
            onClick={() => setUc(uc === u ? '' : u)} />
        ))}
      </Box>
      {items === null ? (
        <Box sx={{ textAlign: 'center', py: 4 }}><CircularProgress sx={{ color: vx.grn }} /></Box>
      ) : items.length === 0 ? (
        <Box sx={banner()}>Nothing yet{q ? ' for that search — an empty result is a real answer.'
          : ' — the firm remembers what gets recorded.'}</Box>
      ) : (
        <Box sx={{ ...card, py: 0.5 }}>
          {items.map((c) => <Row key={c.id} c={c} onOpen={onOpen} />)}
        </Box>
      )}
    </Box>
  );
}

export function VoxQueueTab({ onOpen }: { onOpen: (id: string) => void }) {
  const [items, setItems] = useState<VoxConversation[] | null>(null);
  const [busyId, setBusyId] = useState('');

  const load = () => void voxService
    .list({ status: 'ready,processing_failed,failed_permanently', limit: 100 })
    .then((r) => setItems(r.items.filter((c) =>
      c.status !== 'ready' || !c.entity_id)))   // ready+linked has left the queue
    .catch(() => setItems([]));
  useEffect(load, []);

  if (items === null) {
    return <Box sx={{ textAlign: 'center', py: 4 }}><CircularProgress sx={{ color: vx.grn }} /></Box>;
  }
  return (
    <Box>
      {items.length === 0 && <Box sx={banner()}>Queue clear — nothing needs a human.</Box>}
      {items.length > 0 && (
        <Box sx={{ ...card, py: 0.5 }}>
          {items.map((c) => {
            const failed = c.status.includes('failed');
            const verb = failed
              ? (c.status === 'failed_permanently' ? 'Open ›' : 'Retry & open ›')
              : 'Link the company ›';
            return (
              <Box key={c.id} sx={{ display: 'flex', alignItems: 'center', gap: 1.2, py: 1.1,
                borderBottom: `1px solid ${vx.line}`, '&:last-child': { borderBottom: 'none' } }}>
                <Box sx={{ width: 34, height: 34, borderRadius: '10px', flex: 'none',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  bgcolor: failed ? '#2A2414' : '#173A2C',
                  color: failed ? vx.amberInk : vx.grn2 }}>{failed ? '↻' : '?'}</Box>
                <Box sx={{ minWidth: 0, flex: 1 }}>
                  <Typography sx={{ fontSize: 14, fontWeight: 600 }}>
                    {(c.entity_candidates && c.entity_candidates[0]) || 'Conversation'}
                  </Typography>
                  <Typography sx={{ fontSize: 12.5, color: vx.mut }}>
                    {failed ? `${c.status === 'failed_permanently' ? 'Permanently failed'
                      : `Processing failed · retry ${c.retry_count || 0} of 5`}`
                      : 'Awaiting a company link'} · {c.recorder_name || c.recorder_email}
                  </Typography>
                </Box>
                <Typography onClick={async () => {
                  if (c.status === 'processing_failed') {
                    setBusyId(c.id);
                    try { await voxService.process(c.id); } catch { /* surfaces on open */ }
                    setBusyId('');
                  }
                  onOpen(c.id);
                }}
                  sx={{ color: vx.grn, fontWeight: 700, fontSize: 13, cursor: 'pointer',
                    whiteSpace: 'nowrap', opacity: busyId === c.id ? 0.5 : 1 }}>
                  {verb}
                </Typography>
              </Box>
            );
          })}
        </Box>
      )}
      <Typography sx={{ ...microHeading, mt: 1.5 }}>A workflow view, not a privacy tier</Typography>
      <Typography sx={{ fontSize: 12.5, color: vx.mut }}>
        Everyone sees this queue and anyone can clear it.
      </Typography>
    </Box>
  );
}
