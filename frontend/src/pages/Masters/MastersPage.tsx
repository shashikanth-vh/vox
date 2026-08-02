import { useState } from 'react';
import { ToggleButtonGroup, ToggleButton } from '@mui/material';
import TableRowsIcon from '@mui/icons-material/TableRows';
import GridViewIcon from '@mui/icons-material/GridView';
import SubTabs, { type SubTab } from '../../components/common/SubTabs';
import ClientsPage from '../Clients/ClientsPage';
import FIMasterPage from '../FIMaster/FIMasterPage';
import EmployeesPage from '../Employees/EmployeesPage';
import { useAuth } from '../../auth/AuthContext';
import { canSee } from '../../auth/rbac';

// v15 🗂 Masters — Clients + FI Master + Employees behind an inner sub-switch. Only
// the sub-tabs the role can see are shown; RBAC mirrors the old individual tabs.
const SUBS: (SubTab & { tab: string })[] = [
  { id: 'clients', label: 'Clients', icon: '👥', tab: 'clients' },
  { id: 'fi', label: 'FI Master', icon: '🏦', tab: 'fi' },
  { id: 'emp', label: 'Employees', icon: '🧑‍💼', tab: 'emp' },
];

export default function MastersPage() {
  const { user } = useAuth();
  const visible = SUBS.filter((s) => canSee(user.roles, s.tab));
  const [sub, setSub] = useState(visible[0]?.id ?? 'clients');
  const active = visible.some((s) => s.id === sub) ? sub : visible[0]?.id;
  // The view toggle lives on the sub-tab line and drives FI Master / Employees.
  // (Clients has only a table, so no toggle there.) Reset to table on sub change.
  const [mode, setMode] = useState<'table' | 'cards'>('table');
  const changeSub = (id: string) => { setSub(id); setMode('table'); };
  const hasToggle = active === 'fi' || active === 'emp';

  const toggle = hasToggle ? (
    <ToggleButtonGroup exclusive size="small" value={mode} onChange={(_, v) => v && setMode(v)}>
      <ToggleButton value="table"><TableRowsIcon fontSize="small" sx={{ mr: 0.5 }} />Table view</ToggleButton>
      <ToggleButton value="cards"><GridViewIcon fontSize="small" sx={{ mr: 0.5 }} />Card view</ToggleButton>
    </ToggleButtonGroup>
  ) : null;

  return (
    <>
      <SubTabs items={visible} value={active} onChange={changeSub} right={toggle} />
      {active === 'clients' && <ClientsPage />}
      {active === 'fi' && <FIMasterPage mode={mode} onModeChange={setMode} />}
      {active === 'emp' && <EmployeesPage mode={mode} onModeChange={setMode} />}
    </>
  );
}
