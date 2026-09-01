import { useCallback, useEffect, useRef, useState } from 'react'
import { Plus, Save, Trash2 } from 'lucide-react'

import type { ManagementApi } from '../api'
import type { CatalogTarget, ManagedPolicy } from '../types'
import { ConflictNotice, errorMessage, StatusMessage } from './Feedback'

function ItemEditor({
  id,
  title,
  itemLabel,
  addLabel,
  help,
  values,
  onChange,
}: {
  id: string
  title: string
  itemLabel: string
  addLabel: string
  help: string
  values: string[]
  onChange: (values: string[]) => void
}) {
  const inputs = useRef<Array<HTMLInputElement | null>>([])
  const pendingFocus = useRef<number | null>(null)

  useEffect(() => {
    if (pendingFocus.current === null) return
    inputs.current[pendingFocus.current]?.focus()
    pendingFocus.current = null
  }, [values])

  const add = () => {
    pendingFocus.current = values.length
    onChange([...values, ''])
  }

  return (
    <fieldset className="item-editor" aria-describedby={`${id}-help`}>
      <legend>{title}</legend>
      <small id={`${id}-help`}>{help}</small>
      {values.length ? (
        <div className="item-list">
          {values.map((value, index) => (
            <div className="item-row" key={`${id}-${index}`}>
              <input
                ref={(element) => { inputs.current[index] = element }}
                id={`${id}-${index}`}
                value={value}
                onChange={(event) => onChange(values.map((item, itemIndex) => itemIndex === index ? event.target.value : item))}
                aria-label={`${itemLabel} ${index + 1}`}
                inputMode="email"
                autoCapitalize="none"
                spellCheck={false}
                required
              />
              <button
                type="button"
                className="secondary with-icon item-remove"
                aria-label={`Remove ${itemLabel.toLowerCase()} item ${index + 1}${value ? `: ${value}` : ''}`}
                onClick={() => onChange(values.filter((_item, itemIndex) => itemIndex !== index))}
              ><Trash2 size={16} aria-hidden="true" />Remove</button>
            </div>
          ))}
        </div>
      ) : null}
      <button type="button" className="secondary with-icon item-add" onClick={add}><Plus size={16} aria-hidden="true" />{addLabel}</button>
    </fieldset>
  )
}

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
        <div className="item-editor-grid">
          <ItemEditor
            id="allowed-recipients"
            title="Allowed recipients"
            itemLabel="Recipient"
            addLabel="Add recipient"
            help="Add addresses this server may send to. Empty means sending is disabled."
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
