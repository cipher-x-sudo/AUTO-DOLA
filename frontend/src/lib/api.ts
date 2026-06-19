import type { Job, Niche, NichePromptGroup, SettingsPayload } from "./types"

export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000"

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  })
  if (!response.ok) {
    throw new Error(await response.text())
  }
  return response.json()
}

export const api = {
  health: () => request<{ ok: boolean; service: string }>("/api/health"),
  settings: () => request<SettingsPayload>("/api/settings"),
  saveSettings: (payload: SettingsPayload) => request<SettingsPayload>("/api/settings", { method: "PUT", body: JSON.stringify(payload) }),
  ffmpeg: () => request<{ available: boolean; path: string }>("/api/system/ffmpeg"),
  chrome: () => request<{ available: boolean; path: string }>("/api/system/chrome"),
  videoJobs: () => request<Job[]>("/api/video/jobs"),
  createVideoJob: (payload: unknown) => request<Job>("/api/video/jobs", { method: "POST", body: JSON.stringify(payload) }),
  cancelVideoJob: (id: string) => request<Job>(`/api/video/jobs/${id}/cancel`, { method: "POST" }),
  clearVideoHistory: () => request<{ deleted: number }>("/api/video/jobs", { method: "DELETE" }),
  generatePrompts: (payload: unknown) => request<{ prompts: string[]; model: string }>("/api/prompts/generate", { method: "POST", body: JSON.stringify(payload) }),
  createImageJob: (payload: unknown) => request<Job>("/api/image/jobs", { method: "POST", body: JSON.stringify(payload) }),
  createTtsJob: (payload: unknown) => request<Job>("/api/tts/jobs", { method: "POST", body: JSON.stringify(payload) }),
  logs: () => request<Array<{ id: string; level: string; message: string; created_at: string; job_id?: string }>>("/api/video/logs"),
  niches: () => request<Niche[]>("/api/niches"),
  generateNichePrompts: (payload: unknown) =>
    request<{ groups: NichePromptGroup[]; model: string }>("/api/prompts/generate-niches", { method: "POST", body: JSON.stringify(payload) }),
  saveNichePrompts: (payload: unknown) =>
    request<{ saved_path: string }>("/api/prompts/save-niche-prompts", { method: "POST", body: JSON.stringify(payload) }),
}

export function artifactUrl(id: string) {
  return `${API_BASE}/api/artifacts/${id}/download`
}

export function subscribeJobEvents(
  jobId: string,
  onMessage: (event: { type: string; level?: string; message?: string; [key: string]: unknown }) => void,
  onError?: () => void,
): () => void {
  const source = new EventSource(`${API_BASE}/api/video/jobs/${jobId}/events`)
  source.onmessage = (e) => {
    try {
      onMessage(JSON.parse(e.data))
    } catch (error) {
      console.warn("Ignored malformed job event", error)
    }
  }
  source.onerror = () => {
    onError?.()
    source.close()
  }
  return () => source.close()
}
