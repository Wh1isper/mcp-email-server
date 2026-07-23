import { useState } from 'react'

import type { ManagementApi } from '../api'
import type { DoctorReport, IndexHealth } from '../types'
import { errorMessage, StatusMessage } from './Feedback'

export function HealthPanel({ api }: { api: ManagementApi }) {
  const [doctor, setDoctor] = useState<DoctorReport | null>(null)
  const [index, setIndex] = useState<IndexHealth | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState<unknown>(null)

  const runDoctor = async () => {
    setBusy('doctor'); setError(null)
    try { setDoctor(await api.doctor()) } catch (caught) { setError(caught) } finally { setBusy('') }
  }
  const runIndex = async () => {
    setBusy('index'); setError(null)
    try { setIndex(await api.indexHealth()) } catch (caught) { setError(caught) } finally { setBusy('') }
  }

  return (
    <section aria-labelledby="health-heading">
      <div className="section-heading"><div><p className="eyebrow">Diagnostics</p><h2 id="health-heading">Doctor and index health</h2></div></div>
      <p className="lede">Checks are bounded and opt-in. Results contain categories and remediation, never credential values or provider responses.</p>
      {error ? <StatusMessage message={errorMessage(error)} error /> : null}
      <div className="split">
        <article className="panel action-stack">
          <h3>Configuration doctor</h3>
          <button type="button" disabled={Boolean(busy)} onClick={() => void runDoctor()}>{busy === 'doctor' ? 'Running…' : 'Run doctor'}</button>
          {doctor ? <div aria-live="polite"><dl className="compact-dl"><div><dt>Lifecycle</dt><dd>{doctor.lifecycle}</dd></div><div><dt>Schema</dt><dd>{doctor.schema_version}</dd></div><div><dt>Pending bindings</dt><dd>{doctor.pending_bindings}</dd></div><div><dt>Cleanup required</dt><dd>{doctor.cleanup_required_bindings}</dd></div></dl>{doctor.problems.length ? <ul className="problem-list">{doctor.problems.map((problem) => <li key={problem}>{problem}</li>)}</ul> : <StatusMessage message="No catalog problems reported." />}</div> : null}
        </article>
        <article className="panel action-stack">
          <h3>Metadata index</h3>
          <button type="button" disabled={Boolean(busy)} onClick={() => void runIndex()}>{busy === 'index' ? 'Checking…' : 'Check index health'}</button>
          {index ? <div aria-live="polite"><p className={`state state-${index.status}`}>{index.status}</p><dl className="compact-dl"><div><dt>Indexed accounts</dt><dd>{index.indexed_accounts}</dd></div><div><dt>Pending operations</dt><dd>{index.pending_operations}</dd></div></dl>{index.problems.length ? <ul className="problem-list">{index.problems.map((problem) => <li key={problem}>{problem}</li>)}</ul> : <StatusMessage message="The index reports healthy." />}</div> : null}
        </article>
      </div>
    </section>
  )
}
