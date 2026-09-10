import { defineConfig, devices } from '@playwright/test'
import fs from 'node:fs'
const credentialsFile = process.env.WEB_CREDENTIALS_FILE || '/srv/investment-dashboard/web-access.txt'
let httpCredentials: { username: string; password: string } | undefined
if (fs.existsSync(credentialsFile)) {
 const lines = fs.readFileSync(credentialsFile, 'utf8').trim().split('\n')
 httpCredentials = { username: lines[0].split('：')[1], password: lines[1].split('：')[1] }
}
export default defineConfig({
 testDir: './tests', workers: 1, timeout: 45000, reporter: 'list',
 outputDir: '/tmp/position-web-verification/browser',
 use: { baseURL: process.env.WEB_TEST_URL || 'https://portfolio.nineskyit.top', ignoreHTTPSErrors: process.env.WEB_TEST_INSECURE_TLS === '1', httpCredentials, trace: 'off' },
 projects: [
  { name: 'iphone-webkit', use: { ...devices['iPhone 13'], browserName: 'webkit' } },
  { name: 'mobile-chromium', use: { ...devices['Pixel 7'], browserName: 'chromium', launchOptions: {args:['--disable-dev-shm-usage','--disable-gpu']}  } },
  { name: 'desktop', use: { viewport: { width: 1440, height: 900 }, browserName: 'chromium', launchOptions: {args:['--disable-dev-shm-usage','--disable-gpu']}  } },
 ],
})
