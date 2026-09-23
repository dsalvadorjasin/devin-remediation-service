import { defineConfig, devices } from '@playwright/test'

const isCI = !!process.env.CI

export default defineConfig({
  testDir: './tests',
  outputDir: 'test-results',
  fullyParallel: true,
  forbidOnly: isCI,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    baseURL: 'http://localhost:5173',
    screenshot: 'on',
    video: 'on',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'npm --prefix ../frontend run dev -- --port 5173',
    url: 'http://localhost:5173',
    reuseExistingServer: !isCI,
    timeout: 60_000,
  },
})
