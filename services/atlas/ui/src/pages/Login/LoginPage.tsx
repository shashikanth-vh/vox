import { useEffect, useRef, useState } from 'react';
import { Box, Paper, Typography, TextField, Button, Divider, Alert, CircularProgress } from '@mui/material';
import { useAuth } from '../../auth/AuthContext';
import { GOOGLE_SSO_CLIENT_ID } from '../../api/axiosClient';
import { renderGoogleButton } from '../../auth/googleIdentity';
import { tokens } from '../../theme';
import evamLogo from '../../assets/evam-logo.jpeg';

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
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden>
      <path fill="#FFC107" d="M43.6 20.1H42V20H24v8h11.3C33.7 32.7 29.3 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3l5.7-5.7C34.3 6 29.4 4 24 4 13 4 4 13 4 24s9 20 20 20 20-9 20-20c0-1.3-.1-2.6-.4-3.9z" />
      <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.9 1.2 8 3l5.7-5.7C34.3 6 29.4 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
      <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.3 0-9.7-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z" />
      <path fill="#1976D2" d="M43.6 20.1H42V20H24v8h11.3c-.8 2.2-2.2 4.1-4.1 5.6l6.2 5.2C40.9 35.6 44 30.3 44 24c0-1.3-.1-2.6-.4-3.9z" />
    </svg>
  );

  // Shared input styling: tall, softly rounded, placeholder-led (no visible labels —
  // the fields stay MANDATORY, enforced on submit).
  const fieldSx = {
    '& .MuiOutlinedInput-root': { borderRadius: '12px', bgcolor: '#fff' },
    '& .MuiOutlinedInput-input': { py: '13.5px', fontSize: 15 },
    '& .MuiOutlinedInput-notchedOutline': { borderColor: '#E4E9EC' },
    '& .MuiOutlinedInput-root:hover .MuiOutlinedInput-notchedOutline': { borderColor: '#C9D4D8' },
  } as const;

  return (
    <Box sx={{
      minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', p: 2.5,
      // Deep navy field with quiet teal auras in opposite corners.
      background: 'radial-gradient(900px 600px at 12% 8%, rgba(18,145,126,.14), transparent 60%),'
        + 'radial-gradient(1000px 700px at 88% 92%, rgba(18,145,126,.16), transparent 60%),'
        + 'linear-gradient(150deg,#0C1B31 0%,#122642 55%,#0E2E3C 100%)',
    }}>
      <Paper elevation={0} sx={{
        borderRadius: '22px', p: { xs: '34px 26px 30px', sm: '42px 56px 40px' },
        width: '100%', maxWidth: 520, textAlign: 'center', bgcolor: '#FCFDFD',
        boxShadow: '0 30px 90px rgba(4,10,22,.55)',
      }}>
        {/* Floating brand tile: the Evam wordmark on white, lifted by its own teal glow. */}
        <Box sx={{
          width: 172, height: 72, borderRadius: '20px', mx: 'auto', mb: 2.4, mt: '-72px',
          display: 'flex', alignItems: 'center', justifyContent: 'center', bgcolor: '#fff',
          boxShadow: '0 16px 40px rgba(13,115,119,.35), 0 0 0 6px rgba(252,253,253,1)',
          transform: 'translateY(36px)', position: 'relative', top: -36,
        }}>
          <Box component="img" src={evamLogo} alt="Evam Finance"
            sx={{ width: 132, height: 'auto', display: 'block' }} />
        </Box>

        <Typography sx={{ fontSize: 25, color: tokens.navy, fontWeight: 800, letterSpacing: '-0.4px' }}>
          Welcome to ATLAS
        </Typography>
        <Typography sx={{ color: tokens.muted, fontSize: 14.5, mt: 0.7, mb: 3.2 }}>
          Evam Finance · deal flow &amp; pipeline console
        </Typography>

        <TextField fullWidth placeholder="Username or email" value={u}
          onChange={(e) => { setU(e.target.value); setErr(''); }}
          error={!!err && !u.trim()} sx={{ ...fieldSx, mb: 1.6 }} />
        <TextField fullWidth type="password" placeholder="Password" value={p}
          onChange={(e) => { setP(e.target.value); setErr(''); }}
          error={!!err && !p.trim()} sx={{ ...fieldSx, mb: 2 }}
          onKeyDown={(e) => e.key === 'Enter' && trySignIn()} />

        {err && <Alert severity="warning" sx={{ mb: 1.6, py: 0, fontSize: 12.5, textAlign: 'left', borderRadius: '10px' }}>{err}</Alert>}

        <Button fullWidth size="large" onClick={trySignIn} disabled={busy}
          startIcon={busy ? <CircularProgress size={16} color="inherit" /> : undefined}
          sx={{
            py: 1.45, borderRadius: '12px', fontSize: 15.5, fontWeight: 700, color: '#fff',
            textTransform: 'none',
            background: 'linear-gradient(90deg,#0D7377 0%,#12917E 100%)',
            boxShadow: '0 10px 26px rgba(13,115,119,.35)',
            '&:hover': { background: 'linear-gradient(90deg,#0C686C 0%,#108573 100%)' },
            '&.Mui-disabled': { color: 'rgba(255,255,255,.7)', background: 'linear-gradient(90deg,#0D7377 0%,#12917E 100%)', opacity: 0.7 },
          }}>
          {busy ? 'Signing in…' : 'Sign in'}
        </Button>

        <Divider sx={{ my: 2.2, color: tokens.muted, fontSize: 12 }}>or</Divider>

        {GOOGLE_SSO_CLIENT_ID ? (
          <Box ref={gisRef} sx={{ display: 'flex', justifyContent: 'center', minHeight: 44 }} />
        ) : (
          <Button fullWidth size="large" startIcon={<GoogleG />} onClick={tryGoogle} disabled={busy}
            sx={{
              py: 1.35, borderRadius: '12px', fontSize: 15, fontWeight: 700, textTransform: 'none',
              color: tokens.ink, bgcolor: '#fff', border: '1px solid #E4E9EC',
              '&:hover': { bgcolor: '#F6F9F9', border: '1px solid #C9D4D8' },
            }}>
            Continue with Google
          </Button>
        )}
      </Paper>
    </Box>
  );
}
