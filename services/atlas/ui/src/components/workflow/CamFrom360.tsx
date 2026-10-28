import { useState } from 'react';
import { Alert, Button, CircularProgress, Menu, MenuItem, Snackbar, Tooltip } from '@mui/material';
import DescriptionIcon from '@mui/icons-material/Description';
import CamWorkbenchDialog from './CamWorkbenchDialog';
import { workflowActionsService, type WorkflowAction } from '../../services/workflowActionsService';

/**
 * Generate CAM from Company 360 — the same workbench the lending line's pipeline opens,
 * reached from the company brief so the analyst need not leave it.
 *
 * A CAM belongs to a lending line, so the button asks the workflow plane for that line's
 * `cam-workbench` step and honours its answer: a role or stage that may not prepare a
 * CAM gets the plane's own reason, never a back door around it. One live line opens
 * straight away; several drop a menu.
 */
export default function CamFrom360({ lines, entityId }: {
  /** The company's live lending lines: id and the name the desk knows it by. */
  lines: { id: string; label: string }[];
  entityId?: string | null;
}) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [open, setOpen] = useState<{ action: WorkflowAction; id: string } | null>(null);

  const openLine = async (id: string) => {
    setAnchor(null); setBusy(true);
    try {
      const d = await workflowActionsService.forSubject('Lending', id);
      const a = d.actions.find((x) => x.screen === 'cam-workbench');
      if (!a) setMsg({ text: 'The CAM workbench is not offered to your role on this line.', ok: false });
      else if (!a.enabled) setMsg({ text: a.reason || 'The CAM workbench is not available yet.', ok: false });
      else setOpen({ action: a, id });
    } catch (e: any) {
      setMsg({ text: e?.message || 'Could not reach the workflow plane.', ok: false });
    }
    setBusy(false);
  };

  const none = !lines.length;
  return (
    <>
      <Tooltip arrow title={none ? 'A CAM is written for a lending line — this company has no live one.' : ''}>
        <span>
          <Button size="small" variant="outlined" disabled={none || busy}
            startIcon={busy ? <CircularProgress size={14} /> : <DescriptionIcon />}
            onClick={(e) => (lines.length === 1 ? void openLine(lines[0].id) : setAnchor(e.currentTarget))}>
            Generate CAM
          </Button>
        </span>
      </Tooltip>
      <Menu open={!!anchor} anchorEl={anchor} onClose={() => setAnchor(null)}>
        {lines.map((l) => <MenuItem key={l.id} sx={{ fontSize: 13 }}
          onClick={() => void openLine(l.id)}>{l.label}</MenuItem>)}
      </Menu>
      <Snackbar open={!!msg} autoHideDuration={6000} onClose={() => setMsg(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity={msg?.ok ? 'success' : 'info'} onClose={() => setMsg(null)} sx={{ fontSize: 12.5 }}>
          {msg?.text}</Alert>
      </Snackbar>
      <CamWorkbenchDialog action={open?.action ?? null} subjectId={open?.id ?? ''}
        entityId={entityId || undefined}
        onClose={() => setOpen(null)}
        onDone={(m) => { setOpen(null); setMsg({ text: m, ok: true }); }} />
    </>
  );
}
