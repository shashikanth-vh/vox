import { useRef, useState } from 'react';
import { Box, Paper, Typography, Button } from '@mui/material';
import { tokens } from '../../theme';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { backupService } from '../../services/backupService';
import { newsService } from '../../services/newsService';
import ExportBar from '../../components/common/ExportBar';
import NewsRadar from './NewsRadar';
import MailIntakeDialog from './MailIntakeDialog';
import ApplicationDialog from './ApplicationDialog';

// Port of v12 AUGMENT 16 — the Tools tab. Visible to every role, per the template.
function ToolCard({ icon, title, sub, on, onClick }: {
  icon: string; title: string; sub: string; on?: boolean; onClick?: () => void;
}) {
  return (
    <Paper variant="outlined" onClick={onClick}
      sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 0.4,
        p: '13px 15px', cursor: 'pointer', textAlign: 'left',
        borderColor: on ? tokens.tealHi : tokens.line, bgcolor: on ? '#F0F8F6' : tokens.card }}>
      <Box component="span" sx={{ fontSize: 21 }}>{icon}</Box>
      <Typography sx={{ fontSize: 13.4, fontWeight: 700 }}>{title}</Typography>
      <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>{sub}</Typography>
    </Paper>
  );
}

export default function ToolsPage() {
  const { user } = useAuth();
  const [mail, setMail] = useState(false);
  const [app, setApp] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [, force] = useState(0);

  const onRestore = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = ''; // clear so re-picking the same file re-fires
    if (!f) return;
    if (!window.confirm('Replace ALL current data with this backup? This cannot be undone.')) return;
    const r = new FileReader();
    r.onload = () => {
      const res = backupService.restore(String(r.result), user.full);
      if (!res.ok) { window.alert(res.error); return; }
      force((n) => n + 1);
      window.alert('Backup restored.');
    };
    r.readAsText(f);
  };

  return (
    <>
      <Box sx={{ mb: 1.2 }}>
        <ExportBar onCsv={() => newsService.exportCsv(user.full)} />
      </Box>

      <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 1.2, mb: 1.8 }}>
        <ToolCard icon="📰" title="India News Radar" sub="firms · owners · related" on />
        <ToolCard icon="📧" title="Mail intake" sub="scrape an email" onClick={() => setMail(true)} />
        <ToolCard icon="📝" title="Application form" sub="consent-first intake" onClick={() => setApp(true)} />
        {/* Backup / restore is Admin-only (RBAC: restore can wipe the book). */}
        {can(user.roles, 'backupRestore') && <ToolCard icon="💾" title="Backup" sub="download all data (JSON)" onClick={() => backupService.backup(user.full)} />}
        {can(user.roles, 'backupRestore') && <ToolCard icon="♻️" title="Restore" sub="import a backup file" onClick={() => fileRef.current?.click()} />}
      </Box>
      <input ref={fileRef} type="file" accept=".json" style={{ display: 'none' }} onChange={onRestore} />

      <NewsRadar />

      <MailIntakeDialog open={mail} onClose={() => setMail(false)} />
      <ApplicationDialog open={app} onClose={() => setApp(false)} />
    </>
  );
}
