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
          <SelectFld label="Line" required value={line} disabled={!!presetLine} onChange={(v) => { setLine(v as StageLine); setTarget(''); }} options={['Lending', 'Syndication', 'Asset Monetisation']} />
          <SelectFld label="Target stage" required value={target} onChange={setTarget} options={referenceService.getRefSync(LINE_STAGE_REF[line])} blank />
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
