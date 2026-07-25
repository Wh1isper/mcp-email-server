import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, KeyRound, Mail, MoreHorizontal, Pause, Pencil, Play, Plus, RefreshCw, Trash2, Wrench } from 'lucide-react'

import { ApiError, type ManagementApi } from '../api'
import type { AccountDetails, AccountInput, AccountSummary, BindingState, CatalogTarget, Endpoint, ManagementStatus } from '../types'
import { ConflictNotice, errorMessage, StatusMessage } from './Feedback'
import { CredentialsPanel } from './CredentialsPanel'

const blankEndpoint = (port: number): Endpoint => ({
  host: '',
  port,
  user_name: '',
  use_ssl: true,
  start_ssl: false,
  verify_ssl: true,
})

interface MailSuggestion {
  service: string | null
  incoming: Endpoint
  outgoing: Endpoint
}

type EndpointSuggestionField = 'host' | 'port' | 'user_name' | 'transport' | 'verify_ssl'
type EndpointTouches = Record<EndpointSuggestionField, boolean>

interface SuggestionTouches {
  name: boolean
  full_name: boolean
  incoming: EndpointTouches
  outgoing: EndpointTouches
}

const endpointTouches = (touched: boolean): EndpointTouches => ({
  host: touched,
  port: touched,
  user_name: touched,
  transport: touched,
  verify_ssl: touched,
})

const applyEndpointSuggestion = (current: Endpoint, suggestion: Endpoint, touched: EndpointTouches): Endpoint => ({
  host: touched.host ? current.host : suggestion.host,
  port: touched.port || touched.transport ? current.port : suggestion.port,
  user_name: touched.user_name ? current.user_name : suggestion.user_name,
  use_ssl: touched.transport ? current.use_ssl : suggestion.use_ssl,
  start_ssl: touched.transport ? current.start_ssl : suggestion.start_ssl,
  verify_ssl: touched.verify_ssl ? current.verify_ssl : suggestion.verify_ssl,
})

const mailSuggestion = (emailAddress: string): MailSuggestion => {
  const domain = emailAddress.trim().toLowerCase().split('@')[1] ?? ''
  const known: Record<string, { service: string; incoming: string; outgoing: string; outgoingPort: number; outgoingStartTls?: boolean }> = {
    'fastmail.com': { service: 'Fastmail', incoming: 'imap.fastmail.com', outgoing: 'smtp.fastmail.com', outgoingPort: 465 },
    'gmail.com': { service: 'Google Mail', incoming: 'imap.gmail.com', outgoing: 'smtp.gmail.com', outgoingPort: 465 },
    'googlemail.com': { service: 'Google Mail', incoming: 'imap.gmail.com', outgoing: 'smtp.gmail.com', outgoingPort: 465 },
    'hotmail.com': { service: 'Microsoft Mail', incoming: 'outlook.office365.com', outgoing: 'smtp.office365.com', outgoingPort: 587, outgoingStartTls: true },
    'icloud.com': { service: 'iCloud Mail', incoming: 'imap.mail.me.com', outgoing: 'smtp.mail.me.com', outgoingPort: 587, outgoingStartTls: true },
    'live.com': { service: 'Microsoft Mail', incoming: 'outlook.office365.com', outgoing: 'smtp.office365.com', outgoingPort: 587, outgoingStartTls: true },
    'mac.com': { service: 'iCloud Mail', incoming: 'imap.mail.me.com', outgoing: 'smtp.mail.me.com', outgoingPort: 587, outgoingStartTls: true },
    'me.com': { service: 'iCloud Mail', incoming: 'imap.mail.me.com', outgoing: 'smtp.mail.me.com', outgoingPort: 587, outgoingStartTls: true },
    'outlook.com': { service: 'Microsoft Mail', incoming: 'outlook.office365.com', outgoing: 'smtp.office365.com', outgoingPort: 587, outgoingStartTls: true },
    'yahoo.com': { service: 'Yahoo Mail', incoming: 'imap.mail.yahoo.com', outgoing: 'smtp.mail.yahoo.com', outgoingPort: 465 },
    'zoho.com': { service: 'Zoho Mail', incoming: 'imap.zoho.com', outgoing: 'smtp.zoho.com', outgoingPort: 465 },
  }
  const preset = known[domain]
  const outgoingStartTls = preset?.outgoingStartTls === true
  return {
    service: preset?.service ?? null,
    incoming: { ...blankEndpoint(993), host: preset?.incoming ?? (domain ? `imap.${domain}` : ''), user_name: emailAddress },
    outgoing: {
      ...blankEndpoint(preset?.outgoingPort ?? 465),
      host: preset?.outgoing ?? (domain ? `smtp.${domain}` : ''),
      user_name: emailAddress,
      use_ssl: !outgoingStartTls,
      start_ssl: outgoingStartTls,
    },
  }
}

