import { Navigate } from 'react-router-dom';
import { useAuth } from './AuthContext';
import { canSee } from './rbac';

export default function RoleGuard({ tab, children }: { tab: string; children: React.ReactNode }) {
  const { user } = useAuth();
  if (!canSee(user.roles, tab)) return <Navigate to="/today" replace />;
  return <>{children}</>;
}
