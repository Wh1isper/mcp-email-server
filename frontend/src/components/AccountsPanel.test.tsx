import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { ApiError } from '../api'
import { createMockApi } from '../test/fakes'
import { AccountsPanel } from './AccountsPanel'

const target = { expected_bootstrap_revision: 1, expected_catalog: '/private/managed.sqlite3' }

test('starts with email and password, then derives editable connection details', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  render(<AccountsPanel api={api} target={target} />)

  await user.click(await screen.findByRole('button', { name: 'Add your first account' }))
  await user.type(screen.getByLabelText('Email address'), 'alice.smith+work@gmail.com')

  expect(screen.getByLabelText('Password or app password')).toBeVisible()
  expect(screen.queryByText(/connection details ready|settings filled in/i)).not.toBeInTheDocument()
  expect(screen.getByLabelText('Account nickname')).toHaveValue('alice.smith+work')
  expect(screen.getByLabelText('Name shown on sent mail')).toHaveValue('Alice Smith')
  expect(screen.getByLabelText('Incoming mail server')).toHaveValue('imap.gmail.com')

  await user.click(screen.getByText('More incoming mail options'))
  expect(screen.getByLabelText('Login name')).toHaveValue('alice.smith+work@gmail.com')
  const transport = screen.getByLabelText('Security')
  expect(transport).toHaveValue('tls')
  expect(screen.getByLabelText('Port')).toHaveValue(993)
  await user.selectOptions(transport, 'starttls')
  expect(transport).toHaveValue('starttls')
  const port = screen.getByLabelText('Port')
  expect(port).toHaveValue(143)
  await user.clear(port)
  await user.type(port, '1993')
  await user.selectOptions(transport, 'tls')
  expect(port).toHaveValue(1993)

  await user.click(screen.getByText('Outgoing mail (optional)'))
  await user.click(screen.getByRole('checkbox', { name: 'Enable sending' }))
  expect(screen.getByLabelText('Outgoing mail server')).toHaveValue('smtp.gmail.com')
  await user.type(screen.getByLabelText('Outgoing mail password'), 'temporary-smtp-secret')
  await user.click(screen.getByRole('checkbox', { name: 'Enable sending' }))
  await user.click(screen.getByRole('checkbox', { name: 'Enable sending' }))
  expect(screen.getByLabelText('Outgoing mail password')).toHaveValue('')
})

test('updates each untouched suggestion without overwriting edited fields', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  render(<AccountsPanel api={api} target={target} />)

  await user.click(await screen.findByRole('button', { name: 'Add your first account' }))
  await user.type(screen.getByLabelText('Email address'), 'alice@gmial.com')
  await user.click(screen.getByText('Advanced account settings'))
  await user.clear(screen.getByLabelText('Name shown on sent mail'))
  await user.type(screen.getByLabelText('Name shown on sent mail'), 'alice')
  await user.click(screen.getByText('More incoming mail options'))
  const port = screen.getByLabelText('Port')
  const transport = screen.getByLabelText('Security')
  await user.selectOptions(transport, 'starttls')
  expect(port).toHaveValue(143)

  await user.clear(screen.getByLabelText('Email address'))
  await user.type(screen.getByLabelText('Email address'), 'bob@gmail.com')

  expect(screen.getByLabelText('Account nickname')).toHaveValue('bob')
  expect(screen.getByLabelText('Name shown on sent mail')).toHaveValue('alice')
  expect(screen.getByLabelText('Incoming mail server')).toHaveValue('imap.gmail.com')
  expect(screen.getByLabelText('Login name')).toHaveValue('bob@gmail.com')
  expect(transport).toHaveValue('starttls')
  expect(port).toHaveValue(143)

  await user.clear(port)
  await user.type(port, '993')
  await user.selectOptions(transport, 'tls')
  expect(port).toHaveValue(993)
})

test('reveals closed connection sections when they contain an invalid field', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  render(<AccountsPanel api={api} target={target} />)

  await user.click(await screen.findByRole('button', { name: 'Add your first account' }))
  await user.type(screen.getByLabelText('Email address'), 'alice@example.test')
  await user.type(screen.getByLabelText('Password or app password'), 'one-use-secret')
  const setupSummary = screen.getByText('Advanced account settings')
  await user.click(setupSummary)
  const advancedSummary = screen.getByText('More incoming mail options')
  await user.click(advancedSummary)
  await user.clear(screen.getByLabelText('Login name'))
  await user.click(advancedSummary)
  await user.click(setupSummary)

  const setupDetails = setupSummary.closest('details')
  const advancedDetails = advancedSummary.closest('details')
  expect(setupDetails).not.toHaveAttribute('open')
  expect(advancedDetails).not.toHaveAttribute('open')

  await user.click(screen.getByRole('button', { name: 'Add account' }))

  expect(setupDetails).toHaveAttribute('open')
  expect(advancedDetails).toHaveAttribute('open')
  expect(api.createAccount).not.toHaveBeenCalled()
})

