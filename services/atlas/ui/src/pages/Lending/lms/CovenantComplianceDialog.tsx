import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, TextField, Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { lmsService, type Covenant, type Observation } from '../../../services/lmsService';
import { useAuth } from '../../../auth/AuthContext';
import { can } from '../../../auth/rbac';
import { tokens } from '../../../theme';
import type { LendingRow } from '../lending.types';

/**
 * COVENANT COMPLIANCE — the loan's whole compliance ledger in one dialog (the same
 * pattern as the CP/CS checklist dialog in LOS): the covenants the sanction imposed,
 * every period's observation with its status, and the verbs in place — the OPERATOR
 * records the period's result when the documents arrive; LMS MANAGEMENT waives a
 * breach against a recorded decision.
 *
 * The monthly chase runs itself: the sweep raises each period as it falls due, and a
 * standing reminder sits on the operator's TODAY screen (LMS Management sees it too)
 * until the result is recorded — repeating every cycle until the loan closes.
 */

const OBS_TONE: Record<string, { bg: string; fg: string }> = {
  Pending: { bg: '#EEF1F3', fg: '#5F6E76' },
  Compliant: { bg: '#E5F5EC', fg: '#175E3B' },
  Breached: { bg: '#FDE8E4', fg: '#7C4A3E' },
  Waived: { bg: '#FFF3CD', fg: '#7A5C00' },
  Overdue: { bg: '#FFF3CD', fg: '#7A5C00' },
};

const Chip = ({ label, tone }: { label: string; tone: { bg: string; fg: string } }) => (
  <Typography component="span" sx={{ fontSize: 10.5, fontWeight: 700, px: 0.7,
    borderRadius: 1, bgcolor: tone.bg, color: tone.fg, whiteSpace: 'nowrap' }}>{label}</Typography>
);

