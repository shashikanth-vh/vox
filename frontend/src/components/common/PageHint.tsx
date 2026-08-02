import { Typography } from '@mui/material';
import { tokens } from '../../theme';
export default function PageHint({ children }: { children: React.ReactNode }) {
  return <Typography sx={{ fontSize: 11.6, color: tokens.muted, mb: 1.2 }}>{children}</Typography>;
}
