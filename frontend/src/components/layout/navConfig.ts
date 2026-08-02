export interface NavItem { tab: string; label: string; path: string; icon: string; }
// Order mirrors the template's resequenced tabs (business first, masters last).
// icon values mirror the template's AUGMENT 8 tab-icon map.
// v15 menu diet — 13 tabs collapse to 10: Clients + FI Master + Employees group
// under 🗂 Masters, and Activity Log + Audit under 🕘 Activity (sub-switch inside).
export const NAV: NavItem[] = [
  { tab: 'today', label: 'Today', path: '/today', icon: '⚡' },
  { tab: 'dash', label: 'Dashboard', path: '/dashboard', icon: '📊' },
  { tab: 'leads', label: 'Leads', path: '/leads', icon: '🧲' },
  { tab: 'deals', label: 'Deals', path: '/deals', icon: '🤝' },
  { tab: 'lend', label: 'Lending', path: '/lending', icon: '🏦' },
  { tab: 'syn', label: 'Platform Deals', path: '/syndication', icon: '🔗' },
  { tab: 'am', label: 'Asset Monetisation', path: '/asset-monetisation', icon: '♻️' },
  { tab: 'masters', label: 'Masters', path: '/masters', icon: '🗂️' },
  { tab: 'act', label: 'Activity', path: '/activity-hub', icon: '🕘' },
  { tab: 'tools', label: 'Tools', path: '/tools', icon: '🧰' },
];
