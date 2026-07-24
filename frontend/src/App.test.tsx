import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { App } from './App'
import { createMockApi } from './test/fakes'

test('exchanges bootstrap and presents account-first navigation', async () => {
  const api = createMockApi()
  render(<App api={api} bootstrapToken="one-time" />)

  expect(await screen.findByRole('heading', { name: 'Email accounts' })).toBeInTheDocument()
  expect(api.exchangeBootstrap).toHaveBeenCalledWith('one-time')
  expect(api.session).not.toHaveBeenCalled()
  const navigation = screen.getByRole('navigation', { name: 'Settings sections' })
  expect(within(navigation).getAllByRole('button').map((button) => button.textContent)).toEqual([
    'Email accounts', 'Settings & help',
  ])
  expect(screen.queryByRole('button', { name: 'Credentials' })).not.toBeInTheDocument()
  expect(screen.queryByText('Lifecycle controls')).not.toBeInTheDocument()
})

test('reload resumes a cookie session and opens progressively disclosed settings', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  render(<App api={api} bootstrapToken={null} />)

  await screen.findByRole('heading', { name: 'Email accounts' })
  expect(api.session).toHaveBeenCalledOnce()
  await user.click(screen.getByRole('button', { name: 'Settings & help' }))

  expect(await screen.findByRole('heading', { name: 'Settings & help' })).toBeInTheDocument()
  expect(screen.getByText('Import existing settings')).toBeInTheDocument()
  expect(screen.getByText('Sending & attachment safety')).toBeInTheDocument()
  expect(screen.getByText('Troubleshooting')).toBeInTheDocument()
  expect(api.previewImport).not.toHaveBeenCalled()
  expect(api.policy).not.toHaveBeenCalled()
})

test('logout invalidates the UI session and removes settings controls', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  render(<App api={api} bootstrapToken={null} />)

  await user.click(await screen.findByRole('button', { name: 'Sign out' }))

  await waitFor(() => expect(api.logout).toHaveBeenCalledOnce())
  expect(await screen.findByRole('heading', { name: 'Signed out' })).toBeInTheDocument()
  expect(screen.queryByRole('navigation', { name: 'Settings sections' })).not.toBeInTheDocument()
})

test('shows bounded recovery guidance when bootstrap is stale', async () => {
  const api = createMockApi()
  vi.mocked(api.exchangeBootstrap).mockRejectedValue(new Error('The launch link is invalid or expired.'))
  render(<App api={api} bootstrapToken="stale" />)

  expect(await screen.findByRole('heading', { name: 'Open a fresh settings link' })).toBeInTheDocument()
  expect(screen.queryByRole('navigation', { name: 'Settings sections' })).not.toBeInTheDocument()
})
