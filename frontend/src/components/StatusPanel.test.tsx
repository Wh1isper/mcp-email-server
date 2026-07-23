import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { RevisionConflictError } from '../api'
import { createMockApi } from '../test/fakes'
import { StatusPanel } from './StatusPanel'

test('shows current non-secret conflict summary and does not auto-replay', async () => {
  const user = userEvent.setup()
  const api = createMockApi()
  vi.mocked(api.status).mockResolvedValue({
    mode: 'legacy',
    selected_catalog: 'catalog.db',
    bootstrap_revision: 3,
    restart_required: false,
    catalog_problem: null,
    report: {
      lifecycle: 'ACTIVE', schema_version: 1, catalog_revision: 5,
      account_count: 1, enabled_account_count: 1, pending_bindings: 0,
      cleanup_required_bindings: 0, repair_required_bindings: 0, problems: [],
    },
  })
  vi.mocked(api.selectMode).mockRejectedValue(new RevisionConflictError({
    category: 'conflict', message: 'Catalog revision changed.', current: { catalog_revision: 6, lifecycle: 'ACTIVE' },
  }))
  render(<StatusPanel api={api} />)

  await user.click(await screen.findByRole('button', { name: 'Select managed mode' }))

  expect(await screen.findByRole('heading', { name: 'Review required: configuration changed' })).toBeInTheDocument()
  expect(screen.getByText('6')).toBeInTheDocument()
  expect(api.selectMode).toHaveBeenCalledTimes(1)
})
