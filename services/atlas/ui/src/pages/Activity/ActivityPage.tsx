import { useMemo, useState } from 'react';
import { Box, Chip, Typography, Paper, Button, Dialog, DialogTitle, DialogContent, DialogActions } from '@mui/material';
import { useQuery } from '@tanstack/react-query';
import type { MRT_ColumnDef } from 'material-react-table';
import CommonTable from '../../components/table/CommonTable';
import PageHint from '../../components/common/PageHint';
import { CodeText } from '../../components/common/Pills';
import { activityService } from '../../services/activityService';
import { tokens } from '../../theme';
import type { ActivityRow } from '../../services/activityService';

const AREA_COLOR: Record<string, string> = {
  Leads: tokens.tealHi, Deals: tokens.navy2, Lending: tokens.lend, 'Platform Deals': tokens.synd,
  'Asset Mon': tokens.am, Clients: tokens.ok, FI: '#2D6FC4', Team: tokens.warn, Documents: tokens.teal,
  Session: tokens.muted, System: tokens.muted, Other: tokens.muted,
};

function AreaPill({ area }: { area: string }) {
  return (
    <Box component="span" sx={{ display: 'inline-block', color: '#fff', fontWeight: 700, fontSize: 11,
      px: '10px', py: '2px', borderRadius: '99px', whiteSpace: 'nowrap',
      bgcolor: AREA_COLOR[area] || tokens.muted }}>{area}</Box>
  );
}

const detailRow = (label: string, value: React.ReactNode) => (
  <>
    <Typography sx={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px', color: tokens.muted, fontWeight: 700 }}>{label}</Typography>
    <Typography component="div" sx={{ fontSize: 12.8 }}>{value}</Typography>
  </>
);

export default function ActivityPage() {
  const [area, setArea] = useState<string>('');
  const [view, setView] = useState<ActivityRow | null>(null);
  // Unpaged stream, only for the KPI chips and the area filter list — the table pages server-side.
  const { data: all = [] } = useQuery({ queryKey: ['activity', 'all'], queryFn: () => activityService.listAll() });

  const kpi = useMemo(() => {
    const today = all[0]?.t ? all[0].t.slice(0, 10) : '';
    const who = new Set<string>(), ents = new Set<string>();
    let nToday = 0;
    all.forEach((e) => {
      if (e.t && e.t.slice(0, 10) === today) nToday++;
      if (e.by) who.add(e.by);
      if (e.code) ents.add(e.code);
    });
    return { total: all.length, today: nToday, people: who.size, companies: ents.size };
  }, [all]);

  // No "All" chip — clicking the active chip clears the filter. 'System' isn't a
  // business area anyone filters by, so it's left out of the chip row.
  const areasPresent = useMemo(() => {
    const seen: string[] = [];
    all.forEach((e) => { if (e.area !== 'System' && !seen.includes(e.area)) seen.push(e.area); });
    return seen;
  }, [all]);

  const columns = useMemo<MRT_ColumnDef<ActivityRow>[]>(() => [
    { accessorKey: 't', header: 'When', size: 150,
      muiTableBodyCellProps: { sx: { whiteSpace: 'nowrap', color: tokens.muted } } },
    { accessorKey: 'by', header: 'Who', size: 140, meta: { localFilter: true }, Cell: ({ cell }) => <b>{cell.getValue<string>()}</b> },
    { accessorKey: 'area', header: 'Area', size: 130, Cell: ({ cell }) => <AreaPill area={cell.getValue<string>()} /> },
    { accessorKey: 'text', header: 'Activity', size: 420 },
    { accessorKey: 'company', header: 'Company', size: 200, meta: { localFilter: true },
      Cell: ({ row }) => {
        const { code, company } = row.original;
        if (!code) return '—';
        return (
          <>
            {company && company !== code && <b>{company}</b>}{' '}
            <CodeText code={code} />
          </>
        );
      } },
  ], []);

  const kchip = (label: string, value: number) => (
    <Paper variant="outlined" sx={{ px: 1.5, py: 0.7, borderColor: tokens.line, display: 'flex', alignItems: 'baseline', gap: 0.7,
      borderTop: `2.5px solid ${tokens.tealHi}` }}>
      <Typography component="b" sx={{ fontSize: 15, fontWeight: 700 }}>{value}</Typography>
      <Typography sx={{ fontSize: 12, color: tokens.muted }}>{label}</Typography>
    </Paper>
  );

  return (
    <>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', mb: 1 }}>
        {kchip('activities', kpi.total)}
        {kchip('today', kpi.today)}
        {kchip('people', kpi.people)}
        {kchip('companies touched', kpi.companies)}
        <Box sx={{ flex: 1 }} />
        {areasPresent.map((a) => (
          <Chip key={a} label={a} clickable variant={area === a ? 'filled' : 'outlined'}
            onClick={() => setArea(area === a ? '' : a)}
            sx={area === a ? { bgcolor: AREA_COLOR[a] || tokens.muted, color: '#fff' } : {}} />
        ))}
      </Box>
      <PageHint>Who did what, in plain English — every operation performed in the console, newest first. The technical field-level trail lives on the Audit tab.</PageHint>

      <CommonTable<ActivityRow>
        queryKey={['activity', area]}
        fetcher={(q) => activityService.list(q, area)}
        columns={columns}
        csvName="atlas_activity"
        // No View icon — clicking the row opens the activity detail.
        onRowClick={(r) => setView(r)}
        mobileCard={{
          primary: (r) => r.text,
          value: (r) => <AreaPill area={r.area} />,
        }}
      />

      <Dialog open={!!view} onClose={() => setView(null)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: 15, fontWeight: 700 }}>Activity detail</DialogTitle>
        <DialogContent dividers>
          {view && (
            <Box sx={{ display: 'grid', gridTemplateColumns: 'auto 1fr', columnGap: 2, rowGap: 1.2, alignItems: 'baseline' }}>
              {detailRow('When', view.t)}
              {detailRow('Who', <b>{view.by}</b>)}
              {detailRow('Area', <AreaPill area={view.area} />)}
              {detailRow('Action', view.act)}
              {detailRow('Activity', view.text)}
              {detailRow('Company', view.code
                ? <>{view.company && view.company !== view.code && <b>{view.company}</b>}{' '}<CodeText code={view.code} /></>
                : '—')}
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setView(null)}>Close</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
