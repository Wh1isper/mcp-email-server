import { useEffect, useState } from 'react'
import { KeyRound, Trash2 } from 'lucide-react'

import type { ManagementApi } from '../api'
import type { AccountSummary, BindingRole, BindingState, CatalogTarget, CredentialResult } from '../types'
import { ConflictNotice, errorMessage, StatusMessage } from './Feedback'

interface CredentialsPanelProps {
  api: ManagementApi
  account: AccountSummary
  target: CatalogTarget
  onChanged: () => Promise<void> | void
}

const bindingLabel = (state: BindingState | null): string => {
  if (state === null || state === 'MISSING' || state === 'SUPERSEDED') return 'No password saved'
  if (state === 'ACTIVE') return 'Password saved'
  if (state === 'CLEANUP_REQUIRED') return 'Password removed; cleanup needed'
  throw new Error('Unsupported password binding state')
}

const passwordResultMessage = (state: CredentialResult['state'], service: string): string =>
  state === 'active'
    ? `${service} password saved.`
    : `${service} password saved. An old password copy still needs cleanup from the accounts page.`

export function CredentialsPanel({ api, account, target, onChanged }: CredentialsPanelProps) {
  const [role, setRole] = useState<BindingRole>('incoming')
  const [secret, setSecret] = useState('')
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    setSecret('')
    setConfirmRemove(false)
    setNotice('')
    setError(null)
  }, [account.name, role])
  useEffect(() => () => setSecret(''), [])

  const supportsRole = role === 'incoming' || account.has_outgoing
  const binding = role === 'incoming' ? account.incoming_binding : account.outgoing_binding

  const setCredential = async () => {
    if (!secret || !supportsRole) return
    setBusy(true); setNotice(''); setError(null)
    try {
      const result = await api.setCredential(account.name, role, secret, account.revision, target)
      setSecret('')
      setNotice(passwordResultMessage(result.state, role === 'incoming' ? 'IMAP' : 'SMTP'))
      await onChanged()
    } catch (caught) { setError(caught) } finally {
      setSecret('')
      setBusy(false)
    }
  }

  const removeCredential = async () => {
    setBusy(true); setNotice(''); setError(null)
    try {
      const result = await api.removeCredential(account.name, role, account.revision, target)
      setNotice(result.cleanup_required
        ? 'The password was detached, but cleanup is still required.'
        : `${role === 'incoming' ? 'IMAP' : 'SMTP'} password removed.`)
      setConfirmRemove(false)
      await onChanged()
    } catch (caught) { setError(caught) } finally { setSecret(''); setBusy(false) }
  }

  return (
    <section className="panel account-access" aria-labelledby="access-heading">
      <div className="section-heading">
        <div><p className="eyebrow">Saved password</p><h3 id="access-heading">{account.email_address}</h3></div>
        <span className={`status-pill${binding !== 'ACTIVE' ? ' status-pill-attention' : ''}`}>{bindingLabel(binding)}</span>
      </div>
      <p className="hint">Passwords go directly to secure storage on this device and are cleared from the page after every action.</p>
      {account.has_outgoing ? (
        <fieldset className="role-choice">
          <legend>Mail service</legend>
          <label><input type="radio" name="credential-role" checked={role === 'incoming'} onChange={() => setRole('incoming')} /> Incoming mail (IMAP)</label>
          <label><input type="radio" name="credential-role" checked={role === 'outgoing'} onChange={() => setRole('outgoing')} /> Outgoing mail (SMTP)</label>
        </fieldset>
      ) : null}
      <StatusMessage message={notice} />
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />
      <div className="access-grid">
        <div>
          <label htmlFor="credential-value">New password or app password</label>
          <input id="credential-value" type="password" autoComplete="new-password" value={secret} onChange={(event) => setSecret(event.target.value)} disabled={!supportsRole || busy} />
          <div className="button-row form-actions">
            <button type="button" className="with-icon" disabled={!secret || !supportsRole || busy} onClick={() => void setCredential()}><KeyRound size={17} aria-hidden="true" />{busy ? 'Working…' : 'Save password'}</button>
          </div>
        </div>
        <details className="inline-details">
          <summary>Remove password</summary>
          <div className="details-body action-stack">
            {account.enabled ? <p className="hint">Pause this account before removing its saved password.</p> : null}
            <button type="button" className="text-danger with-icon" disabled={account.enabled || !supportsRole || busy} onClick={() => { setSecret(''); setConfirmRemove(true) }}><Trash2 size={16} aria-hidden="true" />Remove saved password</button>
            {confirmRemove ? (
              <div className="confirmation" role="alert">
                <p>Remove the {role === 'incoming' ? 'IMAP' : 'SMTP'} password from <strong>{account.name}</strong>? This connection will stop working.</p>
                <div className="button-row"><button type="button" className="danger" onClick={() => void removeCredential()}>Remove password</button><button type="button" className="secondary" onClick={() => setConfirmRemove(false)}>Cancel</button></div>
              </div>
            ) : null}
          </div>
        </details>
      </div>
    </section>
  )
}
