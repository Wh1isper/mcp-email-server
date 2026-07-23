import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  outputDir: 'test-results',
  reporter: [['line']],
  use: {
    browserName: 'chromium',
    headless: true,
    screenshot: 'off',
    trace: 'off',
    video: 'off',
  },
})
