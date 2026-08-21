// RBAC + view access — implemented from ATLAS_RBAC_v3.1.xlsx and ATLAS_Views_v2.1.xlsx.
//
// The two workbooks diverge on a handful of cells (the RBAC "View Access" sheet vs the
// dedicated Views workbook). Where they conflict we follow the Views v2.1 workbook and
// the Ownership Model, whose guiding principle is: "Write follows the vertical, read
// follows the deal." Operations + field-level gating come from the Operations / Field
// Rules sheets.
//
// In production this whole map is returned by the backend; referenceService.getRbac() is
// the seam.

export const ROLES = [
  'Admin', 'Management', 'BD Head', 'BDRM', 'Credit Head', 'Deal Analyst',
  'Syn Head', 'Syn RM', 'AM Head', 'AM RM',
  'LMS Operator', 'LMS Management',
] as const;
export type Role = typeof ROLES[number];

// Vertical each role belongs to (drives scoping + dashboard slice).
export const ROLE_VERTICAL: Record<Role, 'System' | 'All' | 'BD' | 'Credit' | 'Syndication' | 'AM' | 'Servicing'> = {
  Admin: 'System', Management: 'All', 'BD Head': 'BD', BDRM: 'BD',
  'Credit Head': 'Credit', 'Deal Analyst': 'Credit',
  'Syn Head': 'Syndication', 'Syn RM': 'Syndication', 'AM Head': 'AM', 'AM RM': 'AM',
  'LMS Operator': 'Servicing', 'LMS Management': 'Servicing',
};

// full = read+write, no scope limit · scoped = read+write on own/assigned rows only
// read = read-only · none = module hidden.
export type Access = 'full' | 'scoped' | 'read' | 'none';

const F: Access = 'full', S: Access = 'scoped', R: Access = 'read', N: Access = 'none';

// Columns are ROLES, in order. Rows are app tab ids.
// order: Admin, Management, BD Head, BDRM, Credit Head, Deal Analyst, Syn Head, Syn RM, AM Head, AM RM
// order: … AM RM, LMS Operator, LMS Management (the servicing pair live on Lending's
// LMS tab — scoped there, read-only visitors elsewhere they need context from).
const VIEW_ROWS: Record<string, Access[]> = {
  today:    [F, F, S, S, S, S, S, S, S, S, S, S],
  dash:     [F, F, S, N, S, N, S, N, S, N, N, N],
  // Firm-wide visibility (deployment policy, 2026-08): every desk SEES every module —
  // the access service seeds the live matrix to match (matrix.py VISIBILITY_READ), and
  // this row opens the Leads tab for the roles the spec left at none. Reading only:
  // every edit is still gated by the operations map + the register's ownership check.
  leads:    [F, F, F, S, R, R, R, R, R, R, N, N],
  deals:    [F, F, F, S, S, S, S, S, S, S, R, R],
  lend:     [F, F, R, R, F, S, R, R, R, R, R, R],
  syn:      [F, F, R, R, R, S, F, S, R, R, N, N],
  am:       [F, F, R, R, R, S, R, R, F, S, N, N],
  fi:       [F, F, R, R, R, R, F, R, R, R, N, N],
  // Firm-wide visibility: the client directory is readable by every desk — it is
  // the join source for every grid's company names.
  clients:  [F, F, R, R, R, R, R, R, R, R, R, R],
  emp:      [F, F, R, R, R, R, R, R, R, R, R, R],
  audit:    [F, N, N, N, N, N, N, N, N, N, N, N],
  activity: [F, N, N, N, N, N, N, N, N, N, N, N],
  tools:    [F, R, R, R, R, R, R, R, R, R, R, R],
};

export const VIEW_ACCESS: Record<string, Record<Role, Access>> = Object.fromEntries(
  Object.entries(VIEW_ROWS).map(([view, cells]) => [
    view, Object.fromEntries(ROLES.map((r, i) => [r, cells[i]])) as Record<Role, Access>,
  ]),
);

// Grouped nav tabs bundle several leaf views.
const GROUP: Record<string, string[]> = { masters: ['clients', 'fi', 'emp'], act: ['audit', 'activity'] };

// ---------------------------------------------------------------------------
// Role STACKING: a user may hold several roles at once. Every check below takes
// `Role | Role[]`; permissions are the UNION (the strongest role wins), per the RBAC
// spec ("when roles overlap, the higher role's permissions apply").
// ---------------------------------------------------------------------------
export type RoleInput = Role | Role[] | undefined;
// v3.7 rename table: a role string issued under its OLD name (a stored grant, a
// not-yet-migrated Access row) still resolves — a rename never strips access.
const ROLE_ALIASES: Record<string, Role> = { 'LMS Authorizer': 'LMS Management' };
const asRoles = (r: RoleInput): Role[] =>
  (r == null ? [] : Array.isArray(r) ? r : [r]).map((x) => ROLE_ALIASES[x as string] ?? x);

