import { useEffect } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import AppLayout from './components/layout/AppLayout';
import RoleGuard from './auth/RoleGuard';
import { useAuth } from './auth/AuthContext';
import Login from './pages/Login/LoginPage';
import Dashboard from './pages/Dashboard/DashboardPage';
import Today from './pages/Today/TodayPage';
import Leads from './pages/Leads/LeadsPage';
import Deals from './pages/Deals/DealsPage';
import Lending from './pages/Lending/LendingPage';
import Syndication from './pages/Syndication/SyndicationPage';
import AssetMon from './pages/AssetMonetisation/AssetMonPage';
import Clients from './pages/Clients/ClientsPage';
import Employees from './pages/Employees/EmployeesPage';
import FIMaster from './pages/FIMaster/FIMasterPage';
import Audit from './pages/Audit/AuditPage';
import Activity from './pages/Activity/ActivityPage';
import Masters from './pages/Masters/MastersPage';
import ActivityHub from './pages/Masters/ActivityHubPage';
import Tools from './pages/Tools/ToolsPage';
import { employeesService } from './services/employeesService';
import { referenceService } from './services/referenceService';

const routes: [string, string, React.ReactNode][] = [
  ['today', '/today', <Today />],
  ['leads', '/leads', <Leads />],
  ['deals', '/deals', <Deals />],
  ['lend', '/lending', <Lending />],
  ['syn', '/syndication', <Syndication />],
  ['am', '/asset-monetisation', <AssetMon />],
  // v15 grouped tabs (each with an inner sub-switch).
  ['masters', '/masters', <Masters />],
  ['act', '/activity-hub', <ActivityHub />],
  // Individual routes kept for direct/deep links (not in the top nav anymore).
  ['fi', '/fi-master', <FIMaster />],
  ['clients', '/clients', <Clients />],
  ['emp', '/employees', <Employees />],
  ['audit', '/audit', <Audit />],
  ['activity', '/activity', <Activity />],
  ['tools', '/tools', <Tools />],
];

export default function App() {
  const { authed } = useAuth();
  // Every dropdown in the app comes from the Register, not from the bundled seed:
  // /v1/ref for the vocabularies (and the role-driven name lists it derives from the
  // people directory), then the people roster itself for the directory lookups. In that
  // order — the roster only fills in name lists /v1/ref did not serve. Once per
  // signed-in session, and fail-soft: an unreachable Register keeps the seeded lists
  // rather than leaving the user with forms they cannot fill in.
  useEffect(() => {
    if (!authed) return;
    void referenceService.hydrate().then(() => employeesService.hydrateRoster());
  }, [authed]);
  if (!authed) return <Login />;
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Navigate to="/today" replace />} />
        {/* Dashboard is gated on 'dash': roles without it (e.g. RM) are redirected to
            Today — including when the active role is switched while viewing it. */}
        <Route path="/dashboard" element={<RoleGuard tab="dash"><Dashboard /></RoleGuard>} />
        {routes.map(([tab, path, el]) => (
          <Route key={path} path={path} element={<RoleGuard tab={tab}>{el}</RoleGuard>} />
        ))}
        <Route path="*" element={<Navigate to="/today" replace />} />
      </Route>
    </Routes>
  );
}
