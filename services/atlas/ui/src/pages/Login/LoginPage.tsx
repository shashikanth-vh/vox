import { useEffect, useRef, useState } from 'react';
import { Box, Paper, Typography, TextField, Button, Divider, Alert, CircularProgress } from '@mui/material';
import { useAuth } from '../../auth/AuthContext';
import { GOOGLE_SSO_CLIENT_ID } from '../../api/axiosClient';
import { renderGoogleButton } from '../../auth/googleIdentity';
import { tokens } from '../../theme';

// Login field label with the mandatory red asterisk (Forms spec: both fields MANDATORY).
function Lbl({ children }: { children: React.ReactNode }) {
  return (
    <Typography sx={{ textAlign: 'left', fontSize: 10.4, fontWeight: 700, color: tokens.muted, mb: '4px', textTransform: 'uppercase', letterSpacing: '.5px' }}>
      {children}<Box component="span" sx={{ color: tokens.bad, ml: '2px' }}>*</Box>
    </Typography>
  );
}

// The brand panel's product chips — the three desks the console runs.
const DESKS = ['Lending', 'Platform Deals', 'Asset Monetisation'];

export default function LoginPage() {
  const { signIn, signInWithGoogle, signInWithGoogleCredential } = useAuth();
  const [u, setU] = useState('');
  const [p, setP] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  // PER-USER Google sign-in (Google Identity Services), switched on by a configured
  // GOOGLE_SSO_CLIENT_ID: Google's own button renders below, the user picks THEIR
  // account, and Google hands back an id_token minted for that person — which the
  // gateway then verifies like any bearer. Without the client id, the legacy
  // fixed-identity shortcut button stays.
  const gisRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!GOOGLE_SSO_CLIENT_ID || !gisRef.current) return;
    renderGoogleButton(GOOGLE_SSO_CLIENT_ID, gisRef.current, (credential) => {
      setErr(''); setBusy(true);
      signInWithGoogleCredential(credential)
        .catch((e: any) => setErr(e?.message || 'Google sign-in failed.'))
        .finally(() => setBusy(false));
    }).catch((e: any) => setErr(e?.message || 'Google sign-in could not load.'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Both fields are mandatory before a credentialed sign-in (Google SSO stays optional).
  // The credentials go to PRISM via authService; anything the backend rejects comes back
  // as an AuthError whose message is already user-facing.
  const trySignIn = async () => {
    if (!u.trim() || !p.trim()) { setErr('Username / email and password are both required.'); return; }
    setErr(''); setBusy(true);
    try {
      await signIn(u.trim(), p);
    } catch (e: any) {
      setErr(e?.message || 'Sign-in failed. Please try again.');
    } finally {
      setBusy(false);
    }
  };

  // Google goes straight to https://oauth2.googleapis.com/token with folder 00c's refresh
  // grant. The username/password fields are irrelevant to it and are deliberately NOT
  // validated — the refresh token is the credential.
  const tryGoogle = async () => {
    setErr(''); setBusy(true);
    try {
      await signInWithGoogle();
    } catch (e: any) {
      setErr(e?.message || 'Google sign-in failed.');
    } finally {
      setBusy(false);
    }
  };

  const GoogleG = () => (
    <svg width="17" height="17" viewBox="0 0 48 48" aria-hidden>
      <path fill="#FFC107" d="M43.6 20.1H42V20H24v8h11.3C33.7 32.7 29.3 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3l5.7-5.7C34.3 6 29.4 4 24 4 13 4 4 13 4 24s9 20 20 20 20-9 20-20c0-1.3-.1-2.6-.4-3.9z" />
      <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.9 1.2 8 3l5.7-5.7C34.3 6 29.4 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
      <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.3 0-9.7-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z" />
      <path fill="#1976D2" d="M43.6 20.1H42V20H24v8h11.3c-.8 2.2-2.2 4.1-4.1 5.6l6.2 5.2C40.9 35.6 44 30.3 44 24c0-1.3-.1-2.6-.4-3.9z" />
    </svg>
  );

  // The e-in-a-ring wordmark, shared by both halves.
  const Mark = ({ size = 54 }: { size?: number }) => (
    <Box sx={{ width: size, height: size, borderRadius: '26%', bgcolor: tokens.teal, color: '#fff', fontWeight: 800,
      fontSize: size * 0.52, display: 'flex', alignItems: 'center', justifyContent: 'center', letterSpacing: '-1px',
      boxShadow: '0 10px 30px rgba(13,115,119,.45)' }}>e</Box>
  );

  return (
    <Box sx={{ minHeight: '100vh', display: 'flex', flexDirection: { xs: 'column', md: 'row' } }}>

      {/* ---- Brand panel: the company's story, not just a backdrop ------------- */}
      <Box sx={{
        flex: { md: '1 1 54%' }, position: 'relative', overflow: 'hidden',
        display: 'flex', flexDirection: 'column', justifyContent: { xs: 'center', md: 'space-between' },
        px: { xs: 3, md: 7 }, py: { xs: 3.5, md: 6 },
        color: '#fff',
        background: 'linear-gradient(150deg,#0A1A2F 0%,#122B47 46%,#0B4A47 100%)',
        // Two slow-breathing radial glows give the panel life without any imagery.
        '&::before, &::after': {
          content: '""', position: 'absolute', borderRadius: '50%', filter: 'blur(70px)', opacity: 0.5,
          animation: 'evamGlow 9s ease-in-out infinite alternate',
        },
        '&::before': { width: 460, height: 460, right: -120, top: -140, background: 'radial-gradient(circle,#12917E 0%,transparent 70%)' },
        '&::after': { width: 380, height: 380, left: -110, bottom: -150, background: 'radial-gradient(circle,#1D5F8A 0%,transparent 70%)', animationDelay: '2.5s' },
        '@keyframes evamGlow': { from: { transform: 'scale(1) translateY(0)' }, to: { transform: 'scale(1.18) translateY(18px)' } },
      }}>
        <Box sx={{ position: 'relative', zIndex: 1, display: 'flex', alignItems: 'center', gap: 1.4 }}>
          <Mark size={44} />
          <Box>
            <Typography sx={{ fontWeight: 800, fontSize: 17, letterSpacing: '.4px', lineHeight: 1.1 }}>EVAM FINANCE</Typography>
            <Typography sx={{ fontSize: 11, opacity: 0.68, letterSpacing: '2.4px', fontWeight: 600 }}>PRISM · ATLAS</Typography>
          </Box>
        </Box>

        <Box sx={{ position: 'relative', zIndex: 1, my: { xs: 3, md: 0 }, maxWidth: 560 }}>
          <Typography sx={{ fontWeight: 800, fontSize: { xs: 26, md: 40 }, lineHeight: 1.12, letterSpacing: '-0.5px' }}>
            Financing India&apos;s<br />
            <Box component="span" sx={{
              background: 'linear-gradient(90deg,#3ED6A9,#8FE3CF)',
              WebkitBackgroundClip: 'text', backgroundClip: 'text', color: 'transparent',
            }}>climate transition</Box>
          </Typography>
          <Typography sx={{ mt: 1.8, fontSize: { xs: 13, md: 15 }, lineHeight: 1.65, opacity: 0.82, maxWidth: 470 }}>
            Debt, syndication and asset monetisation for clean-energy businesses —
            and this console is where that book runs: every lead, every lender,
            every sanction, one register.
          </Typography>
          <Box sx={{ mt: 3, display: 'flex', gap: 1, flexWrap: 'wrap' }}>
            {DESKS.map((d) => (
              <Box key={d} sx={{
                px: 1.6, py: 0.65, borderRadius: 99, fontSize: 12, fontWeight: 600, letterSpacing: '.2px',
                color: '#DFF7EE', border: '1px solid rgba(143,227,207,.35)',
                background: 'rgba(255,255,255,.06)', backdropFilter: 'blur(6px)',
              }}>{d}</Box>
            ))}
          </Box>
        </Box>

        <Typography sx={{ position: 'relative', zIndex: 1, fontSize: 11.5, opacity: 0.55, display: { xs: 'none', md: 'block' } }}>
          © {new Date().getFullYear()} Evam Finance · secured with verified sign-in
        </Typography>
      </Box>

      {/* ---- Sign-in panel ----------------------------------------------------- */}
      <Box sx={{
        flex: { md: '1 1 46%' }, display: 'flex', alignItems: 'center', justifyContent: 'center',
        p: { xs: 2.5, md: 6 }, bgcolor: '#F4F7F7', minHeight: { xs: 'auto', md: '100vh' },
      }}>
        <Paper elevation={0} sx={{ borderRadius: 4, p: '36px 38px 32px', width: '100%', maxWidth: 400, textAlign: 'center',
          border: `1px solid ${tokens.line}`, boxShadow: '0 18px 50px rgba(10,26,47,.10)' }}>
          <Typography sx={{ fontSize: 21, color: tokens.navy, fontWeight: 800, letterSpacing: '-0.3px' }}>Welcome back</Typography>
          <Typography sx={{ color: tokens.muted, fontSize: 12.6, mt: 0.6, mb: 2.6 }}>Sign in to your deal flow &amp; pipeline console</Typography>

          <Box sx={{ mb: 1.2 }}>
            <Lbl>Username or email</Lbl>
            <TextField fullWidth placeholder="Username or email" value={u} onChange={(e) => { setU(e.target.value); setErr(''); }}
              error={!!err && !u.trim()} sx={{ '& .MuiOutlinedInput-input': { py: '8.5px' } }} />
          </Box>
          <Box sx={{ mb: 1.4 }}>
            <Lbl>Password</Lbl>
            <TextField fullWidth type="password" placeholder="Password" value={p} onChange={(e) => { setP(e.target.value); setErr(''); }}
              error={!!err && !p.trim()} sx={{ '& .MuiOutlinedInput-input': { py: '8.5px' } }}
              onKeyDown={(e) => e.key === 'Enter' && trySignIn()} />
          </Box>
          {err && <Alert severity="warning" sx={{ mb: 1.4, py: 0, fontSize: 12, textAlign: 'left' }}>{err}</Alert>}
          <Button fullWidth variant="contained" size="large" onClick={trySignIn} disabled={busy} sx={{ py: 1.2 }}
            startIcon={busy ? <CircularProgress size={16} color="inherit" /> : undefined}>
            {busy ? 'Signing in…' : 'Sign in'}
          </Button>

          <Divider sx={{ my: 1.8, color: tokens.muted, fontSize: 11 }}>or</Divider>

          {GOOGLE_SSO_CLIENT_ID ? (
            <Box ref={gisRef} sx={{ display: 'flex', justifyContent: 'center', minHeight: 44 }} />
          ) : (
            <Button fullWidth variant="outlined" size="large" startIcon={<GoogleG />} onClick={tryGoogle} disabled={busy}
              sx={{ py: 1.2, color: tokens.ink, borderColor: tokens.line, fontWeight: 600 }}>Continue with Google</Button>
          )}
        </Paper>
      </Box>
    </Box>
  );
}