test('clears creation passwords before the non-secret account refresh finishes', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  let finishRefresh: ((accounts: never[]) => void) | undefined
  const refresh = new Promise<never[]>((resolve) => { finishRefresh = resolve })
  vi.mocked(api.accounts).mockResolvedValueOnce([]).mockReturnValueOnce(refresh)
  render(<AccountsPanel api={api} target={target} />)

  await user.click(await screen.findByRole('button', { name: 'Add your first account' }))
  await user.type(screen.getByLabelText('Email address'), 'alice@example.test')
  const secret = screen.getByLabelText('Password or app password')
  await user.type(secret, 'clear-before-refresh')
  await user.click(screen.getByRole('button', { name: 'Add account' }))

  await waitFor(() => expect(api.createAccount).toHaveBeenCalledOnce())
  expect(secret).toHaveValue('')
  await act(async () => {
    finishRefresh?.([])
    await refresh
  })
})

test('opens and focuses the nickname item after a duplicate-name response', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.createAccount).mockRejectedValue(new ApiError(
    400,
    'account_name_exists',
    'The management operation could not be completed.',
  ))
  render(<AccountsPanel api={api} target={target} />)

  await user.click(await screen.findByRole('button', { name: 'Add your first account' }))
  await user.type(screen.getByLabelText('Email address'), 'alice@example.test')
  const secret = screen.getByLabelText('Password or app password')
  await user.type(secret, 'one-use-secret')
  const advanced = screen.getByText('Advanced account settings').closest('details')
  expect(advanced).not.toHaveAttribute('open')

  await user.click(screen.getByRole('button', { name: 'Add account' }))

  expect(await screen.findByText(/nickname is already in use/i)).toBeVisible()
  expect(advanced).toHaveAttribute('open')
  expect(screen.getByLabelText('Account nickname')).toHaveFocus()
  expect(secret).toHaveValue('')
})

test('creates an account with addable and removable tag rows', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  render(<AccountsPanel api={api} target={target} />)

  await user.click(await screen.findByRole('button', { name: 'Add your first account' }))
  await user.type(screen.getByLabelText('Email address'), 'alice@example.test')
  await user.type(screen.getByLabelText('Password or app password'), 'one-use-secret')
  await user.click(screen.getByText('Email tags (optional)'))
  await user.click(screen.getByRole('button', { name: 'Add tag' }))
  await user.type(screen.getByLabelText('Tag 1 name'), 'todo')
  await user.type(screen.getByLabelText('Tag 1 IMAP keyword'), '$label4')
  await user.type(screen.getByLabelText('Tag 1 description'), 'Needs action')
  await user.click(screen.getByRole('checkbox', { name: 'Writable through MCP' }))
  await user.click(screen.getByRole('button', { name: 'Add tag' }))
  await user.type(screen.getByLabelText('Tag 2 name'), 'remove-me')
  await user.click(screen.getByRole('button', { name: 'Remove tag 2: remove-me' }))
  await user.click(screen.getByRole('button', { name: 'Add account' }))

  await waitFor(() => expect(api.createAccount).toHaveBeenCalledWith(expect.objectContaining({
    tags: [{ name: 'todo', keyword: '$label4', description: 'Needs action', writable: true }],
  }), { incoming: 'one-use-secret', outgoing: null }, 1, target))
})

test('edits existing account tags and includes them in the update payload', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  const account = {
    name: 'work', email_address: 'alice@example.test', enabled: true, revision: 3,
    has_outgoing: false, incoming_binding: 'ACTIVE' as const, outgoing_binding: null,
    full_name: 'Alice Example', save_to_sent: true, sent_folder_name: null,
    tags: [{ name: 'todo', keyword: '$label4', description: '', writable: false }],
    incoming: {
      host: 'imap.example.test', port: 993, user_name: 'alice@example.test',
      use_ssl: true, start_ssl: false, verify_ssl: true,
    },
    outgoing: null,
  }
  vi.mocked(api.accounts).mockResolvedValue([account])
  vi.mocked(api.account).mockResolvedValue(account)
  render(<AccountsPanel api={api} target={target} />)

  await user.click(await screen.findByRole('button', { name: 'Edit' }))
  await user.type(screen.getByLabelText('Tag 1 description'), 'Needs action')
  await user.click(screen.getByRole('checkbox', { name: 'Writable through MCP' }))
  await user.click(screen.getByRole('button', { name: 'Add tag' }))
  await user.type(screen.getByLabelText('Tag 2 name'), 'waiting')
  await user.type(screen.getByLabelText('Tag 2 IMAP keyword'), '$label5')
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  await waitFor(() => expect(api.updateAccount).toHaveBeenCalledWith('work', expect.objectContaining({
    expected_revision: 3,
    tags: [
      { name: 'todo', keyword: '$label4', description: 'Needs action', writable: true },
      { name: 'waiting', keyword: '$label5', description: '', writable: false },
    ],
  }), target))
})

