import { useEffect, useRef } from 'react'
import { Plus, Trash2 } from 'lucide-react'

export function ItemEditor({
  id,
  title,
  itemLabel,
  addLabel,
  help,
  values,
  onChange,
}: {
  id: string
  title: string
  itemLabel: string
  addLabel: string
  help: string
  values: string[]
  onChange: (values: string[]) => void
}) {
  const inputs = useRef<Array<HTMLInputElement | null>>([])
  const pendingFocus = useRef<number | null>(null)

  useEffect(() => {
    if (pendingFocus.current === null) return
    inputs.current[pendingFocus.current]?.focus()
    pendingFocus.current = null
  }, [values])

  const add = () => {
    pendingFocus.current = values.length
    onChange([...values, ''])
  }

  return (
    <fieldset className="item-editor" aria-describedby={`${id}-help`}>
      <legend>{title}</legend>
      <small id={`${id}-help`}>{help}</small>
      {values.length ? (
        <div className="item-list">
          {values.map((value, index) => (
            <div className="item-row" key={`${id}-${index}`}>
              <input
                ref={(element) => { inputs.current[index] = element }}
                id={`${id}-${index}`}
                value={value}
                onChange={(event) => onChange(values.map((item, itemIndex) => itemIndex === index ? event.target.value : item))}
                aria-label={`${itemLabel} ${index + 1}`}
                inputMode="email"
                autoCapitalize="none"
                spellCheck={false}
                required
              />
              <button
                type="button"
                className="secondary with-icon item-remove"
                aria-label={`Remove ${itemLabel.toLowerCase()} item ${index + 1}${value ? `: ${value}` : ''}`}
                onClick={() => onChange(values.filter((_item, itemIndex) => itemIndex !== index))}
              ><Trash2 size={16} aria-hidden="true" />Remove</button>
            </div>
          ))}
        </div>
      ) : null}
      <button type="button" className="secondary with-icon item-add" onClick={add}><Plus size={16} aria-hidden="true" />{addLabel}</button>
    </fieldset>
  )
}
