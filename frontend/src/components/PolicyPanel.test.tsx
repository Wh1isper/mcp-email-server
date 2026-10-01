import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { createMockApi } from '../test/fakes'
import { PolicyPanel } from './PolicyPanel'

const target = { expected_bootstrap_revision: 1, expected_catalog: '/private/managed.sqlite3' }

test('edits recipient and sender allowlists as individual items', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.policy).mockResolvedValue({
    revision: 4,
    enable_attachment_download: false,
    enable_attachment_content: false,
    allowed_recipients: ['first@example.test'],
    allowed_senders: ['*@example.test'],
    report_blocked_mutations: false,
    allowed_mutations: ['draft', 'organize', 'delete', 'send', 'append'],
  })
  vi.mocked(api.updatePolicy).mockImplementation((policy) => Promise.resolve({ ...policy, revision: 5 }))
  render(<PolicyPanel api={api} target={target} />)

  expect(await screen.findByLabelText('Recipient 1')).toHaveValue('first@example.test')
  expect(screen.getByLabelText('Sender pattern 1')).toHaveValue('*@example.test')
  await user.click(screen.getByRole('checkbox', { name: 'Allow attachments to be returned through MCP' }))

  await user.click(screen.getByRole('button', { name: 'Add recipient' }))
  const secondRecipient = screen.getByLabelText('Recipient 2')
  expect(secondRecipient).toHaveFocus()
  await user.type(secondRecipient, ' *@example.test ')
  await user.click(screen.getByRole('button', { name: 'Remove recipient item 1: first@example.test' }))

  await user.click(screen.getByRole('button', { name: 'Add sender pattern' }))
  const secondSender = screen.getByLabelText('Sender pattern 2')
  expect(secondSender).toHaveFocus()
  await user.type(secondSender, 'alerts@example.test')
  await user.click(screen.getByRole('button', { name: 'Save safety settings' }))

  await waitFor(() => expect(api.updatePolicy).toHaveBeenCalledWith({
    revision: 4,
    enable_attachment_download: false,
    enable_attachment_content: true,
    allowed_recipients: ['*@example.test'],
    allowed_senders: ['*@example.test', 'alerts@example.test'],
    report_blocked_mutations: false,
    allowed_mutations: ['draft', 'organize', 'delete', 'send', 'append'],
  }, target))
  expect(await screen.findByText('Safety settings saved.')).toBeVisible()
})

test('explains the distinct empty-list behavior', async () => {
  const api = createMockApi()
  render(<PolicyPanel api={api} target={target} />)

  expect(await screen.findByText(/Empty blocks sending and any supplied draft recipients/)).toBeVisible()
  expect(screen.getByText(/\* allows all recipients for sending, forwarding, and drafts/)).toBeVisible()
  expect(screen.getByText(/Empty means all senders may be read/)).toBeVisible()
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
})

test('global grant edits persist the list, not a separate preset policy', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.updatePolicy).mockImplementation((policy) => Promise.resolve({ ...policy, revision: 2 }))
  render(<PolicyPanel api={api} target={target} />)
  const preset = await screen.findByLabelText('Permission preset')
  expect(preset).toHaveValue('read-write')
  await user.selectOptions(preset, 'organizer')
  await user.click(screen.getByRole('button', { name: 'Save safety settings' }))
  await waitFor(() => expect(api.updatePolicy).toHaveBeenCalledWith(expect.objectContaining({ allowed_mutations: ['draft', 'organize'] }), target))
  expect(vi.mocked(api.updatePolicy).mock.calls[0]?.[0]).not.toHaveProperty('preset')
})
