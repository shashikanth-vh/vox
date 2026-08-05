import { useMemo, useState } from 'react';
import { Alert, Box, Typography } from '@mui/material';
import SubTabs from '../../../components/common/SubTabs';
import { useQuery } from '@tanstack/react-query';
import { lendingService } from '../../../services/lendingService';
import { useAuth } from '../../../auth/AuthContext';
import { scopeFor } from '../../../auth/rbac';
import { tokens } from '../../../theme';
import type { LendingRow } from '../lending.types';
import AccountsTab from './AccountsTab';
import CovenantsTab from './CovenantsTab';

/**
 * LMS · Servicing — everything AFTER the first disbursement, over the accounts the
 * register opened automatically at T1. Four tabs: Accounts (the statement ledgers),
 * Covenants (the compliance chase, live until closure), and the two pillars that
 * grow into their own screens later — Collections and EWS — held as placeholders so
 * their future home is already on the map.
 */

const Placeholder = ({ title, body }: { title: string; body: string }) => (
  <Box sx={{ border: `1px dashed ${tokens.line}`, borderRadius: 1, p: 3, mt: 1.5,
    textAlign: 'center' }}>
    <Typography sx={{ fontSize: 14, fontWeight: 600, mb: 0.5 }}>{title}</Typography>
    <Typography sx={{ fontSize: 12.5, color: tokens.muted, maxWidth: 560, mx: 'auto' }}>
      {body}
    </Typography>
  </Box>
);

export default function LmsView() {
  const { user } = useAuth();
  const [tab, setTab] = useState('accounts');

  // One list feeds every tab: the lending lines this user may see (same scope rules as
  // LOS). Servicing shows the DISBURSED family — the lines that can hold an account.
  const { data, error } = useQuery({
    queryKey: ['lending', 'lms'],
    queryFn: () => lendingService.list(
      { pageIndex: 0, pageSize: 200 } as any, scopeFor(user.roles, 'lend', user.name)),
  });
  const rows: LendingRow[] = useMemo(() => {
    const all: LendingRow[] = (data as any)?.rows ?? (Array.isArray(data) ? data : []);
    return all.filter((r) => ['Disbursed', 'Ready for Disbursement', 'CP/CS Completed']
      .includes(r.stage));
  }, [data]);

  return (
    <Box>
      <SubTabs value={tab} onChange={setTab} items={[
        { id: 'accounts', label: 'Accounts', icon: '📒' },
        { id: 'covenants', label: 'Covenants', icon: '📑' },
        { id: 'collections', label: 'Collections', icon: '💰' },
        { id: 'ews', label: 'EWS', icon: '🚨' },
      ]} />
      {!!error && <Alert severity="warning" sx={{ fontSize: 12.5, py: 0.2 }}>
        {(error as any)?.message || 'The lending list could not be read.'}</Alert>}

      {tab === 'accounts' && <AccountsTab rows={rows} />}
      {tab === 'covenants' && <CovenantsTab rows={rows} />}
      {tab === 'collections' && <Placeholder title="Collections"
        body={'Overdue positions, DPD buckets and recovery follow-up graduate here. '
          + 'Until then the overdue position lives on each account (Accounts tab), '
          + 'set by the LMS Authorizer.'} />}
      {tab === 'ews' && <Placeholder title="EWS — Early Warning Signals"
        body={'Cases open AUTOMATICALLY today: a covenant breach or an expired waiver '
          + 'raises an EWS case in the same transaction, deduplicated at the database. '
          + 'This tab becomes the case worklist (triage, assignment, resolution).'} />}
    </Box>
  );
}
