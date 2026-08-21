import { Box, Stack, Tooltip, Typography } from '@mui/material';
import type { PipelineStep } from '../../services/workflowActionsService';
import { tokens } from '../../theme';

/**
 * The credit pipeline as one readable strip — CAM → CCR → Sanction → CP, forking to
 * Disbursement ∥ CP/CS. Box borders carry the state: green done, blue in progress, red
 * where a checker said no, grey not reached. States and notes arrive computed from the
 * plane, off the same facts that gate the action buttons below — this component decides
 * nothing, so the strip and the buttons can never tell two different stories.
 */

const EDGE: Record<PipelineStep['state'], string> = {
  done: tokens.ok, active: tokens.lend, rejected: tokens.bad, pending: tokens.line,
};

function StepBox({ s }: { s: PipelineStep }) {
  const pending = s.state === 'pending';
  return (
    <Tooltip title={s.note || ''} placement="top" arrow>
      <Box sx={{
        border: `2px solid ${EDGE[s.state]}`, borderRadius: '7px', px: 0.9, py: 0.1,
        fontSize: 11.5, fontWeight: 700, lineHeight: '18px', whiteSpace: 'nowrap',
        color: pending ? tokens.muted : EDGE[s.state],
        backgroundColor: s.state === 'active' ? '#EAF2F8'
          : s.state === 'rejected' ? tokens.badBg : tokens.card,
      }}>
        {s.label}
      </Box>
    </Tooltip>
  );
}

const Arrow = () => (
  <Typography component="span" sx={{ color: tokens.muted, fontSize: 12, lineHeight: 1 }}>
    →
  </Typography>
);

export default function PipelineStepper({ steps }: { steps: PipelineStep[] }) {
  if (!steps?.length) return null;
  const line = steps.filter((s) => !s.parallel);
  const fork = steps.filter((s) => s.parallel);
  return (
    <Stack direction="row" spacing={0.7} alignItems="center" flexWrap="wrap" useFlexGap
      sx={{ mb: 1 }}>
      {line.map((s, i) => (
        <Stack key={s.key} direction="row" spacing={0.7} alignItems="center">
          {i > 0 && <Arrow />}
          <StepBox s={s} />
        </Stack>
      ))}
      {fork.length > 0 && (
        <>
          <Arrow />
          {/* After CP the journey forks: the money and the CS chase run side by side. */}
          <Stack spacing={0.5}>
            {fork.map((s) => <StepBox key={s.key} s={s} />)}
          </Stack>
        </>
      )}
    </Stack>
  );
}
