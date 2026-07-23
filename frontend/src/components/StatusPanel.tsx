import { useCallback, useEffect, useState } from 'react'

import type { ManagementApi } from '../api'
import type { ManagementStatus } from '../types'
import { ConflictNotice, errorMessage, StatusMessage } from './Feedback'

export function StatusPanel({ api }: { api: ManagementApi }) {
  const [status, setStatus] = useState<ManagementStatus | null>(null)
  const [database, setDatabase] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    try {
      setStatus(await api.status())
      setError(null)
    } catch (caught) {
      setError(caught)
    }
  }, [api])

  useEffect(() => {
    void load()
  }, [load])

  const act = async (operation: () => Promise<void>, message: string) => {
    setBusy(true)
    setNotice('')
    setError(null)
    try {
      await operation()
      setNotice(message)
      await load()
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  const revision = status?.report?.catalog_revision ?? 0

  return (
    <section aria-labelledby="status-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Setup &amp; status</p>
          <h2 id="status-heading">Runtime authority</h2>
        </div>
        <button type="button" className="secondary" disabled={busy} onClick={() => void load()}>
          Refresh
        </button>
      </div>
      <StatusMessage message={notice} />
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? (
        <StatusMessage message={errorMessage(error)} error />
      ) : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />

      {status ? (
        <div className="card-grid" aria-live="polite">
          <article className="metric-card">
            <span>Current mode</span><strong>{status.mode}</strong>
          </article>
          <article className="metric-card">
            <span>Catalog</span><strong>{status.report?.lifecycle ?? 'Not initialized'}</strong>
          </article>
          <article className="metric-card">
            <span>Enabled accounts</span>
            <strong>{status.report ? `${status.report.enabled_account_count} / ${status.report.account_count}` : '—'}</strong>
          </article>
          <article className="metric-card">
            <span>Restart</span><strong>{status.restart_required ? 'Required' : 'Not required'}</strong>
          </article>
        </div>
      ) : <p role="status">Loading status…</p>}

      {status?.selected_catalog ? <p className="bounded-text">Selected catalog: <code>{status.selected_catalog}</code></p> : null}
      {status?.catalog_problem ? (
        <div className="warning" role="status">
          <strong>Selected catalog is unavailable</strong>
          <p>{status.catalog_problem}. Select legacy mode to recover without opening the catalog.</p>
        </div>
      ) : status?.report?.problems.length ? (
        <div className="warning" role="status">
          <strong>Catalog needs attention</strong>
          <ul>{status.report.problems.map((problem) => <li key={problem}>{problem}</li>)}</ul>
        </div>
      ) : null}

      <div className="split">
        <form
          className="panel"
          onSubmit={(event) => {
            event.preventDefault()
            void act(() => api.initializeCatalog(database), 'Staging catalog initialized. Review it before activation.')
          }}
        >
          <h3>Initialize staging catalog</h3>
          <p className="hint">This explicit local path is used only to initialize the managed catalog.</p>
          <label htmlFor="database">Database path</label>
          <input id="database" value={database} onChange={(event) => setDatabase(event.target.value)} required maxLength={1024} />
          <button disabled={busy || !database.trim()}>Initialize</button>
        </form>

        <div className="panel action-stack">
          <h3>Lifecycle</h3>
          <button
            type="button"
            disabled={busy || status?.report?.lifecycle !== 'STAGING'}
            onClick={() => void act(() => api.activateCatalog(revision), 'Catalog activated. Selection is still explicit.')}
          >
            Validate and activate
          </button>
          <button
            type="button"
            disabled={busy || status?.report?.lifecycle !== 'ACTIVE' || status.mode === 'managed'}
            onClick={() => void act(
              () => api.selectMode('managed', status?.bootstrap_revision ?? 0, revision),
              'Managed mode selected. Restart is required.',
            )}
          >
            Select managed mode
          </button>
          <button
            type="button"
            className="secondary"
            disabled={busy || status?.mode === 'legacy'}
            onClick={() => void act(
              () => api.selectMode('legacy', status?.bootstrap_revision ?? 0),
              'Legacy mode selected. Restart is required.',
            )}
          >
            Select legacy mode
          </button>
          <p className="hint">Activation and selection never import or delete legacy configuration.</p>
        </div>
      </div>
    </section>
  )
}
