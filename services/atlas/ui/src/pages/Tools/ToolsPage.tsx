import { useRef, useState } from 'react';
import { Box, Paper, Typography } from '@mui/material';
import { tokens } from '../../theme';
import { useAuth } from '../../auth/AuthContext';
import { can } from '../../auth/rbac';
import { newsService } from '../../services/newsService';
import ExportBar from '../../components/common/ExportBar';
import NewsRadar from './NewsRadar';
import LedgerDialog from './LedgerDialog';

/**
 * Tools — two cards, both of them real.
 *
 * This page used to carry seven, and most earned their place by existing rather than by
 * working:
 *
 *  * Backup and Restore ran against `db()`, the browser's in-memory store, never the
 *    register. On a live deployment Backup downloaded the local cache and Restore wiped
 *    that cache and announced "Backup restored." having changed nothing on the server —
 *    a control that lies is worse than no control. (Real backup/restore needs an
 *    export-all/import-all pair on the register; it is not a UI change.)
 *  * Mail intake and Application form did work — both created leads — but the desk does
 *    not use them, and a tile nobody presses is a tile that hides the ones they do.
 *  * Import ledger and Export ledger were two tiles for one object, which puts the
 *    DIRECTION before the subject: the desk says "the ledger", not "the import". Worse,
 *    Import sat in the grid beside Restore, the other control that could wipe the book.
 *    They are one card now, and the dialog carries both directions.
 */
function ToolCard({ icon, title, sub, on, onClick }: {
  icon: string; title: string; sub: string; on?: boolean; onClick: () => void;
}) {
  return (
    <Paper variant="outlined" onClick={onClick}
      sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 0.4,
        p: '13px 15px', cursor: 'pointer', textAlign: 'left',
        borderColor: on ? tokens.tealHi : tokens.line, bgcolor: on ? '#F0F8F6' : tokens.card,
        '&:hover': { borderColor: tokens.tealHi } }}>
      <Box component="span" sx={{ fontSize: 21 }}>{icon}</Box>
      <Typography sx={{ fontSize: 13.4, fontWeight: 700 }}>{title}</Typography>
      <Typography sx={{ fontSize: 11.4, color: tokens.muted }}>{sub}</Typography>
    </Paper>
  );
}

export default function ToolsPage() {
  const { user } = useAuth();
  const [ledger, setLedger] = useState(false);
  const radarRef = useRef<HTMLDivElement>(null);
  // The Excel ledger is Admin-only in both directions — the server enforces it too, so
  // hiding it here only spares a non-Admin a refusal they could not act on.
  const canLedger = can(user.roles, 'backupRestore');

  return (
    <>
      <Box sx={{ mb: 1.2 }}>
        <ExportBar onCsv={() => newsService.exportCsv(user.full)} />
      </Box>

      {/* auto-FILL, not auto-fit: with seven cards either behaved the same, but two
          cards in auto-fit tracks would each stretch to half the screen. Empty tracks
          hold their width, so the tiles keep the size they have always had. */}
      <Box sx={{ display: 'grid', gap: 1.2, mb: 1.8,
        gridTemplateColumns: 'repeat(auto-fill,minmax(260px,1fr))' }}>
        {/* The radar is this page's content, so its card is the one shown as current —
            and it now DOES something (it used to have no click handler at all): it
            takes you to the radar, which matters once the page has scrolled. */}
        <ToolCard icon="📰" title="India News Radar" sub="firms · owners · related" on
          onClick={() => radarRef.current?.scrollIntoView({ behavior: 'smooth',
            block: 'start' })} />
        {canLedger && (
          <ToolCard icon="📒" title="Ledger"
            sub="export the book · import a ledger file"
            onClick={() => setLedger(true)} />
        )}
      </Box>

      <Box ref={radarRef}><NewsRadar /></Box>

      <LedgerDialog open={ledger} onClose={() => setLedger(false)} />
    </>
  );
}
