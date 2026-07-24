import { render, screen } from '@testing-library/react'

import { ApiError, RevisionConflictError } from '../api'
import { ConflictNotice, errorMessage, problemMessage } from './Feedback'

test('maps production API reasons while preserving bounded local errors', () => {
  expect(errorMessage(new ApiError(400, 'account_name_exists', 'fixed backend message')))
    .toBe('That account nickname is already in use. Choose a different nickname, re-enter the password, and try again.')
  expect(errorMessage(new ApiError(400, 'credential_store_unavailable', 'fixed backend message')))
    .toBe('Local password storage is unavailable. Check access to the account storage, then try again.')
  expect(errorMessage(new ApiError(400, 'import_preview_stale', 'fixed backend message')))
    .toBe('The earlier settings changed or the review expired. Create and review a new import preview.')
  expect(errorMessage(new ApiError(400, 'invalid_input', 'fixed backend message')))
    .toBe('Some entered values are invalid. Review the form and try again.')
  expect(errorMessage(new Error('Managed catalog revision changed during provider binding update.')))
    .toBe('Account settings changed while the password was being updated. Review the latest settings and try again.')
  expect(errorMessage(new Error('Account catalog is unavailable.')))
    .toBe('Account catalog is unavailable.')
})

test('maps known setup problem codes while preserving account names', () => {
  expect(problemMessage('account_incomplete:work:incoming'))
    .toBe('work needs complete incoming mail settings and a saved password.')
  expect(problemMessage('active_secret_unavailable:team:west:outgoing'))
    .toBe("team:west's outgoing mail password is unavailable. Save it again.")
  expect(problemMessage('no_enabled_account'))
    .toBe('Enable at least one account before finishing setup.')
})

test('preserves paths and user-owned values in conflict summaries', () => {
  const error = new RevisionConflictError({
    category: 'conflict',
    message: 'Settings changed.',
    current: {
      selected_catalog: '/private/managed.sqlite3',
      account_name: 'catalog',
      lifecycle: 'ACTIVE',
    },
  })

  render(<ConflictNotice error={error} onDismiss={() => undefined} />)

  expect(screen.getByText('/private/managed.sqlite3')).toBeVisible()
  expect(screen.getByText('catalog')).toBeVisible()
  expect(screen.getByText('Ready to use')).toBeVisible()
})
