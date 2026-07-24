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
    allowed_recipients: [],
    allowed_senders: [],
    report_blocked_mutations: false,
  },
  policy_action: 'unchanged',
  unsupported_provider_names: [],
  target_revision: 1,
  target_policy_revision: 1,
})

test('previews on entry and does not require confirmation for a no-op plan', async () => {
  const api = createMockApi()
  vi.mocked(api.previewImport).mockResolvedValue(emptyPlan())

  render(<ImportPanel api={api} />)

  expect(await screen.findByText('Already up to date.', { selector: 'strong' })).toBeInTheDocument()
  expect(api.previewImport).toHaveBeenCalledOnce()
  expect(screen.queryByLabelText('Confirmation')).not.toBeInTheDocument()
  expect(api.applyImport).not.toHaveBeenCalled()
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
  })

  render(<ImportPanel api={api} onChanged={onChanged} />)
  await user.click(await screen.findByRole('checkbox', { name: 'I reviewed these accounts and want to copy them.' }))
  await user.click(screen.getByRole('button', { name: 'Import accounts' }))

  expect(api.applyImport).toHaveBeenCalledWith(plan, 'IMPORT')

  expect(await screen.findByText(/password\(s\) still need attention/i)).toBeInTheDocument()
  expect(onChanged).toHaveBeenCalledOnce()
})
