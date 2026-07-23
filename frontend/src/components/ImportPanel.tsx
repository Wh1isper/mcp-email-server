import { useState } from 'react'

import type { ManagementApi } from '../api'
import type { Endpoint, LegacyImportPlan } from '../types'
import { ConflictNotice, errorMessage, StatusMessage } from './Feedback'

function endpointSummary(endpoint: Endpoint): string {
  const transport = endpoint.use_ssl ? 'TLS' : endpoint.start_ssl ? 'STARTTLS' : 'plain'
  return `${endpoint.host}:${endpoint.port}, ${endpoint.user_name}, ${transport}, verification ${endpoint.verify_ssl ? 'on' : 'off'}`
}

export function ImportPanel({ api }: { api: ManagementApi }) {
  const [plan, setPlan] = useState<LegacyImportPlan | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)

  const preview = async () => {
    setBusy(true); setNotice(''); setError(null); setConfirmation(''); setPlan(null)
    try { setPlan(await api.previewImport()) } catch (caught) { setError(caught) } finally { setBusy(false) }
  }

  const apply = async () => {
    if (!plan) return
    setBusy(true); setNotice(''); setError(null)
    try {
      const report = await api.applyImport(plan, confirmation)
      setNotice(`Import completed: ${report.created.length} created, ${report.resumed.length} resumed.`)
      setPlan(null)
      setConfirmation('')
    } catch (caught) {
      setError(caught)
      setPlan(null)
      setConfirmation('')
    } finally { setBusy(false) }
  }

  const hasConflicts = plan?.accounts.some((item) => item.action === 'conflict') ?? false

  return (
    <section aria-labelledby="import-heading">
      <div className="section-heading"><div><p className="eyebrow">Migration</p><h2 id="import-heading">Legacy import</h2></div><button type="button" disabled={busy} onClick={() => void preview()}>{plan ? 'Refresh preview' : 'Preview import'}</button></div>
      <p className="lede">Preview reads a bounded, non-secret source snapshot. Applying never deletes or rewrites the legacy source and is available only for a staging catalog.</p>
      <StatusMessage message={notice} />
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />
      {busy ? <p role="status">Checking legacy configuration…</p> : null}
      {plan ? (
        <div className="panel">
          <h3>Import preview</h3>
          <p className="bounded-text">Created <time dateTime={plan.created_at}>{plan.created_at}</time>; source fingerprint <code>{plan.source_fingerprint}</code>.</p>
          <table>
            <caption>Planned account actions</caption>
            <thead><tr><th scope="col">Account</th><th scope="col">Source configuration</th><th scope="col">Action</th><th scope="col">Credential work</th></tr></thead>
            <tbody>{plan.accounts.map((item) => <tr key={item.name}>
              <th scope="row">{item.name}<br /><small>{item.source.email_address}</small></th>
              <td>
                <div>{item.source.full_name}; sent copy {item.source.save_to_sent ? `on (${item.source.sent_folder_name ?? 'default folder'})` : 'off'}</div>
                <div>Incoming: {endpointSummary(item.source.incoming)}; credential source {item.source.incoming_secret_source}</div>
                <div>Outgoing: {item.source.outgoing ? `${endpointSummary(item.source.outgoing)}; credential source ${item.source.outgoing_secret_source}` : 'none'}</div>
              </td>
              <td>{item.action.replaceAll('_', ' ')}; target revision {item.expected_target_revision ?? 'absent'}</td>
              <td>{item.missing_credentials.length ? item.missing_credentials.join(', ') : 'None'}</td>
            </tr>)}</tbody>
          </table>
          <p>Policy: <strong>{plan.policy_action}</strong>; target revision {plan.target_policy_revision}</p>
          <dl>
            <dt>Attachment download</dt><dd>{plan.source_policy.enable_attachment_download ? 'enabled' : 'disabled'}</dd>
            <dt>Allowed recipients</dt><dd>{plan.source_policy.allowed_recipients.join(', ') || 'None'}</dd>
            <dt>Allowed senders</dt><dd>{plan.source_policy.allowed_senders.join(', ') || 'None'}</dd>
            <dt>Blocked mutation reports</dt><dd>{plan.source_policy.report_blocked_mutations ? 'enabled' : 'disabled'}</dd>
          </dl>
          {plan.unsupported_provider_names.length ? <div className="warning" role="status"><strong>Unsupported providers will not be imported:</strong> {plan.unsupported_provider_names.join(', ')}</div> : null}
          {hasConflicts ? <div className="message message-error" role="alert">Resolve destination conflicts, then create a fresh preview.</div> : (
            <div className="confirmation">
              <p>Applying may create accounts, copy available credentials through the secure backend, and update policy. Type <strong>IMPORT</strong> to confirm this exact preview.</p>
              <label htmlFor="import-confirmation">Confirmation</label>
              <input id="import-confirmation" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" />
              <button type="button" disabled={busy || confirmation !== 'IMPORT'} onClick={() => void apply()}>Apply import</button>
            </div>
          )}
        </div>
      ) : null}
    </section>
  )
}
