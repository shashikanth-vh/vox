import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  workers: 1,
  retries: 0,
  timeout: 360_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.CHAT_E2E_URL || 'https://localhost:8443',
    ignoreHTTPSErrors: true,
    browserName: 'chromium',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    viewport: { width: 1440, height: 1000 },
    // Traces and HAR files include bearer credentials and are deliberately disabled.
    trace: 'off',
    screenshot: 'only-on-failure',
  },
});
