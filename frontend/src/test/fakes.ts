import { vi } from 'vitest'

import type { ManagementApi } from '../api'

export const createMockApi = (): ManagementApi => ({
  exchangeBootstrap: vi.fn().mockResolvedValue(undefined),
  session: vi.fn().mockResolvedValue(undefined),
  logout: vi.fn().mockResolvedValue(undefined),
  status: vi.fn().mockResolvedValue({
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
    running_catalog: '/private/managed.sqlite3',
    default_catalog: '/private/managed.sqlite3',
    legacy_source: { account_count: 0, unsupported_provider_count: 0, policy_customized: false },
    legacy_source_problem: null,
  }),
  initializeDefaultCatalog: vi.fn().mockResolvedValue({
    database: '/private/managed.sqlite3',
    mode: 'managed',
    bootstrap_revision: 1,
    restart_required: true,
    catalog_revision: 1,
  }),
  selectMode: vi.fn().mockResolvedValue(undefined),
  accounts: vi.fn().mockResolvedValue([]),
  account: vi.fn(),
  createAccount: vi.fn().mockResolvedValue({
    incoming: { state: 'active', revision: 2, cleanup_required: 0 },
    outgoing: null,
  }),
  updateAccount: vi.fn(),
  setAccountEnabled: vi.fn(),
  removeAccount: vi.fn().mockResolvedValue({
    status: 'removed',
    revision: 2,
    credentials_examined: 1,
    credentials_cleaned: 1,
    cleanup_required: 0,
  }),
  setCredential: vi.fn().mockResolvedValue({ state: 'active', revision: 2, cleanup_required: 0 }),
  removeCredential: vi.fn().mockResolvedValue({ state: 'removed', revision: 2, cleanup_required: 0 }),
  cleanupCredentials: vi.fn().mockResolvedValue({ examined: 0, cleaned: 0, remaining: 0 }),
  policy: vi.fn().mockResolvedValue({ revision: 1, enable_attachment_download: false, allowed_recipients: [], allowed_senders: [], report_blocked_mutations: false }),
  updatePolicy: vi.fn(),
  previewImport: vi.fn(),
  applyImport: vi.fn(),
  doctor: vi.fn(),
  indexHealth: vi.fn(),
})