test('editing an email address does not silently rename hidden account identity', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  const account = {
    name: 'work',
    email_address: 'alice@example.test',
    enabled: true,
    revision: 3,
    has_outgoing: false,
    incoming_binding: 'ACTIVE' as const,
    outgoing_binding: null,
    full_name: 'Alice Example',
    save_to_sent: true,
    sent_folder_name: null,
    tags: [],
    incoming: {
      host: 'imap.example.test', port: 993, user_name: 'alice@example.test',
      use_ssl: true, start_ssl: false, verify_ssl: true,
    },
    outgoing: null,
  }
  vi.mocked(api.accounts).mockResolvedValue([account])
  vi.mocked(api.account).mockResolvedValue(account)
  render(<AccountsPanel api={api} target={target} />)

  await user.click(await screen.findByRole('button', { name: 'Edit' }))
  await user.clear(screen.getByLabelText('Email address'))
  await user.type(screen.getByLabelText('Email address'), 'bob@example.test')

  expect(screen.getByLabelText('Account nickname')).toHaveValue('work')
  expect(screen.getByLabelText('Name shown on sent mail')).toHaveValue('Alice Example')
  expect(screen.getByLabelText('Login name')).toHaveValue('bob@example.test')
  expect(screen.getByRole('checkbox', { name: 'Enable sending' })).toBeDisabled()
  await user.click(screen.getByText('Outgoing mail (optional)'))
  expect(screen.getByText('Pause this account before adding outgoing mail.')).toBeVisible()
})

test('hides stale account actions when a post-write refresh fails', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  const account = {
    name: 'work', email_address: 'alice@example.test', enabled: true, revision: 3,
    has_outgoing: false, incoming_binding: 'ACTIVE' as const, outgoing_binding: null,
    full_name: 'Alice Example', save_to_sent: true, sent_folder_name: null, tags: [],
    incoming: {
      host: 'imap.example.test', port: 993, user_name: 'alice@example.test',
      use_ssl: true, start_ssl: false, verify_ssl: true,
    },
    outgoing: null,
  }
  vi.mocked(api.accounts)
    .mockResolvedValueOnce([account])
    .mockRejectedValueOnce(new Error('Account refresh failed.'))
  vi.mocked(api.account).mockResolvedValue(account)
  render(<AccountsPanel api={api} target={target} />)

  await user.click(await screen.findByRole('button', { name: 'Edit' }))
  await user.clear(screen.getByLabelText('Name shown on sent mail'))
  await user.type(screen.getByLabelText('Name shown on sent mail'), 'Updated')
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  expect(await screen.findByText(/change was saved, but the latest account state could not be refreshed/i)).toBeVisible()
  expect(screen.getByText('Account refresh failed.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Refresh accounts' })).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Password' })).not.toBeInTheDocument()
})

test('keeps bounded credential cleanup reachable when no active account exposes the leftover binding', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  const baseStatus = await api.status()
  const cleanupStatus = {
    ...baseStatus,
    report: { ...baseStatus.report!, cleanup_required_bindings: 1 },
  }
  const cleanStatus = {
    ...baseStatus,
    report: { ...baseStatus.report!, cleanup_required_bindings: 0, catalog_revision: 2 },
  }
  vi.mocked(api.status)
    .mockResolvedValueOnce(cleanupStatus)
    .mockResolvedValueOnce(cleanupStatus)
    .mockResolvedValueOnce(cleanupStatus)
    .mockResolvedValue(cleanStatus)
  vi.mocked(api.cleanupCredentials).mockResolvedValue({ examined: 1, cleaned: 1, remaining: 0 })

  render(<AccountsPanel api={api} target={target} />)

  const cleanup = await screen.findByRole('button', { name: 'Clean up old password data' })
  expect(screen.getByText(/1 old password copy could not be removed/i)).toBeVisible()
  await user.click(cleanup)

  await waitFor(() => expect(api.cleanupCredentials).toHaveBeenCalledWith(100, 1, target))
  expect(await screen.findByText('Cleanup complete. 1 item(s) removed.')).toBeVisible()
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Clean up old password data' })).not.toBeInTheDocument())
})

test('reports typed password cleanup without exposing backend state jargon', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.createAccount).mockResolvedValue({
    incoming: { state: 'active_cleanup_required', revision: 2, cleanup_required: 1 },
    outgoing: null,
  })
  render(<AccountsPanel api={api} target={target} />)

  await user.click(await screen.findByRole('button', { name: 'Add your first account' }))
  await user.type(screen.getByLabelText('Email address'), 'alice@example.test')
  await user.type(screen.getByLabelText('Password or app password'), 'one-use-secret')
  await user.click(screen.getByRole('button', { name: 'Add account' }))

  expect(await screen.findByText(/old password data still needs cleanup/i)).toBeInTheDocument()
  expect(screen.getByText(/Clean up old password data/i)).toBeInTheDocument()
  expect(screen.queryByText(/active_cleanup_required/i)).not.toBeInTheDocument()
})
