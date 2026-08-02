import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  // Dex answers the password grant on :5556, but it sends no CORS headers and the NGINX
  // edge (:8443) does NOT route /dex — it hands /dex/* to the Register, which answers 401.
  // So proxy /dex through the dev server: the browser stays same-origin and VITE_DEX_URL
  // just points back at this origin.
  const dexTarget = env.VITE_DEX_TARGET || 'http://localhost:5556';
  return {
    plugins: [react()],
    server: {
      port: 5173,
      open: true,
      proxy: {
        '/dex': { target: dexTarget, changeOrigin: true },
        // Escape hatch for the Google refresh grant. Google's token endpoint does answer
        // cross-origin, so this is only needed when a CSP or a corporate proxy gets in the
        // way — set VITE_GOOGLE_TOKEN_URL to http://localhost:5173/google-oauth/token.
        '/google-oauth': {
          target: 'https://oauth2.googleapis.com',
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/google-oauth/, ''),
        },
      },
    },
  };
});
