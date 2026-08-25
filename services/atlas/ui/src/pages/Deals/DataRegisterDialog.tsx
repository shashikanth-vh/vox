import { useCallback, useEffect, useState } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, LinearProgress, Alert, Chip, TextField } from '@mui/material';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import { REQ_DOCS, documentsService } from '../../services/documentsService';
import type { DocEntry, DocIndex } from '../../services/documentsService';
import { clientsService } from '../../services/clientsService';
import { api, errText } from '../../api/http';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { tokens } from '../../theme';

const fmtKB = (b: number) => (b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB');

export default function DataRegisterDialog({ code, entityId: entityIdProp, open, onClose }: {
  code: string; entityId?: string; open: boolean; onClose: () => void }) {
  const { user } = useAuth();
  const ro = !can(user.roles, 'uploadDocs');
  const c = clientsService.get(code);
  // The register keys documents by the COMPANY, so the entity id is what addresses them.
  // A caller that already knows it (the approval review) passes it in — the local client
  // cache only covers companies the clients screen has loaded.
  const entityId: string | undefined = entityIdProp || (c as any)?.entityId;
  const [docs, setDocs] = useState<DocIndex>({});
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');
  // "+ Add another document": the checklist names the REQUIRED set, but a live
  // deal always carries papers the list never predicted — name it, upload it,
  // it lives in the same section with the same verify/remove lifecycle.
  const [addingSec, setAddingSec] = useState('');
  const [newDocName, setNewDocName] = useState('');

  const reload = useCallback(() => {
    documentsService.load(code, entityId).then(setDocs).catch(() => setDocs({}));
  }, [code, entityId]);
  useEffect(() => { if (open) { setErr(''); reload(); } }, [open, reload]);

  const upload = async (sec: string, dk: string, label: string, sectionTitle: string,
                        required: boolean, file?: File) => {
    if (!file) return;
    setErr(''); setBusy(sec + dk);
    const r = await documentsService.upload({ code, entityId, section: sec, slotKey: dk,
      title: label, required, file, by: user.full, sectionTitle });
    setBusy('');
    if (!r.ok) { setErr(r.error || 'The upload was refused.'); return; }
    reload();
  };
  const verify = async (e: DocEntry) => {
    if (!e.id) return;
    setErr(''); setBusy(e.id);
    const r = await documentsService.validate(e.id, '', user.full, code);
    setBusy('');
    if (!r.ok) { setErr(r.error || 'The verification was refused.'); return; }
    reload();
  };
  const remove = async (sec: string, dk: string, sectionTitle: string, e: DocEntry) => {
    if (!confirm(`Remove ${e.label} (${e.name})?`)) return;
    setErr('');
    const r = await documentsService.remove(code, sec, dk, e, user.full, sectionTitle);
    if (!r.ok) { setErr(r.error || 'The removal was refused.'); return; }
    reload();
  };

  // "Entity … not found" here means the COMPANY ROW was deleted while its book lived
  // on (possible before the register's referential guard existed). Soft-deleted rows
  // are restorable, and this id is exactly the row to bring back.
  const senior = user.roles.includes('Admin') || user.roles.includes('Management');
  const entityGone = /entity .* not found/i.test(err);
  const restore = async () => {
    if (!entityId) return;
    setBusy('restore'); setErr('');
    try {
      await api.post(`/entities/${entityId}/restore`);
      reload();
    } catch (e: any) {
      setErr(errText(e?.response?.data) || 'The restore was refused.');
    }
    setBusy('');
  };

  let totReq = 0, gotReq = 0;
  REQ_DOCS.forEach((s) => s.d.forEach((dd) => { if (dd.req) { totReq++; if (docs[s.k]?.[dd.k]) gotReq++; } }));
  const pct = totReq ? Math.round((gotReq / totReq) * 100) : 0;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontSize: 16 }}>📁 Data Register — {c.name}</DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: 12.6, color: tokens.muted, mb: 0.75 }}><b>{gotReq} of {totReq}</b> required documents on file · {pct}% complete</Typography>
        <LinearProgress variant="determinate" value={pct} sx={{ height: 8, borderRadius: 99, mb: 1.5, '& .MuiLinearProgress-bar': { bgcolor: tokens.tealHi } }} />
        <Typography sx={{ fontSize: 11.6, color: tokens.muted, mb: 1.5 }}>
          Upload the documents required under each section. Files are stored on the register and
          can be downloaded again. <b>Verify</b> is the second pair of eyes — the register refuses
          a verification by whoever uploaded the file.
        </Typography>
        {!entityId && (
          <Alert severity="info" sx={{ mb: 1.2, py: 0, fontSize: 12 }}>
            This company is not linked to a register record yet, so uploads stay in this session only.
          </Alert>
        )}
        {err && (
          <Alert severity="warning" sx={{ mb: 1.2, py: 0, fontSize: 12 }}
            onClose={() => setErr('')}
            action={entityGone && senior && entityId ? (
              <Button size="small" color="inherit" disabled={busy === 'restore'}
                onClick={() => void restore()} sx={{ textTransform: 'none' }}>
                {busy === 'restore' ? 'Restoring…' : 'Restore company row'}
              </Button>
            ) : undefined}>
            {err}{entityGone && (
              ' — the company\'s register row was deleted while this line still points '
              + 'at it. An Admin can restore it in place.')}
          </Alert>
        )}

        {REQ_DOCS.map((s) => {
          const sStore = docs[s.k] || {};
          const reqN = s.d.filter((d) => d.req).length;
          const reqGot = s.d.filter((d) => d.req && sStore[d.k]).length;
          return (
            <Box key={s.k} sx={{ border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.4, mb: 1.4 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.5 }}>
                <Typography sx={{ fontSize: 10.6, textTransform: 'uppercase', letterSpacing: '.8px', color: tokens.muted, fontWeight: 700, flex: 1 }}>{s.t}</Typography>
                <Typography sx={{ fontSize: 11, fontWeight: 700, color: reqGot >= reqN ? tokens.ok : tokens.warn }}>{reqGot}/{reqN} required</Typography>
              </Box>
              {[...s.d,
                // ad-hoc documents already stored under this section — every key
                // the fixed checklist does not claim renders as its own row
                ...Object.entries(sStore)
                  .filter(([k]) => !s.d.some((dd) => dd.k === k))
                  .map(([k, e]) => ({ k, n: (e as DocEntry).label || (e as DocEntry).name, req: 0 })),
              ].map((dd) => {
                const e = sStore[dd.k];
                return (
                  <Box key={dd.k} sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.75, borderBottom: `1px dashed ${tokens.line}`, flexWrap: 'wrap' }}>
                    <Box sx={{ width: 20, height: 20, borderRadius: '50%', border: `1.6px solid ${e ? tokens.ok : tokens.line}`, bgcolor: e ? tokens.ok : 'transparent',
                      color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, flexShrink: 0 }}>{e ? '✓' : ''}</Box>
                    <Box sx={{ flex: 1, minWidth: 160 }}>
                      <Typography sx={{ fontSize: 13 }}>{dd.n}{dd.req ? <Box component="span" sx={{ ml: 0.75, fontSize: 10, fontWeight: 700, color: tokens.warn, border: `1px solid ${tokens.warn}`, borderRadius: 99, px: 0.75 }}>required</Box> : null}</Typography>
                      {e && (
                        <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>
                          {e.name} · {fmtKB(e.size)} · {e.by} · {e.when}
                          {e.status && <Chip size="small" label={e.status} sx={{ ml: 0.75, height: 16, fontSize: 10 }}
                            color={e.status === 'Verified' ? 'success' : e.status === 'Rejected' ? 'error' : 'default'} />}
                        </Typography>
                      )}
                    </Box>
                    <Box sx={{ display: 'flex', gap: 0.75 }}>
                      {e && (e.inline || e.id) && <Button size="small" variant="outlined"
                        onClick={() => void documentsService.download(e).then((r) => {
                          if (!r.ok) setErr(r.error || 'The file could not be fetched.');
                        })}>View</Button>}
                      {e?.id && e.status !== 'Verified' && !ro && (
                        <Button size="small" variant="outlined" disabled={busy === e.id}
                          onClick={() => void verify(e)}>Verify</Button>
                      )}
                      {!ro && (
                        <Button size="small" variant={e ? 'outlined' : 'contained'} component="label"
                          disabled={busy === s.k + dd.k}
                          startIcon={!e ? <UploadFileIcon /> : undefined}>
                          {busy === s.k + dd.k ? 'Uploading…' : e ? 'Replace' : 'Upload'}
                          <input type="file" hidden onChange={(ev) => void upload(s.k, dd.k, dd.n, s.t, !!dd.req, ev.target.files?.[0])} />
                        </Button>
                      )}
                      {e && !ro && <Button size="small" color="error" onClick={() => void remove(s.k, dd.k, s.t, e)}>Remove</Button>}
                    </Box>
                  </Box>
                );
              })}
              {!ro && (addingSec === s.k ? (
                <Box sx={{ display: 'flex', gap: 1, mt: 1.2, alignItems: 'center', flexWrap: 'wrap' }}>
                  <TextField size="small" autoFocus placeholder="Document name"
                    value={newDocName} onChange={(ev) => setNewDocName(ev.target.value)}
                    sx={{ flex: 1, minWidth: 180, '& input': { fontSize: 13, py: 0.75 } }} />
                  <Button size="small" variant="contained" component="label"
                    disabled={!newDocName.trim()}>
                    Choose file &amp; upload
                    <input type="file" hidden onChange={(ev) => {
                      const f = ev.target.files?.[0];
                      const key = 'x_' + Date.now().toString(36);
                      setAddingSec(''); const nm = newDocName.trim(); setNewDocName('');
                      void upload(s.k, key, nm, s.t, false, f);
                    }} />
                  </Button>
                  <Button size="small" onClick={() => { setAddingSec(''); setNewDocName(''); }}>Cancel</Button>
                </Box>
              ) : (
                <Button size="small" sx={{ mt: 1, textTransform: 'none', fontSize: 12.5 }}
                  onClick={() => { setAddingSec(s.k); setNewDocName(''); }}>
                  + Add another document
                </Button>
              ))}
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
