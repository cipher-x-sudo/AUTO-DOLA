export type JobStatus = "queued" | "running" | "completed" | "failed" | "cancelled"

export interface JobItem {
  id: string
  prompt: string
  title: string
  status: JobStatus
  action: string
  error?: string | null
  artifact_id?: string | null
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
  tts_default_voice: string
}
