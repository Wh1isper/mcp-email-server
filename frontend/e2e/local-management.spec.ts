import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import { spawn } from 'node:child_process'
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontend = dirname(dirname(fileURLToPath(import.meta.url)))
const repository = resolve(frontend, '..')
const launchPattern = /http:\/\/127\.0\.0\.1:\d+\/manage-[A-Za-z0-9_-]+\/#bootstrap=[A-Za-z0-9_-]+/
const incomingSecret = 'synthetic-browser-incoming-secret'
const rotatedSecret = 'synthetic-browser-rotated-secret'
const legacySecret = 'synthetic-browser-legacy-secret'

const legacyConfig = `
mode = "legacy"
db_location = "db.sqlite3"
credential_storage = "plaintext"

[[emails]]
account_name = "earlier-work"
full_name = "Earlier Browser Test"
email_address = "earlier@example.test"
save_to_sent = true

[emails.incoming]
host = "imap.earlier.example.test"
port = 993
user_name = "earlier@example.test"
password = "${legacySecret}"
use_ssl = true
start_ssl = false
verify_ssl = true
`

const keyringBackend = `
import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class FileKeyring(KeyringBackend):
    priority = 1

    @property
    def path(self):
        return Path(os.environ["MCP_EMAIL_TEST_KEYRING"])

    def _read(self):
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _write(self, values):
        self.path.write_text(json.dumps(values))
        self.path.chmod(0o600)

    def get_password(self, service, username):
        return self._read().get(f"{service}:{username}")

    def set_password(self, service, username, password):
        values = self._read()
        values[f"{service}:{username}"] = password
        self._write(values)

    def delete_password(self, service, username):
        values = self._read()
        key = f"{service}:{username}"
        if key not in values:
            raise PasswordDeleteError("missing synthetic credential")
        del values[key]
        self._write(values)
`

