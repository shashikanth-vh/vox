import { useMemo } from 'react';
import { Box, Typography } from '@mui/material';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import { syndicationService, LSTATE_COLOR, type BankRow } from '../../services/syndicationService';
import { fmt } from '../../utils/format';
import { tokens } from '../../theme';

// Port of v12 vSynReg() on the shared MRT table (no actions column). Aggregate
// register by financial institution; a row click opens the bank's deal ledger.
export default function SynRegisterView({ onOpenBank }: { onOpenBank: (name: string) => void }) {
  // Right-aligned count column, "—" when zero (v12 renders the em dash for 0).
  const num = (id: keyof BankRow, header: string, size = 100): MRT_ColumnDef<BankRow> => ({
    accessorKey: id as string, header, size,
    muiTableHeadCellProps: { align: 'right' },
    muiTableBodyCellProps: { align: 'right', sx: { fontVariantNumeric: 'tabular-nums' } },
    Cell: ({ cell }) => <>{cell.getValue<number>() || '—'}</>,
  });

  const columns = useMemo<MRT_ColumnDef<BankRow>[]>(() => [
    { accessorKey: 'name', header: 'Bank', size: 220, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b> },
    { id: 'dots', header: 'Status distribution', size: 200, enableSorting: false, enableColumnFilter: false,
      Cell: ({ row }) => (
        <Box sx={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center' }}>
          {row.original.dots.map((st, i) => (
            <Box key={i} component="span" title={st}
              sx={{ display: 'inline-block', width: 9, height: 9, borderRadius: '50%', mr: '2px', bgcolor: LSTATE_COLOR[st] || '#94a3b8' }} />
          ))}
        </Box>
      ) },
    num('pursued', '# Pursued'),
    num('sanc', 'Sanctioned', 110),
    num('ip', 'IP Rec', 90),
    num('queries', 'Queries', 90),
    num('imCirc', 'IM Circ', 90),
    num('decl', 'Declined', 100),
    { accessorKey: 'amt', header: '₹ Cr', size: 90,
      muiTableHeadCellProps: { align: 'right' },
      muiTableBodyCellProps: { align: 'right', sx: { fontVariantNumeric: 'tabular-nums' } },
      Cell: ({ cell }) => <>{fmt(cell.getValue<number>(), 1)}</> },
  ], []);

  return (
    <>
      <Typography sx={{ fontSize: 11.8, color: tokens.muted, mb: 1.2 }}>
        Aggregate view by financial institution. Colour dots show status distribution across active + closed
        deals. Click a bank for full deal history.
      </Typography>
      <CommonTable<BankRow>
        queryKey={['syn', 'reg']}
        fetcher={(q) => syndicationService.registerList(q)}
        columns={columns}
        csvName="atlas_platform_deals_by_bank"
        actionsEnabled={false}
        onRowClick={(b) => onOpenBank(b.name)}
      />
    </>
  );
}
