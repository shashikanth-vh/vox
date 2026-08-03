import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Chip, CircularProgress, Typography } from '@mui/material';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import { vocxService, type VocxPreview, type VocxReportRow } from '../../services/vocxService';
import { currentRm } from './rm';
import ReportCard from './ReportCard';
import { tokens } from '../../theme';

/**
 * Past captures — and the queue of ones still waiting to be filed.
 *
 * A capture that was recorded but never approved is the easiest thing in the product to
 * lose: the preview writes nothing, so an RM who recorded a note on the way out of a
 * meeting and then closed the tab has intel that exists and is invisible. VocX keeps
 * those as drafts; this lists them, unapproved first, and opens each one straight into
 * the same approve-and-file card the record flow uses.
 */

const TONE: Record<string, { bg: string; fg: string }> = {
  committed: { bg: 'rgba(45,214,163,.15)', fg: tokens.tealHi },
  ready: { bg: 'rgba(240,180,60,.18)', fg: '#F0B43C' },
  draft: { bg: 'rgba(232,238,242,.12)', fg: 'rgba(232,238,242,.75)' },
};

const isPending = (r: VocxReportRow) => String(r.status || '').toLowerCase() !== 'committed';

function when(iso?: string): string {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  const mins = Math.floor((Date.now() - t) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function ReportsTab({ epoch }: { epoch: number }) {
  const rm = currentRm();
  const [rows, setRows] = useState<VocxReportRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [open, setOpen] = useState<VocxPreview | null>(null);
  const [opening, setOpening] = useState('');
  const [flash, setFlash] = useState('');

  const load = useCallback(async () => {
    if (!rm) { setLoading(false); return; }
    setLoading(true);
    const r = await vocxService.reports(rm);
    setLoading(false);
    if (!r.ok) { setErr(r.error); setRows([]); return; }
    setErr('');
    // Unapproved first — this list is a to-do before it is a history.
    setRows([...r.data].sort((a, b) => Number(isPending(b)) - Number(isPending(a))));
  }, [rm]);

  useEffect(() => { void load(); }, [load, epoch]);

  const openReport = async (row: VocxReportRow) => {
    setErr(''); setOpening(row.capture_id);
    const r = await vocxService.report(rm, row.capture_id);
    setOpening('');
    if (!r.ok) { setErr(r.error); return; }
    setOpen(r.data);
  };

  if (open) {
    return (
      <Box>
        <Button size="small" startIcon={<ArrowBackIcon sx={{ fontSize: 16 }} />}
          onClick={() => setOpen(null)}
          sx={{ textTransform: 'none', m: 1, color: 'rgba(232,238,242,.7)' }}>
          All reports
        </Button>
        <ReportCard
          preview={open.report || open}
          initialStatus={open.status}
          onFiled={(msg) => { setOpen(null); setFlash(msg); void load(); }}
          onDiscarded={() => { setOpen(null); void load(); }}
        />
      </Box>
    );
  }

  const pending = rows.filter(isPending).length;

  return (
    <Box sx={{ p: 1.2 }}>
      {flash && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }}
        onClose={() => setFlash('')}>{flash}</Alert>}
      {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }}
        onClose={() => setErr('')}>{err}</Alert>}

      <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.8 }}>
        <Typography sx={{ fontSize: 11.5, color: 'rgba(232,238,242,.6)', flex: 1 }}>
          {pending > 0
            ? `${pending} waiting to be approved`
            : rows.length ? 'All captures filed' : ''}
        </Typography>
        <Button size="small" onClick={() => void load()} disabled={loading}
          sx={{ textTransform: 'none', fontSize: 11.5, color: tokens.tealHi }}>Refresh</Button>
      </Box>

      {loading && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
          <CircularProgress size={22} sx={{ color: tokens.tealHi }} />
        </Box>
      )}

      {!loading && !rows.length && !err && (
        <Typography sx={{ fontSize: 12.5, color: 'rgba(232,238,242,.55)', py: 3, textAlign: 'center' }}>
          Nothing captured yet. Record a note on the Record tab and it will appear here.
        </Typography>
      )}

      {rows.map((r) => {
        const status = String(r.status || 'draft').toLowerCase();
        const tone = TONE[status] || TONE.draft;
        return (
          <Box
            key={r.capture_id}
            role="button"
            tabIndex={0}
            onClick={() => void openReport(r)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void openReport(r); } }}
            sx={{
              display: 'flex', gap: 1, alignItems: 'flex-start', p: 1, mb: 0.6,
              borderRadius: '8px', cursor: 'pointer',
              border: `1px solid ${isPending(r) ? 'rgba(240,180,60,.35)' : tokens.line}`,
              bgcolor: 'rgba(255,255,255,.03)',
              '&:hover': { bgcolor: 'rgba(255,255,255,.07)' },
              '&:focus-visible': { outline: `2px solid ${tokens.tealHi}`, outlineOffset: 2 },
            }}
          >
            <Chip size="small" label={opening === r.capture_id ? '…' : status}
              sx={{ height: 19, fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                bgcolor: tone.bg, color: tone.fg, flexShrink: 0 }} />
            <Box sx={{ minWidth: 0, flex: 1 }}>
              <Typography sx={{ fontSize: 12.5, fontWeight: 700, color: '#E8EEF2',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.company || r.summary || r.capture_id}
              </Typography>
              {r.summary && r.company && (
                <Typography sx={{ fontSize: 11.5, color: 'rgba(232,238,242,.6)',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.summary}
                </Typography>
              )}
              <Typography sx={{ fontSize: 10.5, color: 'rgba(232,238,242,.42)' }}>
                {[r.entity_code, when(r.updated_at)].filter(Boolean).join(' · ')}
              </Typography>
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}
