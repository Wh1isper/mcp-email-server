import type {
  AccountCreationResult,
  AccountDetails,
  AccountInput,
  AccountRemovalResult,
  AccountSummary,
  AccountUpdate,
  BindingRole,
  CatalogTarget,
  CleanupReport,
  CredentialRemovalResult,
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
  initializeDefaultCatalog(expectedBootstrapRevision: number, requireEmptyInstall: boolean): Promise<{ database: string }>
  activateCatalog(expectedRevision: number, target: CatalogTarget): Promise<void>
  selectMode(
    mode: ManagementMode,
    expectedBootstrapRevision: number,
    expectedCatalogRevision?: number,
  ): Promise<void>
  accounts(): Promise<AccountSummary[]>
  account(name: string): Promise<AccountDetails>
  createAccount(input: AccountInput, secrets: { incoming: string; outgoing: string | null }, expectedCatalogRevision: number, target: CatalogTarget): Promise<AccountCreationResult>
  updateAccount(name: string, input: AccountUpdate, target: CatalogTarget): Promise<AccountDetails>
  setAccountEnabled(name: string, enabled: boolean, expectedRevision: number, target: CatalogTarget): Promise<AccountDetails>
  removeAccount(name: string, expectedRevision: number, confirmation: string, target: CatalogTarget): Promise<AccountRemovalResult>
  setCredential(name: string, role: BindingRole, secret: string, expectedRevision: number, target: CatalogTarget): Promise<CredentialResult>
  removeCredential(name: string, role: BindingRole, expectedRevision: number, target: CatalogTarget): Promise<CredentialRemovalResult>
  cleanupCredentials(limit: number, expectedCatalogRevision: number, target: CatalogTarget): Promise<CleanupReport>
  policy(): Promise<ManagedPolicy>
  updatePolicy(policy: ManagedPolicy, target: CatalogTarget): Promise<ManagedPolicy>
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

  const catalogMutate = async <T>(path: string, body: object, target: CatalogTarget): Promise<T> =>
    mutate<T>(path, { ...body, ...target })

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
    status: () => request<ManagementStatus>('status'),
    initializeDefaultCatalog: (expectedBootstrapRevision, requireEmptyInstall) =>
      mutate('catalog/initialize-default', {
        expected_bootstrap_revision: expectedBootstrapRevision,
        require_empty_install: requireEmptyInstall,
      }),
    activateCatalog: (expectedRevision, target) =>
      catalogMutate('catalog/activate', { expected_revision: expectedRevision }, target),
    selectMode: (mode, expectedBootstrapRevision, expectedCatalogRevision) =>
      mutate('catalog/select', {
        mode,
        expected_bootstrap_revision: expectedBootstrapRevision,
        expected_catalog_revision: expectedCatalogRevision ?? null,
      }),
    accounts: () => request('accounts'),
    account: (name) => request(`accounts/${encodeURIComponent(name)}`),
    createAccount: (input, secrets, expectedCatalogRevision, target) => catalogMutate('accounts/create', { ...input, credentials: secrets, expected_catalog_revision: expectedCatalogRevision }, target),
    updateAccount: (name, input, target) => catalogMutate(`accounts/${encodeURIComponent(name)}/update`, input, target),
    setAccountEnabled: (name, enabled, expectedRevision, target) =>
      catalogMutate(`accounts/${encodeURIComponent(name)}/${enabled ? 'enable' : 'disable'}`, {
        expected_revision: expectedRevision,
      }, target),
    removeAccount: (name, expectedRevision, confirmation, target) =>
      catalogMutate(`accounts/${encodeURIComponent(name)}/remove`, {
        expected_revision: expectedRevision,
        confirmation,
      }, target),
    setCredential: (name, role, secret, expectedRevision, target) =>
      catalogMutate(`accounts/${encodeURIComponent(name)}/credentials/${role}/set`, {
        secret,
        expected_revision: expectedRevision,
      }, target),
    removeCredential: (name, role, expectedRevision, target) =>
      catalogMutate(`accounts/${encodeURIComponent(name)}/credentials/${role}/remove`, {
        expected_revision: expectedRevision,
      }, target),
    cleanupCredentials: (limit, expectedCatalogRevision, target) => catalogMutate('credentials/cleanup', { limit, expected_revision: expectedCatalogRevision }, target),
    policy: () => request('policy'),
    updatePolicy: (policy, target) => catalogMutate('policy/update', {
      expected_revision: policy.revision,
      enable_attachment_download: policy.enable_attachment_download,
      allowed_recipients: policy.allowed_recipients,
      allowed_senders: policy.allowed_senders,
      report_blocked_mutations: policy.report_blocked_mutations,
    }, target),
    previewImport: () => mutate('import/preview', {}),
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
