import { useCallback, useEffect, useState } from 'react'

import type { ManagementApi } from '../api'
import type { ManagedPolicy } from '../types'
import { ConflictNotice, errorMessage, StatusMessage } from './Feedback'

const lines = (value: string): string[] => value.split('\n').map((item) => item.trim()).filter(Boolean)

export function PolicyPanel({ api }: { api: ManagementApi }) {
  const [policy, setPolicy] = useState<ManagedPolicy | null>(null)
  const [recipients, setRecipients] = useState('')
  const [senders, setSenders] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    try {
      const result = await api.policy()
      setPolicy(result)
      setRecipients(result.allowed_recipients.join('\n'))
      setSenders(result.allowed_senders.join('\n'))
      setError(null)
    } catch (caught) { setError(caught) }
  }, [api])
  useEffect(() => { void load() }, [load])

  if (!policy) return <section aria-labelledby="policy-heading"><h2 id="policy-heading">Policy</h2><StatusMessage message={error ? errorMessage(error) : 'Loading policy…'} error={Boolean(error)} /></section>

  return (
    <section aria-labelledby="policy-heading">
      <div className="section-heading"><div><p className="eyebrow">Safety controls</p><h2 id="policy-heading">Catalog policy</h2></div></div>
      <p className="lede">One normalized address or pattern per line. Restrictive changes take effect at the next independent operation.</p>
      <StatusMessage message={notice} />
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />
      <form className="panel" onSubmit={(event) => {
        event.preventDefault(); setBusy(true); setNotice(''); setError(null)
        void (async () => {
          try {
            const result = await api.updatePolicy({ ...policy, allowed_recipients: lines(recipients), allowed_senders: lines(senders) })
            setPolicy(result); setNotice('Policy updated.')
          } catch (caught) { setError(caught) } finally { setBusy(false) }
        })()
      }}>
        <div className="textarea-grid">
          <div><label htmlFor="allowed-recipients">Allowed recipients</label><textarea id="allowed-recipients" rows={8} value={recipients} onChange={(event) => setRecipients(event.target.value)} aria-describedby="recipients-help" /><small id="recipients-help">Leave empty to use the server's deny-by-default behavior.</small></div>
          <div><label htmlFor="allowed-senders">Allowed senders</label><textarea id="allowed-senders" rows={8} value={senders} onChange={(event) => setSenders(event.target.value)} aria-describedby="senders-help" /><small id="senders-help">Patterns are validated and normalized by the management service.</small></div>
        </div>
        <div className="checkbox-column">
          <label><input type="checkbox" checked={policy.enable_attachment_download} onChange={(event) => setPolicy({ ...policy, enable_attachment_download: event.target.checked })} /> Enable attachment materialization</label>
          <label><input type="checkbox" checked={policy.report_blocked_mutations} onChange={(event) => setPolicy({ ...policy, report_blocked_mutations: event.target.checked })} /> Report blocked mail mutations</label>
        </div>
        <button disabled={busy}>{busy ? 'Saving…' : 'Save policy'}</button>
      </form>
    </section>
  )
}