const displayNameFromEmail = (emailAddress: string): string => {
  const localPart = emailAddress.split('@', 1)[0]?.split('+', 1)[0] ?? ''
  return localPart
    .split(/[._-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

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

const matchesTarget = (status: ManagementStatus, target: CatalogTarget): boolean =>
  status.selected_catalog === target.expected_catalog
  && status.bootstrap_revision === target.expected_bootstrap_revision
  && status.report !== null

const bindingLabel = (state: BindingState | null): string => {
  if (state === null || state === 'MISSING' || state === 'SUPERSEDED') return 'Password missing'
  if (state === 'ACTIVE') return 'Ready'
  if (state === 'CLEANUP_REQUIRED') return 'Cleanup needed'
  throw new Error('Unsupported password binding state')
}

function AdvancedEndpointFields({
  id,
  label,
  value,
  portTouched,
  onChange,
}: {
  id: string
  label: string
  value: Endpoint
  portTouched: boolean
  onChange: (value: Endpoint, touchedField: EndpointSuggestionField) => void
}) {
  const update = <K extends keyof Endpoint>(key: K, next: Endpoint[K], touchedField: EndpointSuggestionField) => onChange({ ...value, [key]: next }, touchedField)
  const transport = value.use_ssl ? 'tls' : value.start_ssl ? 'starttls' : 'plain'
  const defaultPort = (nextTransport: string): number => id === 'incoming'
    ? nextTransport === 'tls' ? 993 : 143
    : nextTransport === 'tls' ? 465 : 587
  return (
    <details className="inline-details advanced-fields">
      <summary>More {label} options</summary>
      <div className="details-body form-grid">
        <div><label htmlFor={`${id}-port`}>Port</label><input id={`${id}-port`} type="number" inputMode="numeric" min="1" max="65535" value={Number.isNaN(value.port) ? '' : value.port} onChange={(event) => update('port', event.currentTarget.valueAsNumber, 'port')} required /></div>
        <div><label htmlFor={`${id}-username`}>Login name</label><input id={`${id}-username`} value={value.user_name} onChange={(event) => update('user_name', event.target.value, 'user_name')} required maxLength={320} autoComplete="username" autoCapitalize="none" spellCheck={false} /></div>
        <div>
          <label htmlFor={`${id}-transport`}>Security</label>
          <select
            id={`${id}-transport`}
            value={transport}
            onChange={(event) => {
              const nextTransport = event.target.value
              onChange({
                ...value,
                port: portTouched ? value.port : defaultPort(nextTransport),
                use_ssl: nextTransport === 'tls',
                start_ssl: nextTransport === 'starttls',
              }, 'transport')
            }}
          >
            <option value="tls">Encrypted connection (recommended)</option>
            <option value="starttls">STARTTLS</option>
            <option value="plain">No encryption</option>
          </select>
        </div>
        <label className="field-checkbox"><input type="checkbox" checked={value.verify_ssl} onChange={(event) => update('verify_ssl', event.target.checked, 'verify_ssl')} /> Verify the server certificate</label>
      </div>
    </details>
  )
}

function AccountEditor({
  api,
  current,
  catalogRevision,
  target,
  onSaved,
  onCancel,
}: {
  api: ManagementApi
  current: AccountDetails | null
  catalogRevision: number
  target: CatalogTarget
  onSaved: (message: string) => Promise<void>
  onCancel: () => void
}) {
  const [input, setInput] = useState<AccountInput>(() => current ? editableAccount(current) : blankAccount())
  const [incomingSecret, setIncomingSecret] = useState('')
  const [outgoingSecret, setOutgoingSecret] = useState('')
  const [touches, setTouches] = useState<SuggestionTouches>(() => ({
    name: current !== null,
    full_name: current !== null,
    incoming: {
      ...endpointTouches(current !== null),
      user_name: current !== null && current.incoming.user_name !== current.email_address,
    },
    outgoing: {
      ...endpointTouches(current !== null),
      user_name: current !== null && current.outgoing !== null && current.outgoing.user_name !== current.email_address,
    },
  }))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [outgoingOpen, setOutgoingOpen] = useState(Boolean(current?.outgoing))
  const setupDetailsRef = useRef<HTMLDetailsElement>(null)
  const accountNameRef = useRef<HTMLInputElement>(null)

  useEffect(() => () => {
    setIncomingSecret('')
    setOutgoingSecret('')
  }, [])

  const update = <K extends keyof AccountInput>(key: K, value: AccountInput[K]) => setInput((old) => ({ ...old, [key]: value }))
  const touchEndpoint = (role: 'incoming' | 'outgoing', field: EndpointSuggestionField) => {
    setTouches((old) => ({ ...old, [role]: { ...old[role], [field]: true } }))
  }
  const updateEmail = (emailAddress: string) => {
    setInput((old) => {
      const localPart = emailAddress.includes('@') ? emailAddress.split('@', 1)[0] ?? '' : ''
      const suggestion = mailSuggestion(emailAddress)
      return {
        ...old,
        email_address: emailAddress,
        name: touches.name ? old.name : localPart,
        full_name: touches.full_name ? old.full_name : displayNameFromEmail(emailAddress),
        incoming: applyEndpointSuggestion(old.incoming, suggestion.incoming, touches.incoming),
        outgoing: old.outgoing ? applyEndpointSuggestion(old.outgoing, suggestion.outgoing, touches.outgoing) : null,
      }
    })
  }
  const editing = current !== null
  const addingOutgoingBlocked = Boolean(current && current.outgoing === null && current.enabled)
  const removingOutgoingBlocked = Boolean(
    current
    && current.outgoing !== null
    && current.outgoing_binding !== null
    && current.outgoing_binding !== 'MISSING',
  )
  const outgoingToggleDisabled = addingOutgoingBlocked || removingOutgoingBlocked

  return (
    <form
      className="panel account-editor"
      onInvalid={(event) => {
        if (!(event.target instanceof HTMLElement)) return
        let ancestor: HTMLElement | null = event.target.parentElement
        while (ancestor) {
          if (ancestor instanceof HTMLDetailsElement) ancestor.open = true
          ancestor = ancestor.parentElement
        }
      }}
      onSubmit={(event) => {
        event.preventDefault()
        setBusy(true)
        setError(null)
        void (async () => {
          try {
            if (current) {
              const addingOutgoing = current.outgoing === null && input.outgoing !== null
              const removingOutgoing = current.outgoing !== null && input.outgoing === null
              await api.updateAccount(current.name, { ...input, expected_revision: current.revision }, target)
              await onSaved(addingOutgoing
                ? 'SMTP server added. Open Password to save its password before enabling this account.'
                : removingOutgoing
                  ? 'Outgoing mail disabled for this account.'
                  : 'Account settings saved.')
            } else {
              const result = await api.createAccount(input, {
                incoming: incomingSecret,
                outgoing: input.outgoing ? outgoingSecret : null,
              }, catalogRevision, target)
              setIncomingSecret('')
              setOutgoingSecret('')
              const attention = [result.incoming, result.outgoing].filter(
                (outcome) => outcome && (outcome.state !== 'active' || outcome.cleanup_required > 0),
              )
              await onSaved(attention.length
                ? 'Account created, but old password data still needs cleanup. Use Clean up old password data from the accounts page.'
                : 'Email account added.')
            }
          } catch (caught) {
            setError(caught)
            if (caught instanceof ApiError && caught.category === 'account_name_exists') {
              if (setupDetailsRef.current) setupDetailsRef.current.open = true
              accountNameRef.current?.focus()
            }
          } finally {
            setIncomingSecret('')
            setOutgoingSecret('')
            setBusy(false)
          }
        })()
      }}
    >
      <div className="section-heading editor-heading"><div><p className="eyebrow">Email account</p><h2>{editing ? `Edit ${current.email_address}` : 'Add email account'}</h2></div><button type="button" className="text-button" onClick={onCancel}>Back to accounts</button></div>
      <p className="lede">{editing ? 'Update this account. Saved passwords will not change.' : 'Enter your email address and password. Common server settings are filled in automatically.'}</p>
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />

      <div className="form-grid primary-fields quick-setup">
          <div className="field">
            <label htmlFor="email-address">Email address</label>
            <div className="input-shell"><Mail size={17} aria-hidden="true" /><input id="email-address" type="email" value={input.email_address} onChange={(event) => updateEmail(event.target.value)} required maxLength={320} autoComplete="email" inputMode="email" autoCapitalize="none" spellCheck={false} /></div>
          </div>
          {editing ? (
            <div className="field"><label htmlFor="full-name">Name shown on sent mail</label><input id="full-name" value={input.full_name} onChange={(event) => update('full_name', event.target.value)} required maxLength={200} autoComplete="name" /></div>
          ) : (
            <div className="field">
              <label htmlFor="incoming-secret">Password or app password</label>
              <div className="input-shell"><KeyRound size={17} aria-hidden="true" /><input id="incoming-secret" type="password" autoComplete="current-password" value={incomingSecret} onChange={(event) => setIncomingSecret(event.target.value)} required /></div>
              <small>Some email services require an app password.</small>
            </div>
          )}
      </div>

      <details ref={setupDetailsRef} className="setup-details" open={editing}>
        <summary>Advanced account settings</summary>
        <div className="details-body">
          <div className="form-grid">
            {!editing ? <div className="field"><label htmlFor="full-name">Name shown on sent mail</label><input id="full-name" value={input.full_name} onChange={(event) => { setTouches((old) => ({ ...old, full_name: true })); update('full_name', event.target.value) }} required maxLength={200} autoComplete="name" /></div> : null}
            <div className="field"><label htmlFor="account-name">Account nickname</label><input ref={accountNameRef} id="account-name" value={input.name} onChange={(event) => { setTouches((old) => ({ ...old, name: true })); update('name', event.target.value) }} required maxLength={100} autoComplete="off" autoCapitalize="none" spellCheck={false} /><small>Identifies this account in MCP tools and command-line commands. It is not shown to email recipients.</small></div>
            <div className="field wide"><label htmlFor="incoming-host">Incoming mail server</label><input id="incoming-host" value={input.incoming.host} onChange={(event) => { touchEndpoint('incoming', 'host'); update('incoming', { ...input.incoming, host: event.target.value }) }} required maxLength={255} placeholder="imap.example.com" inputMode="url" autoCapitalize="none" spellCheck={false} /></div>
          </div>
          <AdvancedEndpointFields id="incoming" label="incoming mail" value={input.incoming} portTouched={touches.incoming.port} onChange={(value, field) => { touchEndpoint('incoming', field); update('incoming', value) }} />
        </div>
      </details>

      <details className="setup-details outgoing-section" open={outgoingOpen} onToggle={(event) => setOutgoingOpen(event.currentTarget.open)}>
        <summary>Outgoing mail (optional)</summary>
        <div className="details-body">
          <div className="section-heading compact-heading"><p className="hint">Enable this only if you want to send from this address.</p><label className="switch"><input type="checkbox" checked={input.outgoing !== null} disabled={outgoingToggleDisabled} onChange={(event) => {
            if (!event.target.checked) setOutgoingSecret('')
            if (event.target.checked) setTouches((old) => ({ ...old, outgoing: endpointTouches(false) }))
            update('outgoing', event.target.checked ? mailSuggestion(input.email_address).outgoing : null)
          }} /> Enable sending</label></div>
          {addingOutgoingBlocked ? <p className="hint">Pause this account before adding outgoing mail.</p> : null}
          {removingOutgoingBlocked ? <p className="hint">Remove the saved SMTP password in Password before disabling outgoing mail.</p> : null}
          {input.outgoing ? (
          <div className="outgoing-body">
            <div className="form-grid">
              <div className="field"><label htmlFor="outgoing-host">Outgoing mail server</label><input id="outgoing-host" value={input.outgoing.host} onChange={(event) => { touchEndpoint('outgoing', 'host'); update('outgoing', input.outgoing ? { ...input.outgoing, host: event.target.value } : null) }} required maxLength={255} placeholder="smtp.example.com" inputMode="url" autoCapitalize="none" spellCheck={false} /></div>
              {!editing ? <div className="field"><label htmlFor="outgoing-secret">Outgoing mail password</label><input id="outgoing-secret" type="password" autoComplete="current-password" value={outgoingSecret} onChange={(event) => setOutgoingSecret(event.target.value)} required /><small>Usually the same password or app password.</small></div> : current.outgoing === null ? <div className="field-note"><strong>Add the password next</strong><span>Save these details, then open Password for this account.</span></div> : null}
            </div>
            <AdvancedEndpointFields id="outgoing" label="outgoing mail" value={input.outgoing} portTouched={touches.outgoing.port} onChange={(value, field) => { touchEndpoint('outgoing', field); update('outgoing', value) }} />
            <details className="inline-details">
              <summary>Sent folder options</summary>
              <div className="details-body">
                <label className="field-checkbox"><input type="checkbox" checked={input.save_to_sent} onChange={(event) => update('save_to_sent', event.target.checked)} /> Save outgoing messages to Sent</label>
                <label htmlFor="sent-folder">Sent folder (optional)</label><input id="sent-folder" value={input.sent_folder_name ?? ''} onChange={(event) => update('sent_folder_name', event.target.value || null)} maxLength={255} placeholder="Choose automatically" />
              </div>
            </details>
          </div>
          ) : null}
        </div>
      </details>

      <div className="button-row form-actions"><button className="with-icon" disabled={busy}>{!busy ? <Plus size={17} aria-hidden="true" /> : null}{busy ? 'Saving…' : editing ? 'Save changes' : 'Add account'}</button></div>
    </form>
  )
}

export function AccountsPanel({ api, target, onChanged }: { api: ManagementApi; target: CatalogTarget; onChanged?: () => void }) {
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const [catalogRevision, setCatalogRevision] = useState(0)
  const [cleanupRequired, setCleanupRequired] = useState(0)
  const [cleanupBusy, setCleanupBusy] = useState(false)
  const [editor, setEditor] = useState<'new' | AccountDetails | null>(null)
  const [accessName, setAccessName] = useState('')
  const [busyName, setBusyName] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [removeName, setRemoveName] = useState('')
  const [notice, setNotice] = useState('')
  const [refreshFailed, setRefreshFailed] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    try {
      const statusBefore = await api.status()
      if (!matchesTarget(statusBefore, target)) throw new Error('The selected account storage changed. Refresh status before continuing.')
      const accountResult = await api.accounts()
      const statusAfter = await api.status()
      if (!matchesTarget(statusAfter, target)) throw new Error('The selected account storage changed while accounts were loading.')
      setAccounts(accountResult)
      setCatalogRevision(statusAfter.report?.catalog_revision ?? 0)
      setCleanupRequired(statusAfter.report?.cleanup_required_bindings ?? 0)
      setRefreshFailed(false)
      setError(null)
      return true
    } catch (caught) {
      setRefreshFailed(true)
      setError(caught)
      return false
    }
  }, [api, target])

  useEffect(() => { void load() }, [load])

  const reloadAll = async (): Promise<boolean> => {
    const refreshed = await load()
    onChanged?.()
    if (!refreshed) setNotice('The change was saved, but the latest account state could not be refreshed. Refresh accounts before making another change.')
    return refreshed
  }

  const cleanupCredentials = async () => {
    setCleanupBusy(true)
    setError(null)
    try {
      const status = await api.status()
      if (!matchesTarget(status, target) || !status.report) {
        throw new Error('The selected account storage changed. Refresh accounts before cleanup.')
      }
      const report = await api.cleanupCredentials(100, status.report.catalog_revision, target)
      setNotice(report.remaining
        ? `Cleanup removed ${report.cleaned} item(s); ${report.remaining} still need attention.`
        : `Cleanup complete. ${report.cleaned} item(s) removed.`)
      await reloadAll()
    } catch (caught) {
      setError(caught)
    } finally {
      setCleanupBusy(false)
    }
  }

  const selectEdit = async (name: string) => {
    setBusyName(name)
    try { setEditor(await api.account(name)) } catch (caught) { setError(caught) } finally { setBusyName('') }
  }

  const toggleEnabled = async (account: AccountSummary, enabled: boolean) => {
    setBusyName(account.name)
    setError(null)
    try {
      await api.setAccountEnabled(account.name, enabled, account.revision, target)
      setNotice(`${account.email_address} ${enabled ? 'enabled' : 'paused'}.`)
      await reloadAll()
    } catch (caught) { setError(caught) } finally { setBusyName('') }
  }

  const remove = async (account: AccountSummary) => {
    setBusyName(account.name)
    setError(null)
    try {
      const result = await api.removeAccount(account.name, account.revision, confirmation, target)
      setNotice(result.cleanup_required
        ? `${account.email_address} was removed; old password data still requires cleanup.`
        : `${account.email_address} was removed.`)
      setRemoveName('')
      setConfirmation('')
      await reloadAll()
    } catch (caught) { setError(caught) } finally { setBusyName('') }
  }

  if (refreshFailed) {
    return (
      <section aria-labelledby="accounts-heading">
        <div className="page-heading"><h1 id="accounts-heading">Email accounts</h1><p className="lede">The saved account list is temporarily unavailable.</p></div>
        <StatusMessage message={notice} />
        <StatusMessage message={errorMessage(error)} error />
        <div className="panel action-stack"><p>Refresh the account list before making another change. Out-of-date account details will not be reused.</p><button type="button" className="secondary with-icon" onClick={() => void (async () => { if (await load()) setNotice('Account list refreshed.') })()}><RefreshCw size={16} aria-hidden="true" />Refresh accounts</button></div>
      </section>
    )
  }

  const accessAccount = accounts.find((account) => account.name === accessName)
  if (accessAccount) {
    return (
      <section aria-label="Account password and connection">
        <button type="button" className="text-button back-link with-icon" onClick={() => setAccessName('')}><ArrowLeft size={16} aria-hidden="true" />Back to email accounts</button>
        <CredentialsPanel key={accessAccount.name} api={api} target={target} account={accessAccount} onChanged={async () => { await reloadAll() }} />
      </section>
    )
  }

  if (editor) {
    return <section aria-label="Email account editor"><AccountEditor api={api} current={editor === 'new' ? null : editor} catalogRevision={catalogRevision} target={target} onCancel={() => setEditor(null)} onSaved={async (message) => { const refreshed = await reloadAll(); setEditor(null); if (refreshed) setNotice(message) }} /></section>
  }

  return (
    <section aria-labelledby="accounts-heading">
      <div className="section-heading page-heading"><div><h1 id="accounts-heading">Email accounts</h1><p className="lede">Add an inbox or update its account and password settings.</p></div>{accounts.length ? <button type="button" className="with-icon" onClick={() => setEditor('new')}><Plus size={17} aria-hidden="true" />Add email account</button> : null}</div>
      <StatusMessage message={notice} />
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />
      {cleanupRequired > 0 ? (
        <div className="panel action-stack" aria-labelledby="credential-cleanup-heading">
          <div><p className="eyebrow">Password maintenance</p><h2 id="credential-cleanup-heading">Old password data needs cleanup</h2></div>
          <p>{cleanupRequired} old password {cleanupRequired === 1 ? 'copy could' : 'copies could'} not be removed earlier. Cleanup never removes the password currently in use.</p>
          <div><button type="button" className="secondary with-icon" disabled={cleanupBusy} onClick={() => void cleanupCredentials()}><Wrench size={16} aria-hidden="true" />{cleanupBusy ? 'Cleaning up…' : 'Clean up old password data'}</button></div>
        </div>
      ) : null}
      {accounts.length === 0 ? (
        <div className="empty account-empty"><div className="empty-mark" aria-hidden="true"><Mail size={22} /></div><h2>No email accounts yet</h2><p>Start with your email address and password. Connection settings are filled in automatically and remain editable.</p><button type="button" className="with-icon" onClick={() => setEditor('new')}><Plus size={17} aria-hidden="true" />Add your first account</button></div>
      ) : (
        <div className="account-list">
          {accounts.map((account) => {
            return (
              <article className="account-card" key={account.name}>
                <div className="account-identity"><span className="account-avatar" aria-hidden="true"><Mail size={18} /></span><div><h2>{account.email_address}</h2><p>{account.name}</p></div></div>
                <div className="account-statuses">
                  <span className={`status-pill${account.enabled ? '' : ' status-pill-muted'}`}>{account.enabled ? 'Enabled' : 'Paused'}</span>
                  <span className={`status-pill${account.incoming_binding !== 'ACTIVE' ? ' status-pill-attention' : ''}`}>IMAP: {bindingLabel(account.incoming_binding)}</span>
                  {account.has_outgoing ? <span className={`status-pill${account.outgoing_binding !== 'ACTIVE' ? ' status-pill-attention' : ''}`}>SMTP: {bindingLabel(account.outgoing_binding)}</span> : <span className="status-pill status-pill-muted">Receive only</span>}
                </div>
                <div className="account-actions">
                  <button type="button" className="with-icon" disabled={Boolean(busyName)} onClick={() => setAccessName(account.name)}><KeyRound size={16} aria-hidden="true" />Password</button>
                  <button type="button" className="secondary with-icon" disabled={Boolean(busyName)} onClick={() => void selectEdit(account.name)}><Pencil size={16} aria-hidden="true" />Edit</button>
                  <details className="account-more">
                    <summary><MoreHorizontal size={17} aria-hidden="true" />More</summary>
                    <div className="details-body action-stack">
                      <button type="button" className="secondary with-icon" disabled={Boolean(busyName)} onClick={() => void toggleEnabled(account, !account.enabled)}>{account.enabled ? <Pause size={16} aria-hidden="true" /> : <Play size={16} aria-hidden="true" />}{account.enabled ? 'Pause account' : 'Enable account'}</button>
                      <button type="button" className="text-danger with-icon" disabled={Boolean(busyName)} onClick={() => { setRemoveName(account.name); setConfirmation('') }}><Trash2 size={16} aria-hidden="true" />Remove account</button>
                    </div>
                  </details>
                </div>
                {removeName === account.name ? (
                  <div className="confirmation" role="group" aria-labelledby={`remove-${account.name}`}>
                    <p id={`remove-${account.name}`}><strong>Remove {account.email_address}?</strong> Type the account nickname <strong>{account.name}</strong> to confirm.</p>
                    <label htmlFor={`confirm-${account.name}`}>Account nickname</label>
                    <input id={`confirm-${account.name}`} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" autoCapitalize="none" spellCheck={false} />
                    <div className="button-row"><button type="button" className="danger" disabled={confirmation !== account.name || Boolean(busyName)} onClick={() => void remove(account)}>Remove account</button><button type="button" className="secondary" onClick={() => setRemoveName('')}>Cancel</button></div>
                  </div>
                ) : null}
              </article>
            )
          })}
        </div>
      )}
    </section>
  )
}
