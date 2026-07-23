import { useCallback, useEffect, useState } from 'react'

import type { ManagementApi } from '../api'
import type { AccountSummary, BindingRole } from '../types'
import { ConflictNotice, errorMessage, StatusMessage } from './Feedback'

export function CredentialsPanel({ api }: { api: ManagementApi }) {
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const [accountName, setAccountName] = useState('')
  const [role, setRole] = useState<BindingRole>('incoming')
  const [secret, setSecret] = useState('')
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    try {
      const result = await api.accounts()
      setAccounts(result)
      setAccountName((old) => old || result[0]?.name || '')
    } catch (caught) { setError(caught) }
  }, [api])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    setSecret('')
    setConfirmRemove(false)
  }, [accountName, role])
  useEffect(() => () => setSecret(''), [])

  const current = accounts.find((account) => account.name === accountName)
  const supportsRole = role === 'incoming' || current?.has_outgoing === true

  const setCredential = async () => {
    if (!current || !secret) return
    setBusy(true); setNotice(''); setError(null)
    try {
      const result = await api.setCredential(current.name, role, secret, current.revision)
      setNotice(result.state === 'active' ? `${role} credential is active.` : `${role} credential state: ${result.state.replaceAll('_', ' ')}. Run health checks for remediation.`)
      await load()
    } catch (caught) { setError(caught) } finally {
      setSecret('')
      setBusy(false)
    }
  }

  const removeCredential = async () => {
    if (!current) return
    setBusy(true); setNotice(''); setError(null)
    try {
      const result = await api.removeCredential(current.name, role, current.revision)
      setNotice(`${role} credential removal state: ${result.state.replaceAll('_', ' ')}.`)
      setConfirmRemove(false)
      await load()
    } catch (caught) { setError(caught) } finally { setSecret(''); setBusy(false) }
  }

  const connectivity = async (selectedRole: BindingRole) => {
    if (!current) return
    setBusy(true); setNotice(''); setError(null)
    try {
      const result = await api.testConnectivity(current.name, selectedRole)
      setNotice(`${selectedRole.toUpperCase()} connectivity: ${result.message}`)
    } catch (caught) { setError(caught) } finally { setSecret(''); setBusy(false) }
  }

  const repair = async (action: 'resume' | 'rollback') => {
    if (!current) return
    setBusy(true); setNotice(''); setError(null)
    try {
      const result = await api.repairCredential(current.name, role, action, current.revision)
      setNotice(`${role} credential repair state: ${result.state.replaceAll('_', ' ')}.`)
      await load()
    } catch (caught) { setError(caught) } finally { setSecret(''); setBusy(false) }
  }

  const cleanup = async () => {
    setBusy(true); setNotice(''); setError(null)
    try {
      const status = await api.status()
      if (!status.report) throw new Error('No managed catalog is configured.')
      const report = await api.cleanupCredentials(100, status.report.catalog_revision)
      setNotice(`Cleanup examined ${report.examined}, cleaned ${report.cleaned}, remaining ${report.remaining}.`)
      await load()
    } catch (caught) { setError(caught) } finally { setSecret(''); setBusy(false) }
  }

  return (
    <section aria-labelledby="credentials-heading">
      <div className="section-heading"><div><p className="eyebrow">Secrets &amp; providers</p><h2 id="credentials-heading">Credentials and connectivity</h2></div></div>
      <p className="lede">Credential values are submitted once and cleared after every outcome. Connectivity checks never save values or change account state.</p>
      <StatusMessage message={notice} />
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />
      <div className="split">
        <div className="panel">
          <h3>Set or rotate credential</h3>
          <label htmlFor="credential-account">Account</label>
          <select id="credential-account" value={accountName} onChange={(event) => setAccountName(event.target.value)}>
            {accounts.length === 0 ? <option value="">No accounts available</option> : accounts.map((account) => <option key={account.name}>{account.name}</option>)}
          </select>
          <fieldset className="choice-group"><legend>Provider role</legend><label><input type="radio" name="role" checked={role === 'incoming'} onChange={() => setRole('incoming')} /> Incoming (IMAP)</label><label><input type="radio" name="role" checked={role === 'outgoing'} onChange={() => setRole('outgoing')} disabled={!current?.has_outgoing} /> Outgoing (SMTP)</label></fieldset>
          <label htmlFor="credential-value">New credential</label>
          <input id="credential-value" type="password" autoComplete="new-password" value={secret} onChange={(event) => setSecret(event.target.value)} disabled={!supportsRole || busy} />
          <button type="button" disabled={!current || !secret || !supportsRole || busy} onClick={() => void setCredential()}>{busy ? 'Working…' : 'Set credential'}</button>
          <button type="button" className="danger" disabled={!current || !supportsRole || busy} onClick={() => { setSecret(''); setConfirmRemove(true) }}>Remove credential…</button>
          {confirmRemove ? <div className="confirmation" role="alert"><p>Remove the {role} credential from <strong>{current?.name}</strong>? The binding becomes unusable before cleanup.</p><div className="button-row"><button type="button" className="danger" onClick={() => void removeCredential()}>Confirm removal</button><button type="button" className="secondary" onClick={() => setConfirmRemove(false)}>Cancel</button></div></div> : null}
        </div>
        <div className="panel action-stack">
          <h3>Connectivity</h3>
          <button type="button" disabled={!current || busy} onClick={() => void connectivity('incoming')}>Test IMAP</button>
          <button type="button" disabled={!current?.has_outgoing || busy} onClick={() => void connectivity('outgoing')}>Test SMTP</button>
          <hr />
          <h3>Credential repair</h3>
          <p className="hint">Resume or roll back an interrupted candidate only when the selected binding requires explicit repair.</p>
          {current && (role === 'incoming' ? current.incoming_binding : current.outgoing_binding) === 'PENDING_REPAIR_REQUIRED' ? <div className="button-row">
            <button type="button" disabled={busy} onClick={() => void repair('resume')}>Resume candidate</button>
            <button type="button" className="secondary" disabled={busy} onClick={() => void repair('rollback')}>Roll back candidate</button>
          </div> : <p className="hint">The selected binding does not require explicit repair.</p>}
          <p className="hint">Bounded cleanup inspects superseded cleanup candidates only.</p>
          <button type="button" className="secondary" disabled={busy} onClick={() => void cleanup()}>Run bounded cleanup</button>
        </div>
      </div>
    </section>
  )
}
