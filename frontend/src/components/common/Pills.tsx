import { Chip } from '@mui/material';
import { tokens } from '../../theme';

export function LensPill({ lens }: { lens?: string }) {
  if (!lens) return null;
  const mit = lens === 'Mitigation';
  return <Chip label={mit ? 'MIT' : 'ADP'} size="small"
    sx={{ fontWeight: 700, bgcolor: mit ? '#E4F1EC' : '#E8ECF8', color: mit ? '#22684A' : '#39518F' }} />;
}

export function TempPill({ temp }: { temp?: string }) {
  const map: Record<string, [string, string]> = {
    Hot: ['#FBE9E4', '#A93B22'], Warm: ['#FBF3E1', '#8F6512'], Cold: ['#EDF1F3', '#5F6E76'],
  };
  const c = temp && map[temp];
  if (!c) return <span style={{ color: tokens.muted }}>—</span>;
  return <Chip label={temp} size="small" sx={{ fontWeight: 700, bgcolor: c[0], color: c[1] }} />;
}

export function ProductFlags({ lend, syn, am, full }: { lend?: boolean; syn?: boolean; am?: boolean; full?: boolean }) {
  const flag = (on: boolean | undefined, label: string, color: string) => (
    <span style={{ display: 'inline-block', borderRadius: 5, padding: '2px 7px', fontSize: 10.4,
      fontWeight: 700, color: '#fff', marginRight: 3, background: color, opacity: on ? 1 : 0.16 }}>{label}</span>
  );
  // `full` spells the products out (drawer); the compact form keeps single-letter tags (table cells).
  return <>{flag(lend, full ? 'Lending' : 'L', tokens.lend)}{flag(syn, full ? 'Platform Deals' : 'S', tokens.synd)}{flag(am, full ? 'Asset Mon' : 'AM', tokens.am)}</>;
}

// v12 LIFE_STAGES / LIFE_CLR — the Vistaar journey stage on every client.
export const LIFE_STAGES = ['Prospect', 'Onboarded', 'Active', 'Serviced', 'Vistaar — Expansion', 'Dormant'];
const LIFE_CLR: Record<string, string> = {
  'Prospect': '#5F6E76', 'Onboarded': '#1F6FA8', 'Active': '#0D7377',
  'Serviced': '#2E7D4F', 'Vistaar — Expansion': '#7A4FC4', 'Dormant': '#9AA7AD',
};

export function LifePill({ stage }: { stage?: string }) {
  const v = stage || 'Prospect';
  return (
    <span style={{ display: 'inline-block', background: LIFE_CLR[v] || '#5F6E76', color: '#fff',
      fontWeight: 700, fontSize: 10.4, padding: '2px 9px', borderRadius: 99, whiteSpace: 'nowrap' }}>{v}</span>
  );
}

export function CodeText({ code }: { code?: string }) {
  return <span style={{ fontFamily: 'ui-monospace,Consolas,monospace', fontSize: 11.4, color: tokens.teal, fontWeight: 600 }}>{code}</span>;
}
