import { useCallback, useEffect, useRef, useState } from 'react';
import type { DragEvent } from 'react';
import { Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, Alert, TextField, IconButton, Menu, MenuItem, Tooltip, Tabs, Tab, LinearProgress } from '@mui/material';
import FolderIcon from '@mui/icons-material/Folder';
import FileUploadOutlinedIcon from '@mui/icons-material/FileUploadOutlined';
import CreateNewFolderOutlinedIcon from '@mui/icons-material/CreateNewFolderOutlined';
import AddIcon from '@mui/icons-material/Add';
import MoreVertIcon from '@mui/icons-material/MoreVert';
import { REQ_DOCS, documentsService } from '../../services/documentsService';
import type { DocEntry, DocIndex } from '../../services/documentsService';
import { clientsService } from '../../services/clientsService';
import { api, errText } from '../../api/http';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { tokens } from '../../theme';

const fmtKB = (b: number) => (b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB');

// A checklist slot can hold SEVERAL files — every director's KYC, twelve monthly bank
// statements, three years of audited accounts. The register keeps one live file per slot
// key (uploading into an occupied key supersedes it: that is "Replace"), so the second,
// third… file of a slot is filed under its own key: "dirkyc~2", "dirkyc~3". The register's
// checklist rollup reads the part before "~" as the slot, so they all count for it.
const SEP = '~';
const baseKey = (k: string) => k.split(SEP)[0];
const keyNo = (k: string) => (k.includes(SEP) ? Number(k.split(SEP)[1]) || 0 : 1);

// Small file-type badge, coloured the way people recognise them.
function FileBadge({ name, missing }: { name: string; missing?: boolean }) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  const [label, color] =
    missing ? ['', tokens.line]
      : ext === 'pdf' ? ['PDF', '#D93025']
        : ['xls', 'xlsx', 'xlsm', 'csv'].includes(ext) ? ['X', '#1D7044']
          : ['doc', 'docx'].includes(ext) ? ['W', '#2B579A']
            : ['png', 'jpg', 'jpeg', 'gif', 'webp'].includes(ext) ? ['IMG', '#7A4FC4']
              : ['zip', 'rar', '7z'].includes(ext) ? ['ZIP', tokens.am]
                : ['FILE', tokens.muted];
  return (
    <Box sx={{ width: 28, height: 28, borderRadius: 1, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: label.length > 2 ? 8.5 : 12, fontWeight: 800, letterSpacing: '.2px',
      ...(missing ? { border: `1.5px dashed ${color}` } : { bgcolor: color, color: '#fff' }) }}>{label}</Box>
  );
}

function ReviewPill({ e, req }: { e?: DocEntry; req: boolean }) {
  const [label, fg, bg] =
    !e ? (req ? ['Missing', tokens.bad, tokens.badBg] : ['Optional', tokens.muted, tokens.paper])
      : e.status === 'Verified' ? ['Accepted', tokens.ok, tokens.okBg]
        : e.status === 'Rejected' ? ['Rejected', tokens.bad, tokens.badBg]
          : ['Needs review', '#8A5D0F', '#FBEFD5'];
  return (
    <Box component="span" sx={{ display: 'inline-block', px: 1.1, py: 0.3, borderRadius: 1.2, fontSize: 11.5, fontWeight: 600,
      color: fg, bgcolor: bg, whiteSpace: 'nowrap' }}>{label}</Box>
  );
}

type Row = { k: string; n: string; req: number };
type FileRef = { key: string; e: DocEntry };
// Add files to a checklist slot (each gets the next free key), REPLACE one file (its own
// key, so the register supersedes exactly that file), or file loose papers in a folder.
type Pick =
  | { kind: 'slot'; sec: string; dk: string; label: string; required: boolean }
  | { kind: 'replace'; sec: string; key: string; label: string; required: boolean }
  | { kind: 'adhoc'; sec: string };

