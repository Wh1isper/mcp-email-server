import { ApiError, RevisionConflictError } from '../api'

const LOCAL_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  'Credential backend unavailable.': 'Password storage unavailable.',
  'Managed catalog revision changed during provider binding update.': 'Account settings changed while the password was being updated. Review the latest settings and try again.',
}

const API_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  account_limit_reached: 'This account storage has reached its account limit. Remove an unused account or use another settings file.',
  account_name_exists: 'That account nickname is already in use. Choose a different nickname, re-enter the password, and try again.',
  bootstrap_unavailable: 'Local setup information is unavailable or busy. Reopen the app and try again.',
  catalog_not_configured: 'Account storage is not ready. Return to setup status and prepare it before continuing.',
  catalog_unavailable: 'Account storage is unavailable. Reopen the app and review Setup details.',
  conflict: 'These settings changed elsewhere. Review the latest values and try again.',
  credential_store_unavailable: 'Local password storage is unavailable. Check access to the account storage, then try again.',
  host_rejected: 'This page is not using the expected local address. Close it and reopen the app.',
  import_preview_stale: 'The earlier settings changed or the review expired. Create and review a new import preview.',
  import_target_changed: 'The destination settings changed. Create and review a new import preview.',
  internal_error: 'The app could not complete that action. Try again.',
  invalid_input: 'Some entered values are invalid. Review the form and try again.',
  invalid_request: 'Some entered values are invalid. Review the form and try again.',
  management_error: 'The app could not complete that action. Review the settings and try again.',
  origin_rejected: 'This page is not allowed to change local settings. Close it and reopen the app.',
  request_failed: 'The request failed. Try again.',
  storage_unavailable: 'Local management storage is unavailable. Check file access and try again.',
  response_too_large: 'The result was too large to display safely.',
  session_required: 'This local session has ended. Close the page and reopen the app.',
}

export const errorMessage = (error: unknown): string => {
  if (error instanceof ApiError) return API_ERROR_MESSAGES[error.category] ?? 'The request failed. Try again.'
  if (error instanceof Error) return LOCAL_ERROR_MESSAGES[error.message] ?? error.message
  return 'Something went wrong. Try again.'
}

const accountProblem = (problem: string, category: string): { account: string; role: 'incoming' | 'outgoing' } | null => {
  const prefix = `${category}:`
  if (!problem.startsWith(prefix)) return null
  const role = problem.endsWith(':outgoing') ? 'outgoing' : problem.endsWith(':incoming') ? 'incoming' : null
  if (!role) return null
  const account = problem.slice(prefix.length, -(role.length + 1))
  return account ? { account, role } : null
}

export const problemMessage = (problem: string): string => {
  if (problem === 'no_enabled_account') return 'Enable at least one account before finishing setup.'
  const incomplete = accountProblem(problem, 'account_incomplete')
  if (incomplete) {
    const service = incomplete.role === 'outgoing' ? 'outgoing mail' : 'incoming mail'
    return `${incomplete.account} needs complete ${service} settings and a saved password.`
  }
  const unavailable = accountProblem(problem, 'active_secret_unavailable')
  if (unavailable) {
    const service = unavailable.role === 'outgoing' ? 'outgoing mail' : 'incoming mail'
    return `${unavailable.account}'s ${service} password is unavailable. Save it again.`
  }
  return 'Setup needs attention. Review the account settings and try again.'
}

const summaryLabel = (key: string): string => {
  const labels: Record<string, string> = {
    account_name: 'Account name',
    account_revision: 'Account version',
    bootstrap_revision: 'Setup version',
    catalog_revision: 'Settings version',
    lifecycle: 'Setup status',
    mode: 'Settings source',
    policy_revision: 'Safety settings version',
    running_mode: 'Active settings source',
    selected_catalog: 'Settings file',
  }
  return labels[key] ?? 'Changed setting'
}

const summaryValue = (key: string, value: unknown): string => {
  if (key === 'lifecycle' && value === 'STAGING') return 'Setup in progress'
  if (key === 'lifecycle' && value === 'ACTIVE') return 'Ready to use'
  if ((key === 'mode' || key === 'running_mode') && value === 'legacy') return 'Previous settings'
  if ((key === 'mode' || key === 'running_mode') && value === 'managed') return 'New account settings'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  return 'Updated elsewhere'
}

export function StatusMessage({ message, error = false }: { message: string; error?: boolean }) {
  if (!message) return null
  return (
    <p className={error ? 'message message-error' : 'message'} role={error ? 'alert' : 'status'}>
      {message}
    </p>
  )
}

export function ConflictNotice({ error, onDismiss }: { error: unknown; onDismiss: () => void }) {
  if (!(error instanceof RevisionConflictError)) return null
  return (
    <section className="conflict" role="alert" aria-labelledby="conflict-heading">
      <h3 id="conflict-heading">Review the latest settings</h3>
      <p>These settings changed in another window or process, so your action was not repeated. Review the current values before trying again.</p>
      <dl>
        {Object.entries(error.current).map(([key, value]) => (
          <div key={key}>
            <dt>{summaryLabel(key)}</dt>
            <dd>{summaryValue(key, value)}</dd>
          </div>
        ))}
      </dl>
      <button type="button" className="secondary" onClick={onDismiss}>Got it</button>
    </section>
  )
}
