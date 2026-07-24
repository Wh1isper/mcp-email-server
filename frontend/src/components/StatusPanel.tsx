import { useCallback, useEffect, useRef, useState } from 'react'
import { CheckCircle2, Import, Plus, Power, RefreshCw, RotateCcw } from 'lucide-react'

import type { ManagementApi } from '../api'
import type { CatalogTarget, Lifecycle, ManagementStatus } from '../types'
import { ConflictNotice, errorMessage, problemMessage, StatusMessage } from './Feedback'

interface StatusPanelProps {
  api: ManagementApi
  refreshKey?: number
  catalogRevisionHint?: number | null
  onStatusChange?: (status: ManagementStatus) => void
  onNavigate?: (view: 'accounts' | 'settings') => void
}

const legacyHasContent = (status: ManagementStatus): boolean => Boolean(
  status.legacy_source
  && (
    status.legacy_source.account_count > 0
    || status.legacy_source.unsupported_provider_count > 0
    || status.legacy_source.policy_customized
  ),
)

const modeLabel = (mode: ManagementStatus['mode']): string => mode === 'managed' ? 'New account settings' : 'Previous settings'
const lifecycleLabel = (lifecycle: Lifecycle): string => lifecycle === 'ACTIVE' ? 'Ready to use' : 'Setup in progress'

