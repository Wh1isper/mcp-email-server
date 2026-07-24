import { useEffect, useMemo, useState } from 'react'
import { AtSign, LogOut, Mail, Settings2 } from 'lucide-react'

import type { ManagementApi } from './api'
import type { CatalogTarget, ManagementStatus } from './types'
import { AccountsPanel } from './components/AccountsPanel'
import { errorMessage } from './components/Feedback'
import { SettingsPanel } from './components/SettingsPanel'
import { StatusPanel } from './components/StatusPanel'

type View = 'accounts' | 'settings'

const legacyHasContent = (status: ManagementStatus | null): boolean => Boolean(
  status?.legacy_source
  && (
    status.legacy_source.account_count > 0
    || status.legacy_source.unsupported_provider_count > 0
    || status.legacy_source.policy_customized
  ),
)

export function App({ api, bootstrapToken }: { api: ManagementApi; bootstrapToken: string | null }) {
  const [auth, setAuth] = useState<'loading' | 'ready' | 'failed' | 'logged-out'>('loading')
  const [authError, setAuthError] = useState('')
  const [view, setView] = useState<View>('accounts')
  const [loggingOut, setLoggingOut] = useState(false)
  const [managementStatus, setManagementStatus] = useState<ManagementStatus | null>(null)
  const [statusRefreshKey, setStatusRefreshKey] = useState(0)
  const [catalogRevisionHint, setCatalogRevisionHint] = useState<{ workspace: string; revision: number } | null>(null)

  useEffect(() => {
    let active = true
    void (async () => {
      try {
        if (bootstrapToken) await api.exchangeBootstrap(bootstrapToken)
        else await api.session()
        if (active) setAuth('ready')
      } catch (error) {
        if (active) {
          setAuthError(errorMessage(error))
          setAuth('failed')
        }
      }
    })()
    return () => { active = false }
  }, [api, bootstrapToken])

  const selectedCatalog = managementStatus?.selected_catalog ?? null
  const selectedBootstrapRevision = managementStatus?.bootstrap_revision ?? null
  const catalogTarget = useMemo<CatalogTarget | null>(() => selectedCatalog && selectedBootstrapRevision !== null
    ? {
        expected_bootstrap_revision: selectedBootstrapRevision,
        expected_catalog: selectedCatalog,
      }
    : null, [selectedBootstrapRevision, selectedCatalog])
  const workspaceKey = catalogTarget
    ? `${catalogTarget.expected_bootstrap_revision}:${catalogTarget.expected_catalog}`
    : 'unavailable'
  const activeRevisionHint = catalogRevisionHint?.workspace === workspaceKey
    ? catalogRevisionHint.revision
    : null

  if (auth === 'loading') return <main className="centered"><div className="spinner" aria-hidden="true" /><p role="status">Securing this local settings session…</p></main>
  if (auth === 'failed') return <main className="centered"><section className="auth-card" role="alert"><p className="eyebrow">Session unavailable</p><h1>Open a fresh settings link</h1><p>{authError}</p><p>Close this tab and run <code>mcp-email-server ui</code> again if the local process was restarted or this one-time link was already used.</p></section></main>
  if (auth === 'logged-out') return <main className="centered"><section className="auth-card"><p className="eyebrow">Session ended</p><h1>Signed out</h1><p>The local settings session was invalidated. You can close this tab safely.</p></section></main>

  const catalogReady = Boolean(catalogTarget && managementStatus?.report)
  const notifyConfigurationChanged = () => setStatusRefreshKey((current) => current + 1)

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <header className="topbar">
        <div className="brand"><span className="brand-mark" aria-hidden="true"><AtSign size={19} strokeWidth={2.3} /></span><div><strong>Local Email</strong><span>Account settings</span></div></div>
        <div className="local-badge"><span aria-hidden="true" /> Private local session</div>
        <button type="button" className="secondary with-icon" disabled={loggingOut} onClick={() => {
          setLoggingOut(true)
          void api.logout().finally(() => { setAuth('logged-out'); setLoggingOut(false) })
        }}><LogOut size={16} aria-hidden="true" />{loggingOut ? 'Signing out…' : 'Sign out'}</button>
      </header>
      <nav className="primary-nav" aria-label="Settings sections">
        <button type="button" className="with-icon" aria-current={view === 'accounts' ? 'page' : undefined} onClick={() => setView('accounts')}><Mail size={17} aria-hidden="true" />Email accounts</button>
        <button type="button" className="with-icon" aria-current={view === 'settings' ? 'page' : undefined} onClick={() => setView('settings')}><Settings2 size={17} aria-hidden="true" />Settings &amp; help</button>
      </nav>
      <main id="main-content" className="content">
        <StatusPanel
          api={api}
          refreshKey={statusRefreshKey}
          catalogRevisionHint={activeRevisionHint}
          onStatusChange={setManagementStatus}
          onNavigate={setView}
        />
        {catalogReady && catalogTarget ? (
          view === 'accounts'
            ? <AccountsPanel key={workspaceKey} api={api} target={catalogTarget} onChanged={notifyConfigurationChanged} />
            : <SettingsPanel
                key={workspaceKey}
                api={api}
                target={catalogTarget}
                hasLegacySource={legacyHasContent(managementStatus)}
                onChanged={notifyConfigurationChanged}
                onPolicyRevision={(revision) => setCatalogRevisionHint({ workspace: workspaceKey, revision })}
              />
        ) : null}
      </main>
      <footer><span>This interface manages settings only. It cannot read mail.</span><span>Available only on this device</span></footer>
    </div>
  )
}
