import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { ALL_MUTATIONS, grantSummary, MutationGrants } from './MutationGrants'

function Editor() {
  const [grants, setGrants] = useState([...ALL_MUTATIONS])
  return <><MutationGrants id="test" value={grants} onChange={setGrants} /><output>{grantSummary(grants)}</output></>
}

test('presets edit only the fixed grants and checkboxes derive the preset', async () => {
  const user = userEvent.setup()
  render(<Editor />)
  const preset = screen.getByLabelText('Permission preset')
  expect(preset).toHaveValue('read-write')
  expect(screen.getAllByRole('checkbox')).toHaveLength(5)
  for (const checkbox of screen.getAllByRole('checkbox')) expect(checkbox).toBeChecked()
  await user.selectOptions(preset, 'draft-assistant')
  expect(screen.getByRole('status')).toHaveTextContent('Read + draft')
  await user.click(screen.getByRole('checkbox', { name: /Organize/ }))
  expect(preset).toHaveValue('organizer')
  await user.click(screen.getByRole('checkbox', { name: /Append/ }))
  expect(preset).toHaveValue('custom')
  await user.selectOptions(preset, 'readonly')
  expect(screen.getByRole('status')).toHaveTextContent('Read only (no mutations)')
  for (const checkbox of screen.getAllByRole('checkbox')) expect(checkbox).not.toBeChecked()
  await user.selectOptions(preset, 'read-write')
  for (const checkbox of screen.getAllByRole('checkbox')) expect(checkbox).toBeChecked()
})

test('inherited grant controls cannot be edited', () => {
  render(<MutationGrants id="inherited" value={ALL_MUTATIONS} disabled onChange={vi.fn()} />)
  expect(screen.getByLabelText('Permission preset')).toBeDisabled()
  for (const checkbox of screen.getAllByRole('checkbox')) expect(checkbox).toBeDisabled()
})