export function StatusPanel({ api, refreshKey = 0, catalogRevisionHint = null, onStatusChange, onNavigate }: StatusPanelProps) {
  const [status, setStatus] = useState<ManagementStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)
  const autoAttemptedRevision = useRef<number | null>(null)
  const catalogRevisionHintRef = useRef<number | null>(null)
  const loadSequence = useRef(0)

  const load = useCallback(async () => {
    const sequence = ++loadSequence.current
    try {
      const next = await api.status()
      if (sequence !== loadSequence.current) return null
      const hint = catalogRevisionHintRef.current
      const resolved = hint !== null && next.report && hint > next.report.catalog_revision
        ? { ...next, report: { ...next.report, catalog_revision: hint } }
        : next
      setStatus(resolved)
      onStatusChange?.(resolved)
      setError(null)
      return resolved
    } catch (caught) {
      if (sequence === loadSequence.current) setError(caught)
      return null
    }
  }, [api, onStatusChange])

  useEffect(() => { void load() }, [load, refreshKey])
  useEffect(() => {
    catalogRevisionHintRef.current = catalogRevisionHint
    if (catalogRevisionHint === null) return
    setStatus((current) => current?.report && catalogRevisionHint > current.report.catalog_revision
      ? { ...current, report: { ...current.report, catalog_revision: catalogRevisionHint } }
      : current)
  }, [catalogRevisionHint])

  const act = useCallback(async (operation: () => Promise<void>, message: string): Promise<boolean> => {
    setBusy(true)
    setNotice('')
    setError(null)
    try {
      await operation()
      setNotice(message)
      await load()
      return true
    } catch (caught) {
      setError(caught)
      return false
    } finally {
      setBusy(false)
    }
  }, [load])

  useEffect(() => {
    if (
      !status
      || status.bootstrap_exists
      || status.bootstrap_revision !== 0
      || status.mode !== 'legacy'
      || status.selected_catalog
      || status.legacy_source_problem
      || legacyHasContent(status)
      || autoAttemptedRevision.current === status.bootstrap_revision
    ) return

    autoAttemptedRevision.current = status.bootstrap_revision
    void act(
      async () => { await api.initializeDefaultCatalog(status.bootstrap_revision, true) },
      '',
    )
  }, [act, api, status])

  const hasLegacy = status ? legacyHasContent(status) : false
  const revision = status?.report?.catalog_revision ?? 0
  const catalogTarget: CatalogTarget | null = status?.selected_catalog
    ? { expected_bootstrap_revision: status.bootstrap_revision, expected_catalog: status.selected_catalog }
    : null
  const catalogProblems = status?.report?.problems.filter(
    (problem) => !(problem === 'no_enabled_account' && status.report?.account_count === 0),
  ) ?? []
  const settled = Boolean(
    status
    && status.mode === 'managed'
    && status.running_mode === 'managed'
    && status.report?.lifecycle === 'ACTIVE'
    && !status.restart_required,
  )
  const emptyWorkspace = Boolean(
    status?.selected_catalog
    && status.report?.account_count === 0
    && !hasLegacy
    && !status.catalog_problem
    && !status.legacy_source_problem,
  )
  const hideBanner = settled || emptyWorkspace

  const prepare = async () => {
    if (!status) return
    const succeeded = await act(
      () => api.initializeDefaultCatalog(status.bootstrap_revision, false).then(() => undefined),
      hasLegacy ? 'Import is ready for review.' : 'Your private account settings are ready.',
    )
    if (succeeded) onNavigate?.(hasLegacy ? 'settings' : 'accounts')
  }

  const mainContent = () => {
    if (!status) return <div className="setup-copy"><strong>Loading account settings…</strong><span>Checking the local configuration.</span></div>
    if (status.catalog_problem) return <div className="setup-copy"><strong>Account settings are unavailable</strong><span>Open technical details below for recovery information.</span></div>
    if (status.legacy_source_problem) return <div className="setup-copy"><strong>Existing settings need attention</strong><span>Fix the earlier settings before importing them.</span></div>
    if (!status.selected_catalog) {
      return hasLegacy ? (
        <>
          <div className="setup-copy"><strong>Existing email settings found</strong><span>Prepare a private copy, then review exactly what will be imported.</span></div>
          <button type="button" className="with-icon" disabled={busy} onClick={() => void prepare()}><Import size={17} aria-hidden="true" />Import existing settings</button>
        </>
      ) : (
        <>
          <div className="setup-copy"><strong>{busy ? 'Preparing account settings…' : 'Account storage is not ready'}</strong><span>This stays private on your device.</span></div>
          {!busy ? <button type="button" className="with-icon" onClick={() => void prepare()}><Plus size={17} aria-hidden="true" />Prepare account settings</button> : null}
        </>
      )
    }
    if (!status.report) return <div className="setup-copy"><strong>Account settings are unavailable</strong><span>Refresh or review technical details.</span></div>
    if (status.report.account_count === 0) {
      return hasLegacy ? (
        <>
          <div className="setup-copy"><strong>Ready to import your existing accounts</strong><span>Review the source before copying any settings.</span></div>
          <button type="button" className="with-icon" onClick={() => onNavigate?.('settings')}><Import size={17} aria-hidden="true" />Review import</button>
        </>
      ) : (
        <div className="setup-copy"><strong>Account settings ready</strong><span>Add your first account when you’re ready. You only need an email address and password to start.</span></div>
      )
    }
    if (status.report.lifecycle === 'STAGING') {
      return (
        <>
          <div className="setup-copy"><strong>{status.report.account_count} account{status.report.account_count === 1 ? '' : 's'} saved</strong><span>Finish setup after every enabled account has its required server and password settings.</span></div>
          <button type="button" className="with-icon" disabled={busy || !catalogTarget || catalogProblems.length > 0} onClick={() => void act(
            () => catalogTarget ? api.activateCatalog(revision, catalogTarget) : Promise.resolve(),
            'Account settings validated. One final confirmation remains.',
          )}><CheckCircle2 size={17} aria-hidden="true" />Finish setup</button>
        </>
      )
    }
    if (status.mode === 'legacy') {
      return (
        <>
          <div className="setup-copy"><strong>Your accounts are ready</strong><span>Confirm when you want the mail server to use them after restart.</span></div>
          <button type="button" className="with-icon" disabled={busy} onClick={() => void act(
            () => api.selectMode('managed', status.bootstrap_revision, revision),
            'These accounts will be used after the mail server restarts.',
          )}><Power size={17} aria-hidden="true" />Use these accounts</button>
        </>
      )
    }
    return <div className="setup-copy"><strong>Email accounts configured</strong><span>{status.restart_required ? 'Restart the mail server to apply this change.' : `${status.report.enabled_account_count} enabled account${status.report.enabled_account_count === 1 ? '' : 's'} in use.`}</span></div>
  }

  return (
    <section className="setup-area" aria-label="Account setup status">
      {!hideBanner ? (
        <div className={`setup-banner${status?.restart_required && status.mode === 'managed' ? ' setup-banner-attention' : ''}`} role="status">
          <span className="setup-indicator" aria-hidden="true" />
          {mainContent()}
        </div>
      ) : null}
      <StatusMessage message={notice} />
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />
      {catalogProblems.length ? <div className="warning" role="status"><strong>Setup needs attention</strong><ul>{catalogProblems.map((problem) => <li key={problem}>{problemMessage(problem)}</li>)}</ul></div> : null}
      {status ? (
        <details className="technical-details">
          <summary>Setup details</summary>
          <div className="technical-grid">
            <dl className="compact-dl">
              <div><dt>Next restart will use</dt><dd>{modeLabel(status.mode)}</dd></div>
              <div><dt>Mail server is using</dt><dd>{modeLabel(status.running_mode)}</dd></div>
              <div><dt>Setup status</dt><dd>{status.report ? lifecycleLabel(status.report.lifecycle) : 'Unavailable'}</dd></div>
              <div><dt>Setup version</dt><dd>{status.bootstrap_revision}</dd></div>
            </dl>
            {status.selected_catalog ? <p className="bounded-text"><strong>Settings file</strong><br /><code>{status.selected_catalog}</code></p> : null}
            <div className="button-row">
              <button type="button" className="secondary with-icon" disabled={busy} onClick={() => void load()}><RefreshCw size={16} aria-hidden="true" />Refresh status</button>
              {status.mode === 'managed' ? <button type="button" className="secondary with-icon" disabled={busy} onClick={() => void act(
                () => api.selectMode('legacy', status.bootstrap_revision),
                'Previous settings will be used after restart.',
              )}><RotateCcw size={16} aria-hidden="true" />Use previous settings after restart</button> : null}
            </div>
          </div>
        </details>
      ) : null}
    </section>
  )
}