const TABS = ['Files', 'Conversations', 'Tasks', 'Decisions', 'Lenders'] as const;
const GRID = 'minmax(0,1fr) 118px 36px';

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
  const [progress, setProgress] = useState('');
  const [sel, setSel] = useState(REQ_DOCS[0].k);
  const [tab, setTab] = useState<(typeof TABS)[number]>('Files');
  // Where a drag is hovering: a slot key, '*' for the folder itself, or nothing.
  const [dropAt, setDropAt] = useState('');
  // Folders named here but not yet holding a file. The register has no empty
  // folders — a section exists once something is filed under it — so these
  // live in the session until the first upload makes them real.
  const [newFolders, setNewFolders] = useState<string[]>([]);
  const [folderDlg, setFolderDlg] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ el: HTMLElement; sec: string; dd: Row; f: FileRef; title: string; slot: boolean } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const pickRef = useRef<Pick | null>(null);

  const reload = useCallback(() => {
    documentsService.load(code, entityId).then(setDocs).catch(() => setDocs({}));
  }, [code, entityId]);
  useEffect(() => { if (open) { setErr(''); setSel(REQ_DOCS[0].k); reload(); } }, [open, reload]);

  // Every folder: the checklist sections, then any section the register already
  // holds that the checklist does not name, then the ones just created here.
  const folders: { k: string; t: string; d: Row[] }[] = [
    ...REQ_DOCS,
    ...[...Object.keys(docs), ...newFolders]
      .filter((k, i, a) => a.indexOf(k) === i && !REQ_DOCS.some((s) => s.k === k))
      .map((k) => ({ k, t: k, d: [] as Row[] })),
  ];

  /** The files a checklist slot holds: its own key and every "key~n", in filing order. */
  const filesOf = (sec: string, dk: string): FileRef[] =>
    Object.entries(docs[sec] || {})
      .filter(([k]) => k === dk || k.startsWith(dk + SEP))
      .map(([key, e]) => ({ key, e }))
      .sort((a, b) => keyNo(a.key) - keyNo(b.key));

  /** n slot keys not yet holding a live file: the slot's own key first, then "~2", "~3"… */
  const freeKeys = (sec: string, dk: string, n: number): string[] => {
    const taken = new Set(Object.keys(docs[sec] || {}));
    const out: string[] = [];
    for (let i = 1; out.length < n && i < 500; i++) {
      const k = i === 1 ? dk : `${dk}${SEP}${i}`;
      if (!taken.has(k)) out.push(k);
    }
    return out;
  };

  const rowsOf = (f: { k: string; d: Row[] }): Row[] => {
    const store = docs[f.k] || {};
    return [...f.d,
      // ad-hoc documents already stored under this section — every key the fixed
      // checklist does not claim (its "~n" extras belong to their slot) is its own row
      ...Object.entries(store)
        .filter(([k]) => !f.d.some((dd) => dd.k === baseKey(k)))
        .map(([k, e]) => ({ k, n: e.label || e.name, req: 0 }))];
  };

  // Files go up one at a time, so a refusal names its file and the rest still land.
  const uploadFiles = async (p: Pick, files: File[]) => {
    if (!files.length) return;
    const title = folders.find((f) => f.k === p.sec)?.t || p.sec;
    const list = p.kind === 'replace' ? files.slice(0, 1) : files;
    const keys = p.kind === 'slot' ? freeKeys(p.sec, p.dk, list.length) : [];
    const fails: string[] = [];
    setErr('');
    for (let i = 0; i < list.length; i++) {
      const f = list[i];
      const slotKey = p.kind === 'slot' ? keys[i]
        : p.kind === 'replace' ? p.key
          : 'x_' + Date.now().toString(36) + i;
      setBusy(p.kind === 'adhoc' ? p.sec + '*' : p.sec + (p.kind === 'slot' ? p.dk : baseKey(p.key)));
      setProgress(list.length > 1 ? `${i + 1} of ${list.length}` : '');
      const r = await documentsService.upload({ code, entityId, section: p.sec, slotKey,
        title: p.kind === 'adhoc' ? f.name.replace(/\.[^.]+$/, '') : p.label,
        required: p.kind === 'adhoc' ? false : p.required, file: f, by: user.full, sectionTitle: title });
      if (!r.ok) fails.push(`${f.name}: ${r.error || 'the upload was refused'}`);
    }
    setBusy(''); setProgress('');
    if (fails.length) setErr(fails.length === 1 ? fails[0] : `${fails.length} of ${list.length} uploads failed — ${fails.join(' · ')}`);
    reload();
  };
  // Replace takes one file (it supersedes one document); every other pick takes many.
  const pick = (p: Pick) => {
    pickRef.current = p;
    if (fileRef.current) { fileRef.current.multiple = p.kind !== 'replace'; fileRef.current.click(); }
  };
  const onPicked = (list: FileList | null) => {
    const p = pickRef.current; pickRef.current = null;
    if (p && list) void uploadFiles(p, Array.from(list));
  };

  // Drag and drop: onto a checklist row files into that slot; anywhere else in the
  // pane files loose papers into the open folder.
  const dragHas = (ev: DragEvent) => Array.from(ev.dataTransfer?.types || []).includes('Files');
  const dragOver = (ev: DragEvent, at: string) => {
    if (ro || busy || !dragHas(ev)) return;
    ev.preventDefault(); ev.stopPropagation();
    ev.dataTransfer.dropEffect = 'copy';
    if (dropAt !== at) setDropAt(at);
  };
  const drop = (ev: DragEvent, p: Pick) => {
    if (ro || busy || !dragHas(ev)) return;
    ev.preventDefault(); ev.stopPropagation();
    setDropAt('');
    void uploadFiles(p, Array.from(ev.dataTransfer.files || []));
  };

  const verify = async (e: DocEntry) => {
    if (!e.id) return;
    setErr(''); setBusy(e.id);
    const r = await documentsService.validate(e.id, '', user.full, code);
    setBusy('');
    if (!r.ok) { setErr(r.error || 'The verification was refused.'); return; }
    reload();
  };
  const [ren, setRen] = useState<{ id: string; name: string } | null>(null);
  const [renBusy, setRenBusy] = useState(false);
  const renameSave = async () => {
    if (!ren || !ren.name.trim()) return;
    setRenBusy(true); setErr('');
    const r = await documentsService.rename(ren.id, ren.name);
    setRenBusy(false);
    if (!r.ok) { setErr(r.error || 'The rename was refused.'); return; }
    setRen(null); reload();
  };
  const remove = async (sec: string, key: string, sectionTitle: string, e: DocEntry) => {
    if (!confirm(`Remove ${e.label} (${e.name})?`)) return;
    setErr('');
    const r = await documentsService.remove(code, sec, key, e, user.full, sectionTitle);
    if (!r.ok) { setErr(r.error || 'The removal was refused.'); return; }
    reload();
  };
  const view = (e: DocEntry) => void documentsService.download(e).then((r) => {
    if (!r.ok) setErr(r.error || 'The file could not be fetched.');
  });
  const createFolder = () => {
    const name = (folderDlg || '').trim();
    if (!name) return;
    const hit = folders.find((f) => f.t.toLowerCase() === name.toLowerCase());
    if (!hit) setNewFolders((p) => [...p, name]);
    setSel(hit ? hit.k : name); setFolderDlg(null);
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

  const folder = folders.find((f) => f.k === sel) || folders[0];

  const reqCount = (f: { k: string; d: Row[] }) => {
    const n = f.d.filter((d) => d.req).length;
    return { n, got: f.d.filter((d) => d.req && filesOf(f.k, d.k).length > 0).length };
  };
  // The whole checklist, for the line under the title.
  let totReq = 0, gotReq = 0;
  REQ_DOCS.forEach((s) => { const rc = reqCount(s); totReq += rc.n; gotReq += rc.got; });
  const pct = totReq ? Math.round((gotReq / totReq) * 100) : 0;

  const folderItem = (k: string, t: string) => {
    const active = sel === k;
    const f = folders.find((x) => x.k === k);
    const rc = f ? reqCount(f) : null;
    return (
      <Box key={k} component="button" type="button" onClick={() => setSel(k)}
        sx={{ all: 'unset', boxSizing: 'border-box', width: '100%', display: 'flex', alignItems: 'center', gap: 1.1,
          px: 1.2, py: 0.85, borderRadius: 1.5, cursor: 'pointer', fontSize: 13.5,
          fontWeight: active ? 700 : 500, color: tokens.ink, bgcolor: active ? '#E3F2EE' : 'transparent',
          '&:hover': { bgcolor: active ? '#E3F2EE' : tokens.paper },
          '&:focus-visible': { outline: `2px solid ${tokens.teal}`, outlineOffset: -2 } }}>
        <FolderIcon sx={{ fontSize: 20, color: '#C99A2E' }} />
        <Box component="span" sx={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t}</Box>
        {rc && rc.n > 0 && rc.got < rc.n && (
          <Tooltip title={`${rc.n - rc.got} required missing`}>
            <Box component="span" sx={{ fontSize: 11, fontWeight: 700, color: tokens.warn }}>{rc.got}/{rc.n}</Box>
          </Tooltip>
        )}
      </Box>
    );
  };

  const headCell = { fontSize: 12.5, fontWeight: 600, color: tokens.muted };
  const lineSx = (at: string) => ({
    display: 'grid', gridTemplateColumns: GRID, alignItems: 'center', columnGap: 1.5,
    px: 1.5, py: 1.2, borderBottom: `1px solid ${tokens.line}`,
    ...(dropAt === at ? { outline: `2px dashed ${tokens.teal}`, outlineOffset: -2, bgcolor: '#E3F2EE' } : {}),
  });

  /** One file line: badge, name (and the slot it belongs to), review, actions. */
  const fileLine = (sec: string, title: string, dd: Row, f: FileRef, slot: boolean, sub?: string) => (
    <>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.4, minWidth: 0 }}>
        <FileBadge name={f.e.name} />
        <Box sx={{ minWidth: 0 }}>
          <Tooltip title={`${f.e.name} · ${fmtKB(f.e.size)} · ${f.e.by} · ${f.e.when}`} placement="top-start">
            <Typography noWrap sx={{ fontSize: 14, color: tokens.ink }}>{f.e.name}</Typography>
          </Tooltip>
          {sub && <Typography noWrap sx={{ fontSize: 11.5, color: tokens.muted }}>{sub}</Typography>}
        </Box>
      </Box>
      <Box>{busy === f.e.id ? <Typography sx={{ fontSize: 12, color: tokens.muted }}>Working…</Typography> : <ReviewPill e={f.e} req={!!dd.req} />}</Box>
      <Box>
        <IconButton aria-label={`Actions for ${f.e.name}`}
          onClick={(ev) => setMenu({ el: ev.currentTarget, sec, dd, f, title, slot })}>
          <MoreVertIcon sx={{ fontSize: 20 }} />
        </IconButton>
      </Box>
    </>
  );

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth PaperProps={{ sx: { height: 'min(720px, 88vh)' } }}>
      <DialogTitle sx={{ fontSize: 16, display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>Data Register — {c.name}</Box>
        {totReq > 0 && (
          <Box sx={{ width: 220 }}>
            <Typography sx={{ fontSize: 11.5, color: tokens.muted, mb: 0.4 }}>
              <b>{gotReq} of {totReq}</b> required on file · {pct}%
            </Typography>
            <LinearProgress variant="determinate" value={pct}
              sx={{ height: 5, borderRadius: 99, bgcolor: tokens.paper, '& .MuiLinearProgress-bar': { bgcolor: tokens.tealHi } }} />
          </Box>
        )}
      </DialogTitle>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} variant="scrollable" scrollButtons="auto"
        sx={{ px: 2, borderTop: `1px solid ${tokens.line}`,
          '& .MuiTab-root': { textTransform: 'none', fontSize: 14.5, fontWeight: 500, color: tokens.ink, minHeight: 46, px: 2.5 },
          '& .MuiTab-root.Mui-selected': { color: tokens.teal, fontWeight: 700 },
          '& .MuiTabs-indicator': { bgcolor: tokens.teal, height: 3 } }}>
        {TABS.map((t) => <Tab key={t} value={t} label={t} />)}
      </Tabs>
      <DialogContent dividers sx={{ p: 0, display: 'flex', minHeight: 0 }}>
        <input ref={fileRef} type="file" hidden multiple onChange={(ev) => { onPicked(ev.target.files); ev.target.value = ''; }} />

        {tab !== 'Files' ? (
          <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <Typography sx={{ fontSize: 13, color: tokens.muted }}>Nothing in {tab} yet.</Typography>
          </Box>
        ) : (<>
        {/* Folders */}
        <Box sx={{ width: 230, flexShrink: 0, borderRight: `1px solid ${tokens.line}`, p: 1.2, overflowY: 'auto',
          display: 'flex', flexDirection: 'column', gap: 0.3 }}>
          {folders.map((f) => folderItem(f.k, f.t))}
        </Box>

        {/* Pane — a drop anywhere here files loose papers into the open folder */}
        <Box sx={{ flex: 1, minWidth: 0, p: 2.5, overflowY: 'auto',
            ...(dropAt === '*' ? { outline: `2px dashed ${tokens.teal}`, outlineOffset: -6, bgcolor: '#F4FAF8' } : {}) }}
          onDragOver={(ev) => folder && dragOver(ev, '*')}
          onDragLeave={(ev) => { if (ev.currentTarget === ev.target) setDropAt(''); }}
          onDrop={(ev) => folder && drop(ev, { kind: 'adhoc', sec: folder.k })}>
          {!entityId && (
            <Alert severity="info" sx={{ mb: 1.5, py: 0, fontSize: 12 }}>
              This company is not linked to a register record yet, so uploads stay in this session only.
            </Alert>
          )}
          {err && (
            <Alert severity="warning" sx={{ mb: 1.5, py: 0, fontSize: 12 }}
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

          {folder && (
            <>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2, mb: 2, flexWrap: 'wrap' }}>
                <Typography sx={{ fontSize: 22, fontWeight: 800, color: tokens.ink, flex: 1, minWidth: 0 }}>{folder.t}</Typography>
                {!ro && (
                  <>
                    <Button variant="outlined" startIcon={<FileUploadOutlinedIcon />}
                      disabled={!!busy}
                      onClick={() => pick({ kind: 'adhoc', sec: folder.k })}
                      sx={{ fontSize: 13.5, px: 2, py: 0.8, borderRadius: 1.5, borderColor: tokens.teal, color: tokens.teal }}>
                      {busy === folder.k + '*' ? `Uploading${progress ? ' ' + progress : ''}…` : 'Upload'}
                    </Button>
                    <Button variant="outlined" startIcon={<CreateNewFolderOutlinedIcon />}
                      onClick={() => setFolderDlg('')}
                      sx={{ fontSize: 13.5, px: 2, py: 0.8, borderRadius: 1.5, borderColor: tokens.line, color: tokens.ink }}>
                      New folder
                    </Button>
                  </>
                )}
              </Box>
              {!ro && (
                <Typography sx={{ fontSize: 12, color: tokens.muted, mt: -1, mb: 1.5 }}>
                  Select several files at once, or drag them here. Dropped on a checklist row they file under that row.
                </Typography>
              )}

              <Box sx={{ display: 'grid', gridTemplateColumns: GRID, alignItems: 'center', columnGap: 1.5,
                bgcolor: tokens.paper, borderRadius: 1.5, px: 1.5, py: 1.1 }}>
                <Box sx={headCell}>Name</Box><Box sx={headCell}>Review</Box><Box />
              </Box>

              {rowsOf(folder).length === 0 && (
                <Typography sx={{ fontSize: 13, color: tokens.muted, textAlign: 'center', py: 5 }}>
                  No files in this folder yet.{!ro && ' Use Upload, or drop files here.'}
                </Typography>
              )}

              {rowsOf(folder).map((dd) => {
                const slot = folder.d.some((x) => x.k === dd.k);
                const files: FileRef[] = slot ? filesOf(folder.k, dd.k)
                  : (docs[folder.k]?.[dd.k] ? [{ key: dd.k, e: docs[folder.k][dd.k] }] : []);
                const rowBusy = busy === folder.k + dd.k;
                const addPick: Pick = { kind: 'slot', sec: folder.k, dk: dd.k, label: dd.n, required: !!dd.req };
                const dropProps = slot && !ro ? {
                  onDragOver: (ev: DragEvent) => dragOver(ev, dd.k),
                  onDrop: (ev: DragEvent) => drop(ev, addPick),
                } : {};

                // An empty slot: its name, Required, and Upload (several files allowed).
                if (!files.length) {
                  return (
                    <Box key={dd.k} sx={lineSx(dd.k)} {...dropProps}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.4, minWidth: 0 }}>
                        <FileBadge name="" missing />
                        <Box sx={{ minWidth: 0 }}>
                          <Typography noWrap sx={{ fontSize: 14, color: tokens.muted }}>{dd.n}</Typography>
                          {dd.req ? <Typography sx={{ fontSize: 11.5, color: tokens.warn, fontWeight: 600 }}>Required</Typography> : null}
                        </Box>
                      </Box>
                      <Box>
                        {!ro ? (
                          <Button size="small" variant="text" disabled={!!busy}
                            startIcon={<FileUploadOutlinedIcon sx={{ fontSize: 16 }} />}
                            onClick={() => pick(addPick)} sx={{ fontSize: 12.5, px: 0.5 }}>
                            {rowBusy ? `Uploading${progress ? ' ' + progress : ''}…` : 'Upload'}
                          </Button>
                        ) : <ReviewPill req={!!dd.req} />}
                      </Box>
                      <Box />
                    </Box>
                  );
                }

                // A loose paper: one file, its own row.
                if (!slot) {
                  return <Box key={dd.k} sx={lineSx(dd.k)}>{fileLine(folder.k, folder.t, dd, files[0], false)}</Box>;
                }

                // A checklist slot with files: one line per file under the slot's name,
                // then "Add files" to put more under the same row.
                return (
                  <Box key={dd.k} {...dropProps}
                    sx={{ borderBottom: `1px solid ${tokens.line}`,
                      ...(dropAt === dd.k ? { outline: `2px dashed ${tokens.teal}`, outlineOffset: -2, bgcolor: '#E3F2EE' } : {}) }}>
                    {files.length > 1 && (
                      <Typography sx={{ fontSize: 12.5, fontWeight: 600, color: tokens.muted, px: 1.5, pt: 1.1 }}>
                        {dd.n} · {files.length} files
                      </Typography>
                    )}
                    {files.map((f) => (
                      <Box key={f.key} sx={{ display: 'grid', gridTemplateColumns: GRID, alignItems: 'center', columnGap: 1.5,
                        px: 1.5, py: files.length > 1 ? 0.8 : 1.2 }}>
                        {fileLine(folder.k, folder.t, dd, f, true, files.length > 1 ? undefined : (f.e.name !== dd.n ? dd.n : undefined))}
                      </Box>
                    ))}
                    {!ro && (
                      <Box sx={{ px: 1.5, pb: 1, pl: '58px' }}>
                        <Button size="small" variant="text" disabled={!!busy} startIcon={<AddIcon sx={{ fontSize: 16 }} />}
                          onClick={() => pick(addPick)} sx={{ fontSize: 12, px: 0.5, minWidth: 0 }}>
                          {rowBusy ? `Uploading${progress ? ' ' + progress : ''}…` : 'Add files'}
                        </Button>
                      </Box>
                    )}
                  </Box>
                );
              })}
            </>
          )}
        </Box>
        </>)}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="outlined">Done</Button>
      </DialogActions>

      <Menu anchorEl={menu?.el} open={!!menu} onClose={() => setMenu(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }} transformOrigin={{ vertical: 'top', horizontal: 'right' }}>
        {menu && (menu.f.e.inline || menu.f.e.id) && (
          <MenuItem onClick={() => { const e = menu.f.e; setMenu(null); view(e); }}>View</MenuItem>
        )}
        {menu && menu.f.e.id && menu.f.e.status !== 'Verified' && !ro && (
          <MenuItem onClick={() => { const e = menu.f.e; setMenu(null); void verify(e); }}>Verify</MenuItem>
        )}
        {menu && !ro && (
          <MenuItem onClick={() => { const m = menu; setMenu(null);
            pick({ kind: 'replace', sec: m.sec, key: m.f.key, label: m.dd.n, required: !!m.dd.req }); }}>Replace with new version</MenuItem>
        )}
        {menu && menu.slot && !ro && (
          <MenuItem onClick={() => { const m = menu; setMenu(null);
            pick({ kind: 'slot', sec: m.sec, dk: m.dd.k, label: m.dd.n, required: !!m.dd.req }); }}>Add more files</MenuItem>
        )}
        {/* Ad-hoc documents (keys minted x_…) own their name — standard slots take theirs from the checklist. */}
        {menu && menu.f.e.id && menu.f.key.startsWith('x_') && !ro && (
          <MenuItem onClick={() => { const e = menu.f.e; setMenu(null); setRen({ id: e.id!, name: e.label || e.name }); }}>Rename</MenuItem>
        )}
        {menu && !ro && (
          <MenuItem sx={{ color: tokens.bad }}
            onClick={() => { const m = menu; setMenu(null); void remove(m.sec, m.f.key, m.title, m.f.e); }}>Remove</MenuItem>
        )}
      </Menu>

      <Dialog open={folderDlg !== null} onClose={() => setFolderDlg(null)} maxWidth="xs" fullWidth>
        <DialogTitle>New folder</DialogTitle>
        <DialogContent>
          <TextField autoFocus fullWidth size="small" sx={{ mt: 1 }} label="Folder name" placeholder="e.g. Order book"
            value={folderDlg || ''} onChange={(ev) => setFolderDlg(ev.target.value)}
            onKeyDown={(ev) => { if (ev.key === 'Enter') createFolder(); }}
            helperText="The folder is saved on the register once a file is uploaded to it." />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setFolderDlg(null)}>Cancel</Button>
          <Button variant="contained" disabled={!(folderDlg || '').trim()} onClick={createFolder}>Create</Button>
        </DialogActions>
      </Dialog>

      <Dialog open={!!ren} onClose={() => setRen(null)} maxWidth="xs" fullWidth>
        <DialogTitle>Rename document</DialogTitle>
        <DialogContent>
          <TextField autoFocus fullWidth size="small" sx={{ mt: 1 }} label="Document name"
            value={ren?.name || ''} onChange={(ev) => setRen((p2) => p2 && ({ ...p2, name: ev.target.value }))}
            onKeyDown={(ev) => { if (ev.key === 'Enter') void renameSave(); }} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRen(null)}>Cancel</Button>
          <Button variant="contained" disabled={renBusy || !ren?.name.trim()} onClick={() => void renameSave()}>Rename</Button>
        </DialogActions>
      </Dialog>
    </Dialog>
  );
}
