import { useState, useEffect } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, IconButton, Alert } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { FieldGrid, TextFld, SelectFld } from '../../components/common/Field';
import { CodeText } from '../../components/common/Pills';
import { referenceService } from '../../services/referenceService';
import { stageRequestService, type StageLine } from '../../services/stageRequestService';
import { useAuth } from '../../auth/AuthContext';
import { tokens } from '../../theme';

// Target-stage options per line (Stage-change sheet: "relevant stage list based on Line").
const LINE_STAGE_REF: Record<StageLine, string> = {
  Lending: 'Lending Stage', Syndication: 'Status of Proposal', 'Asset Monetisation': 'Asset Mon Status',
};

// Legal next stages per line and CURRENT stage — the client mirror of the register's
// transition graphs, so the request only OFFERS moves an approval can actually apply
// (the full vocabulary let an approver approve a jump the register then refused).
// Gated stages (e.g. Sanctioned needs committee evidence) may still be refused at
// approval time with the policy's own message — the graph is the first filter.
const LINE_NEXT: Partial<Record<StageLine, Record<string, string[]>>> = {
  Lending: {
    'Data Awaited': ['Diligence', 'On Hold', 'Rejected'],
    'Diligence': ['Note Circulated', 'Data Awaited', 'On Hold', 'Rejected'],
    'Note Circulated': ['Sanctioned', 'Diligence', 'On Hold', 'Rejected'],
    'Sanctioned': ['CP/CS Completed', 'Note Circulated', 'On Hold'],
    'CP/CS Completed': ['Ready for Disbursement', 'Sanctioned', 'On Hold'],
    'Ready for Disbursement': ['Disbursed', 'CP/CS Completed', 'On Hold'],
    'Disbursed': ['On Hold'],
    'On Hold': ['Data Awaited', 'Diligence', 'Note Circulated', 'Sanctioned',
      'CP/CS Completed', 'Ready for Disbursement', 'Disbursed'],
    'Rejected': ['Data Awaited', 'Diligence'],
  },
  Syndication: {
    'Deal Sourced': ['Docs Pending', 'On Hold', 'Withdrawn', 'Rejected', 'Dropped'],
    'Docs Pending': ['IM in Prep', 'Deal Sourced', 'On Hold', 'Withdrawn', 'Rejected', 'Dropped'],
    'IM in Prep': ['IM Circulated', 'Docs Pending', 'On Hold', 'Withdrawn', 'Rejected', 'Dropped'],
    'IM Circulated': ['Queries Received', 'IM in Prep', 'On Hold', 'Withdrawn', 'Rejected', 'Dropped'],
    'Queries Received': ['IP Received', 'IM Circulated', 'On Hold', 'Withdrawn', 'Rejected', 'Dropped'],
    'IP Received': ['Sanctioned', 'Queries Received', 'On Hold', 'Withdrawn', 'Rejected', 'Dropped'],
    'Sanctioned': ['Disbursed', 'On Hold'],
    'Disbursed': ['On Hold'],
    'On Hold': ['Deal Sourced', 'Docs Pending', 'IM in Prep', 'IM Circulated',
      'Queries Received', 'IP Received', 'Sanctioned'],
    'Withdrawn': [], 'Rejected': [], 'Dropped': [],
  },
};

export default function StageChangeDialog({ open, code, presetLine, refId, currentStage, onClose, onDone }: {
  open: boolean; code: string; presetLine?: StageLine; refId?: string; currentStage?: string; onClose: () => void; onDone: () => void;
}) {
  const { user } = useAuth();
  const [line, setLine] = useState<StageLine>(presetLine || 'Lending');
  const [target, setTarget] = useState('');
  const [reason, setReason] = useState('');
  const [err, setErr] = useState('');
  useEffect(() => { if (open) { setLine(presetLine || 'Lending'); setTarget(''); setReason(''); setErr(''); } }, [open, presetLine]);

  const submit = () => {
    // Forms spec (Stage-change): Line, Target stage and Reason are all MANDATORY.
    if (!target) { setErr('Target stage is required.'); return; }
    if (!reason.trim()) { setErr('Reason for change is required.'); return; }
    stageRequestService.create({ code, line, refId, currentStage: currentStage || '', targetStage: target, reason: reason.trim() }, user.full);
    onDone(); onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>Request stage change
        <Typography sx={{ fontSize: 11.6, color: tokens.muted }}>Group Code: <CodeText code={code} /></Typography>
        <IconButton onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <FieldGrid>
          {/* Asset Monetisation is deliberately absent: the AM book has no approval
              lane — its status is a direct edit for AM roles. */}
          <SelectFld label="Line" required value={line} disabled={!!presetLine} onChange={(v) => { setLine(v as StageLine); setTarget(''); }} options={['Lending', 'Syndication']} />
          <SelectFld label="Target stage" required value={target} onChange={setTarget}
            options={LINE_NEXT[line]?.[(currentStage || '').trim()]
              ?? referenceService.getRefSync(LINE_STAGE_REF[line])} blank />
        </FieldGrid>
        <Box sx={{ mt: 1.2 }}><TextFld label="Reason for change" required value={reason} onChange={setReason} multiline /></Box>
        {currentStage && <Typography sx={{ fontSize: 11.6, color: tokens.muted, mt: 1 }}>Current: <b>{currentStage}</b>. An approver (Admin, Management or the vertical Head) will review this request.</Typography>}
        {err && <Alert severity="warning" sx={{ mt: 1.2, py: 0, fontSize: 12 }}>{err}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined">Cancel</Button>
        <Button onClick={submit} variant="contained">Submit request</Button>
      </DialogActions>
    </Dialog>
  );
}
