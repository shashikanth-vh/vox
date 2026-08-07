import { db, today } from '../api/atlasStore';
import { writeAudit } from './auditService';
import { lendingService } from './lendingService';
import { syndicationService } from './syndicationService';
import { assetMonService } from './assetMonService';
import type { Role } from '../auth/rbac';

// Stage-change request (Copilot workflow). Non-approvers raise a request on a product
// line; Admin / Management / the relevant vertical Head approve or reject. Approval
// applies the actual stage change to the line.
export type StageLine = 'Lending' | 'Syndication' | 'Asset Monetisation';

export interface StageRequest {
  id: string;
  code: string;
  line: StageLine;
  refId?: string;          // the specific product-line row this targets
  currentStage: string;
  targetStage: string;
  reason: string;
  by: string;
  at: string;
  status: 'Pending' | 'Approved' | 'Rejected';
  decidedBy?: string;
  decidedAt?: string;
}

const bag = (): StageRequest[] => {
  const d = db() as any;
  d.stageRequests = d.stageRequests || [];
  return d.stageRequests as StageRequest[];
};

// Approve/reject rights: Admin + Management on any line; the vertical Head on their line
// (Operations sheet: "Admin, Mgmt, and relevant vertical Head can approve").
export function canApproveLine(roles: Role[], line: StageLine): boolean {
  if (roles.includes('Admin') || roles.includes('Management')) return true;
  if (line === 'Lending') return roles.includes('Credit Head');
  if (line === 'Syndication') return roles.includes('Syn Head');
  if (line === 'Asset Monetisation') return roles.includes('AM Head');
  return false;
}

// REQUEST rights are per line too: a requester asks about THEIR OWN desk's line, not
// another desk's — an AM RM browsing a company never requests a LENDING stage move
// (review feedback). Asset Monetisation has no request lane at all: the AM book is a
// plain update surface, its status a direct edit for AM roles.
const REQUEST_LINE_ROLES: Record<StageLine, Role[]> = {
  Lending: ['BD Head', 'BDRM', 'Deal Analyst'],
  Syndication: ['BD Head', 'BDRM', 'Syn RM'],
  'Asset Monetisation': [],
};
export function canRequestLine(roles: Role[], line: StageLine): boolean {
  return roles.some((r) => REQUEST_LINE_ROLES[line].includes(r));
}

export const stageRequestService = {
  all(): StageRequest[] { return [...bag()]; },
  pending(): StageRequest[] { return bag().filter((r) => r.status === 'Pending'); },
  forCode(code: string): StageRequest[] { return bag().filter((r) => r.code === code); },

  create(input: Omit<StageRequest, 'id' | 'by' | 'at' | 'status'>, by: string): StageRequest {
    const r: StageRequest = { ...input, id: 'SR' + Date.now(), by, at: today(), status: 'Pending' };
    bag().unshift(r);
    writeAudit(by, 'Stage-change requested', r.code, `${r.line}: ${r.currentStage || '—'} → ${r.targetStage}`);
    return r;
  },

  decide(id: string, approve: boolean, by: string) {
    const r = bag().find((x) => x.id === id);
    if (!r || r.status !== 'Pending') return;
    r.status = approve ? 'Approved' : 'Rejected';
    r.decidedBy = by; r.decidedAt = today();
    if (approve && r.refId) {
      if (r.line === 'Lending') lendingService.updateStage(r.refId, r.targetStage, by);
      else if (r.line === 'Syndication') syndicationService.update(r.refId, 'status', r.targetStage, by);
      else if (r.line === 'Asset Monetisation') assetMonService.update(r.refId, 'status', r.targetStage, by);
    }
    writeAudit(by, approve ? 'Stage-change approved' : 'Stage-change rejected', r.code, `${r.line} → ${r.targetStage}`);
  },
};
