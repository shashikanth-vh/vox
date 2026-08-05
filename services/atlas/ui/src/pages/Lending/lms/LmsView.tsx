import { useMemo } from 'react';
import { Alert, Box } from '@mui/material';
import { useQuery } from '@tanstack/react-query';
import { lendingService } from '../../../services/lendingService';
import { useAuth } from '../../../auth/AuthContext';
import { scopeFor } from '../../../auth/rbac';
import type { LendingRow } from '../lending.types';
import AccountsTab from './AccountsTab';

/**
 * LMS · Servicing — the same shape as LOS: ONE table (the serviced book, a row per
 * loan account) and a drawer that carries the loan's whole post-disbursement life —
 * disbursement schedule, statement ledger, conditions register, covenant compliance,
 * classification & closure. Collections and EWS graduate into their own screens when
 * they grow past what the drawer holds.
 */
export default function LmsView() {
  const { user } = useAuth();

  const { data, error } = useQuery({
    queryKey: ['lending', 'lms'],
    queryFn: () => lendingService.list(
      { pageIndex: 0, pageSize: 200 } as any, scopeFor(user.roles, 'lend', user.name)),
  });
  // WHOSE horizon: a user who is ONLY servicing (LMS roles and nothing else) starts
  // at the seam — a line appears once a booking can arrive. Everyone who also works
  // origination (Admin, Management, the credit desk) keeps the fuller pipeline view,
  // CP/CS Completed included, because for them LOS and LMS are one journey.
  const lmsOnly = user.roles.length > 0 &&
    user.roles.every((r) => r === 'LMS Operator' || r === 'LMS Authorizer');
  const rows: LendingRow[] = useMemo(() => {
    const all: LendingRow[] = (data as any)?.rows ?? (Array.isArray(data) ? data : []);
    const stages = lmsOnly
      ? ['Disbursed', 'Ready for Disbursement']
      : ['Disbursed', 'Ready for Disbursement', 'CP/CS Completed'];
    return all.filter((r) => stages.includes(r.stage));
  }, [data, lmsOnly]);

  return (
    <Box>
      {!!error && <Alert severity="warning" sx={{ fontSize: 12.5, py: 0.2 }}>
        {(error as any)?.message || 'The lending list could not be read.'}</Alert>}
      <AccountsTab rows={rows} />
    </Box>
  );
}
