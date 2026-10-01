import type { MutationClass } from '../types'

export const ALL_MUTATIONS: MutationClass[] = ['draft', 'organize', 'delete', 'send', 'append']
const labels: Record<MutationClass, string> = {
  draft: 'Draft — save drafts',
  organize: 'Organize — flags, tags, move and archive',
  delete: 'Delete — trash or delete messages',
  send: 'Send — send, reply and forward',
  append: 'Append — import messages',
}
const presets: Record<string, MutationClass[]> = {
  'read-write': ALL_MUTATIONS,
  readonly: [],
  'draft-assistant': ['draft'],
  organizer: ['draft', 'organize'],
}
const presetFor = (value: MutationClass[]) => Object.entries(presets).find(([, grants]) =>
  grants.length === value.length && grants.every((grant) => value.includes(grant)),
)?.[0] ?? 'custom'

export function grantSummary(grants: MutationClass[]): string {
  return grants.length ? `Read + ${ALL_MUTATIONS.filter((grant) => grants.includes(grant)).join(', ')}` : 'Read only (no mutations)'
}

export function MutationGrants({ id, value, onChange, disabled = false }: {
  id: string
  value: MutationClass[]
  onChange: (value: MutationClass[]) => void
  disabled?: boolean
}) {
  return (
    <fieldset className="item-editor" disabled={disabled}>
      <legend>Mutation grants</legend>
      <label htmlFor={`${id}-preset`}>Permission preset</label>
      <select id={`${id}-preset`} value={presetFor(value)} onChange={(event) => {
        const preset = presets[event.target.value]
        if (preset) onChange([...preset])
      }}>
        <option value="read-write">Read/write</option>
        <option value="readonly">Read only</option>
        <option value="draft-assistant">Draft assistant</option>
        <option value="organizer">Organizer</option>
        <option value="custom">Custom</option>
      </select>
      <p className="hint">Presets only edit these five grants. Reading is always available; other safety and provider checks still apply.</p>
      <div className="checkbox-column">
        {ALL_MUTATIONS.map((grant) => <label key={grant}><input type="checkbox" checked={value.includes(grant)} onChange={(event) => onChange(ALL_MUTATIONS.filter((item) => item === grant ? event.target.checked : value.includes(item)))} />{labels[grant]}</label>)}
      </div>
    </fieldset>
  )
}
