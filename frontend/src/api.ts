import type {
  AccountDetails,
  AccountInput,
  AccountRemovalResult,
  AccountSummary,
  AccountUpdate,
  BindingRole,
  CleanupReport,
  ConnectivityResult,
  CredentialRemovalResult,
  CredentialRepairResult,
  CredentialResult,
  CurrentConflict,
  DoctorReport,
  IndexHealth,
  LegacyImportPlan,
  LegacyImportReport,
  ManagedPolicy,
  ManagementMode,
  ManagementStatus,
} from './types'

interface ErrorBody {
  category?: string
  message?: string
  current?: Record<string, unknown>
}

export class ApiError extends Error {
  readonly status: number
  readonly category: string

  constructor(status: number, category: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.category = category
  }
}

export class RevisionConflictError extends ApiError {
  readonly current: Record<string, unknown>

  constructor(conflict: CurrentConflict) {
    super(409, 'conflict', conflict.message)
    this.name = 'RevisionConflictError'
    this.current = conflict.current
  }
}

export interface ManagementApi {
  exchangeBootstrap(token: string): Promise<void>
  session(): Promise<void>
  logout(): Promise<void>
  status(): Promise<ManagementStatus>
  initializeCatalog(database: string): Promise<void>
  activateCatalog(expectedRevision: number): Promise<void>
  selectMode(
    mode: ManagementMode,
    expectedBootstrapRevision: number,
    expectedCatalogRevision?: number,
  ): Promise<void>
  accounts(): Promise<AccountSummary[]>
  account(name: string): Promise<AccountDetails>
  createAccount(input: AccountInput, secrets: { incoming: string; outgoing: string | null }, expectedCatalogRevision: number): Promise<void>
  updateAccount(name: string, input: AccountUpdate): Promise<AccountDetails>
  setAccountEnabled(name: string, enabled: boolean, expectedRevision: number): Promise<AccountDetails>
  removeAccount(name: string, expectedRevision: number, confirmation: string): Promise<AccountRemovalResult>
  setCredential(name: string, role: BindingRole, secret: string, expectedRevision: number): Promise<CredentialResult>
  removeCredential(name: string, role: BindingRole, expectedRevision: number): Promise<CredentialRemovalResult>
  repairCredential(name: string, role: BindingRole, action: 'resume' | 'rollback', expectedRevision: number): Promise<CredentialRepairResult>
  cleanupCredentials(limit: number, expectedCatalogRevision: number): Promise<CleanupReport>
  policy(): Promise<ManagedPolicy>
  updatePolicy(policy: ManagedPolicy): Promise<ManagedPolicy>
  testConnectivity(name: string, role: BindingRole): Promise<ConnectivityResult>
  previewImport(): Promise<LegacyImportPlan>
  applyImport(plan: LegacyImportPlan, confirmation: string): Promise<LegacyImportReport>
  doctor(): Promise<DoctorReport>
  indexHealth(): Promise<IndexHealth>
}

const safeMessage = (status: number, body: ErrorBody): string =>
  typeof body.message === 'string' && body.message.length <= 500
    ? body.message
    : `The request failed (${status}).`

export const readBootstrapFragment = (): string | null => {
  const fragment = window.location.hash.startsWith('#') ? window.location.hash.slice(1) : ''
  const parameters = new URLSearchParams(fragment)
  const values = parameters.getAll('bootstrap')
  const token = values.length === 1 ? values[0] : null
  // Remove all fragment material synchronously, before any network request.
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
  return token && token.length <= 4096 ? token : null
}

const route = (path: string): string => {
  const base = window.location.pathname.endsWith('/')
    ? window.location.pathname
    : `${window.location.pathname}/`
  return `${base}api/${path}`
}

