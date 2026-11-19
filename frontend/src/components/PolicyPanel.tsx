import { useCallback, useEffect, useState } from 'react'
import { Save } from 'lucide-react'

import type { ManagementApi } from '../api'
import type { CatalogTarget, ManagedPolicy } from '../types'
import { grantSummary, MutationGrants } from './MutationGrants'
import { ConflictNotice, errorMessage, StatusMessage } from './Feedback'
import { ItemEditor } from './ItemEditor'

const cleaned = (values: string[]): string[] => values.map((value) => value.trim()).filter(Boolean)

export function PolicyPanel({ api, target, onRevision }: { api: ManagementApi; target: CatalogTarget; onRevision?: (revision: number) => void }) {
  const [policy, setPolicy] = useState<ManagedPolicy | null>(null)
  const [recipients, setRecipients] = useState<string[]>([])
  const [senders, setSenders] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    try {
      const result = await api.policy()
      setPolicy(result)
      setRecipients(result.allowed_recipients)
      setSenders(result.allowed_senders)
      setError(null)
    } catch (caught) { setError(caught) }
  }, [api])
  useEffect(() => { void load() }, [load])

  if (!policy) return <section aria-label="Safety settings"><StatusMessage message={error ? errorMessage(error) : 'Loading safety settings…'} error={Boolean(error)} /></section>

  return (
    <section aria-label="Safety settings">
      <StatusMessage message={notice} />
      {error && !(error instanceof Error && error.name === 'RevisionConflictError') ? <StatusMessage message={errorMessage(error)} error /> : null}
      <ConflictNotice error={error} onDismiss={() => setError(null)} />
      <form onSubmit={(event) => {
        event.preventDefault(); setBusy(true); setNotice(''); setError(null)
        void (async () => {
          try {
            const result = await api.updatePolicy({
              ...policy,
              allowed_recipients: cleaned(recipients),
              allowed_senders: cleaned(senders),
            }, target)
            setPolicy(result)
            setRecipients(result.allowed_recipients)
            setSenders(result.allowed_senders)
            onRevision?.(result.revision)
            setNotice('Safety settings saved.')
          } catch (caught) { setError(caught) } finally { setBusy(false) }
        })()
      }}>
        <MutationGrants id="global-grants" value={policy.allowed_mutations} onChange={(grants) => setPolicy({ ...policy, allowed_mutations: grants })} />
        <p className="hint">Global permissions: {grantSummary(policy.allowed_mutations)}.</p>
        <p className="hint">Global defaults apply to accounts that inherit. Account overrides replace this list; an explicit empty account list remains read-only regardless of global defaults.</p>
        <div className="item-editor-grid">
          <ItemEditor
            id="allowed-recipients"
            title="Allowed recipients"
            itemLabel="Recipient"
            addLabel="Add recipient"
            help="Add addresses or glob patterns such as *@example.com. * allows all recipients for sending, forwarding, and drafts. Empty blocks sending and any supplied draft recipients; recipientless drafts remain allowed."
            values={recipients}
            onChange={setRecipients}
          />
          <ItemEditor
            id="allowed-senders"
            title="Allowed senders"
            itemLabel="Sender pattern"
            addLabel="Add sender pattern"
            help="Add addresses or patterns to restrict readable senders. Empty means all senders may be read."
            values={senders}
            onChange={setSenders}
          />
        </div>
        <div className="checkbox-column">
          <label><input type="checkbox" checked={policy.enable_attachment_download} onChange={(event) => setPolicy({ ...policy, enable_attachment_download: event.target.checked })} /> Allow attachments to be saved as files</label>
          <label><input type="checkbox" checked={policy.enable_attachment_content} onChange={(event) => setPolicy({ ...policy, enable_attachment_content: event.target.checked })} /> Allow attachments to be returned through MCP</label>
          <label><input type="checkbox" checked={policy.report_blocked_mutations} onChange={(event) => setPolicy({ ...policy, report_blocked_mutations: event.target.checked })} /> Show when an email action is blocked by these settings</label>
        </div>
        <button className="with-icon" disabled={busy}><Save size={17} aria-hidden="true" />{busy ? 'Saving…' : 'Save safety settings'}</button>
      </form>
    </section>
  )
}
