import { useState, type ReactNode } from 'react'
import { CircleHelp, Import, ShieldCheck } from 'lucide-react'

import type { ManagementApi } from '../api'
import type { CatalogTarget } from '../types'
import { HealthPanel } from './HealthPanel'
import { ImportPanel } from './ImportPanel'
import { PolicyPanel } from './PolicyPanel'

function SettingsDisclosure({
  icon,
  title,
  description,
  defaultOpen = false,
  children,
}: {
  icon: ReactNode
  title: string
  description: string
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <details className="settings-disclosure" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary><span className="settings-icon" aria-hidden="true">{icon}</span><span><strong>{title}</strong><small>{description}</small></span></summary>
      <div className="settings-body">{open ? children : null}</div>
    </details>
  )
}

export function SettingsPanel({
  api,
  target,
  hasLegacySource,
  onChanged,
  onPolicyRevision,
}: {
  api: ManagementApi
  target: CatalogTarget
  hasLegacySource: boolean
  onChanged?: () => void
  onPolicyRevision?: (revision: number) => void
}) {
  return (
    <section aria-labelledby="settings-heading">
      <div className="page-heading"><p className="eyebrow">Optional controls</p><h1 id="settings-heading">Settings &amp; help</h1><p className="lede">Most people do not need anything here. Open a section only when you want to copy earlier settings, restrict sending, or troubleshoot.</p></div>
      <div className="settings-list">
        <SettingsDisclosure
          icon={<Import size={19} />}
          title="Import existing settings"
          description="Review and copy accounts from an earlier file or environment setup."
          defaultOpen={hasLegacySource}
        >
          <ImportPanel api={api} onChanged={onChanged} />
        </SettingsDisclosure>
        <SettingsDisclosure
          icon={<ShieldCheck size={19} />}
          title="Sending & attachment safety"
          description="Control who can send and receive, or allow attachments to be saved as files."
        >
          <PolicyPanel api={api} target={target} onRevision={onPolicyRevision} />
        </SettingsDisclosure>
        <SettingsDisclosure
          icon={<CircleHelp size={19} />}
          title="Troubleshooting"
          description="Check account setup and email search for problems."
        >
          <HealthPanel api={api} />
        </SettingsDisclosure>
      </div>
    </section>
  )
}
