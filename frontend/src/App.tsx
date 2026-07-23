import { useEffect, useRef, useState } from 'react'

import type { ManagementApi } from './api'
import { AccountsPanel } from './components/AccountsPanel'
import { CredentialsPanel } from './components/CredentialsPanel'
import { errorMessage } from './components/Feedback'
import { HealthPanel } from './components/HealthPanel'
import { ImportPanel } from './components/ImportPanel'
import { PolicyPanel } from './components/PolicyPanel'
import { StatusPanel } from './components/StatusPanel'

type View = 'status' | 'accounts' | 'credentials' | 'policy' | 'migration' | 'health'
const views: { id: View; label: string }[] = [
  { id: 'status', label: 'Setup & status' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'credentials', label: 'Credentials' },
  { id: 'policy', label: 'Policy' },
  { id: 'migration', label: 'Migration' },
  { id: 'health', label: 'Health' },
]

export function App({ api, bootstrapToken }: { api: ManagementApi; bootstrapToken: string | null }) {
  const [auth, setAuth] = useState<'loading' | 'ready' | 'failed' | 'logged-out'>('loading')
  const [authError, setAuthError] = useState('')
  const [view, setView] = useState<View>('status')
  const [loggingOut, setLoggingOut] = useState(false)
  const tabRefs = useRef(new Map<View, HTMLButtonElement>())

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

  if (auth === 'loading') return <main className="centered"><div className="spinner" aria-hidden="true" /><p role="status">Securing this local management session…</p></main>
  if (auth === 'failed') return <main className="centered"><section className="auth-card" role="alert"><p className="eyebrow">Session unavailable</p><h1>Open a fresh management link</h1><p>{authError}</p><p>Close this tab and run <code>mcp-email-server ui</code> again if the local process was restarted or this one-time link was already used.</p></section></main>
  if (auth === 'logged-out') return <main className="centered"><section className="auth-card"><p className="eyebrow">Session ended</p><h1>Signed out</h1><p>The local management session was invalidated. You can close this tab safely.</p></section></main>

  const moveTab = (current: number, delta: number) => {
    const target = views[(current + delta + views.length) % views.length]
    if (target) {
      setView(target.id)
      tabRefs.current.get(target.id)?.focus()
    }
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <header className="topbar">
        <div className="brand"><span className="brand-mark" aria-hidden="true">@</span><div><strong>Local Email</strong><span>Management plane</span></div></div>
        <div className="local-badge"><span aria-hidden="true" /> Local session</div>
        <button type="button" className="secondary" disabled={loggingOut} onClick={() => {
          setLoggingOut(true)
          void api.logout().finally(() => { setAuth('logged-out'); setLoggingOut(false) })
        }}>{loggingOut ? 'Signing out…' : 'Sign out'}</button>
      </header>
      <nav className="tabs" aria-label="Management sections" role="tablist">
        {views.map((item, index) => <button
          key={item.id}
          ref={(node) => { if (node) tabRefs.current.set(item.id, node); else tabRefs.current.delete(item.id) }}
          type="button"
          role="tab"
          id={`tab-${item.id}`}
          aria-selected={view === item.id}
          aria-controls={`panel-${item.id}`}
          tabIndex={view === item.id ? 0 : -1}
          onClick={() => setView(item.id)}
          onKeyDown={(event) => {
            if (event.key === 'ArrowRight') { event.preventDefault(); moveTab(index, 1) }
            if (event.key === 'ArrowLeft') { event.preventDefault(); moveTab(index, -1) }
            if (event.key === 'Home') { event.preventDefault(); moveTab(0, 0) }
            if (event.key === 'End') { event.preventDefault(); moveTab(views.length - 1, 0) }
          }}
        >{item.label}</button>)}
      </nav>
      <main id="main-content" className="content" role="tabpanel" aria-labelledby={`tab-${view}`}>
        <div id={`panel-${view}`}>
          {view === 'status' ? <StatusPanel api={api} /> : null}
          {view === 'accounts' ? <AccountsPanel api={api} /> : null}
          {view === 'credentials' ? <CredentialsPanel api={api} /> : null}
          {view === 'policy' ? <PolicyPanel api={api} /> : null}
          {view === 'migration' ? <ImportPanel api={api} /> : null}
          {view === 'health' ? <HealthPanel api={api} /> : null}
        </div>
      </main>
      <footer><span>No mail content is available in this interface.</span><span>Loopback management only</span></footer>
    </div>
  )
}