// Privilege ranking — used to pick the single "primary" role for labels/defaults.
export const ROLE_RANK: Record<Role, number> = {
  Admin: 100, Management: 90,
  'BD Head': 70, 'Credit Head': 70, 'Syn Head': 70, 'AM Head': 70, 'LMS Management': 70,
  BDRM: 40, 'Deal Analyst': 40, 'Syn RM': 40, 'AM RM': 40, 'LMS Operator': 40,
};
export function primaryRole(roles: Role[]): Role {
  return [...roles].sort((a, b) => ROLE_RANK[b] - ROLE_RANK[a])[0] || 'BDRM';
}

const ACCESS_ORDER: Access[] = ['none', 'read', 'scoped', 'full'];

function oneAccess(role: Role, tab: string): Access {
  if (GROUP[tab]) {
    const levels = GROUP[tab].map((t) => oneAccess(role, t));
    if (levels.includes('full')) return 'full';
    if (levels.includes('scoped')) return 'scoped';
    if (levels.includes('read')) return 'read';
    return 'none';
  }
  const row = VIEW_ACCESS[tab];
  return row ? row[role] : 'full'; // views not in the matrix are ungated
}

export function viewAccess(role: RoleInput, tab: string): Access {
  const roles = asRoles(role);
  if (!roles.length) return 'full';
  return roles.reduce<Access>((best, r) => {
    const a = oneAccess(r, tab);
    return ACCESS_ORDER.indexOf(a) > ACCESS_ORDER.indexOf(best) ? a : best;
  }, 'none');
}

export function canSee(role: RoleInput, tab: string): boolean {
  return viewAccess(role, tab) !== 'none';
}

// Coarse writability for a whole view (full/scoped can write; read/none cannot). Fine-
// grained edit rights use can(role, op) below.
export function canWrite(role: RoleInput, tab: string): boolean {
  const a = viewAccess(role, tab);
  return a === 'full' || a === 'scoped';
}

// ---------------------------------------------------------------------------
// Operations matrix — who can perform each action (Operations sheet, reconciled).
// ---------------------------------------------------------------------------
export type Op =
  | 'addLead' | 'editLead' | 'reassignLead' | 'pushToDeals' | 'createClient'
  | 'editDealProfile' | 'editDealOwnership' | 'addProduct'
  | 'assignAnalystLending' | 'assignAnalystSyn' | 'assignAnalystAM'
  | 'changeLendingStage' | 'editLending'
  | 'editSyn' | 'addLender' | 'logChase' | 'logResponse' | 'advanceMatrix'
  | 'editAM' | 'addNote' | 'logInteraction'
  | 'editFI' | 'editEmployee' | 'addEmployee' | 'uploadDocs' | 'snoozeToday'
  | 'deleteRow' | 'requestStageChange' | 'approveRequest'
  | 'exportCsv' | 'backupRestore' | 'newsScan'
  | 'lmsOperate' | 'lmsAuthorize';

const ALL: Role[] = [...ROLES];

