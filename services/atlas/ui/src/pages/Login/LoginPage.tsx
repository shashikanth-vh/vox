import { useEffect, useRef, useState } from 'react';
import { Box, Paper, Typography, TextField, Button, Divider, Alert, CircularProgress } from '@mui/material';
import { useAuth } from '../../auth/AuthContext';
import { GOOGLE_SSO_CLIENT_ID } from '../../api/axiosClient';
import { tokens } from '../../theme';

// Login field label with the mandatory red asterisk (Forms spec: both fields MANDATORY).
function Lbl({ children }: { children: React.ReactNode }) {
  return (
    <Typography sx={{ textAlign: 'left', fontSize: 10.4, fontWeight: 700, color: tokens.muted, mb: '4px', textTransform: 'uppercase', letterSpacing: '.5px' }}>
      {children}<Box component="span" sx={{ color: tokens.bad, ml: '2px' }}>*</Box>
    </Typography>
  );
}

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
    if (!GOOGLE_SSO_CLIENT_ID) return;
    const setup = () => {
      const g = (window as any).google?.accounts?.id;
      if (!g || !gisRef.current) return;
      g.initialize({
        client_id: GOOGLE_SSO_CLIENT_ID,
        callback: (resp: any) => {
          setErr(''); setBusy(true);
          signInWithGoogleCredential(String(resp?.credential || ''))
            .catch((e: any) => setErr(e?.message || 'Google sign-in failed.'))
            .finally(() => setBusy(false));
        },
      });
      g.renderButton(gisRef.current, {
        theme: 'outline', size: 'large', width: 320, text: 'continue_with',
      });
    };
    if ((window as any).google?.accounts?.id) { setup(); return; }
    const s = document.createElement('script');
    s.src = 'https://accounts.google.com/gsi/client';
    s.async = true;
    s.onload = setup;
    document.head.appendChild(s);
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

  return (
    <Box sx={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', p: 2.5,
      background: 'linear-gradient(160deg,#16233E 0%,#1B2A4A 55%,#0D3B3E 100%)' }}>
      <Paper elevation={0} sx={{ borderRadius: 4, p: '38px 40px 34px', width: '100%', maxWidth: 400, textAlign: 'center',
        boxShadow: '0 24px 70px rgba(5,12,25,.5)' }}>
        <Box sx={{ width: 58, height: 58, borderRadius: 3.5, bgcolor: tokens.teal, color: '#fff', fontWeight: 800, fontSize: 30,
          display: 'flex', alignItems: 'center', justifyContent: 'center', mx: 'auto', mb: 1.8, letterSpacing: '-1px' }}>e</Box>
        <Typography sx={{ fontSize: 21, color: tokens.navy, fontWeight: 700 }}>Welcome to ATLAS</Typography>
        <Typography sx={{ color: tokens.muted, fontSize: 12.6, mt: 0.6, mb: 2.6 }}>Evam Finance · deal flow &amp; pipeline console</Typography>

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
  );
}
