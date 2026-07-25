export type ManagementMode = 'legacy' | 'managed'

export interface CatalogTarget {
  expected_bootstrap_revision: number
  expected_catalog: string
}

export type BindingRole = 'incoming' | 'outgoing'
export type BindingState = 'MISSING' | 'ACTIVE' | 'SUPERSEDED' | 'CLEANUP_REQUIRED'

export interface DoctorReport {
  schema_version: number
  catalog_revision: number
  account_count: number
  enabled_account_count: number
  cleanup_required_bindings: number
  problems: string[]
}

export interface LegacySourceSummary {
  account_count: number
  unsupported_provider_count: number
  policy_customized: boolean
}

export interface ManagementStatus {
  mode: ManagementMode
  selected_catalog: string | null
  bootstrap_revision: number
  restart_required: boolean
  report: DoctorReport | null
  catalog_problem: string | null
  bootstrap_exists: boolean
  running_mode: ManagementMode
  running_catalog: string | null
  default_catalog: string | null
  legacy_source: LegacySourceSummary | null
  legacy_source_problem: string | null
}

export interface Endpoint {
  host: string
  port: number
  use_ssl: boolean
  start_ssl: boolean
  verify_ssl: boolean
  user_name: string
}

export interface AccountSummary {
  name: string
  email_address: string
  enabled: boolean
  revision: number
  has_outgoing: boolean
  incoming_binding: BindingState
  outgoing_binding: BindingState | null
}

export interface AccountDetails extends AccountSummary {
  full_name: string
  save_to_sent: boolean
  sent_folder_name: string | null
  incoming: Endpoint
  outgoing: Endpoint | null
}

export interface AccountInput {
  name: string
  full_name: string
  email_address: string
  save_to_sent: boolean
  sent_folder_name: string | null
  incoming: Endpoint
  outgoing: Endpoint | null
}

export interface AccountUpdate extends AccountInput {
  expected_revision: number
}

export interface ManagedPolicy {
  revision: number
  enable_attachment_download: boolean
  allowed_recipients: string[]
  allowed_senders: string[]
  report_blocked_mutations: boolean
}

export interface LegacyAccountSource {
  name: string
  full_name: string
  email_address: string
  incoming: Endpoint
  incoming_secret_source: 'plaintext' | 'keyring' | 'environment'
  outgoing: Endpoint | null
  outgoing_secret_source: 'plaintext' | 'keyring' | 'environment' | null
  save_to_sent: boolean
  sent_folder_name: string | null
}

export interface LegacyPolicySource {
  enable_attachment_download: boolean
  allowed_recipients: string[]
  allowed_senders: string[]
  report_blocked_mutations: boolean
}

export interface LegacyImportAccountPlan {
  name: string
  action: 'create' | 'resume_credentials' | 'unchanged' | 'conflict'
  source: LegacyAccountSource
  expected_target_revision: number | null
  missing_credentials: BindingRole[]
}

export interface LegacyImportPlan {
  preview_token: string
  source_fingerprint: string
  created_at: string
  accounts: LegacyImportAccountPlan[]
  source_policy: LegacyPolicySource
  policy_action: 'update' | 'unchanged'
  unsupported_provider_names: string[]
  target_revision: number
  target_policy_revision: number
}

export interface LegacyImportReport {
  created: string[]
  resumed: string[]
  attention_required: string[]
  mode: ManagementMode
  bootstrap_revision: number
  restart_required: boolean
}

export interface AccountRemovalResult {
  status: 'removed'
  revision: number
  credentials_examined: number
  credentials_cleaned: number
  cleanup_required: number
}

export interface CredentialResult {
  state: 'active' | 'active_cleanup_required'
  revision: number
  cleanup_required: number
}

export interface AccountCreationResult {
  incoming: CredentialResult
  outgoing: CredentialResult | null
}

export interface CredentialRemovalResult {
  state: 'removed' | 'removed_cleanup_required'
  revision: number
  cleanup_required: number
}

export interface CleanupReport {
  examined: number
  cleaned: number
  remaining: number
}

export interface IndexHealth {
  status: 'healthy' | 'degraded' | 'unavailable'
  indexed_accounts: number
  pending_operations: number
  problems: string[]
}

export interface CurrentConflict {
  category: 'conflict'
  message: string
  current: Record<string, unknown>
}
