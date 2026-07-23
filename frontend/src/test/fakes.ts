import { vi } from 'vitest'

import type { ManagementApi } from '../api'

export const createMockApi = (): ManagementApi => ({
  exchangeBootstrap: vi.fn().mockResolvedValue(undefined),
  session: vi.fn().mockResolvedValue(undefined),
  logout: vi.fn().mockResolvedValue(undefined),
  status: vi.fn().mockResolvedValue({
    mode: 'legacy',
    selected_catalog: null,
    bootstrap_revision: 0,
    restart_required: false,
    report: null,
    catalog_problem: null,
  }),
  initializeCatalog: vi.fn().mockResolvedValue(undefined),
  activateCatalog: vi.fn().mockResolvedValue(undefined),
  selectMode: vi.fn().mockResolvedValue(undefined),
  accounts: vi.fn().mockResolvedValue([]),
  account: vi.fn(),
  createAccount: vi.fn().mockResolvedValue(undefined),
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
  repairCredential: vi.fn().mockResolvedValue({ state: 'active', revision: 2, cleanup_required: 0 }),
  cleanupCredentials: vi.fn().mockResolvedValue({ examined: 0, cleaned: 0, remaining: 0 }),
  policy: vi.fn().mockResolvedValue({ revision: 1, enable_attachment_download: false, allowed_recipients: [], allowed_senders: [], report_blocked_mutations: false }),
  updatePolicy: vi.fn(),
  testConnectivity: vi.fn().mockResolvedValue({ role: 'incoming', status: 'ok', message: 'Connection succeeded.' }),
  previewImport: vi.fn(),
  applyImport: vi.fn(),
  doctor: vi.fn(),
  indexHealth: vi.fn(),
})
