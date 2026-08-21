import { useState } from 'react';
import { Box, ListItemText, Menu, MenuItem, Stack, Tooltip, Typography } from '@mui/material';
import type { PipelineStep, WorkflowAction } from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * The credit pipeline as one readable strip — CAM → CCR → Sanction → CP, forking to
 * Disbursement ∥ CP/CS — and, when the plane maps its verbs onto the boxes, the strip IS
 * the controls: each box opens the step's action. Box borders carry the state: green
 * done, blue in progress, red where a checker said no, grey not reached.
 *
 * Clicking a box with exactly one live verb opens it straight away; a box with several
 * (a returned committee run: revise / resubmit) drops a menu; a box whose verbs are all
 * gated drops the same menu with each refusal's reason — the sequence-teaching that the
 * old button row carried on hover survives the row's retirement.
 *
 * States and notes arrive computed from the plane, off the same facts that gate the
 * verbs — this component decides nothing, so the strip can never disagree with the
 * platform about what is possible.
 */

const EDGE: Record<PipelineStep['state'], string> = {
  done: tokens.ok, active: tokens.lend, rejected: tokens.bad, pending: tokens.line,
};

function StepBox({ s, verbs, onPick }: {
  s: PipelineStep;
  verbs: WorkflowAction[];
  onPick: (a: WorkflowAction, readOnly?: boolean) => void;
}) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const pending = s.state === 'pending';
  const live = verbs.filter((v) => v.enabled);
  // A viewable step with nothing actionable still opens its artefact — the recorded
  // checklist, read-only — rather than a menu of refusals. The checklist screen is
  // the carrier; the plane says whether there is anything on record to show.
  const viewer = s.viewable && !live.length
    ? verbs.find((v) => v.screen === 'cpcs-checklist') : undefined;
  const clickable = verbs.length > 0;
  const onClick = (e: React.MouseEvent<HTMLElement>) => {
    if (!clickable) return;
    if (live.length === 1) { onPick(live[0]); return; }
    if (viewer) { onPick(viewer, true); return; }
    setAnchor(e.currentTarget);   // several live, or all gated: show the step's verbs
  };
  return (
    <>
      <Tooltip title={s.note || ''} placement="top" arrow>
        <Box onClick={onClick} sx={{
          border: `2px solid ${EDGE[s.state]}`, borderRadius: '7px', px: 0.9, py: 0.1,
          fontSize: 11.5, fontWeight: 700, lineHeight: '18px', whiteSpace: 'nowrap',
          color: pending ? tokens.muted : EDGE[s.state],
          backgroundColor: s.state === 'active' ? '#EAF2F8'
            : s.state === 'rejected' ? tokens.badBg : tokens.card,
          ...(clickable && {
            cursor: 'pointer',
            '&:hover': { boxShadow: '0 1px 6px rgba(15,40,50,.22)' },
          }),
        }}>
          {s.label}
        </Box>
      </Tooltip>
      <Menu anchorEl={anchor} open={!!anchor} onClose={() => setAnchor(null)}>
        {verbs.map((v) => (
          <MenuItem key={v.key} disabled={!v.enabled} sx={{ py: 0.4 }}
            onClick={() => { setAnchor(null); onPick(v); }}>
            <ListItemText
              primary={v.label}
              secondary={v.enabled ? undefined : (v.reason || 'Not available yet')}
              primaryTypographyProps={{ fontSize: 12.8 }}
              secondaryTypographyProps={{ fontSize: 11.2, whiteSpace: 'normal',
                maxWidth: 300 }} />
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}

const Arrow = () => (
  <Typography component="span" sx={{ color: tokens.muted, fontSize: 12, lineHeight: 1 }}>
    →
  </Typography>
);

export default function PipelineStepper({ steps, actions = [], onOpen }: {
  steps: PipelineStep[];
  /** The plane's verbs; each box lists the ones tagged with its `step`. */
  actions?: WorkflowAction[];
  onOpen?: (a: WorkflowAction, readOnly?: boolean) => void;
}) {
  if (!steps?.length) return null;
  const line = steps.filter((s) => !s.parallel);
  const fork = steps.filter((s) => s.parallel);
  const verbsOf = (s: PipelineStep) => (onOpen ? actions.filter((a) => a.step === s.key) : []);
  const pick = (a: WorkflowAction, ro?: boolean) => onOpen?.(a, ro);
  return (
    <Stack direction="row" spacing={0.7} alignItems="center" flexWrap="wrap" useFlexGap
      sx={{ mb: 1 }}>
      {line.map((s, i) => (
        <Stack key={s.key} direction="row" spacing={0.7} alignItems="center">
          {i > 0 && <Arrow />}
          <StepBox s={s} verbs={verbsOf(s)} onPick={pick} />
        </Stack>
      ))}
      {fork.length > 0 && (
        <>
          <Arrow />
          {/* After CP the journey forks: the money and the CS chase run side by side. */}
          <Stack spacing={0.5}>
            {fork.map((s) => <StepBox key={s.key} s={s} verbs={verbsOf(s)} onPick={pick} />)}
          </Stack>
        </>
      )}
    </Stack>
  );
}
