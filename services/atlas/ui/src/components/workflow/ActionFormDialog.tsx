import { useEffect, useState } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Alert, Typography } from '@mui/material';
import { FieldGrid, TextFld, SelectFld } from '../common/Field';
import { workflowActionsService, type WorkflowAction } from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * One dialog for EVERY maker action. The plane sends the field list; this renders it.
 *
 * There is no per-action component and deliberately so — adding a workflow step should be
 * a change to the catalogue in the orchestrator, not a new dialog here. The two steps whose
 * shape a generic form genuinely cannot carry (the CP/CS checklist grid and handover
 * package assembly) get bespoke screens later; everything else is a handful of fields.
 */
export default function ActionFormDialog({ action, onClose, onDone }: {
  action: WorkflowAction | null;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [values, setValues] = useState<Record<string, any>>({});
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!action) return;
    const seed: Record<string, any> = {};
    action.form.forEach((f) => { if (f.default !== undefined) seed[f.name] = f.default; });
    setValues(seed); setErr(''); setBusy(false);
  }, [action]);

  if (!action) return null;
  const set = (k: string, v: any) => setValues((p) => ({ ...p, [k]: v }));

  const submit = async () => {
    const missing = workflowActionsService.missing(action, values);
    if (missing.length) { setErr(`Required: ${missing.join(', ')}.`); return; }
    setBusy(true);
    const r = await workflowActionsService.run(action, values);
    setBusy(false);
    if (!r.ok) { setErr(r.error || 'The workflow plane refused this step.'); return; }
    // A reference the plane issued (the auto-numbered credit note) is the one thing
    // the maker needs to carry away from this step — say it in the confirmation.
    const minted = r.data?.credit_note_reference;
    onDone(`${action.label} — done.${minted ? ` Credit note ${minted}.` : ''}`);
    onClose();
  };

  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>{action.label}</DialogTitle>
      <DialogContent dividers>
        <FieldGrid>
          {action.form.map((f) => (
            f.type === 'select'
              ? <SelectFld key={f.name} label={f.label} required={f.required}
                  value={values[f.name] ?? ''} onChange={(v) => set(f.name, v)}
                  options={f.options || []} blank />
              : <TextFld key={f.name} label={f.label} required={f.required}
                  type={f.type === 'number' ? 'number' : f.type === 'date' ? 'date' : undefined}
                  multiline={f.type === 'textarea'}
                  value={values[f.name] ?? ''} onChange={(v) => set(f.name, v)}
                  placeholder={f.placeholder} />
          ))}
        </FieldGrid>
        {action.form.filter((f) => f.help).map((f) => (
          <Typography key={f.name} sx={{ fontSize: 11.6, color: tokens.muted, mt: 0.8 }}>
            <b>{f.label}:</b> {f.help}
          </Typography>
        ))}
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined" disabled={busy}>Cancel</Button>
        <Button onClick={submit} variant="contained" disabled={busy}>
          {busy ? 'Working…' : action.label}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
