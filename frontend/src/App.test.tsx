import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { App } from './App'
import { createMockApi } from './test/fakes'

test('exchanges bootstrap then exposes only management sections', async () => {
  const api = createMockApi()
  render(<App api={api} bootstrapToken="one-time" />)

  expect(await screen.findByRole('heading', { name: 'Runtime authority' })).toBeInTheDocument()
  expect(api.exchangeBootstrap).toHaveBeenCalledWith('one-time')
  expect(api.session).not.toHaveBeenCalled()
  expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
    'Setup & status', 'Accounts', 'Credentials', 'Policy', 'Migration', 'Health',
  ])
  expect(screen.queryByRole('tab', { name: /mail/i })).not.toBeInTheDocument()
})

test('reload resumes a cookie session and keyboard navigation changes sections', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  render(<App api={api} bootstrapToken={null} />)

  const statusTab = await screen.findByRole('tab', { name: 'Setup & status' })
  expect(api.session).toHaveBeenCalledOnce()
  statusTab.focus()
  await user.keyboard('{ArrowRight}')

  expect(screen.getByRole('tab', { name: 'Accounts' })).toHaveAttribute('aria-selected', 'true')
  expect(await screen.findByRole('heading', { name: 'Managed accounts' })).toBeInTheDocument()
})

test('logout invalidates the UI session and removes management controls', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  render(<App api={api} bootstrapToken={null} />)

  await user.click(await screen.findByRole('button', { name: 'Sign out' }))

  await waitFor(() => expect(api.logout).toHaveBeenCalledOnce())
  expect(await screen.findByRole('heading', { name: 'Signed out' })).toBeInTheDocument()
  expect(screen.queryByRole('tab')).not.toBeInTheDocument()
})

test('shows bounded recovery guidance when bootstrap is stale', async () => {
  const api = createMockApi()
  vi.mocked(api.exchangeBootstrap).mockRejectedValue(new Error('The launch link is invalid or expired.'))
  render(<App api={api} bootstrapToken="stale" />)

  expect(await screen.findByRole('heading', { name: 'Open a fresh management link' })).toBeInTheDocument()
  expect(screen.queryByRole('tab')).not.toBeInTheDocument()
})
