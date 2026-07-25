import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { RevisionConflictError } from '../api'
import { createMockApi } from '../test/fakes'
import { StatusPanel } from './StatusPanel'

test('shows current non-secret conflict summary and does not auto-replay', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.status).mockResolvedValue({
    mode: 'legacy',
    selected_catalog: 'catalog.db',
    bootstrap_revision: 3,
    restart_required: false,
    catalog_problem: null,
    bootstrap_exists: true,
    running_mode: 'legacy',
    running_catalog: 'catalog.db',
    default_catalog: 'managed.sqlite3',
    legacy_source: { account_count: 0, unsupported_provider_count: 0, policy_customized: false },
    legacy_source_problem: null,
    report: {
      schema_version: 1, catalog_revision: 5,
      account_count: 1, enabled_account_count: 1,
      cleanup_required_bindings: 0, problems: [],
    },
  })
  vi.mocked(api.selectMode).mockRejectedValue(new RevisionConflictError({
    category: 'conflict', message: 'Catalog revision changed.', current: { catalog_revision: 6 },
  }))
  render(<StatusPanel api={api} />)

  await user.click(await screen.findByRole('button', { name: 'Use these accounts' }))

  expect(await screen.findByRole('heading', { name: 'Review the latest settings' })).toBeInTheDocument()
  expect(screen.getByText('Settings version')).toBeInTheDocument()
  expect(screen.getByText('6')).toBeInTheDocument()
  expect(screen.getByRole('alert')).not.toHaveTextContent(/catalog|revision|legacy/i)
  expect(api.selectMode).toHaveBeenCalledTimes(1)
})

test('uses a newer policy revision hint when selecting managed settings', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.status).mockResolvedValue({
    mode: 'legacy',
    selected_catalog: '/private/managed.sqlite3',
    bootstrap_revision: 1,
    restart_required: false,
    report: {
      schema_version: 3, catalog_revision: 5,
      account_count: 1, enabled_account_count: 1,
      cleanup_required_bindings: 0, problems: [],
    },
    catalog_problem: null,
    bootstrap_exists: true,
    running_mode: 'legacy',
    running_catalog: null,
    default_catalog: '/private/managed.sqlite3',
    legacy_source: { account_count: 0, unsupported_provider_count: 0, policy_customized: false },
    legacy_source_problem: null,
  })
  const view = render(<StatusPanel api={api} />)
  await screen.findByRole('button', { name: 'Use these accounts' })

  view.rerender(<StatusPanel api={api} catalogRevisionHint={6} />)
  await user.click(screen.getByRole('button', { name: 'Use these accounts' }))

  expect(api.selectMode).toHaveBeenCalledWith('managed', 1, 6)
})

test('shows structural account problems without a separate activation action', async () => {
  const api = createMockApi()
  vi.mocked(api.status).mockResolvedValue({
    mode: 'managed',
    selected_catalog: '/private/managed.sqlite3',
    bootstrap_revision: 2,
    restart_required: true,
    report: {
      schema_version: 3, catalog_revision: 7,
      account_count: 1, enabled_account_count: 1,
      cleanup_required_bindings: 0,
      problems: ['account_incomplete:work:incoming'],
    },
    catalog_problem: null,
    bootstrap_exists: true,
    running_mode: 'legacy',
    running_catalog: null,
    default_catalog: '/private/managed.sqlite3',
    legacy_source: { account_count: 0, unsupported_provider_count: 0, policy_customized: false },
    legacy_source_problem: null,
  })

  render(<StatusPanel api={api} />)

  expect(await screen.findByText(/work needs complete incoming mail settings and a saved password/i)).toBeVisible()
  expect(screen.getByText(/restart the mail server to apply this change/i)).toBeVisible()
  expect(screen.queryByRole('button', { name: /finish setup/i })).not.toBeInTheDocument()
  expect(screen.queryByText(/test.*connection|provider/i)).not.toBeInTheDocument()
})

