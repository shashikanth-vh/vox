import { Box } from '@mui/material';
import { tokens } from '../../theme';

const MOBILE = '@media (max-width:760px)';

export interface SubTab { id: string; label: string; icon?: string }

// v15 `.subbar` / `.subtab` — pill sub-switch inside a grouped tab (Masters, Activity).
// `right` renders on the same line, pushed to the far end (e.g. a view toggle).
export default function SubTabs({ items, value, onChange, right }: {
  items: SubTab[]; value: string; onChange: (id: string) => void; right?: React.ReactNode;
}) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: '6px', mb: '12px', pb: '2px', flexWrap: 'wrap' }}>
      <Box sx={{ display: 'flex', gap: '6px', overflowX: 'auto', flex: 1, minWidth: 0 }}>
        {items.map((it) => {
          const on = it.id === value;
          return (
            <Box component="button" key={it.id} onClick={() => onChange(it.id)}
              sx={{
                border: `1px solid ${on ? tokens.navy : tokens.line}`,
                bgcolor: on ? tokens.navy : tokens.card,
                color: on ? '#fff' : tokens.ink,
                borderRadius: '99px', px: '15px', py: '7px', fontSize: '12.6px', fontWeight: 700,
                cursor: 'pointer', whiteSpace: 'nowrap', fontFamily: 'inherit',
                '&:hover': on ? undefined : { borderColor: tokens.tealHi },
              }}>
              {it.icon ? it.icon + ' ' : ''}{it.label}
            </Box>
          );
        })}
      </Box>
      {/* On a phone the pill strip and a `right` control fight over one line: the strip
          is flex:1/minWidth:0, so it collapses to nothing and Clients/FI/Employees read
          as if the view toggle were sitting on top of them. Give `right` its own
          full-width row there instead — the container already wraps. */}
      {right && (
        <Box sx={{ display: 'flex', justifyContent: 'flex-end', flexShrink: 0,
          [MOBILE]: { flexBasis: '100%', order: 2, mt: '4px' } }}>
          {right}
        </Box>
      )}
    </Box>
  );
}
