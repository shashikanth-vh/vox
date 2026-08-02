import { useState } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, LinearProgress } from '@mui/material';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import { REQ_DOCS, documentsService } from '../../services/documentsService';
import type { DocEntry } from '../../services/documentsService';
import { clientsService } from '../../services/clientsService';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { tokens } from '../../theme';

const fmtKB = (b: number) => (b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB');

export default function DataRegisterDialog({ code, open, onClose }: { code: string; open: boolean; onClose: () => void }) {
  const { user } = useAuth();
  const ro = !can(user.roles, 'uploadDocs');
  const [, force] = useState(0);
  const bump = () => force((n) => n + 1);
  const c = clientsService.get(code);

  const upload = (sec: string, dk: string, label: string, sectionTitle: string, file?: File) => {
    if (!file) return;
    const entry: DocEntry = { name: file.name, size: file.size, type: file.type || 'file', when: new Date().toISOString().slice(0, 16).replace('T', ' '), by: user.full, label };
    const done = () => { documentsService.put(code, sec, dk, entry, user.full, sectionTitle); bump(); };
    if (file.size <= documentsService.maxInline) {
      const r = new FileReader();
      r.onload = () => { entry.data = String(r.result); entry.inline = true; done(); };
      r.onerror = () => { entry.inline = false; done(); };
      r.readAsDataURL(file);
    } else { entry.inline = false; done(); }
  };
  const view = (e: DocEntry) => {
    if (!e.data) return;
    const a = document.createElement('a'); a.href = e.data; a.download = e.name; document.body.appendChild(a); a.click(); a.remove();
  };
  const remove = (sec: string, dk: string, sectionTitle: string, e: DocEntry) => {
    if (!confirm(`Remove ${e.label} (${e.name})?`)) return;
    documentsService.remove(code, sec, dk, user.full, sectionTitle); bump();
  };

  let totReq = 0, gotReq = 0;
  REQ_DOCS.forEach((s) => s.d.forEach((dd) => { if (dd.req) { totReq++; if (documentsService.get(code, s.k, dd.k)) gotReq++; } }));
  const pct = totReq ? Math.round((gotReq / totReq) * 100) : 0;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>📁 Data Register — {c.name}</DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 12.6, color: tokens.muted, mb: 0.75 }}><b>{gotReq} of {totReq}</b> required documents on file · {pct}% complete</Typography>
        <LinearProgress variant="determinate" value={pct} sx={{ height: 8, borderRadius: 99, mb: 1.5, '& .MuiLinearProgress-bar': { bgcolor: tokens.tealHi } }} />
        <Typography sx={{ fontSize: 11.6, color: tokens.muted, mb: 1.5 }}>
          Upload the documents required under each section. Files up to 400&nbsp;KB stay viewable in this session; larger files are recorded (name, size, owner, time).
        </Typography>

        {REQ_DOCS.map((s) => {
          const sStore = documentsService.section(code, s.k);
          const reqN = s.d.filter((d) => d.req).length;
          const reqGot = s.d.filter((d) => d.req && sStore[d.k]).length;
          return (
            <Box key={s.k} sx={{ border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.4, mb: 1.4 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.5 }}>
                <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.muted, fontWeight: 700, flex: 1 }}>{s.t}</Typography>
                <Typography sx={{ fontSize: 11, fontWeight: 700, color: reqGot >= reqN ? tokens.ok : tokens.warn }}>{reqGot}/{reqN} required</Typography>
              </Box>
              {s.d.map((dd) => {
                const e = sStore[dd.k];
                return (
                  <Box key={dd.k} sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.75, borderBottom: `1px dashed ${tokens.line}`, flexWrap: 'wrap' }}>
                    <Box sx={{ width: 20, height: 20, borderRadius: '50%', border: `1.6px solid ${e ? tokens.ok : tokens.line}`, bgcolor: e ? tokens.ok : 'transparent',
                      color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, flexShrink: 0 }}>{e ? '✓' : ''}</Box>
                    <Box sx={{ flex: 1, minWidth: 160 }}>
                      <Typography sx={{ fontSize: 13 }}>{dd.n}{dd.req ? <Box component="span" sx={{ ml: 0.75, fontSize: 10, fontWeight: 700, color: tokens.warn, border: `1px solid ${tokens.warn}`, borderRadius: 99, px: 0.75 }}>required</Box> : null}</Typography>
                      {e && <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>{e.name} · {fmtKB(e.size)} · {e.by} · {e.when}</Typography>}
                    </Box>
                    <Box sx={{ display: 'flex', gap: 0.75 }}>
                      {e?.inline && <Button size="small" variant="outlined" onClick={() => view(e)}>View</Button>}
                      {!ro && (
                        <Button size="small" variant={e ? 'outlined' : 'contained'} component="label" startIcon={!e ? <UploadFileIcon /> : undefined}>
                          {e ? 'Replace' : 'Upload'}
                          <input type="file" hidden onChange={(ev) => upload(s.k, dd.k, dd.n, s.t, ev.target.files?.[0])} />
                        </Button>
                      )}
                      {e && !ro && <Button size="small" color="error" onClick={() => remove(s.k, dd.k, s.t, e)}>Remove</Button>}
                    </Box>
                  </Box>
                );
              })}
            </Box>
          );
        })}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined">Done</Button>
      </DialogActions>
    </Dialog>
  );
}