export const createApi = (fetcher: typeof fetch = window.fetch.bind(window)): ManagementApi => {
  let csrf: string | null = null

  const request = async <T>(path: string, init: RequestInit = {}, mutation = false): Promise<T> => {
    const headers = new Headers(init.headers)
    headers.set('Accept', 'application/json')
    if (mutation) {
      headers.set('Content-Type', 'application/json')
      if (!csrf) throw new ApiError(401, 'session_required', 'This management session is not authenticated.')
      headers.set('X-CSRF-Token', csrf)
    }
    const response = await fetcher(route(path), {
      ...init,
      headers,
      credentials: 'same-origin',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
      cache: 'no-store',
    })
    const body = (await response.json().catch(() => ({}))) as ErrorBody & T
    if (!response.ok) {
      if (response.status === 409) {
        throw new RevisionConflictError({
          category: 'conflict',
          message: safeMessage(response.status, body),
          current: body.current ?? {},
        })
      }
      throw new ApiError(
        response.status,
        typeof body.category === 'string' ? body.category : 'request_failed',
        safeMessage(response.status, body),
      )
    }
    return body
  }

  const mutate = async <T>(path: string, body: object): Promise<T> =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body) }, true)

  return {
    async exchangeBootstrap(token) {
      const result = await request<{ csrf: string }>('bootstrap', {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: '{}',
      })
      csrf = result.csrf
    },
    async session() {
      const result = await request<{ csrf: string }>('session')
      csrf = result.csrf
    },
    async logout() {
      try {
        await mutate<unknown>('session/logout', {})
      } finally {
        csrf = null
      }
    },
    status: () => request('status'),
    initializeCatalog: (database) => mutate('catalog/initialize', { database }),
    activateCatalog: (expectedRevision) =>
      mutate('catalog/activate', { expected_revision: expectedRevision }),
    selectMode: (mode, expectedBootstrapRevision, expectedCatalogRevision) =>
      mutate('catalog/select', {
        mode,
        expected_bootstrap_revision: expectedBootstrapRevision,
        expected_catalog_revision: expectedCatalogRevision ?? null,
      }),
    accounts: () => request('accounts'),
    account: (name) => request(`accounts/${encodeURIComponent(name)}`),
    createAccount: (input, secrets, expectedCatalogRevision) => mutate('accounts/create', { ...input, credentials: secrets, expected_catalog_revision: expectedCatalogRevision }),
    updateAccount: (name, input) => mutate(`accounts/${encodeURIComponent(name)}/update`, input),
    setAccountEnabled: (name, enabled, expectedRevision) =>
      mutate(`accounts/${encodeURIComponent(name)}/${enabled ? 'enable' : 'disable'}`, {
        expected_revision: expectedRevision,
      }),
    removeAccount: (name, expectedRevision, confirmation) =>
      mutate(`accounts/${encodeURIComponent(name)}/remove`, {
        expected_revision: expectedRevision,
        confirmation,
      }),
    setCredential: (name, role, secret, expectedRevision) =>
      mutate(`accounts/${encodeURIComponent(name)}/credentials/${role}/set`, {
        secret,
        expected_revision: expectedRevision,
      }),
    removeCredential: (name, role, expectedRevision) =>
      mutate(`accounts/${encodeURIComponent(name)}/credentials/${role}/remove`, {
        expected_revision: expectedRevision,
      }),
    repairCredential: (name, role, action, expectedRevision) =>
      mutate(`accounts/${encodeURIComponent(name)}/credentials/${role}/repair`, {
        action,
        expected_revision: expectedRevision,
      }),
    cleanupCredentials: (limit, expectedCatalogRevision) => mutate('credentials/cleanup', { limit, expected_revision: expectedCatalogRevision }),
    policy: () => request('policy'),
    updatePolicy: (policy) => mutate('policy/update', { ...policy, expected_revision: policy.revision }),
    testConnectivity: (name, role) =>
      mutate(`accounts/${encodeURIComponent(name)}/connectivity`, { role }),
    previewImport: () => request('import/preview'),
    applyImport: (plan, confirmation) =>
      mutate('import/apply', {
        preview_token: plan.preview_token,
        expected_revision: plan.target_revision,
        confirmation,
      }),
    doctor: () => request('doctor'),
    indexHealth: () => request('index-health'),
  }
}
