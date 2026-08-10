import { useState } from 'react';
import { Box, Paper, Typography } from '@mui/material';
import { tokens } from '../../theme';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { newsService } from '../../services/newsService';
import ExportBar from '../../components/common/ExportBar';
import NewsRadar from './NewsRadar';
import LedgerDialog from './LedgerDialog';

/**
 * Tools — two things, both of them real.
 *
 * This page used to carry seven tiles, and most of them earned their place by existing
 * rather than by working:
 *
 *  * Backup and Restore ran against `db()`, the browser's in-memory store, never the
 *    register. On a live deployment Backup downloaded the local cache and Restore wiped
 *    that cache and announced "Backup restored." having changed nothing on the server —
 *    a control that lies is worse than no control. (Real backup/restore needs an
 *    export-all/import-all pair on the register; it is not a UI change.)
 *  * Mail intake and Application form did work — both created leads — but the desk does
 *    not use them, and a tile nobody presses is a tile that hides the ones they do.
 *  * India News Radar was not a tile at all: it had no click handler, and the radar
 *    rendered below it regardless. One dead tile beside one live tile reads as broken.
 *
 * What is left is the ledger — one door, both directions — and the radar as the page's
 * actual content.
 */
function ToolCard({ icon, title, sub, onClick }: {
  icon: string; title: string; sub: string; onClick: () => void;
}) {
  return (
    <Paper variant="outlined" onClick={onClick}
      sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 0.4,
        p: '13px 15px', cursor: 'pointer', textAlign: 'left', maxWidth: 280,
        borderColor: tokens.line, bgcolor: tokens.card,
        '&:hover': { borderColor: tokens.tealHi, bgcolor: '#F0F8F6' } }}>
      <Box component="span" sx={{ fontSize: 21 }}>{icon}</Box>
      <Typography sx={{ fontSize: 13.4, fontWeight: 700 }}>{title}</Typography>
      <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>{sub}</Typography>
    </Paper>
  );
}

function SectionHead({ title, sub, right }: {
  title: string; sub?: string; right?: React.ReactNode;
}) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 1.5, mb: 1.2, flexWrap: 'wrap' }}>
      <Box>
        <Typography sx={{ fontSize: 11, fontWeight: 700, color: tokens.muted,
          textTransform: 'uppercase', letterSpacing: '.6px' }}>{title}</Typography>
        {sub && <Typography sx={{ fontSize: 12.2, color: tokens.muted }}>{sub}</Typography>}
      </Box>
      {right && <Box sx={{ ml: 'auto' }}>{right}</Box>}
    </Box>
  );
}

export default function ToolsPage() {
  const { user } = useAuth();
  const [ledger, setLedger] = useState(false);
  // The Excel ledger is Admin-only in both directions — the server enforces it too, so
  // this only spares a non-Admin a refusal they could not act on.
  const canLedger = can(user.roles, 'backupRestore');

  return (
    <>
      {canLedger && (
        <Box sx={{ mb: 2.4 }}>
          <SectionHead title="Ledger"
            sub="The desk's Excel ledger, in and out of PRISM." />
          <ToolCard icon="📒" title="Ledger" sub="export the book · import a ledger file"
            onClick={() => setLedger(true)} />
        </Box>
      )}

      {/* The radar IS this page's content, so it gets a heading and its own export bar —
          "Export this view" sat at the very top, above a row of tiles it had nothing to
          do with, and exported the news. */}
      <SectionHead title="India News Radar" sub="firms · owners · related"
        right={<ExportBar onCsv={() => newsService.exportCsv(user.full)} />} />
      <NewsRadar />

      <LedgerDialog open={ledger} onClose={() => setLedger(false)} />
    </>
  );
}
