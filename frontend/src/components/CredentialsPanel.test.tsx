import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { createMockApi } from '../test/fakes'
import { CredentialsPanel } from './CredentialsPanel'

const account = {
  name: 'work',
  email_address: 'user@example.test',
  enabled: true,
  revision: 4,
  has_outgoing: true,
  incoming_binding: 'ACTIVE' as const,
  outgoing_binding: 'ACTIVE' as const,
}

test('clears a credential after a failed submission and never writes browser storage', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.accounts).mockResolvedValue([account])
  vi.mocked(api.setCredential).mockRejectedValue(new Error('Credential backend unavailable.'))
  const localSpy = vi.spyOn(Storage.prototype, 'setItem')
  render(<CredentialsPanel api={api} />)

  const secret = await screen.findByLabelText('New credential')
  await user.type(secret, 'sentinel-super-secret')
  await user.click(screen.getByRole('button', { name: 'Set credential' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Credential backend unavailable.')
  expect(secret).toHaveValue('')
  expect(api.setCredential).toHaveBeenCalledWith('work', 'incoming', 'sentinel-super-secret', 4)
  expect(localSpy).not.toHaveBeenCalled()
})

test('clears credential input when provider role changes', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.accounts).mockResolvedValue([account])
  render(<CredentialsPanel api={api} />)

  const secret = await screen.findByLabelText('New credential')
  await user.type(secret, 'temporary-value')
  await user.click(screen.getByRole('radio', { name: 'Outgoing (SMTP)' }))

  expect(secret).toHaveValue('')
})
