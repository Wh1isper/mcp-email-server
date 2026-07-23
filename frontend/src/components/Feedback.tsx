import { RevisionConflictError } from '../api'

export const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : 'An unexpected error occurred.'

export function StatusMessage({ message, error = false }: { message: string; error?: boolean }) {
  if (!message) return null
  return (
    <p className={error ? 'message message-error' : 'message'} role={error ? 'alert' : 'status'}>
      {message}
    </p>
  )
}

export function ConflictNotice({ error, onDismiss }: { error: unknown; onDismiss: () => void }) {
  if (!(error instanceof RevisionConflictError)) return null
  return (
    <section className="conflict" role="alert" aria-labelledby="conflict-heading">
      <h3 id="conflict-heading">Review required: configuration changed</h3>
      <p>{error.message} This action was not replayed. Refresh and review the current summary.</p>
      <dl>
        {Object.entries(error.current).map(([key, value]) => (
          <div key={key}>
            <dt>{key.replaceAll('_', ' ')}</dt>
            <dd>{typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ? String(value) : 'Updated on server'}</dd>
          </div>
        ))}
      </dl>
      <button type="button" onClick={onDismiss}>Dismiss</button>
    </section>
  )
}
