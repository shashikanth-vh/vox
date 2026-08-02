import { useState } from 'react';
import SubTabs, { type SubTab } from '../../components/common/SubTabs';
import ActivityPage from '../Activity/ActivityPage';
import AuditPage from '../Audit/AuditPage';
import { useAuth } from '../../auth/AuthContext';
import { canSee } from '../../auth/rbac';

// v15 🕘 Activity — Activity Log + Audit trail behind an inner sub-switch.
const SUBS: (SubTab & { tab: string })[] = [
  { id: 'activity', label: 'Activity Log', icon: '🕘', tab: 'activity' },
  { id: 'audit', label: 'Audit trail', icon: '📜', tab: 'audit' },
];

export default function ActivityHubPage() {
  const { user } = useAuth();
  const visible = SUBS.filter((s) => canSee(user.roles, s.tab));
  const [sub, setSub] = useState(visible[0]?.id ?? 'activity');
  const active = visible.some((s) => s.id === sub) ? sub : visible[0]?.id;

  return (
    <>
      <SubTabs items={visible} value={active} onChange={setSub} />
      {active === 'activity' && <ActivityPage />}
      {active === 'audit' && <AuditPage />}
    </>
  );
}
