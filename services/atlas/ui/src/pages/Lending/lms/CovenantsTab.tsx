import { useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  MenuItem, Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { lmsService, type Covenant, type Observation } from '../../../services/lmsService';
import { useAuth } from '../../../auth/AuthContext';
import { can, whoCan } from '../../../auth/rbac';
import { tokens } from '../../../theme';
import type { LendingRow } from '../lending.types';

/**
 * The covenant chase — live from first disbursement until closure. Definitions come
 * from the sanction letter (seeded at terms entry); the SWEEP generates one observation
 * per period, marks overdue, expires waivers. Here the desk records each period's
 * outcome: a reporting covenant is complied by submission; a financial covenant
 * submits the tested value and the register computes breach — a breach opens an EWS
 * case in the same transaction. Waivers apply only against a recorded, time-boxed
 * waiver decision.
 */

const STATUS_TONE: Record<string, 'default' | 'success' | 'warning' | 'error' | 'info'> = {
  Pending: 'info', Compliant: 'success', Breached: 'error',
  Waived: 'warning', Overdue: 'warning',
};

export default function CovenantsTab({ rows }: { rows: LendingRow[] }) {
  const { user } = useAuth();
  const operate = can(user.roles, 'lmsOperate');
  const authorize = can(user.roles, 'lmsAuthorize');

  // One company at a time — covenants read by entity.
  const companies = useMemo(() => {
    const seen = new Map<string, LendingRow>();
    rows.forEach((r) => { if (r.entityId && !seen.has(r.entityId)) seen.set(r.entityId, r); });
    return [...seen.values()];
  }, [rows]);
  const [entityId, setEntityId] = useState('');
  useEffect(() => {
    if (!entityId && companies.length) setEntityId(companies[0].entityId!);
  }, [companies, entityId]);

  const [covs, setCovs] = useState<Covenant[]>([]);
  const [obs, setObs] = useState<Observation[]>([]);
  const [err, setErr] = useState('');
  const [info, setInfo] = useState('');
  const [busy, setBusy] = useState(false);
  // The result dialog (one observation at a time).
  const [target, setTarget] = useState<Observation | null>(null);
  const [actual, setActual] = useState('');
  const [note, setNote] = useState('');
  const [waiveRef, setWaiveRef] = useState('');

  const load = async (eid: string) => {
    if (!eid) return;
    setErr('');
    try {
      const [c, o] = await Promise.all([
        lmsService.covenants(eid), lmsService.observations(eid)]);
      setCovs(c); setObs(o);
    } catch (e: any) { setErr(e?.message || String(e)); }
  };
  useEffect(() => { void load(entityId); }, [entityId]);  // eslint-disable-line react-hooks/exhaustive-deps

  const targetDef = target
    ? covs.find((c) => c.id === (target.details || {})['covenant_id']) : undefined;
  const isFinancial = !!(target && (target.details || {}).operator);

  const submit = async () => {
    if (!target) return;
    setErr(''); setBusy(true);
    try {
      const out = await lmsService.submitResult(target.id, {
        ...(isFinancial ? { actual_value: Number(actual) } : {}),
        ...(note ? { note } : {}),
      });
      setInfo(out.breached
        ? `Breached — EWS case ${out.ews_case_id ? 'opened' : 'already open'} for ${out.covenant_name}.`
        : `${out.covenant_name}: compliant for ${out.period}.`);
      setTarget(null); setActual(''); setNote('');
      await load(entityId);
    } catch (e: any) { setErr(e?.message || String(e)); }
    setBusy(false);
  };

  const waive = async () => {
    if (!target) return;
    setErr(''); setBusy(true);
    try {
      const out = await lmsService.waive(target.id, waiveRef.trim(), note || undefined);
      setInfo(`${out.covenant_name}: waived until ${out.waiver_valid_until}.`);
      setTarget(null); setWaiveRef(''); setNote('');
      await load(entityId);
    } catch (e: any) { setErr(e?.message || String(e)); }
    setBusy(false);
  };

  return (
    <Box sx={{ mt: 1 }}>
      {err && <Alert severity="warning" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setErr('')}>{err}</Alert>}
      {info && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }} onClose={() => setInfo('')}>{info}</Alert>}

      {!companies.length && (
        <Typography sx={{ fontSize: 12.5, color: tokens.muted, mt: 1 }}>
          No serviced lines yet — covenants start their chase at the first disbursement.
        </Typography>
      )}
      {companies.length > 0 && (
        <TextField select size="small" label="Company" value={entityId}
          onChange={(e) => setEntityId(e.target.value)} sx={{ minWidth: 260, mb: 1.2 }}>
          {companies.map((c) => (
            <MenuItem key={c.entityId} value={c.entityId!}>{c._name || c.code}</MenuItem>
          ))}
        </TextField>
      )}

      {covs.length > 0 && (
        <>
          <Typography sx={{ fontSize: 12.5, fontWeight: 600, mb: 0.4 }}>
            Covenant register ({covs.length})
          </Typography>
          <Box sx={{ display: 'flex', gap: 0.6, flexWrap: 'wrap', mb: 1.4 }}>
            {covs.map((c) => (
              <Chip key={c.id} size="small" variant="outlined"
                label={`${c.name} · ${c.frequency}${c.first_due_on ? '' : ' · starts at disbursement'}`} />
            ))}
          </Box>
        </>
      )}

      <Typography sx={{ fontSize: 12.5, fontWeight: 600, mb: 0.4 }}>
        Observations — one per period
      </Typography>
      {!obs.length && (
        <Typography sx={{ fontSize: 12, color: tokens.muted }}>
          None generated yet — the recurring sweep creates each period as it falls due
          (and marks overdue / expires lapsed waivers).
        </Typography>
      )}
      {obs.length > 0 && (
        <Table size="small" sx={{ '& td, & th': { fontSize: 12 } }}>
          <TableHead>
            <TableRow sx={{ '& th': { fontWeight: 600, color: tokens.muted } }}>
              <TableCell>Covenant</TableCell><TableCell>Due</TableCell>
              <TableCell>Status</TableCell><TableCell align="right">Target</TableCell>
              <TableCell align="right">Actual</TableCell><TableCell>Submitted</TableCell>
              <TableCell />
            </TableRow>
          </TableHead>
          <TableBody>
            {obs.map((o) => (
              <TableRow key={o.id} hover>
                <TableCell>{o.covenant_name}</TableCell>
                <TableCell>{o.due_date}</TableCell>
                <TableCell>
                  <Chip size="small" color={STATUS_TONE[o.status] || 'default'} label={o.status}
                    sx={{ height: 20, fontSize: 11 }} />
                  {o.waiver_status === 'Expired' && (
                    <Chip size="small" color="error" variant="outlined" label="waiver expired"
                      sx={{ height: 20, fontSize: 10.5, ml: 0.5 }} />
                  )}
                </TableCell>
                <TableCell align="right">{o.target_value ?? '—'}</TableCell>
                <TableCell align="right">{o.actual_value ?? '—'}</TableCell>
                <TableCell>{o.submitted_date || '—'}</TableCell>
                <TableCell align="right">
                  {['Pending', 'Overdue'].includes(o.status) && operate && (
                    <Button size="small" sx={{ textTransform: 'none', fontSize: 11.5 }}
                      onClick={() => { setTarget(o); setActual(''); setNote(''); setWaiveRef(''); }}>
                      Record result
                    </Button>
                  )}
                  {o.status === 'Breached' && authorize && (
                    <Button size="small" color="warning" sx={{ textTransform: 'none', fontSize: 11.5 }}
                      onClick={() => { setTarget(o); setActual(''); setNote(''); setWaiveRef(''); }}>
                      Waive…
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {!operate && (
        <Typography sx={{ fontSize: 11.5, color: tokens.muted, mt: 1 }}>
          Results are recorded by: {whoCan('lmsOperate')}. Waivers: {whoCan('lmsAuthorize')}.
        </Typography>
      )}

      {/* ---- record result / waive — one observation ---------------------------- */}
      <Dialog open={!!target} onClose={() => setTarget(null)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontSize: 15 }}>
          {target?.status === 'Breached' ? 'Waive breach' : 'Record result'} — {target?.covenant_name}
        </DialogTitle>
        <DialogContent dividers>
          <Typography sx={{ fontSize: 12, color: tokens.muted, mb: 1 }}>
            Period {target?.period}{targetDef?.description ? ` · ${targetDef.description}` : ''}
          </Typography>
          {target?.status !== 'Breached' && (
            <>
              {isFinancial ? (
                <TextField fullWidth size="small" type="number" sx={{ mb: 1 }}
                  label={`Actual ${String((target?.details || {}).metric || 'value')} (target ${(target?.details || {}).operator} ${target?.target_value})`}
                  value={actual} onChange={(e) => setActual(e.target.value)} />
              ) : (
                <Alert severity="info" sx={{ py: 0.2, fontSize: 12, mb: 1 }}>
                  A reporting obligation — recording the submission IS the compliance.
                </Alert>
              )}
            </>
          )}
          {target?.status === 'Breached' && (
            <TextField fullWidth size="small" sx={{ mb: 1 }}
              label="Waiver decision reference (recorded, time-boxed)"
              helperText="The waiver applies only against a recorded senior-credit decision."
              value={waiveRef} onChange={(e) => setWaiveRef(e.target.value)} />
          )}
          <TextField fullWidth size="small" label="Note (optional)" value={note}
            onChange={(e) => setNote(e.target.value)} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setTarget(null)} variant="outlined" disabled={busy}>Cancel</Button>
          {target?.status === 'Breached' ? (
            <Button onClick={() => void waive()} variant="contained" color="warning"
              disabled={busy || !waiveRef.trim()}>
              {busy ? 'Applying…' : 'Apply waiver'}
            </Button>
          ) : (
            <Button onClick={() => void submit()} variant="contained"
              disabled={busy || (isFinancial && !actual)}>
              {busy ? 'Submitting…' : 'Submit'}
            </Button>
          )}
        </DialogActions>
      </Dialog>
    </Box>
  );
}