export default function CovenantComplianceDialog({ row, open, onClose, onChanged }: {
  row: LendingRow | null; open: boolean; onClose: () => void; onChanged?: () => void;
}) {
  const { user } = useAuth();
  const operate = can(user.roles, 'lmsOperate');
  const authorize = can(user.roles, 'lmsAuthorize');

  const [covs, setCovs] = useState<Covenant[]>([]);
  const [obs, setObs] = useState<Observation[]>([]);
  const [err, setErr] = useState('');
  const [info, setInfo] = useState('');
  const [busy, setBusy] = useState('');
  const [act, setAct] = useState<{ id: string; kind: 'result' | 'waive';
    actual: string; when: string; ref: string; note: string } | null>(null);
  const [touched, setTouched] = useState(false);

  const load = async () => {
    if (!row) return;
    setErr('');
    try {
      if (row.entityId) {
        setCovs((await lmsService.covenants(row.entityId).catch(() => []))
          .filter((c) => !c.lending_id || c.lending_id === row.id));
        setObs(await lmsService.observations(row.entityId, row.id).catch(() => []));
      } else { setCovs([]); setObs([]); }
    } catch (e: any) { setErr(e?.message || String(e)); }
  };

  useEffect(() => {
    if (!open) return;
    setErr(''); setInfo(''); setBusy(''); setAct(null); setTouched(false);
    void load();
  }, [open, row?.id]);  // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (fn: () => Promise<string>) => {
    setErr(''); setInfo(''); setBusy('x');
    try { setInfo(await fn()); setTouched(true); await load(); }
    catch (e: any) { setErr(e?.message || String(e)); }
    setBusy('');
  };

  const close = () => { if (touched) onChanged?.(); onClose(); };

  return (
    <Dialog open={open} onClose={close} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>
        Covenant compliance — {row?._name}
        <IconButton onClick={close} sx={{ position: 'absolute', right: 8, top: 6 }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setErr('')}>{err}</Alert>}
        {info && <Alert severity={info.includes('BREACHED') ? 'warning' : 'success'}
          sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setInfo('')}>{info}</Alert>}

        {/* ---- what the sanction imposed ---------------------------------------- */}
        <Typography sx={{ fontSize: 12.5, fontWeight: 600, mb: 0.5 }}>
          Covenants on this loan ({covs.length})
        </Typography>
        {covs.length === 0 && (
          <Typography sx={{ fontSize: 12, color: tokens.muted, mb: 1 }}>
            None on record — covenants seed from the sanction terms.
          </Typography>
        )}
        {covs.map((c) => (
          <Box key={c.id} sx={{ display: 'flex', gap: 1, alignItems: 'baseline', py: 0.3,
            borderBottom: `1px dashed ${tokens.line}`, '&:last-of-type': { borderBottom: 'none' } }}>
            <Chip label={c.covenant_type} tone={{ bg: '#E7EEF9', fg: '#33518f' }} />
            <Typography sx={{ fontSize: 12.4, flex: 1 }}>{c.name}</Typography>
            <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
              {[c.frequency, c.metric && c.operator && c.threshold != null
                ? `${c.metric} ${c.operator} ${c.threshold}` : null,
              c.first_due_on ? `from ${c.first_due_on}` : 'starts at disbursement']
                .filter(Boolean).join(' · ')}
            </Typography>
          </Box>
        ))}

        {/* ---- every period, its status, the verbs ------------------------------ */}
        <Typography sx={{ fontSize: 12.5, fontWeight: 600, mt: 2, mb: 0.5 }}>
          Observations ({obs.length})
        </Typography>
        {obs.length === 0 && (
          <Typography sx={{ fontSize: 12, color: tokens.muted }}>
            None raised yet — the sweep mints each period as it falls due.
          </Typography>
        )}
        {obs.map((o) => (
          <Box key={o.id} sx={{ py: 0.35, borderBottom: `1px dashed ${tokens.line}`,
            '&:last-of-type': { borderBottom: 'none' } }}>
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center' }}>
              <Typography sx={{ fontSize: 12.4, flex: 1 }}>{o.covenant_name || '—'}</Typography>
              {o.actual_value != null && (
                <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>
                  actual {o.actual_value}{o.target_value != null ? ` / target ${o.target_value}` : ''}
                </Typography>
              )}
              <Typography sx={{ fontSize: 11.5, color: tokens.muted }}>{o.due_date || ''}</Typography>
              <Chip label={o.status} tone={OBS_TONE[o.status] || OBS_TONE.Pending} />
              {operate && ['Pending', 'Overdue'].includes(o.status) && act?.id !== o.id && (
                <Button size="small" variant="outlined" disabled={!!busy}
                  onClick={() => setAct({ id: o.id, kind: 'result', actual: '',
                    when: new Date().toISOString().slice(0, 10), ref: '', note: '' })}
                  sx={{ textTransform: 'none', fontSize: 11.5, py: 0.1 }}>
                  Record result…
                </Button>
              )}
              {authorize && o.status === 'Breached' && act?.id !== o.id && (
                <Button size="small" variant="outlined" color="warning" disabled={!!busy}
                  onClick={() => setAct({ id: o.id, kind: 'waive', actual: '',
                    when: '', ref: '', note: '' })}
                  sx={{ textTransform: 'none', fontSize: 11.5, py: 0.1 }}>
                  Waive…
                </Button>
              )}
            </Box>
            {act?.id === o.id && act.kind === 'result' && (
              <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mt: 0.5, flexWrap: 'wrap' }}>
                <TextField size="small" type="number" label="Actual value (if financial)"
                  value={act.actual} sx={{ width: 180 }}
                  onChange={(e) => setAct({ ...act, actual: e.target.value })} />
                <TextField size="small" type="date" label="Submitted on"
                  InputLabelProps={{ shrink: true }} value={act.when} sx={{ width: 150 }}
                  onChange={(e) => setAct({ ...act, when: e.target.value })} />
                <Button size="small" variant="contained" disabled={!!busy}
                  onClick={() => run(async () => {
                    const r = await lmsService.submitResult(o.id, {
                      ...(act.actual ? { actual_value: Number(act.actual) } : {}),
                      ...(act.when ? { submitted_on: act.when } : {}),
                    });
                    setAct(null);
                    return r.breached
                      ? `${o.covenant_name}: BREACHED — an EWS case opened with the result.`
                      : `${o.covenant_name}: recorded — ${r.status}.`;
                  })}
                  sx={{ textTransform: 'none', fontSize: 11.5 }}>
                  {busy ? 'Recording…' : 'Submit'}
                </Button>
                <Button size="small" onClick={() => setAct(null)}
                  sx={{ textTransform: 'none', fontSize: 11.5 }}>Cancel</Button>
              </Box>
            )}
            {act?.id === o.id && act.kind === 'waive' && (
              <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mt: 0.5, flexWrap: 'wrap' }}>
                <TextField size="small" label="Waiver decision ref (required)" autoFocus
                  value={act.ref} sx={{ flex: 1, minWidth: 180 }}
                  onChange={(e) => setAct({ ...act, ref: e.target.value })} />
                <TextField size="small" label="Note" value={act.note} sx={{ flex: 1, minWidth: 140 }}
                  onChange={(e) => setAct({ ...act, note: e.target.value })} />
                <Button size="small" variant="contained" color="warning"
                  disabled={!!busy || !act.ref.trim()}
                  onClick={() => run(async () => {
                    await lmsService.waive(o.id, act.ref.trim(), act.note.trim() || undefined);
                    setAct(null);
                    return `${o.covenant_name}: waived against the recorded decision.`;
                  })}
                  sx={{ textTransform: 'none', fontSize: 11.5 }}>
                  {busy ? 'Waiving…' : 'Confirm waiver'}
                </Button>
                <Button size="small" onClick={() => setAct(null)}
                  sx={{ textTransform: 'none', fontSize: 11.5 }}>Cancel</Button>
              </Box>
            )}
          </Box>
        ))}

        <Alert severity="info" sx={{ mt: 1.5, py: 0.3, fontSize: 12 }}>
          The monthly chase runs itself: each period appears as a standing reminder on
          the LMS Operator's <b>Today</b> screen (LMS Management sees it too) — call the
          borrower, collect the documents, record the result here or right from the
          reminder. It repeats every cycle until the loan closes.
        </Alert>
      </DialogContent>
      <DialogActions>
        <Button onClick={close} variant="outlined">Close</Button>
      </DialogActions>
    </Dialog>
  );
}
