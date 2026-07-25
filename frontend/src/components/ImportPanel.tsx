import { useCallback, useEffect, useState } from 'react'
import { Download, RefreshCw } from 'lucide-react'

import type { ManagementApi } from '../api'
import type { Endpoint, LegacyAccountSource, LegacyImportPlan } from '../types'
import { ConflictNotice, errorMessage, StatusMessage } from './Feedback'

function endpointSummary(endpoint: Endpoint): string {
  const transport = endpoint.use_ssl ? 'TLS' : endpoint.start_ssl ? 'STARTTLS' : 'plain'
  return `${endpoint.host}:${endpoint.port} · ${endpoint.user_name} · ${transport}`
}

const passwordSourceLabel = (source: LegacyAccountSource['incoming_secret_source'] | null): string => {
  if (source === 'keyring') return 'secure password storage'
  if (source === 'environment') return 'the environment setup'
  return 'the earlier settings file'
}

const actionLabel = (action: LegacyImportPlan['accounts'][number]['action']): string => {
  if (action === 'create') return 'Add account'
  if (action === 'resume_credentials') return 'Add missing password'
  if (action === 'unchanged') return 'Already imported'
  return 'Needs review'
}

export function ImportPanel({ api, onChanged }: { api: ManagementApi; onChanged?: () => void }) {
  const [plan, setPlan] = useState<LegacyImportPlan | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)

  const preview = useCallback(async () => {
    setBusy(true); setNotice(''); setError(null); setConfirmed(false); setPlan(null)
    try { setPlan(await api.previewImport()) } catch (caught) { setError(caught) } finally { setBusy(false) }
  }, [api])

  useEffect(() => { void preview() }, [preview])

  const apply = async () => {
    if (!plan) return
    setBusy(true); setNotice(''); setError(null)
    try {
      const report = await api.applyImport(plan, confirmed ? 'IMPORT' : '')
      setNotice(report.attention_required.length
        ? `Imported ${report.created.length} account(s); ${report.attention_required.length} password(s) still need attention.`
        : report.mode === 'managed'
          ? `Import complete. ${report.created.length} account(s) added. Restart the mail server to use the new settings.`
          : `Imported ${report.created.length} supported account(s). Previous settings remain in use because some account types need review.`)
      setPlan(null)
      setConfirmed(false)
      onChanged?.()
    } catch (caught) {
      setError(caught)
      setPlan(null)
      setConfirmed(false)
    } finally { setBusy(false) }
  }

  const hasConflicts = plan?.accounts.some((item) => item.action === 'conflict') ?? false
  const hasChanges = Boolean(plan && (
    plan.policy_action === 'update'
    || plan.accounts.some((item) => item.action === 'create' || item.action === 'resume_credentials')
  ))
  const changeCount = plan?.accounts.filter((item) => item.action === 'create' || item.action === 'resume_credentials').length ?? 0

  return (
    <section aria-labelledby="import-heading">
      <div className="section-heading"><div><p className="eyebrow">Existing configuration</p><h2 id="import-heading">Import email accounts</h2></div><button type="button" className="secondary with-icon" disabled={busy} onClick={() => void preview()}><RefreshCw size={16} aria-hidden="true" />Check again</button></div>
      <p className="lede">Nothing is copied until you review and confirm. The original settings are left unchanged.</p>
      <StatusMessage message={notice} />
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />
      {busy ? <p role="status">Checking existing settings…</p> : null}
      {plan ? (
        <div className="import-preview">
          <div className="import-summary"><strong>{changeCount} account{changeCount === 1 ? '' : 's'} ready to import</strong><span>{plan.policy_action === 'update' ? 'Safety settings will also be copied.' : 'Safety settings are already up to date.'}</span></div>
          {plan.unsupported_provider_names.length ? <div className="warning" role="status"><strong>These account types cannot be copied automatically:</strong> {plan.unsupported_provider_names.join(', ')}</div> : null}
          <div className="import-account-list">
            {plan.accounts.map((item) => (
              <article className="import-account" key={item.name}>
                <div><h3>{item.source.email_address}</h3><p>{item.source.full_name}</p></div>
                <span className={`status-pill${item.action === 'conflict' ? ' status-pill-attention' : ''}`}>{actionLabel(item.action)}</span>
                <details className="inline-details">
                  <summary>Source details</summary>
                  <div className="details-body">
                    <p><strong>Incoming:</strong> {endpointSummary(item.source.incoming)} · password from {passwordSourceLabel(item.source.incoming_secret_source)}</p>
                    <p><strong>Outgoing:</strong> {item.source.outgoing ? `${endpointSummary(item.source.outgoing)} · password from ${passwordSourceLabel(item.source.outgoing_secret_source)}` : 'Not configured'}</p>
                    <p><strong>Sent copy:</strong> {item.source.save_to_sent ? item.source.sent_folder_name ?? 'Default Sent folder' : 'Off'}</p>
                  </div>
                </details>
              </article>
            ))}
          </div>
          <details className="technical-details">
            <summary>Technical preview details</summary>
            <div className="details-body"><p>Created <time dateTime={plan.created_at}>{plan.created_at}</time></p><p className="bounded-text">Source ID: <code>{plan.source_fingerprint}</code></p><p>Account settings version: {plan.target_revision}; safety settings version: {plan.target_policy_revision}</p></div>
          </details>
          {hasConflicts ? <div className="message message-error" role="alert">One or more accounts conflict with saved settings. Resolve them before importing.</div> : !hasChanges ? (
            <div className="confirmation action-stack">
              <div className="message" role="status"><strong>Accounts are already copied.</strong> Finish setup to verify the saved passwords and use these settings.</div>
              <button type="button" className="with-icon" disabled={busy} onClick={() => void apply()}><Download size={17} aria-hidden="true" />Finish setup</button>
            </div>
          ) : (
            <div className="confirmation action-stack">
              <label className="confirmation-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> I reviewed these accounts and want to copy them.</label>
              <p className="hint">The earlier settings will stay unchanged.</p>
              <button type="button" className="with-icon" disabled={busy || !confirmed} onClick={() => void apply()}><Download size={17} aria-hidden="true" />Import accounts</button>
            </div>
          )}
        </div>
      ) : null}
    </section>
  )
}
