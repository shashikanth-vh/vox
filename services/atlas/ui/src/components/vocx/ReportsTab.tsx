import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, Chip, CircularProgress, IconButton, InputBase, Typography } from '@mui/material';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import SearchIcon from '@mui/icons-material/Search';
import CloseIcon from '@mui/icons-material/Close';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import { vocxService, type VocxPreview, type VocxReportRow } from '../../services/vocxService';
import { currentRm } from './rm';
import ReportCard from './ReportCard';
import { vx } from './vocxStyles';

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
  committed: { bg: 'rgba(45,214,163,.15)', fg: vx.grn },
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

export default function ReportsTab({ epoch, active = true }:
                                   { epoch: number; active?: boolean }) {
  const rm = currentRm();
  const [rows, setRows] = useState<VocxReportRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [open, setOpen] = useState<VocxPreview | null>(null);
  const [opening, setOpening] = useState('');
  const [flash, setFlash] = useState('');
  const [q, setQ] = useState('');
  const [searching, setSearching] = useState(false);
  // Deleting a draft is two clicks on the same trash icon — an inline arm-then-confirm,
  // because a browser confirm() looks alien inside the panel and a full dialog is heavy
  // for a draft that was never filed. The armed state disarms itself after a moment.
  const [armed, setArmed] = useState('');
  const [deleting, setDeleting] = useState('');
  const disarm = useRef<ReturnType<typeof setTimeout> | null>(null);

  // No `if (!rm) return` guard. Whose reports these are is decided by VocX from the
  // verified identity the edge forwards, not by this value — so skipping the call when
  // the session happens to carry no short name showed an empty list for captures that
  // were on the server all along. `rm` rides along for older builds and is ignored.
  const load = useCallback(async () => {
    setLoading(true);
    const r = await vocxService.reports(rm);
    setLoading(false);
    if (!r.ok) { setErr(r.error); setRows([]); return; }
    setErr('');
    // Unapproved first — this list is a to-do before it is a history.
    setRows([...r.data].sort((a, b) => Number(isPending(b)) - Number(isPending(a))));
  }, [rm]);

  // Both tabs stay mounted (see VocxPanel) so a recording survives a tab switch — so
  // the list fetches when it is actually on screen, not every time the panel opens.
  useEffect(() => { if (active) void load(); }, [load, epoch, active]);

  const openReport = async (row: VocxReportRow) => {
    setErr(''); setOpening(row.capture_id);
    const r = await vocxService.report(rm, row.capture_id);
    setOpening('');
    if (!r.ok) { setErr(r.error); return; }
    setOpen(r.data);
  };

  const removeDraft = async (row: VocxReportRow) => {
    if (armed !== row.capture_id) {
      setArmed(row.capture_id);
      if (disarm.current) clearTimeout(disarm.current);
      disarm.current = setTimeout(() => setArmed(''), 3000);
      return;
    }
    if (disarm.current) clearTimeout(disarm.current);
    setArmed(''); setDeleting(row.capture_id);
    const r = await vocxService.remove(rm, row.capture_id);
    setDeleting('');
    if (!r.ok) { setErr(r.error); return; }
    setFlash('Draft deleted.');
    void load();
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
  const needle = q.trim().toLowerCase();
  const visible = !needle ? rows : rows.filter((r) =>
    [r.company, r.summary, r.entity_code, r.capture_id, r.status]
      .some((f) => (f || '').toLowerCase().includes(needle)));

  return (
    <Box sx={{ p: 1.2 }}>
      {flash && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }}
        onClose={() => setFlash('')}>{flash}</Alert>}
      {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }}
        onClose={() => setErr('')}>{err}</Alert>}

      <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.8, gap: 0.4 }}>
        {searching ? (
          <InputBase
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape') { setQ(''); setSearching(false); } }}
            placeholder="Search company, summary, code…"
            sx={{ flex: 1, fontSize: 12, color: vx.ink, px: 1, py: 0.2,
              bgcolor: vx.card, border: `1px solid ${vx.line}`, borderRadius: '8px' }}
          />
        ) : (
          <Typography sx={{ fontSize: 11.5, color: vx.mut, flex: 1 }}>
            {pending > 0
              ? `${pending} waiting to be approved`
              : rows.length ? 'All captures filed' : ''}
          </Typography>
        )}
        <IconButton size="small" aria-label={searching ? 'Close search' : 'Search reports'}
          onClick={() => { if (searching) setQ(''); setSearching(!searching); }}
          sx={{ color: searching ? vx.grn : vx.mut, p: 0.4 }}>
          {searching ? <CloseIcon sx={{ fontSize: 16 }} /> : <SearchIcon sx={{ fontSize: 16 }} />}
        </IconButton>
        <Button size="small" onClick={() => void load()} disabled={loading}
          sx={{ textTransform: 'none', fontSize: 11.5, color: vx.grn }}>Refresh</Button>
      </Box>

      {loading && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
          <CircularProgress size={22} sx={{ color: vx.grn }} />
        </Box>
      )}

      {!loading && !rows.length && !err && (
        <Typography sx={{ fontSize: 12.5, color: vx.mut, py: 3, textAlign: 'center' }}>
          Nothing captured yet. Record a note on the Record tab and it will appear here.
        </Typography>
      )}

      {!loading && rows.length > 0 && !visible.length && (
        <Typography sx={{ fontSize: 12.5, color: vx.mut, py: 3, textAlign: 'center' }}>
          No reports match “{q.trim()}”.
        </Typography>
      )}

      {visible.map((r) => {
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
              display: 'flex', gap: 1, alignItems: 'flex-start', p: 1.5, mb: 1,
              borderRadius: '12px', cursor: 'pointer',
              border: `1px solid ${isPending(r) ? 'rgba(240,180,60,.35)' : vx.line}`,
              bgcolor: vx.card,
              '&:hover': { bgcolor: vx.card2 },
              '&:focus-visible': { outline: `2px solid ${vx.grn}`, outlineOffset: 2 },
            }}
          >
            <Chip size="small" label={opening === r.capture_id ? '…' : status}
              sx={{ height: 19, fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                bgcolor: tone.bg, color: tone.fg, flexShrink: 0 }} />
            <Box sx={{ minWidth: 0, flex: 1 }}>
              <Typography sx={{ fontSize: 16, fontWeight: 700, color: vx.ink,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.company || r.summary || r.capture_id}
              </Typography>
              {r.summary && r.company && (
                <Typography sx={{ fontSize: 11.5, color: vx.mut,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.summary}
                </Typography>
              )}
              <Typography sx={{ fontSize: 10.5, color: vx.mut }}>
                {[r.entity_code, when(r.updated_at)].filter(Boolean).join(' · ')}
              </Typography>
            </Box>
            {/* Unfiled captures can be thrown away from the list; a COMMITTED report is
                already in the register and lives by the register's rules, not a trash
                icon here. First click arms (icon turns red), second click deletes. */}
            {isPending(r) && (
              <IconButton
                size="small"
                aria-label={armed === r.capture_id ? 'Click again to delete' : 'Delete draft'}
                title={armed === r.capture_id ? 'Click again to delete' : 'Delete draft'}
                disabled={deleting === r.capture_id}
                onClick={(e) => { e.stopPropagation(); void removeDraft(r); }}
                sx={{ p: 0.4, flexShrink: 0, alignSelf: 'center',
                  color: armed === r.capture_id ? '#F26D6D' : 'rgba(232,238,242,.45)',
                  '&:hover': { color: '#F26D6D' } }}
              >
                {deleting === r.capture_id
                  ? <CircularProgress size={14} sx={{ color: '#F26D6D' }} />
                  : <DeleteOutlineIcon sx={{ fontSize: 17 }} />}
              </IconButton>
            )}
          </Box>
        );
      })}
    </Box>
  );
}
