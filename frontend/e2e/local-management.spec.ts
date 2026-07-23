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

function startUi() {
  const root = mkdtempSync(join(tmpdir(), 'mcp-email-browser-'))
  chmodSync(root, 0o700)
  writeFileSync(join(root, 'browser_keyring.py'), keyringBackend)
  const catalog = join(root, 'catalog.sqlite3')
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
    await expect(page.getByRole('heading', { name: 'Runtime authority' })).toBeVisible()
    expect(page.url()).not.toContain('#bootstrap=')

    await page.reload()
    await expect(page.getByRole('heading', { name: 'Runtime authority' })).toBeVisible()

    const staleContext = await browser.newContext()
    const stalePage = await staleContext.newPage()
    await stalePage.goto(launchUrl)
    await expect(stalePage.getByRole('heading', { name: 'Open a fresh management link' })).toBeVisible()
    expect(stalePage.url()).not.toContain('#bootstrap=')
    await staleContext.close()

    await page.getByLabel('Database path').fill(ui.catalog)
    await page.getByRole('button', { name: 'Initialize' }).click()
    await expect(page.getByText('Staging catalog initialized. Review it before activation.')).toBeVisible()

    const statusTab = page.getByRole('tab', { name: 'Setup & status' })
    await statusTab.focus()
    await statusTab.press('ArrowRight')
    await expect(page.getByRole('tab', { name: 'Accounts' })).toBeFocused()
    await expect(page.getByRole('heading', { name: 'Managed accounts', exact: true })).toBeVisible()

    await page.getByRole('button', { name: 'Add account' }).click()
    await page.getByLabel('Account name').fill('work')
    await page.getByLabel('Display name').fill('Browser Test')
    await page.getByLabel('Email address').fill('browser@example.test')
    await page.getByLabel('Host', { exact: true }).fill('imap.example.test')
    await page.getByLabel('User name', { exact: true }).fill('browser@example.test')
    await page.getByLabel('Incoming credential').fill(incomingSecret)
    await page.getByRole('button', { name: 'Create account' }).click()
    await expect(page.getByRole('heading', { name: 'work' })).toBeVisible()
    await expect(page.getByText('Account saved.')).toBeVisible()
    expect(await page.locator('body').innerText()).not.toContain(incomingSecret)

    await page.getByRole('button', { name: 'Disable' }).click()
    await expect(page.getByText('Disabled', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Enable' }).click()
    await expect(page.getByText('Enabled', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Edit' }).click()
    await page.getByLabel('Display name').fill('Browser Test Updated')
    await page.getByRole('button', { name: 'Save changes' }).click()
    await expect(page.getByText('Account saved.')).toBeVisible()

    await page.getByRole('tab', { name: 'Credentials' }).click()
    await expect(page.getByLabel('Account')).toHaveValue('work')
    await page.getByLabel('New credential').fill(rotatedSecret)
    await page.getByRole('button', { name: 'Set credential' }).click()
    await expect(page.getByText('incoming credential is active.')).toBeVisible()
    await expect(page.getByLabel('New credential')).toHaveValue('')

    await page.route('**/api/accounts/work/credentials/incoming/set', async (route) => {
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
    await page.getByLabel('New credential').fill('synthetic-conflict-secret')
    await page.getByRole('button', { name: 'Set credential' }).click()
    await expect(page.getByRole('heading', { name: 'Review required: configuration changed' })).toBeVisible()
    await expect(page.getByLabel('New credential')).toHaveValue('')

    await page.getByRole('tab', { name: 'Migration' }).click()
    await page.getByRole('button', { name: 'Preview import' }).click()
    await expect(page.getByRole('heading', { name: 'Import preview' })).toBeVisible()
    await page.getByLabel('Confirmation').fill('IMPORT')
    await page.getByRole('button', { name: 'Apply import' }).click()
    await expect(page.getByText('Import completed: 0 created, 0 resumed.')).toBeVisible()

    await page.getByRole('tab', { name: 'Accounts' }).click()
    await page.getByRole('button', { name: 'Remove…' }).click()
    await page.getByRole('textbox', { name: 'Account name' }).fill('work')
    await page.getByRole('button', { name: 'Confirm removal' }).click()
    await expect(page.getByRole('heading', { name: 'No managed accounts' })).toBeVisible()

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
    await expect(page.getByRole('heading', { name: 'Open a fresh management link' })).toBeVisible()
    expect(pageErrors).toEqual([])
  } finally {
    await ui.stop()
  }

  expect(ui.child.exitCode).not.toBeNull()
})