const OPS: Record<Op, Role[]> = {
  addLead:            ['Admin', 'Management', 'BD Head', 'BDRM'],
  editLead:           ['Admin', 'Management', 'BD Head', 'BDRM'],
  reassignLead:       ['Admin', 'Management', 'BD Head'],
  pushToDeals:        ['Admin', 'Management', 'BD Head', 'BDRM'],
  createClient:       ['Admin', 'Management', 'BD Head', 'BDRM'],
  editDealProfile:    ['Admin', 'Management', 'BD Head', 'BDRM'],
  editDealOwnership:  ['Admin', 'Management', 'BD Head'],
  addProduct:         ['Admin', 'Management', 'BD Head', 'BDRM', 'Credit Head', 'Syn Head', 'AM Head'],
  assignAnalystLending: ['Admin', 'Management', 'Credit Head'],
  assignAnalystSyn:   ['Admin', 'Management', 'Credit Head'],
  assignAnalystAM:    ['Admin', 'Management', 'Credit Head'],
  changeLendingStage: ['Admin', 'Management', 'Credit Head', 'Deal Analyst'],
  editLending:        ['Admin', 'Management', 'Credit Head', 'Deal Analyst'],
  editSyn:            ['Admin', 'Management', 'Syn Head', 'Syn RM', 'Deal Analyst'],
  addLender:          ['Admin', 'Management', 'Syn Head', 'Syn RM'],
  logChase:           ['Admin', 'Management', 'Syn Head', 'Syn RM'],
  logResponse:        ['Admin', 'Management', 'Syn Head', 'Syn RM'],
  advanceMatrix:      ['Admin', 'Management', 'Syn Head', 'Syn RM'],
  editAM:             ['Admin', 'Management', 'AM Head', 'AM RM', 'Deal Analyst'],
  addNote:            ALL,
  logInteraction:     ALL,
  editFI:             ['Admin', 'Management', 'Syn Head'],
  editEmployee:       ['Admin', 'Management'],
  addEmployee:        ['Admin', 'Management'],
  uploadDocs:         ALL,
  snoozeToday:        ALL,
  deleteRow:          ['Admin'],
  requestStageChange: ['BD Head', 'BDRM', 'Deal Analyst', 'Syn RM', 'AM RM'],
  approveRequest:     ['Admin', 'Management', 'BD Head', 'Credit Head', 'Syn Head', 'AM Head'],
  exportCsv:          ALL,
  backupRestore:      ['Admin'],
  newsScan:           ALL,
  lmsOperate:         ['Admin', 'Management', 'Credit Head', 'Deal Analyst', 'LMS Operator', 'LMS Management'],
  // v3.8: booking approval is the SERVICING desk's check — the credit desk records
  // the attestation but never settles it (Admin/Management keep the override).
  lmsAuthorize:       ['Admin', 'Management', 'LMS Management'],
};

/**
 * Who may do this, in words — for a control that is offered but not available to you.
 *
 * A greyed control with no explanation is the same failure as a hidden one: the user
 * cannot tell whether the platform is broken, their data is wrong, or they simply are not
 * the right person. Naming the roles answers it in one line, and matches how the workflow
 * Actions panel explains a step it cannot offer.
 */
export function whoCan(op: Op): string {
  const roles = OPS[op] || [];
  return roles.length ? `This is done by ${roles.join(', ')}.` : 'Not available to any role.';
}

export function can(role: RoleInput, op: Op): boolean {
  return asRoles(role).some((r) => OPS[op].includes(r));
}

// ---------------------------------------------------------------------------
// Row-level scoping — "read follows the deal, write follows the vertical."
// Returns null when the role sees all rows for a view (full/read/none); otherwise the
// owner fields + names to match a row against. Applied in services before paging.
// ---------------------------------------------------------------------------
export interface RowScope { fields: string[]; names: string[]; }

const SCOPE_FIELDS: Record<string, string[]> = {
  leads: ['rm'], deals: ['rm', 'an'], lend: ['an', 'rm'], syn: ['rm', 'an'],
  am: ['rm', 'an'], today: ['rm', 'an'],
};

export function scopeFor(role: RoleInput, view: string, userName: string): RowScope | null {
  // full/read (from ANY held role) → sees all rows; only pure-scoped access filters.
  if (viewAccess(role, view) !== 'scoped') return null;
  return { fields: SCOPE_FIELDS[view] || ['rm'], names: [userName] };
}

/**
 * Does a single row fall inside a scope? (fields OR-matched against names.)
 *
 * MOCK MODE ONLY — see the services, which apply this to the bundled store and never to
 * an API response. On the platform the register is the authority on scope, and its rule
 * is far wider than this one: a row is yours through an assignment, through any line of
 * the same company, because YOU CREATED IT, because it belongs to someone who reports to
 * you, or because you are the vertical Head of an unassigned line. Re-applying this
 * name-equality test on top of that answer silently threw rows away — an RM's own new
 * lead vanished from her Leads tab because the `rm` field held a different spelling of
 * her name than her sign-in display name.
 */
export function inScope(scope: RowScope | null, row: any): boolean {
  if (!scope) return true;
  return scope.fields.some((f) => scope.names.includes((row?.[f] ?? '').toString()));
}

// ---------------------------------------------------------------------------
// Derived tab list per role (grouped tabs included) — kept for referenceService.getRbac
// and any legacy callers.
// ---------------------------------------------------------------------------
export const ROLE_TABS: Record<Role, string[]> = Object.fromEntries(
  ROLES.map((role) => {
    const leaves = Object.keys(VIEW_ROWS).filter((t) => canSee(role, t));
    const groups = Object.keys(GROUP).filter((g) => canSee(role, g));
    return [role, [...leaves, ...groups]];
  }),
) as Record<Role, string[]>;
