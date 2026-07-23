import { useCallback, useEffect, useState } from 'react'

import type { ManagementApi } from '../api'
import type { AccountDetails, AccountInput, AccountSummary, Endpoint } from '../types'
import { ConflictNotice, errorMessage, StatusMessage } from './Feedback'

const blankEndpoint = (port: number): Endpoint => ({
  host: '',
  port,
  user_name: '',
  use_ssl: true,
  start_ssl: false,
  verify_ssl: true,
})

const blankAccount = (): AccountInput => ({
  name: '',
  full_name: '',
  email_address: '',
  save_to_sent: true,
  sent_folder_name: null,
  incoming: blankEndpoint(993),
  outgoing: null,
})

const editableAccount = (current: AccountDetails): AccountInput => ({
  name: current.name,
  full_name: current.full_name,
  email_address: current.email_address,
  save_to_sent: current.save_to_sent,
  sent_folder_name: current.sent_folder_name,
  incoming: current.incoming,
  outgoing: current.outgoing,
})

function EndpointFields({
  id,
  title,
  value,
  onChange,
}: {
  id: string
  title: string
  value: Endpoint
  onChange: (value: Endpoint) => void
}) {
  const update = <K extends keyof Endpoint>(key: K, next: Endpoint[K]) => onChange({ ...value, [key]: next })
  return (
    <fieldset>
      <legend>{title}</legend>
      <div className="form-grid">
        <div><label htmlFor={`${id}-host`}>Host</label><input id={`${id}-host`} value={value.host} onChange={(event) => update('host', event.target.value)} required maxLength={255} /></div>
        <div><label htmlFor={`${id}-port`}>Port</label><input id={`${id}-port`} type="number" min="1" max="65535" value={value.port} onChange={(event) => update('port', event.currentTarget.valueAsNumber)} required /></div>
        <div className="wide"><label htmlFor={`${id}-username`}>User name</label><input id={`${id}-username`} value={value.user_name} onChange={(event) => update('user_name', event.target.value)} required maxLength={320} /></div>
      </div>
      <div className="checkbox-row">
        <label><input type="checkbox" checked={value.use_ssl} onChange={(event) => update('use_ssl', event.target.checked)} /> TLS from connection start</label>
        <label><input type="checkbox" checked={value.start_ssl} onChange={(event) => update('start_ssl', event.target.checked)} /> Upgrade with STARTTLS</label>
        <label><input type="checkbox" checked={value.verify_ssl} onChange={(event) => update('verify_ssl', event.target.checked)} /> Verify server certificate</label>
      </div>
    </fieldset>
  )
}

function AccountEditor({
  api,
  current,
  catalogRevision,
  onSaved,
  onCancel,
}: {
  api: ManagementApi
  current: AccountDetails | null
  catalogRevision: number
  onSaved: () => Promise<void>
  onCancel: () => void
}) {
  const [input, setInput] = useState<AccountInput>(() => current ? editableAccount(current) : blankAccount())
  const [incomingSecret, setIncomingSecret] = useState('')
  const [outgoingSecret, setOutgoingSecret] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => () => {
    setIncomingSecret('')
    setOutgoingSecret('')
  }, [])

  const update = <K extends keyof AccountInput>(key: K, value: AccountInput[K]) => setInput((old) => ({ ...old, [key]: value }))
  const editing = current !== null

  return (
    <form
      className="panel account-editor"
      onSubmit={(event) => {
        event.preventDefault()
        setBusy(true)
        setError(null)
        void (async () => {
          try {
            if (current) {
              await api.updateAccount(current.name, { ...input, expected_revision: current.revision })
            } else {
              await api.createAccount(input, {
                incoming: incomingSecret,
                outgoing: input.outgoing ? outgoingSecret : null,
              }, catalogRevision)
            }
            await onSaved()
          } catch (caught) {
            setError(caught)
          } finally {
            // Candidate values are single-use component state, cleared on every outcome.
            setIncomingSecret('')
            setOutgoingSecret('')
            setBusy(false)
          }
        })()
      }}
    >
      <div className="section-heading"><h3>{editing ? `Edit ${current.name}` : 'Add managed account'}</h3><button type="button" className="text-button" onClick={onCancel}>Cancel</button></div>
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />
      <div className="form-grid">
        <div><label htmlFor="account-name">Account name</label><input id="account-name" value={input.name} onChange={(event) => update('name', event.target.value)} required maxLength={100} /></div>
        <div><label htmlFor="full-name">Display name</label><input id="full-name" value={input.full_name} onChange={(event) => update('full_name', event.target.value)} required maxLength={200} /></div>
        <div className="wide"><label htmlFor="email-address">Email address</label><input id="email-address" type="email" value={input.email_address} onChange={(event) => update('email_address', event.target.value)} required maxLength={320} /></div>
      </div>
      <EndpointFields id="incoming" title="Incoming (IMAP)" value={input.incoming} onChange={(value) => update('incoming', value)} />
      {!editing ? <div><label htmlFor="incoming-secret">Incoming credential</label><input id="incoming-secret" type="password" autoComplete="new-password" value={incomingSecret} onChange={(event) => setIncomingSecret(event.target.value)} required /></div> : null}
      <label className="toggle"><input type="checkbox" checked={input.outgoing !== null} onChange={(event) => update('outgoing', event.target.checked ? blankEndpoint(465) : null)} /> Configure outgoing SMTP</label>
      {input.outgoing ? (
        <>
          <EndpointFields id="outgoing" title="Outgoing (SMTP)" value={input.outgoing} onChange={(value) => update('outgoing', value)} />
          {!editing ? <div><label htmlFor="outgoing-secret">Outgoing credential</label><input id="outgoing-secret" type="password" autoComplete="new-password" value={outgoingSecret} onChange={(event) => setOutgoingSecret(event.target.value)} required /></div> : null}
        </>
      ) : null}
      <div className="checkbox-row">
        <label><input type="checkbox" checked={input.save_to_sent} onChange={(event) => update('save_to_sent', event.target.checked)} /> Save outgoing messages to Sent</label>
      </div>
      <div><label htmlFor="sent-folder">Sent mailbox override (optional)</label><input id="sent-folder" value={input.sent_folder_name ?? ''} onChange={(event) => update('sent_folder_name', event.target.value || null)} maxLength={255} /></div>
      <button disabled={busy || (!editing && !incomingSecret) || (input.outgoing !== null && !editing && !outgoingSecret)}>{busy ? 'Saving…' : editing ? 'Save changes' : 'Create account'}</button>
    </form>
  )
}

