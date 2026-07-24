import { useState } from 'react'
import { DatabaseZap, Stethoscope } from 'lucide-react'

import type { ManagementApi } from '../api'
import type { DoctorReport, IndexHealth } from '../types'
import { errorMessage, problemMessage, StatusMessage } from './Feedback'

const searchProblemMessage = (problem: string): string => {
  if (problem === 'partial_coverage') return 'Some accounts are not fully available in search yet.'
  if (problem === 'index_unavailable') return 'Email search status is unavailable.'
  return problemMessage(problem)
}

const searchStatusLabel = (status: IndexHealth['status']): string => {
  if (status === 'healthy') return 'Working'
  if (status === 'degraded') return 'Needs attention'
  return 'Unavailable'
}

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
      <div className="section-heading"><div><p className="eyebrow">Diagnostics</p><h2 id="health-heading">Troubleshooting checks</h2></div></div>
      <p className="lede">Run these only when setup or email search is not working. Results never contain passwords or mail content.</p>
      {error ? <StatusMessage message={errorMessage(error)} error /> : null}
      <div className="split">
        <article className="panel action-stack">
          <h3>Account setup</h3>
          <button type="button" className="with-icon" disabled={Boolean(busy)} onClick={() => void runDoctor()}><Stethoscope size={17} aria-hidden="true" />{busy === 'doctor' ? 'Running…' : 'Check account setup'}</button>
          {doctor ? <div aria-live="polite"><dl className="compact-dl"><div><dt>Setup status</dt><dd>{doctor.lifecycle === 'ACTIVE' ? 'Ready to use' : 'In progress'}</dd></div><div><dt>Enabled accounts</dt><dd>{doctor.enabled_account_count} of {doctor.account_count}</dd></div><div><dt>Old passwords to clean up</dt><dd>{doctor.cleanup_required_bindings}</dd></div></dl>{doctor.problems.length ? <ul className="problem-list">{doctor.problems.map((problem) => <li key={problem}>{problemMessage(problem)}</li>)}</ul> : <StatusMessage message="No account setup problems found." />}</div> : null}
        </article>
        <article className="panel action-stack">
          <h3>Email search</h3>
          <button type="button" className="with-icon" disabled={Boolean(busy)} onClick={() => void runIndex()}><DatabaseZap size={17} aria-hidden="true" />{busy === 'index' ? 'Checking…' : 'Check email search index'}</button>
          {index ? <div aria-live="polite"><p className={`state state-${index.status}`}>{searchStatusLabel(index.status)}</p><dl className="compact-dl"><div><dt>Accounts ready for search</dt><dd>{index.indexed_accounts}</dd></div><div><dt>Search updates pending</dt><dd>{index.pending_operations}</dd></div></dl>{index.problems.length ? <ul className="problem-list">{index.problems.map((problem) => <li key={problem}>{searchProblemMessage(problem)}</li>)}</ul> : <StatusMessage message="Email search is working." />}</div> : null}
        </article>
      </div>
    </section>
  )
}
