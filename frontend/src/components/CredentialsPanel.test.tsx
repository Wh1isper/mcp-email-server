import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { createMockApi } from '../test/fakes'
import { CredentialsPanel } from './CredentialsPanel'

const target = { expected_bootstrap_revision: 1, expected_catalog: '/private/managed.sqlite3' }

const account = {
  name: 'work',
  email_address: 'user@example.test',
  enabled: true,
  revision: 4,
  has_outgoing: true,
  incoming_binding: 'ACTIVE' as const,
  outgoing_binding: 'ACTIVE' as const,
}

test('clears a password after a failed submission and never writes browser storage', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.setCredential).mockRejectedValue(new Error('Credential backend unavailable.'))
  const localSpy = vi.spyOn(Storage.prototype, 'setItem')
  render(<CredentialsPanel api={api} target={target} account={account} onChanged={vi.fn()} />)

  const secret = screen.getByLabelText('New password or app password')
  await user.type(secret, 'sentinel-super-secret')
  await user.click(screen.getByRole('button', { name: 'Save password' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Password storage unavailable.')
  expect(secret).toHaveValue('')
  expect(api.setCredential).toHaveBeenCalledWith('work', 'incoming', 'sentinel-super-secret', 4, target)
  expect(localSpy).not.toHaveBeenCalled()
})

test('clears a saved password before the non-secret refresh finishes', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  let finishRefresh: (() => void) | undefined
  const refresh = new Promise<void>((resolve) => { finishRefresh = resolve })
  const onChanged = vi.fn(() => refresh)
  render(<CredentialsPanel api={api} target={target} account={account} onChanged={onChanged} />)

  const secret = screen.getByLabelText('New password or app password')
  await user.type(secret, 'clear-before-refresh')
  await user.click(screen.getByRole('button', { name: 'Save password' }))

  await waitFor(() => expect(api.setCredential).toHaveBeenCalledOnce())
  expect(secret).toHaveValue('')
  expect(onChanged).toHaveBeenCalledOnce()
  await act(async () => {
    finishRefresh?.()
    await refresh
  })
})

test('reports cleanup required after a successful password save', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.setCredential).mockResolvedValue({
    state: 'active_cleanup_required',
    revision: 4,
    cleanup_required: 1,
  })
  render(<CredentialsPanel api={api} target={target} account={account} onChanged={vi.fn()} />)

  await user.type(screen.getByLabelText('New password or app password'), 'temporary-value')
  await user.click(screen.getByRole('button', { name: 'Save password' }))

  expect(await screen.findByText(/old password copy still needs cleanup/i)).toBeVisible()
  expect(screen.queryByRole('button', { name: /resume|roll back/i })).not.toBeInTheDocument()
})

test('clears password input when mail service changes', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  render(<CredentialsPanel api={api} target={target} account={account} onChanged={vi.fn()} />)

  const secret = screen.getByLabelText('New password or app password')
  await user.type(secret, 'temporary-value')
  await user.click(screen.getByRole('radio', { name: 'Outgoing mail (SMTP)' }))

  expect(secret).toHaveValue('')
})

test('explains that an account must be paused before password removal', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  render(<CredentialsPanel api={api} target={target} account={account} onChanged={vi.fn()} />)

  await user.click(screen.getByText('Remove password'))

  expect(screen.getByText('Pause this account before removing its saved password.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Remove saved password' })).toBeDisabled()
})

test('keeps provider connectivity diagnostics out of the password workflow', () => {
  const api = createMockApi()
  render(<CredentialsPanel api={api} target={target} account={account} onChanged={vi.fn()} />)

  expect(screen.getByRole('button', { name: 'Save password' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /test connection/i })).not.toBeInTheDocument()
})
