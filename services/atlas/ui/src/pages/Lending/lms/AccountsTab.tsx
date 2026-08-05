import { useState } from 'react';
import {
  Box, Button, Table, TableBody, TableCell, TableHead, TableRow, Typography,
} from '@mui/material';
import { tokens } from '../../../theme';
import { fmt } from '../../../utils/format';
import type { LendingRow } from '../lending.types';
import AccountDialog from './AccountDialog';

/**
 * The serviced book: one row per lending line in the disbursed family. Opening a row
 * shows the loan account the register opened at T1 — statement ledger included. A line
 * not yet disbursed simply says so when opened; nothing here creates an account by hand.
 */
export default function AccountsTab({ rows }: { rows: LendingRow[] }) {
  const [open, setOpen] = useState<LendingRow | null>(null);

  if (!rows.length) {
    return (
      <Typography sx={{ fontSize: 12.5, color: tokens.muted, mt: 2 }}>
        No serviced lines yet — a loan account opens automatically on the first
        confirmed disbursement tranche (LOS → Disburse).
      </Typography>
    );
  }
  return (
    <Box sx={{ mt: 1 }}>
      <Table size="small" sx={{ '& td, & th': { fontSize: 12.5 } }}>
        <TableHead>
          <TableRow sx={{ '& th': { fontWeight: 600, color: tokens.muted } }}>
            <TableCell>Group Code</TableCell>
            <TableCell>Company</TableCell>
            <TableCell align="right">₹ Cr</TableCell>
            <TableCell>Stage</TableCell>
            <TableCell>Analyst</TableCell>
            <TableCell />
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.id} hover sx={{ cursor: 'pointer' }}
              onClick={() => setOpen(r)}>
              <TableCell>{r.code}</TableCell>
              <TableCell><b>{r._name}</b></TableCell>
              <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                {fmt(r.amt)}
              </TableCell>
              <TableCell>{r.stage}</TableCell>
              <TableCell>{r.an}</TableCell>
              <TableCell align="right">
                <Button size="small" sx={{ textTransform: 'none', fontSize: 12 }}>
                  Open account
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <AccountDialog row={open} onClose={() => setOpen(null)} />
    </Box>
  );
}
