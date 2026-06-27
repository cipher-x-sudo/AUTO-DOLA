export type JobStatus = "queued" | "running" | "completed" | "failed" | "cancelled"

export interface JobItem {
  id: string
  prompt: string
  title: string
  status: JobStatus
  action: string
  error?: string | null
  diagnostic_json?: Record<string, unknown>
  artifact_id?: string | null
  created_at: string
  updated_at: string
}

export interface Artifact {
  id: string
  kind: string
  filename: string
  mime_type: string
  size_bytes: number
  created_at: string
}

export interface Job {
  id: string
  kind: "video" | "image" | "tts"
  status: JobStatus
  title: string
  created_at: string
  updated_at: string
  total: number
  done: number
  failed: number
  config_json: Record<string, unknown>
  error?: string | null
  dola_cookie_snapshots_json?: Array<Record<string, unknown>>
  items: JobItem[]
  artifacts: Artifact[]
}

export interface SettingsPayload {
  dola_auth_cookies: string
  yousmind_api_key: string
  gemini_api_key: string
  gemini_base_url: string
  gemini_model: string
  default_ratio: string
  default_duration: number
  default_parallel: number
  output_dir: string
  proxy_enabled: boolean
  proxy_url: string
  vpn_enabled: boolean
  vpn_usernames: string
  vpn_password: string
  vpn_password_saved: boolean
  tts_default_voice: string
  dola_mode: "direct" | "browser" | "hybrid"
}

export interface DolaBrowserStatus {
  ok: boolean
  cdp: boolean
  page_url: string
  profile_persistent: boolean
  manual_url: string
  mode: "direct" | "browser" | "hybrid"
  browser_proxy_active?: boolean
  browser_proxy_host?: string
  browser_vpn_active?: boolean
  browser_vpn_enabled?: boolean
  browser_vpn_config?: string
  browser_vpn_ip?: string
  browser_ip?: string
  page_count?: number
  active_browser_count?: number
  max_browser_slots?: number
  active_cdp_ports?: number[]
  last_submit_endpoint?: string
  last_dola_error?: string
  error?: string
}

export interface Niche {
  id: string
  name: string
  filename: string
  size_bytes: number
}

export interface NichePromptGroup {
  niche_id: string
  niche_name: string
  filename: string
  requested_count: number
  prompts: string[]
  saved_path: string
}