export function AccountsPanel({ api }: { api: ManagementApi }) {
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const [catalogRevision, setCatalogRevision] = useState(0)
  const [editor, setEditor] = useState<'new' | AccountDetails | null>(null)
  const [busyName, setBusyName] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [removeName, setRemoveName] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    try {
      const [accountResult, status] = await Promise.all([api.accounts(), api.status()])
      setAccounts(accountResult)
      setCatalogRevision(status.report?.catalog_revision ?? 0)
      setError(null)
    } catch (caught) {
      setError(caught)
    }
  }, [api])

  useEffect(() => { void load() }, [load])

  const selectEdit = async (name: string) => {
    setBusyName(name)
    try { setEditor(await api.account(name)) } catch (caught) { setError(caught) } finally { setBusyName('') }
  }

  const lifecycle = async (account: AccountSummary, enabled: boolean) => {
    setBusyName(account.name)
    setError(null)
    try {
      await api.setAccountEnabled(account.name, enabled, account.revision)
      setNotice(`${account.name} ${enabled ? 'enabled' : 'disabled'}.`)
      await load()
    } catch (caught) { setError(caught) } finally { setBusyName('') }
  }

  const remove = async (account: AccountSummary) => {
    setBusyName(account.name)
    setError(null)
    try {
      const result = await api.removeAccount(account.name, account.revision, confirmation)
      setNotice(
        result.cleanup_required
          ? `${account.name} was soft-removed; ${result.cleanup_required} credential candidate(s) still require cleanup.`
          : `${account.name} was soft-removed and all credential candidates were cleaned.`,
      )
      setRemoveName('')
      setConfirmation('')
      await load()
    } catch (caught) { setError(caught) } finally { setBusyName('') }
  }

  if (editor) {
    return <section aria-labelledby="accounts-heading"><h2 id="accounts-heading">Accounts</h2><AccountEditor api={api} current={editor === 'new' ? null : editor} catalogRevision={catalogRevision} onCancel={() => setEditor(null)} onSaved={async () => { setEditor(null); setNotice('Account saved.'); await load() }} /></section>
  }

  return (
    <section aria-labelledby="accounts-heading">
      <div className="section-heading"><div><p className="eyebrow">Configuration</p><h2 id="accounts-heading">Managed accounts</h2></div><button type="button" onClick={() => setEditor('new')}>Add account</button></div>
      <StatusMessage message={notice} />
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />
      {accounts.length === 0 ? <div className="empty"><h3>No managed accounts</h3><p>Add an account to configure provider endpoints and credentials.</p></div> : (
        <div className="account-list">
          {accounts.map((account) => (
            <article className="account-card" key={account.name}>
              <div><h3>{account.name}</h3><p>{account.email_address}</p></div>
              <dl className="compact-dl"><div><dt>State</dt><dd>{account.enabled ? 'Enabled' : 'Disabled'}</dd></div><div><dt>IMAP credential</dt><dd>{account.incoming_binding.replaceAll('_', ' ')}</dd></div><div><dt>SMTP</dt><dd>{account.has_outgoing ? account.outgoing_binding?.replaceAll('_', ' ') : 'Not configured'}</dd></div></dl>
              <div className="button-row">
                <button type="button" className="secondary" disabled={Boolean(busyName)} onClick={() => void selectEdit(account.name)}>Edit</button>
                <button type="button" className="secondary" disabled={Boolean(busyName)} onClick={() => void lifecycle(account, !account.enabled)}>{account.enabled ? 'Disable' : 'Enable'}</button>
                <button type="button" className="danger" disabled={Boolean(busyName)} onClick={() => { setRemoveName(account.name); setConfirmation('') }}>Remove…</button>
              </div>
              {removeName === account.name ? (
                <div className="confirmation" role="group" aria-labelledby={`remove-${account.name}`}>
                  <p id={`remove-${account.name}`}><strong>Soft-remove {account.name}?</strong> This immediately disables provider work. Type the account name to confirm.</p>
                  <label htmlFor={`confirm-${account.name}`}>Account name</label>
                  <input id={`confirm-${account.name}`} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
                  <div className="button-row"><button type="button" className="danger" disabled={confirmation !== account.name || Boolean(busyName)} onClick={() => void remove(account)}>Confirm removal</button><button type="button" className="secondary" onClick={() => setRemoveName('')}>Cancel</button></div>
                </div>
              ) : null}
            </article>
          ))}
        </div>
      )}
    </section>
  )
}
