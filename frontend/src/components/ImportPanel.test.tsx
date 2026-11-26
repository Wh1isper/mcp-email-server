import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { createMockApi } from '../test/fakes'
import type { LegacyImportPlan } from '../types'
import { ImportPanel } from './ImportPanel'

const emptyPlan = (): LegacyImportPlan => ({
  preview_token: 'preview-token',
  source_fingerprint: 'fingerprint',
  created_at: '2026-07-24T00:00:00+00:00',
  accounts: [],
  source_policy: {
    enable_attachment_download: false,
    enable_attachment_content: false,
    enable_pgp_decryption: false,
    allowed_recipients: [],
    allowed_senders: [],
    report_blocked_mutations: false,
  },
  policy_action: 'unchanged',
  unsupported_provider_names: [],
  target_revision: 1,
  target_policy_revision: 1,
})

test('finalizes a no-change plan without requiring confirmation', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  const onChanged = vi.fn()
  const plan = emptyPlan()
  vi.mocked(api.previewImport).mockResolvedValue(plan)
  vi.mocked(api.applyImport).mockResolvedValue({
    created: [],
    resumed: [],
    attention_required: [],
    mode: 'managed',
    bootstrap_revision: 2,
    restart_required: true,
  })

  render(<ImportPanel api={api} onChanged={onChanged} />)

  expect(await screen.findByText('Accounts are already copied.', { selector: 'strong' })).toBeInTheDocument()
  expect(api.previewImport).toHaveBeenCalledOnce()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Finish setup' }))
  expect(api.applyImport).toHaveBeenCalledWith(plan, '')
  expect(onChanged).toHaveBeenCalledOnce()
})

test('reports password attention and notifies the account workspace after import', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  const onChanged = vi.fn()
  const plan = emptyPlan()
  plan.policy_action = 'update'
  vi.mocked(api.previewImport).mockResolvedValue(plan)
  vi.mocked(api.applyImport).mockResolvedValue({
    created: ['work'],
    resumed: [],
    attention_required: ['work:incoming:cleanup_required'],
    mode: 'managed',
    bootstrap_revision: 2,
    restart_required: true,
  })

  render(<ImportPanel api={api} onChanged={onChanged} />)
  await user.click(await screen.findByRole('checkbox', { name: 'I reviewed these accounts and want to copy them.' }))
  await user.click(screen.getByRole('button', { name: 'Import accounts' }))

  expect(api.applyImport).toHaveBeenCalledWith(plan, 'IMPORT')

  expect(await screen.findByText(/password\(s\) still need attention/i)).toBeInTheDocument()
  expect(onChanged).toHaveBeenCalledOnce()
})


test('shows source protocol access before import confirmation', async () => {
  const api = createMockApi()
  const plan = emptyPlan()
  plan.accounts = [{
    name: 'work', action: 'create', expected_target_revision: null, missing_credentials: [],
    source: {
      name: 'work', full_name: 'Work', email_address: 'work@example.test',
      incoming: { host: 'imap.example.test', port: 993, use_ssl: true, start_ssl: false, verify_ssl: true, user_name: 'work' },
      incoming_secret_source: 'plaintext', outgoing: null, outgoing_secret_source: null,
      save_to_sent: false, sent_folder_name: null, allow_protocol_commands: true,
    },
  }]
  vi.mocked(api.previewImport).mockResolvedValue(plan)
  render(<ImportPanel api={api} onChanged={vi.fn()} />)
  expect(await screen.findByText(/Advanced protocol commands are enabled in this source/)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Import accounts' })).toBeDisabled()
})