function startUi(initialConfig?: string) {
  const root = mkdtempSync(join(tmpdir(), 'mcp-email-browser-'))
  chmodSync(root, 0o700)
  writeFileSync(join(root, 'browser_keyring.py'), keyringBackend)
  if (initialConfig) {
    writeFileSync(join(root, 'config.toml'), initialConfig)
    chmodSync(join(root, 'config.toml'), 0o600)
  }
  const catalog = join(root, 'managed.sqlite3')
  const command = process.env.MCP_EMAIL_UI_COMMAND ?? 'uv run mcp-email-server ui --no-open --port 0'
  const child = spawn('script', ['-q', '-e', '-c', command, '/dev/null'], {
    cwd: repository,
    detached: true,
    env: {
      ...process.env,
      MCP_EMAIL_SERVER_CONFIG_PATH: join(root, 'config.toml'),
      MCP_EMAIL_TEST_KEYRING: join(root, 'keyring.json'),
      PYTHON_KEYRING_BACKEND: 'browser_keyring.FileKeyring',
      PYTHONPATH: [root, process.env.PYTHONPATH].filter(Boolean).join(':'),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  let output = ''
  const launchUrl = new Promise<string>((resolveLaunch, rejectLaunch) => {
    const timeout = setTimeout(() => rejectLaunch(new Error('Local UI did not produce an interactive launch URL.')), 20_000)
    const inspect = (chunk: Buffer) => {
      output = `${output}${chunk.toString()}`.slice(-16_384)
      const match = output.match(launchPattern)
      if (match?.[0]) {
        clearTimeout(timeout)
        resolveLaunch(match[0])
      }
    }
    child.stdout.on('data', inspect)
    child.stderr.on('data', inspect)
    child.once('exit', (code) => {
      clearTimeout(timeout)
      rejectLaunch(new Error(`Local UI exited before browser launch (code ${String(code)}).`))
    })
  })

  const stop = async () => {
    if (child.exitCode === null && child.pid) {
      process.kill(-child.pid, 'SIGTERM')
      await Promise.race([
        new Promise<void>((resolveExit) => child.once('exit', () => resolveExit())),
        new Promise<void>((resolveTimeout) => setTimeout(resolveTimeout, 5_000)),
      ])
    }
    if (child.exitCode === null && child.pid) process.kill(-child.pid, 'SIGKILL')
    rmSync(root, { recursive: true, force: true })
  }

  return { catalog, child, launchUrl, stop }
}

async function openWithRetry(page: Page, url: string): Promise<void> {
  let lastError: unknown
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded' })
      return
    } catch (error) {
      lastError = error
      await page.waitForTimeout(100)
    }
  }
  throw lastError
}

test('real browser completes secure local management workflows', async ({ browser, page }) => {
  test.skip(process.platform === 'win32', 'The V2 filesystem and PTY security contract is POSIX-only.')
  const ui = startUi()
  const pageErrors: string[] = []
  const requestUrls: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('request', (request) => requestUrls.push(request.url()))

  try {
    const launchUrl = await ui.launchUrl
    await openWithRetry(page, launchUrl)
    await expect(page.getByRole('heading', { name: 'Email accounts', exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'No email accounts yet' })).toBeVisible()
    await expect(page.getByText('Account settings ready')).toHaveCount(0)
    await expect(page.getByText('Setup details')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Add your first account' })).toHaveCount(1)
    expect(page.url()).not.toContain('#bootstrap=')

    await page.reload()
    await expect(page.getByRole('heading', { name: 'Email accounts', exact: true })).toBeVisible()

    const staleContext = await browser.newContext()
    const stalePage = await staleContext.newPage()
    await stalePage.goto(launchUrl)
    await expect(stalePage.getByRole('heading', { name: 'Open a fresh settings link' })).toBeVisible()
    expect(stalePage.url()).not.toContain('#bootstrap=')
    await staleContext.close()

    expect(ui.catalog).toContain('managed.sqlite3')
    const navigation = page.getByRole('navigation', { name: 'Settings sections' })
    await expect(navigation.getByRole('button')).toHaveCount(2)
    await navigation.getByRole('button', { name: 'Email accounts' }).focus()
    await page.keyboard.press('Tab')
    await expect(navigation.getByRole('button', { name: 'Settings & help' })).toBeFocused()

    await page.getByRole('button', { name: 'Add your first account' }).click()
    const backAction = page.getByRole('button', { name: 'Back to accounts' })
    expect((await backAction.boundingBox())?.height).toBeGreaterThanOrEqual(44)
    await page.getByLabel('Email address').fill('browser@example.test')
    await page.getByLabel('Password or app password').fill(incomingSecret)
    const setupDetails = page.locator('details.setup-details:not(.outgoing-section)')
    const setupSummary = page.getByText('Advanced account settings')
    await setupSummary.click()
    await page.getByLabel('Name shown on sent mail').fill('Browser Test')
    await expect(page.getByLabel('Incoming mail server')).toHaveValue('imap.example.test')
    const incomingOptions = page.locator('details.advanced-fields').first()
    await page.getByText('More incoming mail options').click()
    const incomingLogin = page.getByLabel('Login name')
    await incomingLogin.fill('')
    await page.getByText('More incoming mail options').click()
    await setupSummary.click()
    await page.getByRole('button', { name: 'Add account' }).click()
    await expect(setupDetails).toHaveJSProperty('open', true)
    await expect(incomingOptions).toHaveJSProperty('open', true)
    await expect(incomingLogin).toBeFocused()
    await incomingLogin.fill('browser@example.test')
    await page.getByRole('button', { name: 'Add account' }).click()
    await expect(page.getByRole('heading', { name: 'browser@example.test' })).toBeVisible()
    await expect(page.getByText('Email account added.')).toBeVisible()
    expect(await page.locator('body').innerText()).not.toContain(incomingSecret)

    await navigation.getByRole('button', { name: 'Settings & help' }).click()
    const safetySummary = page.locator('details.settings-disclosure > summary').filter({ hasText: 'Sending & attachment safety' })
    await safetySummary.focus()
    await expect(safetySummary).toBeFocused()
    expect(await safetySummary.evaluate((element) => getComputedStyle(element).boxShadow)).not.toBe('none')
    await safetySummary.click()
    await page.getByRole('button', { name: 'Add recipient' }).click()
    await page.getByLabel('Recipient 1').fill('browser-recipient@example.test')
    await page.getByRole('button', { name: 'Add sender pattern' }).click()
    await page.getByLabel('Sender pattern 1').fill('*@example.test')
    await page.getByRole('button', { name: 'Save safety settings' }).click()
    await expect(page.getByText('Safety settings saved.')).toBeVisible()
    await navigation.getByRole('button', { name: 'Email accounts' }).click()

    await page.getByRole('button', { name: 'Finish setup' }).click()
    await expect(page.getByRole('button', { name: 'Use these accounts' })).toBeVisible()
    await expect(page.getByText(/restart the mail server/i)).toHaveCount(0)
    await page.getByRole('button', { name: 'Use these accounts' }).click()
    await expect(page.getByText(/Restart the mail server to apply this change/i)).toBeVisible()

    await page.getByText('More', { exact: true }).click()
    await page.getByRole('button', { name: 'Pause account' }).click()
    await expect(page.getByText('Paused', { exact: true })).toBeVisible()
    await page.locator('details.account-more').evaluate((details: HTMLDetailsElement) => { details.open = true })
    await page.getByRole('button', { name: 'Enable account' }).click()
    await expect(page.getByText('Enabled', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: 'Edit' }).click()
    await page.getByLabel('Name shown on sent mail').fill('Browser Test Updated')
    await page.getByRole('button', { name: 'Save changes' }).click()
    await expect(page.getByText('Account settings saved.')).toBeVisible()

    await page.getByRole('button', { name: 'Password' }).click()
    await expect(page.getByRole('heading', { name: 'browser@example.test' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Test connection' })).toHaveCount(0)
    await page.getByLabel('New password or app password').fill(rotatedSecret)
    await page.getByRole('button', { name: 'Save password' }).click()
    await expect(page.getByText('IMAP password saved.')).toBeVisible()
    await expect(page.getByLabel('New password or app password')).toHaveValue('')

    await page.route('**/api/accounts/browser/credentials/incoming/set', async (route) => {
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({
          category: 'conflict',
          message: 'The managed configuration changed.',
          current: { account: 'work', revision: 99 },
        }),
      })
    }, { times: 1 })
    await page.getByLabel('New password or app password').fill('synthetic-conflict-secret')
    await page.getByRole('button', { name: 'Save password' }).click()
    await expect(page.getByRole('heading', { name: 'Review the latest settings' })).toBeVisible()
    await expect(page.getByLabel('New password or app password')).toHaveValue('')

    await page.getByRole('button', { name: 'Back to email accounts' }).click()
    await navigation.getByRole('button', { name: 'Settings & help' }).click()
    await expect(page.getByRole('heading', { name: 'Settings & help' })).toBeVisible()
    await page.getByText('Import existing settings', { exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Import email accounts' })).toBeVisible()
    await expect(page.getByText('0 accounts ready to import', { exact: true })).toBeVisible()
    await expect(page.getByText('Safety settings will also be copied.', { exact: true })).toBeVisible()
    await expect(page.getByRole('checkbox', { name: 'I reviewed these accounts and want to copy them.' })).toBeVisible()

    await navigation.getByRole('button', { name: 'Email accounts' }).click()
    await page.getByText('More', { exact: true }).click()
    await page.getByRole('button', { name: 'Remove account' }).click()
    await page.getByRole('textbox', { name: 'Account nickname' }).fill('browser')
    await page.getByRole('button', { name: 'Remove account' }).last().click()
    await expect(page.getByRole('heading', { name: 'No email accounts yet' })).toBeVisible()

    const storage = await page.evaluate(() => ({
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
      body: document.body.innerText,
    }))
    expect(storage.local).toEqual([])
    expect(storage.session).toEqual([])
    expect(storage.body).not.toContain(incomingSecret)
    expect(storage.body).not.toContain(rotatedSecret)
    expect(requestUrls.every((url) => !url.includes('bootstrap='))).toBe(true)

    await page.getByRole('button', { name: 'Sign out' }).click()
    await expect(page.getByRole('heading', { name: 'Signed out' })).toBeVisible()
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Open a fresh settings link' })).toBeVisible()
    expect(pageErrors).toEqual([])
  } finally {
    await ui.stop()
  }

  expect(ui.child.exitCode).not.toBeNull()
})


test('real browser explicitly prepares and imports detected earlier settings', async ({ page }) => {
  test.skip(process.platform === 'win32', 'The V2 filesystem and PTY security contract is POSIX-only.')
  const ui = startUi(legacyConfig)
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))

  try {
    await openWithRetry(page, await ui.launchUrl)
    await expect(page.getByText('Existing email settings found')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Import existing settings' })).toBeVisible()
    await expect(page.getByText('Account settings ready')).toHaveCount(0)

    await page.getByRole('button', { name: 'Import existing settings' }).click()
    await expect(page.getByRole('heading', { name: 'Settings & help' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Import email accounts' })).toBeVisible()
    await page.getByText('Source details', { exact: true }).click()
    await expect(page.getByText(/password from the earlier settings file/i)).toBeVisible()
    expect(await page.locator('body').innerText()).not.toContain(legacySecret)

    await page.getByRole('checkbox', { name: 'I reviewed these accounts and want to copy them.' }).check()
    await page.getByRole('button', { name: 'Import accounts' }).click()
    await expect(page.getByText('Import complete. 1 account(s) added and 0 password(s) restored.')).toBeVisible()

    await page.getByRole('navigation', { name: 'Settings sections' }).getByRole('button', { name: 'Email accounts' }).click()
    await expect(page.getByRole('heading', { name: 'earlier@example.test' })).toBeVisible()
    expect(await page.locator('body').innerText()).not.toContain(legacySecret)
    expect(pageErrors).toEqual([])
  } finally {
    await ui.stop()
  }

  expect(ui.child.exitCode).not.toBeNull()
})
