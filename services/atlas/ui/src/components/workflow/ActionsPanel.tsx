import { useCallback, useEffect, useRef, useState } from 'react';
import { Box, Button, Stack, Tooltip, Typography, Alert } from '@mui/material';
import ActionFormDialog from './ActionFormDialog';
import CpcsChecklistDialog from './CpcsChecklistDialog';
import HandoverPackageDialog from './HandoverPackageDialog';
import ExecutedAgreementDialog from './ExecutedAgreementDialog';
import CamWorkbenchDialog from './CamWorkbenchDialog';
import DisburseDialog from './DisburseDialog';
import SanctionTermsDialog from './SanctionTermsDialog';
import PipelineStepper from './PipelineStepper';
import { workflowActionsService, type SubjectActions, type SubjectType, type WorkflowAction }
  from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * What this user can do NEXT on one line — the maker's counterpart to Today's approver
 * queue, and served by the same plane (`GET /v1/workflows/actions`).
 *
 * Unavailable steps are shown DISABLED with the plane's reason on hover, not hidden. A
 * hidden button teaches nothing; "Available once the committee has sanctioned this
 * facility" teaches the sequence. It is also the honest opposite of the old stage
 * dropdown, which offered four stages the register would always refuse and explained
 * none of them.
 */
export default function ActionsPanel({ subjectType, subjectId, code, entityId, onStage }: {
  subjectType: SubjectType;
  subjectId: string;
  /** The company — the handover package picks its documents from that file. */
  code?: string;
  entityId?: string;
  /** The SERVER moved the subject's stage (auto-approval, auto-move) — the parent can
   *  reflect it into its own fields without a page refresh. */
  onStage?: (stage: string) => void;
}) {
  const [data, setData] = useState<SubjectActions | null>(null);
  const [open, setOpen] = useState<WorkflowAction | null>(null);
  // A settled CP / CP-CS box opens its recorded checklist to LOOK at, not to edit.
  const [viewOnly, setViewOnly] = useState(false);
  const [done, setDone] = useState('');
  const lastStage = useRef('');
  const settleTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(() => {
    let alive = true;
    workflowActionsService.forSubject(subjectType, subjectId).then((d) => {
      if (!alive) return;
      setData(d);
      const s = String((d as any)?.subject?.stage || '');
      if (s && lastStage.current && s !== lastStage.current) onStage?.(s);
      if (s) lastStage.current = s;
    });
    return () => { alive = false; };
  }, [subjectType, subjectId, onStage]);

  useEffect(load, [load]);
  useEffect(() => () => { if (settleTimer.current) clearInterval(settleTimer.current); }, []);

  // The auto-approval and the automatic stage moves land seconds AFTER a dialog
  // closes — the strip (and the drawer, via onStage) must catch the settled state
  // without a manual refresh. A short re-poll after every completed action covers
  // the policy's poll-approve-move window; it stops by itself.
  const finish = useCallback((m: string) => {
    setDone(m); load();
    if (settleTimer.current) clearInterval(settleTimer.current);
    let n = 0;
    settleTimer.current = setInterval(() => {
      n += 1; load();
      if (n >= 8 && settleTimer.current) { clearInterval(settleTimer.current); settleTimer.current = null; }
    }, 2500);
  }, [load]);

  if (!data || !data.actions.length) return null;

  // With a pipeline, the strip's boxes ARE the controls: each box opens its step's
  // verb (or drops a menu of them, refusal reasons included). Only a verb the plane
  // did not map to a box still renders as a button — nothing is ever unreachable.
  const rowActions = data.pipeline ? data.actions.filter((a) => !a.step) : data.actions;

  return (
    <>
      {data.pipeline && (
        <Box sx={{ border: `1px solid ${tokens.line}`, borderRadius: 2,
          p: 1.3, pb: 0.6, mt: 1.6, mb: 1.6 }}>
          <Stack direction="row" alignItems="center" spacing={0.6} sx={{ mb: 0.9 }}>
            <Typography sx={{ fontSize: 11, fontWeight: 700, letterSpacing: '.8px',
              textTransform: 'uppercase', color: tokens.muted }}>
              Credit pipeline
            </Typography>
            <Tooltip arrow placement="top" title={
              'The credit governance journey for this lending line: CAM (credit note) '
              + '→ CCR (credit committee decision) → Sanction → CP (conditions '
              + 'precedent from the sanction letter). After the CP approval, '
              + 'Disbursement and the CP/CS chase (conditions subsequent) run in '
              + 'parallel. Green = done, blue = in progress, grey = ahead — tap any '
              + 'box to open that step.'}>
              <Box component="span" sx={{ fontSize: 13, color: tokens.muted,
                cursor: 'help', lineHeight: 1, userSelect: 'none' }}>ⓘ</Box>
            </Tooltip>
          </Stack>
          <PipelineStepper steps={data.pipeline} actions={data.actions}
            onOpen={(a, ro) => { setViewOnly(!!ro); setOpen(a); }} />
        </Box>
      )}
      {data.run && (
        <Typography sx={{ fontSize: 11.8, color: tokens.muted, mb: 0.8 }}>
          Run in flight — <b>{data.run.stage || data.run.status}</b>
        </Typography>
      )}
      {done && <Alert severity="success" sx={{ mb: 1, py: 0, fontSize: 12 }}
        onClose={() => setDone('')}>{done}</Alert>}
      <Stack direction="row" spacing={0.8} flexWrap="wrap" useFlexGap>
        {rowActions.map((a) => (
          // A disabled MUI button swallows pointer events, so the tooltip wraps a span.
          <Tooltip key={a.key} title={a.enabled ? '' : (a.reason || 'Not available yet')}
            placement="top" arrow>
            <span>
              <Button size="small" variant={a.enabled ? 'outlined' : 'text'}
                disabled={!a.enabled} onClick={() => setOpen(a)}
                sx={{ fontSize: 12, textTransform: 'none' }}>
                {a.label}
              </Button>
            </span>
          </Tooltip>
        ))}
      </Stack>
      {/* A step with its own screen opens that screen; everything else is built from the
          form the plane sent. The panel does not know what either screen contains. */}
      <ActionFormDialog action={open && !open.screen ? open : null}
        onClose={() => setOpen(null)} onDone={finish} />
      <CpcsChecklistDialog action={open?.screen === 'cpcs-checklist' ? open : null}
        readOnly={viewOnly}
        onClose={() => { setOpen(null); setViewOnly(false); }}
        onDone={finish} />
      <HandoverPackageDialog action={open?.screen === 'handover-package' ? open : null}
        code={code || ''} entityId={entityId}
        onClose={() => setOpen(null)} onDone={finish} />
      <ExecutedAgreementDialog action={open?.screen === 'executed-agreement' ? open : null}
        code={code || ''} entityId={entityId}
        onClose={() => setOpen(null)} onDone={finish} />
      <CamWorkbenchDialog action={open?.screen === 'cam-workbench' ? open : null}
        subjectId={subjectId} entityId={entityId}
        onClose={() => setOpen(null)} onDone={finish} />
      <SanctionTermsDialog action={open?.screen === 'sanction-terms' ? open : null}
        stage={String((data as any)?.subject?.stage || '')}
        onClose={() => setOpen(null)} onDone={finish} />
      <DisburseDialog action={open?.screen === 'disburse' ? open : null}
        onClose={() => setOpen(null)} onDone={finish} />
    </>
  );
}