test('automatically initializes and selects managed settings only for a backend-proven empty installation', async () => {
  const api = createMockApi()
  vi.mocked(api.status)
    .mockResolvedValueOnce({
      mode: 'legacy',
      selected_catalog: null,
      bootstrap_revision: 0,
      restart_required: false,
      report: null,
      catalog_problem: null,
      bootstrap_exists: false,
      running_mode: 'legacy',
      running_catalog: null,
      default_catalog: '/private/managed.sqlite3',
      legacy_source: { account_count: 0, unsupported_provider_count: 0, policy_customized: false },
      legacy_source_problem: null,
    })
    .mockResolvedValue({
      mode: 'managed',
      selected_catalog: '/private/managed.sqlite3',
      bootstrap_revision: 1,
      restart_required: true,
      report: {
        schema_version: 3, catalog_revision: 1,
        account_count: 0, enabled_account_count: 0,
        cleanup_required_bindings: 0, problems: [],
      },
      catalog_problem: null,
      bootstrap_exists: true,
      running_mode: 'legacy',
      running_catalog: null,
      default_catalog: '/private/managed.sqlite3',
      legacy_source: { account_count: 0, unsupported_provider_count: 0, policy_customized: false },
      legacy_source_problem: null,
    })

  render(<StatusPanel api={api} />)

  expect(await screen.findByText('Setup details')).toBeInTheDocument()
  await waitFor(() => expect(api.initializeDefaultCatalog).toHaveBeenCalledOnce())
  await waitFor(() => expect(api.status).toHaveBeenCalledTimes(2))
  expect(api.initializeDefaultCatalog).toHaveBeenCalledWith(0, true)
  expect(screen.getByText('New account settings')).toBeInTheDocument()
  expect(screen.queryByText('Account settings ready')).not.toBeInTheDocument()
})

test('earlier settings stay selected while an explicit import is prepared', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  const onNavigate = vi.fn()
  vi.mocked(api.status)
    .mockResolvedValueOnce({
      mode: 'legacy',
      selected_catalog: null,
      bootstrap_revision: 0,
      restart_required: false,
      report: null,
      catalog_problem: null,
      bootstrap_exists: true,
      running_mode: 'legacy',
      running_catalog: null,
      default_catalog: '/private/managed.sqlite3',
      legacy_source: { account_count: 1, unsupported_provider_count: 0, policy_customized: true },
      legacy_source_problem: null,
    })
    .mockResolvedValue({
      mode: 'legacy',
      selected_catalog: '/private/managed.sqlite3',
      bootstrap_revision: 1,
      restart_required: false,
      report: {
        schema_version: 3, catalog_revision: 1,
        account_count: 0, enabled_account_count: 0,
        cleanup_required_bindings: 0, problems: ['no_enabled_account'],
      },
      catalog_problem: null,
      bootstrap_exists: true,
      running_mode: 'legacy',
      running_catalog: null,
      default_catalog: '/private/managed.sqlite3',
      legacy_source: { account_count: 1, unsupported_provider_count: 0, policy_customized: true },
      legacy_source_problem: null,
    })

  const { rerender } = render(<StatusPanel api={api} onNavigate={onNavigate} />)

  const action = await screen.findByRole('button', { name: 'Import existing settings' })
  expect(api.initializeDefaultCatalog).not.toHaveBeenCalled()
  await user.click(action)

  await waitFor(() => expect(api.initializeDefaultCatalog).toHaveBeenCalledWith(0, false))
  expect(await screen.findByText(/previous settings remain in use until the import succeeds/i)).toBeVisible()
  expect(onNavigate).toHaveBeenCalledWith('settings')

  vi.mocked(api.status).mockResolvedValueOnce({
    mode: 'managed',
    selected_catalog: '/private/managed.sqlite3',
    bootstrap_revision: 2,
    restart_required: true,
    report: {
      schema_version: 3, catalog_revision: 2,
      account_count: 1, enabled_account_count: 1,
      cleanup_required_bindings: 0, problems: [],
    },
    catalog_problem: null,
    bootstrap_exists: true,
    running_mode: 'legacy',
    running_catalog: null,
    default_catalog: '/private/managed.sqlite3',
    legacy_source: { account_count: 1, unsupported_provider_count: 0, policy_customized: true },
    legacy_source_problem: null,
  })
  rerender(<StatusPanel api={api} refreshKey={1} onNavigate={onNavigate} />)

  expect(await screen.findByText('Email accounts configured')).toBeVisible()
  expect(screen.queryByText(/previous settings remain in use until the import succeeds/i)).not.toBeInTheDocument()
})

test('empty account context avoids duplicate setup actions', async () => {
  const api = createMockApi()
  render(<StatusPanel api={api} />)

  expect(await screen.findByText('Setup details')).toBeInTheDocument()
  expect(screen.queryByText('Account settings ready')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Add email account' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Use previous settings after restart' })).not.toBeInTheDocument()
})
