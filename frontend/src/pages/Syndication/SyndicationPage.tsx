import { useState } from 'react';
import { Box, Typography, ToggleButtonGroup, ToggleButton } from '@mui/material';
import WhatshotIcon from '@mui/icons-material/Whatshot';
import GridOnIcon from '@mui/icons-material/GridOn';
import TableRowsIcon from '@mui/icons-material/TableRows';
import { useQueryClient } from '@tanstack/react-query';
import PageHint from '../../components/common/PageHint';
import ChaseView from './ChaseView';
import MatrixView from './MatrixView';
import SynRegisterView from './SynRegisterView';
import CompanyDrawer from '../Deals/CompanyDrawer';
import AddProductDialog from '../Deals/AddProductDialog';
import BankLedgerDialog from '../FIMaster/BankLedgerDialog';
import { tokens } from '../../theme';

// v12 vSyn: three modes in this order — Chase list · Matrix · Register (by bank).
type Mode = 'chase' | 'matrix' | 'reg';

export default function SyndicationPage() {
  const qc = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);
  const [addProd, setAddProd] = useState<string | null>(null);
  const [bank, setBank] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>('chase');
  const refresh = () => qc.invalidateQueries();

  const hint = mode === 'matrix' ? 'Matrix is read-only — it renders the Chase List state'
    : mode === 'reg' ? 'Click a bank for its full deal ledger'
      : 'Lender-wise Platform Deals chase list. Log chases and replies; advance each lender’s status.';

  return (
    <>
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1, gap: 1, flexWrap: 'wrap' }}>
        <PageHint>Chase lenders, watch the dot matrix, or read the by-bank register. Click a company to manage lenders and advance statuses.</PageHint>
        <ToggleButtonGroup exclusive size="small" value={mode} onChange={(_, v) => v && setMode(v)}>
          <ToggleButton value="chase"><WhatshotIcon fontSize="small" sx={{ mr: 0.5 }} />Chase list</ToggleButton>
          <ToggleButton value="matrix"><GridOnIcon fontSize="small" sx={{ mr: 0.5 }} />Matrix</ToggleButton>
          <ToggleButton value="reg"><TableRowsIcon fontSize="small" sx={{ mr: 0.5 }} />Register (by bank)</ToggleButton>
        </ToggleButtonGroup>
      </Box>
      <Typography sx={{ fontSize: 11.6, color: tokens.muted, mb: 1 }}>{hint}</Typography>

      {mode === 'chase' ? (
        <ChaseView onOpenCompany={(c) => setOpen(c)} />
      ) : mode === 'matrix' ? (
        <MatrixView onOpenCompany={(c) => setOpen(c)} />
      ) : (
        <SynRegisterView onOpenBank={(n) => setBank(n)} />
      )}

      <CompanyDrawer code={open} onClose={() => setOpen(null)} onChanged={refresh} onAddProduct={(c) => setAddProd(c)} />
      <AddProductDialog code={addProd} onClose={() => setAddProd(null)} onDone={refresh} />
      {/* v12 openBank — the by-bank register opens a lender's full deal ledger. */}
      <BankLedgerDialog bankName={bank} onClose={() => setBank(null)} onOpenCompany={(c) => { setBank(null); setOpen(c); }} />
    </>
  );
}
