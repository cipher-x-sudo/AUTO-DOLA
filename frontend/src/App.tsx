import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import {
  Check,
  Copy,
  Download,
  FileText,
  FileVideo,
  Folder,
  Loader2,
  Play,
  Search,
  Settings2,
  Square,
  Terminal,
  Trash2,
  Upload,
  Wand2,
  Zap,
} from "lucide-react"
import { toast } from "sonner"
import { API_BASE, api, artifactUrl, browserScreenshotUrl, subscribeJobEvents } from "@/lib/api"
import type { Artifact, DolaBrowserStatus, Job, JobItem, Niche, NichePromptGroup, SettingsPayload } from "@/lib/types"
import { Layout } from "@/components/Layout"
import { JobTable } from "@/components/JobTable"
import { Badge, Button, Card, Input, Progress, Select, Textarea } from "@/components/ui"

const DOCKER_OUTPUT_DIR = "/data/downloads"
const HOST_OUTPUT_LABEL = import.meta.env.VITE_OUTPUT_LABEL ?? "Downloads/AUTO-DOLA"
const PROMPT_UI_BATCH_SIZE = 5
const QUEUE_PAGE_SIZE = 10
const GEMINI_MODELS = [
  { value: "gemini-2.5-flash-lite", label: "Gemini 3.1 Flash Lite" },
  { value: "gemini-2.5-flash-thinking", label: "Gemini 3.1 Flash Lite (Thinking)" },
  { value: "gemini-3.5-flash-extra-low", label: "Gemini 3.5 Flash (Low)" },
  { value: "gemini-3.5-flash-low", label: "Gemini 3.5 Flash (Medium)" },
  { value: "gemini-2.5-pro", label: "Gemini 2.5 Pro" },
  { value: "gemini-3.1-flash-lite", label: "Gemini 3.1 Flash Lite (3.1)" },
  { value: "gemini-3.1-pro-low", label: "Gemini 3.1 Pro (Low)" },
  { value: "gemini-2.5-flash", label: "Gemini 3.1 Flash Lite (Recommended)" },
  { value: "gemini-3-flash", label: "Gemini 3 Flash" },
  { value: "gemini-3-flash-agent", label: "Gemini 3.5 Flash (High)" },
  { value: "gemini-3.1-pro-high", label: "Gemini 3.1 Pro (High)" },
] as const

const emptySettings: SettingsPayload = {
  dola_auth_cookies: "",
  yousmind_api_key: "",
  gemini_api_key: "",
  gemini_base_url: "https://generativelanguage.googleapis.com/v1beta",
  gemini_model: "gemini-2.5-flash",
  default_ratio: "9:16",
  default_duration: 10,
  default_parallel: 5,
  output_dir: "",
  proxy_enabled: false,
  proxy_url: "",
  vpn_enabled: false,
  vpn_usernames: "",
  vpn_password: "",
  vpn_password_saved: false,
  browser_headless: false,
  direct_dola_submit_enabled: true,
  tts_default_voice: "en-US-AriaNeural",
  dola_mode: "hybrid",
}

type Page = "video" | "prompts" | "gallery" | "history" | "settings"

const pageRoutes: Record<Page, string> = {
  video: "/video",
  prompts: "/prompt-generator",
  gallery: "/gallery",
  history: "/history",
  settings: "/settings",
}

function pageFromPath(pathname: string): Page {
  if (pathname === "/prompt-generator" || pathname === "/prompts") return "prompts"
  if (pathname === "/gallery") return "gallery"
  if (pathname === "/history") return "history"
  if (pathname === "/settings") return "settings"
  return "video"
}

interface LogRow {
  id: string
  level: string
  message: string
  created_at: string
  job_id?: string | null
}

interface VideoArtifact {
  artifact: Artifact
  job: Job
}

interface QueueRow extends JobItem {
  jobId: string
  jobCreatedAt: string
}

interface EngineTelemetryStats {
  total: number
  queued: number
  generating: number
  done: number
  failed: number
  timeoutError: number
  captchaBlock: number
  highDemand: number
  dolaPolicy: number
  noTextbox: number
  browserEc: number
  noExport: number
}

export default function App() {
  const [page, setPage] = useState<Page>(() => pageFromPath(window.location.pathname))
  const [jobs, setJobs] = useState<Job[]>([])
  const [settings, setSettings] = useState<SettingsPayload>(emptySettings)
  const [logs, setLogs] = useState<LogRow[]>([])
  const [browserStatus, setBrowserStatus] = useState<DolaBrowserStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [connectionError, setConnectionError] = useState("")
  const [studioPromptText, setStudioPromptText] = useState("")
  const refreshInFlightRef = useRef(false)
  const refreshQueuedRef = useRef(false)
  const sseRefreshTimerRef = useRef<number | null>(null)

  useEffect(() => {
    document.documentElement.classList.add("dark")
  }, [])

  useEffect(() => {
    const normalizedPath = pageRoutes[pageFromPath(window.location.pathname)]
    if (window.location.pathname !== normalizedPath) {
      window.history.replaceState({}, "", normalizedPath)
    }

    function handlePopState() {
      setPage(pageFromPath(window.location.pathname))
    }

    window.addEventListener("popstate", handlePopState)
    return () => window.removeEventListener("popstate", handlePopState)
  }, [])

  const navigate = useCallback((nextPage: Page) => {
    setPage(nextPage)
    const nextPath = pageRoutes[nextPage]
    if (window.location.pathname !== nextPath) {
      window.history.pushState({}, "", nextPath)
    }
  }, [])

  const performRefresh = useCallback(async () => {
    try {
      const status = await api.studioStatus()
      setJobs(status.jobs)
      setSettings(status.settings)
      setLogs(status.logs)
      setBrowserStatus(status.browser)
      setConnectionError("")
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : "Studio connection failed")
    } finally {
      setLoading(false)
    }
  }, [])

  const refresh = useCallback(async () => {
    if (refreshInFlightRef.current) {
      refreshQueuedRef.current = true
      return
    }
    refreshInFlightRef.current = true
    try {
      do {
        refreshQueuedRef.current = false
        await performRefresh()
      } while (refreshQueuedRef.current)
    } finally {
      refreshInFlightRef.current = false
    }
  }, [performRefresh])

  const runningJobIds = useMemo(() => jobs.filter((job) => job.status === "running" || job.status === "queued").map((job) => job.id).sort().join(","), [jobs])

  useEffect(() => {
    refresh()
    let timer: number | undefined
    const schedule = () => {
      timer = window.setTimeout(async () => {
        if (!document.hidden) await refresh()
        schedule()
      }, runningJobIds ? 5000 : 15000)
    }
    const onVisibilityChange = () => {
      if (!document.hidden) refresh()
    }
    schedule()
    document.addEventListener("visibilitychange", onVisibilityChange)
    return () => {
      if (timer) window.clearTimeout(timer)
      document.removeEventListener("visibilitychange", onVisibilityChange)
    }
  }, [refresh, runningJobIds])

  useEffect(() => {
    const activeJobIds = runningJobIds ? runningJobIds.split(",") : []
    if (!activeJobIds.length) return
    const requestDebouncedRefresh = () => {
      if (sseRefreshTimerRef.current) window.clearTimeout(sseRefreshTimerRef.current)
      sseRefreshTimerRef.current = window.setTimeout(() => refresh(), 500)
    }
    const cleanups = activeJobIds.map((jobId) => subscribeJobEvents(jobId, requestDebouncedRefresh, () => undefined))
    return () => {
      cleanups.forEach((cleanup) => cleanup())
      if (sseRefreshTimerRef.current) window.clearTimeout(sseRefreshTimerRef.current)
    }
  }, [refresh, runningJobIds])

  const activeJob = useMemo(() => jobs.find((job) => job.status === "running" || job.status === "queued") ?? jobs[0], [jobs])
  const videos = useMemo(() => collectVideoArtifacts(jobs), [jobs])

  return (
    <Layout page={page} setPage={(next) => navigate(next as Page)} loading={loading}>
      {connectionError && <div className="mb-3 rounded-md border border-red-500/40 bg-red-950/40 px-3 py-2 text-xs font-semibold text-red-200">Connection issue: {connectionError}</div>}
      {page === "video" && (
        <VideoConsole
          settings={settings}
          jobs={jobs}
          activeJob={activeJob}
          logs={logs}
          browserStatus={browserStatus}
          promptText={studioPromptText}
          setPromptText={setStudioPromptText}
          onRefresh={refresh}
          onSettingsSaved={setSettings}
        />
      )}
      {page === "prompts" && (
        <PromptGenerator
          settings={settings}
          onSettingsSaved={setSettings}
          onUsePrompts={(text) => { setStudioPromptText(text); navigate("video") }}
        />
      )}
      {page === "gallery" && <GalleryPage videos={videos} />}
      {page === "history" && <History jobs={jobs} onRefresh={refresh} />}
      {page === "settings" && (
        <SettingsPage
          settings={settings}
          browserStatus={browserStatus}
          onSettingsSaved={setSettings}
          onRefresh={refresh}
        />
      )}
    </Layout>
  )
}

function VideoConsole({
  settings,
  jobs,
  activeJob,
  logs,
  browserStatus,
  promptText,
  setPromptText,
  onRefresh,
  onSettingsSaved,
}: {
  settings: SettingsPayload
  jobs: Job[]
  activeJob?: Job
  logs: LogRow[]
  browserStatus: DolaBrowserStatus | null
  promptText: string
  setPromptText: (value: string) => void
  onRefresh: () => void
  onSettingsSaved: (settings: SettingsPayload) => void
}) {
  const [ratio, setRatio] = useState(settings.default_ratio || "9:16")
  const [duration, setDuration] = useState(settings.default_duration || 10)
  const [parallel, setParallel] = useState(settings.default_parallel || 30)
  const [cleanWatermark, setCleanWatermark] = useState(true)
  const [saveMode, setSaveMode] = useState("final")
  const [submitting, setSubmitting] = useState(false)
  const [logSearch, setLogSearch] = useState("")

  useEffect(() => {
    setRatio((current) => current || settings.default_ratio || "9:16")
    setDuration((current) => current || settings.default_duration || 10)
    setParallel((current) => current || settings.default_parallel || 30)
  }, [settings])

  const displayJobs = useMemo(() => {
    const activeJobs = jobs.filter((job) => ["queued", "running"].includes(job.status) && job.items.length > 0)
    if (activeJobs.length) return [...activeJobs].sort(compareJobsOldestFirst)
    const latestNonEmptyJob = jobs.find((job) => job.items.length > 0)
    return latestNonEmptyJob ? [latestNonEmptyJob] : []
  }, [jobs])
  const primaryDisplayJob = displayJobs.at(-1) ?? activeJob
  const displayJobIds = useMemo(() => new Set(displayJobs.map((job) => job.id)), [displayJobs])
  const displayLogs = useMemo(
    () => logs.filter((row) => !row.job_id || displayJobIds.has(row.job_id)),
    [displayJobIds, logs],
  )
  const queueRows = useMemo(
    () =>
      displayJobs
        .flatMap((job) => job.items.map((item) => ({ ...item, jobId: job.id, jobCreatedAt: job.created_at })))
        .sort(compareQueueRows),
    [displayJobs],
  )
  const displaySnapshots = useMemo(() => displayJobs.flatMap((job) => job.dola_cookie_snapshots_json ?? []), [displayJobs])

  const stats = useMemo(() => {
    const items = queueRows
    return {
      total: items.length,
      queued: items.filter((item) => item.status === "queued").length,
      generating: items.filter((item) => item.status === "running").length,
      done: items.filter((item) => item.status === "completed").length,
      failed: items.filter((item) => item.status === "failed").length,
      videos: displayJobs.flatMap((job) => job.artifacts).filter((artifact) => artifact.kind === "video").length,
    }
  }, [displayJobs, queueRows])
  const telemetry = useMemo(() => buildEngineTelemetry(displayJobs, displayLogs), [displayJobs, displayLogs])
  const telemetryState = telemetry.captchaBlock || telemetry.noTextbox || telemetry.browserEc ? "BLOCKED" : telemetry.queued || telemetry.generating ? "RUNNING" : "READY"

  const progressTotal = displayJobs.reduce((total, job) => total + job.total, 0)
  const progressDone = displayJobs.reduce((total, job) => total + job.done + job.failed, 0)
  const progress = progressTotal ? Math.round((progressDone / progressTotal) * 100) : 0
  const filteredLogs = displayLogs
    .filter(isStudioLogVisible)
    .filter((row) => row.message.toLowerCase().includes(logSearch.toLowerCase()) || row.level.toLowerCase().includes(logSearch.toLowerCase()))

  async function submit() {
    const prompts = promptLines(promptText).map((prompt) => ({ title: prompt.slice(0, 70), prompt }))
    if (!prompts.length) {
      toast.error("Add at least one prompt.")
      return
    }
    setSubmitting(true)
    try {
      await api.createVideoJob({ prompts, ratio, duration, parallel, save_folder: DOCKER_OUTPUT_DIR, clean_watermark: cleanWatermark, save_mode: saveMode })
      toast.success("Video generation queued")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to queue video job")
    } finally {
      setSubmitting(false)
    }
  }

  async function stopGeneration() {
    const activeDisplayJobs = displayJobs.filter((job) => ["queued", "running"].includes(job.status))
    if (!activeDisplayJobs.length) return
    try {
      await Promise.all(activeDisplayJobs.map((job) => api.cancelVideoJob(job.id)))
      await api.killAllDolaBrowserSlots()
      toast.success("Force stopped generation and killed browser/VPN slots")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to stop job")
    }
  }

  async function killAllSlots() {
    try {
      const result = await api.killAllDolaBrowserSlots()
      toast.success(`Killed ${result.closed_browser_slots} browser slot(s), ${result.closed_vpn_slots} VPN slot(s)`)
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to kill browser/VPN slots")
    }
  }

  async function setBrowserHeadless(value: boolean) {
    try {
      const saved = await api.saveSettings({ ...settings, browser_headless: value })
      onSettingsSaved(saved)
      toast.success(value ? "Headless mode enabled" : "Visible browser mode enabled")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to save browser mode")
    }
  }

  async function setDirectDolaSubmitEnabled(value: boolean) {
    try {
      const saved = await api.saveSettings({ ...settings, direct_dola_submit_enabled: value })
      onSettingsSaved(saved)
      toast.success(value ? "Direct Dola HTTP submit enabled" : "Direct Dola HTTP submit disabled")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to save direct submit setting")
    }
  }

  async function resumePoll(jobId: string, itemId: string) {
    try {
      await api.resumeVideoItemPoll(jobId, itemId)
      toast.success("Resume poll queued")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to queue resume poll")
    }
  }

  async function forceStopItem(jobId: string, itemId: string) {
    try {
      await api.forceStopVideoItem(jobId, itemId)
      toast.success("Force stop requested")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to force stop item")
    }
  }

  async function restartItem(jobId: string, itemId: string) {
    try {
      await api.restartVideoItem(jobId, itemId)
      toast.success("Restart queued")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to restart item")
    }
  }


  return (
    <div className="mx-auto max-w-[1760px] space-y-5">
      <div className="flex flex-col gap-3 border-b border-border/70 pb-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <h1 className="text-xl font-black tracking-tight sm:text-2xl">Seedance Vid Gen</h1>
          <p className="mt-1 text-xs font-semibold text-muted-foreground">Bulk generate high quality Seedance AI videos through Dola automation.</p>
        </div>
      </div>

      <OutputLocation path={activeJobOutputLabel(primaryDisplayJob) || HOST_OUTPUT_LABEL} containerPath={activeJobOutputContainer(primaryDisplayJob) || DOCKER_OUTPUT_DIR} basePath={HOST_OUTPUT_LABEL} />

      <EngineTelemetry stats={telemetry} state={telemetryState} />

      <Card className="px-4 py-3">
        <div className="mb-2 flex items-center justify-between gap-3">
          <div className="text-[11px] font-black uppercase tracking-[0.2em] text-muted-foreground">Progress</div>
          <div className="text-xs font-black text-foreground">{progressDone} / {progressTotal} done ({progress}%)</div>
        </div>
        <Progress value={progress} />
      </Card>

      <section className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(380px,0.42fr)_minmax(520px,0.58fr)]">
        <div className="space-y-4">
          <GenerationSettings
            ratio={ratio}
            setRatio={setRatio}
            duration={duration}
            setDuration={setDuration}
            parallel={parallel}
            setParallel={setParallel}
            cleanWatermark={cleanWatermark}
            setCleanWatermark={setCleanWatermark}
            saveMode={saveMode}
            setSaveMode={setSaveMode}
            settings={settings}
            browserStatus={browserStatus}
            submitting={submitting}
            hasActiveJob={displayJobs.some((job) => ["queued", "running"].includes(job.status))}
            onStart={submit}
            onStop={stopGeneration}
            onKillAllSlots={killAllSlots}
            onSetBrowserHeadless={setBrowserHeadless}
            onSetDirectDolaSubmitEnabled={setDirectDolaSubmitEnabled}
          />
          <GenerationQueue
            items={queueRows}
            snapshots={displaySnapshots}
            onResumePoll={resumePoll}
            onForceStop={forceStopItem}
            onRestart={restartItem}
            browserUrl={browserStatus?.manual_url || "http://localhost:6080"}
          />
        </div>

        <div className="space-y-4">
          <PromptsPanel promptText={promptText} setPromptText={setPromptText} count={promptLines(promptText).length} />
          <LiveLogConsole logs={filteredLogs} search={logSearch} setSearch={setLogSearch} />
        </div>
      </section>

    </div>
  )
}

function OutputLocation({ path, containerPath, basePath }: { path: string; containerPath: string; basePath: string }) {
  return (
    <Card className="p-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-primary/15 text-primary">
            <Folder size={22} />
          </div>
          <div className="min-w-0">
            <div className="text-xs font-black uppercase tracking-[0.16em] text-muted-foreground">Video output location</div>
            <div className="mt-2 truncate font-mono text-xs font-bold text-foreground">{path}</div>
            <div className="mt-1 text-[11px] font-semibold text-muted-foreground">
              Base: {basePath}. Container path: {containerPath}
            </div>
          </div>
        </div>
      </div>
    </Card>
  )
}

type NetworkMode = "direct" | "proxy" | "vpn"

function networkModeLabel(settings: SettingsPayload): string {
  if (settings.vpn_enabled) return "OpenVPN"
  if (settings.proxy_enabled) return "Proxy"
  return "Direct"
}

function SettingsPage({
  settings,
  browserStatus,
  onSettingsSaved,
  onRefresh,
}: {
  settings: SettingsPayload
  browserStatus: DolaBrowserStatus | null
  onSettingsSaved: (settings: SettingsPayload) => void
  onRefresh: () => void
}) {
  const [networkMode, setNetworkMode] = useState<NetworkMode>(() => settings.vpn_enabled ? "vpn" : settings.proxy_enabled ? "proxy" : "direct")
  const [proxyUrl, setProxyUrl] = useState(settings.proxy_url || "")
  const [vpnUsernames, setVpnUsernames] = useState(settings.vpn_usernames || "")
  const [vpnPassword, setVpnPassword] = useState("")
  const [vpnConfigs, setVpnConfigs] = useState<Array<{ name: string; size_bytes: number }>>([])
  const [vpnStatus, setVpnStatus] = useState<{ connected: boolean; config_name?: string; username_masked?: string; ip?: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [testingProxy, setTestingProxy] = useState(false)
  const [testingVpn, setTestingVpn] = useState(false)
  const [settingsDirty, setSettingsDirty] = useState(false)
  const vpnFileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (settingsDirty) return
    setNetworkMode(settings.vpn_enabled ? "vpn" : settings.proxy_enabled ? "proxy" : "direct")
    setProxyUrl(settings.proxy_url || "")
    setVpnUsernames(settings.vpn_usernames || "")
    setVpnPassword("")
  }, [settings, settingsDirty])

  useEffect(() => {
    refreshVpn()
  }, [])

  async function saveNetworkSettings() {
    setSaving(true)
    try {
      const saved = await api.saveSettings({
        ...settings,
        proxy_enabled: networkMode === "proxy",
        proxy_url: proxyUrl,
        vpn_enabled: networkMode === "vpn",
        vpn_usernames: vpnUsernames,
        vpn_password: vpnPassword,
      })
      setSettingsDirty(false)
      onSettingsSaved(saved)
      toast.success("Network settings saved")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to save network settings")
    } finally {
      setSaving(false)
    }
  }

  async function testProxy() {
    if (!proxyUrl.trim()) {
      toast.error("Add proxy URL first.")
      return
    }
    setTestingProxy(true)
    try {
      const result = await api.testProxy(proxyUrl)
      result.ok ? toast.success(result.ip ? `Proxy reachable: ${result.ip}` : result.message) : toast.error(result.message)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Proxy test failed")
    } finally {
      setTestingProxy(false)
    }
  }

  async function refreshVpn() {
    try {
      const [configs, status] = await Promise.all([api.vpnConfigs(), api.vpnStatus()])
      setVpnConfigs(configs.configs)
      setVpnStatus({ connected: status.connected, config_name: status.config_name, username_masked: status.username_masked, ip: status.ip })
    } catch {
      setVpnStatus(null)
    }
  }

  async function uploadVpnFiles(files: FileList | null) {
    if (!files?.length) return
    try {
      for (const file of Array.from(files)) {
        await api.uploadVpnConfig(file)
      }
      toast.success("VPN configs uploaded")
      refreshVpn()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "VPN upload failed")
    }
  }

  async function deleteVpnConfig(name: string) {
    try {
      await api.deleteVpnConfig(name)
      toast.success("VPN config deleted")
      refreshVpn()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "VPN delete failed")
    }
  }

  async function testVpn() {
    setTestingVpn(true)
    try {
      const saved = await api.saveSettings({ ...settings, proxy_enabled: false, vpn_enabled: true, vpn_usernames: vpnUsernames, vpn_password: vpnPassword })
      setSettingsDirty(false)
      onSettingsSaved(saved)
      const result = await api.testVpn()
      toast.success(result.ip ? `VPN reachable: ${result.ip}` : "VPN reachable")
      refreshVpn()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "VPN test failed")
    } finally {
      setTestingVpn(false)
    }
  }

  async function testIsolatedVpn() {
    setTestingVpn(true)
    try {
      const saved = await api.saveSettings({ ...settings, proxy_enabled: false, vpn_enabled: true, vpn_usernames: vpnUsernames, vpn_password: vpnPassword })
      setSettingsDirty(false)
      onSettingsSaved(saved)
      const result = await api.testIsolatedVpn()
      toast.success(`Isolated VPN slot ready${result.ip ? `: ${result.ip}` : ""}`)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Isolated VPN slot test failed")
    } finally {
      setTestingVpn(false)
    }
  }

  return (
    <div className="mx-auto max-w-[1100px] space-y-5">
      <div className="border-b border-border/70 pb-4">
        <h1 className="text-xl font-black tracking-tight sm:text-2xl">Settings</h1>
        <p className="mt-1 text-xs font-semibold text-muted-foreground">Choose one network mode for Dola session and browser submit.</p>
      </div>

      <Card className="p-4">
        <SectionTitle icon={<Settings2 size={15} />} title="Network Mode" />
        <div className="mt-4 grid grid-cols-3 gap-2">
          {(["direct", "proxy", "vpn"] as NetworkMode[]).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => {
                setNetworkMode(mode)
                setSettingsDirty(true)
              }}
              className={`h-11 rounded-md border text-xs font-black uppercase tracking-wide ${
                networkMode === mode ? "border-primary bg-primary text-primary-foreground" : "border-border bg-background text-muted-foreground hover:bg-muted"
              }`}
            >
              {mode === "direct" ? "Direct" : mode === "proxy" ? "Proxy" : "OpenVPN"}
            </button>
          ))}
        </div>

        {networkMode === "proxy" && (
          <div className="mt-4 grid gap-3">
            <Field label="Proxy URL">
              <Input value={proxyUrl} onChange={(event) => {
                setProxyUrl(event.target.value)
                setSettingsDirty(true)
              }} placeholder="http://user:pass@host:port" />
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Button variant="secondary" onClick={testProxy} disabled={testingProxy || !proxyUrl.trim()}>
                {testingProxy ? <Loader2 className="animate-spin" size={16} /> : <Zap size={16} />}
                Test Proxy
              </Button>
              <Button variant="secondary" onClick={saveNetworkSettings} disabled={saving}>
                {saving ? <Loader2 className="animate-spin" size={16} /> : <Settings2 size={16} />}
                Save
              </Button>
            </div>
          </div>
        )}

        {networkMode === "vpn" && (
          <div className="mt-4 grid gap-3">
            <Field label="VPN username">
              <Input value={vpnUsernames} onChange={(event) => {
                setVpnUsernames(event.target.value)
                setSettingsDirty(true)
              }} placeholder="Shared username for all VPN files" />
            </Field>
            <Field label={settings.vpn_password_saved ? "VPN shared password (saved)" : "VPN shared password"}>
              <Input type="password" value={vpnPassword} onChange={(event) => {
                setVpnPassword(event.target.value)
                setSettingsDirty(true)
              }} placeholder={settings.vpn_password_saved ? "Leave blank to keep saved password" : "Shared VPN password"} />
            </Field>
            <input ref={vpnFileRef} type="file" accept=".ovpn" multiple className="hidden" onChange={(event) => uploadVpnFiles(event.target.files)} />
            <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
              <Button variant="secondary" onClick={() => vpnFileRef.current?.click()}>
                <Upload size={16} />
                Upload .ovpn
              </Button>
              <Button variant="secondary" onClick={testVpn} disabled={testingVpn || !vpnUsernames.trim()}>
                {testingVpn ? <Loader2 className="animate-spin" size={16} /> : <Zap size={16} />}
                Test VPN
              </Button>
              <Button variant="secondary" onClick={testIsolatedVpn} disabled={testingVpn || !vpnUsernames.trim()}>
                {testingVpn ? <Loader2 className="animate-spin" size={16} /> : <Zap size={16} />}
                Test Isolated Slot
              </Button>
              <Button variant="secondary" onClick={saveNetworkSettings} disabled={saving}>
                {saving ? <Loader2 className="animate-spin" size={16} /> : <Settings2 size={16} />}
                Save
              </Button>
            </div>
            <div className="rounded-md border border-border bg-background p-3 text-[11px] font-semibold text-muted-foreground">
              <div className="truncate">VPN status: {vpnStatus?.connected ? `connected ${vpnStatus.config_name || ""} ${vpnStatus.ip || ""}` : "disconnected"}</div>
              <div className="mt-2 flex flex-wrap gap-2">
                {vpnConfigs.length ? vpnConfigs.map((config) => (
                  <span key={config.name} className="inline-flex max-w-full items-center gap-2 rounded-md bg-muted px-2 py-1">
                    <span className="truncate">{config.name}</span>
                    <button type="button" onClick={() => deleteVpnConfig(config.name)} className="text-red-300 hover:text-red-200">delete</button>
                  </span>
                )) : <span>No .ovpn configs uploaded</span>}
              </div>
            </div>
          </div>
        )}

        {networkMode === "direct" && (
          <div className="mt-4 grid gap-3">
            <div className="rounded-md border border-border bg-background p-3 text-xs font-semibold text-muted-foreground">Direct mode disables proxy and OpenVPN for Dola submit.</div>
            <Button variant="secondary" onClick={saveNetworkSettings} disabled={saving}>
              {saving ? <Loader2 className="animate-spin" size={16} /> : <Settings2 size={16} />}
              Save Direct Mode
            </Button>
          </div>
        )}
      </Card>

      <Card className="p-4">
        <SectionTitle icon={<Terminal size={15} />} title="Dola Browser" />
        <div className="mt-3 text-xs font-semibold text-muted-foreground">
          <div>Current mode: {networkModeLabel(settings)}</div>
          <div>Browser: {browserStatus?.ok ? "connected" : "disconnected"}</div>
          <div>Proxy: {browserStatus?.browser_proxy_active ? `active ${browserStatus.browser_proxy_host || ""}` : "inactive"}</div>
          <div>VPN: {browserStatus?.browser_vpn_active ? `active ${browserStatus.browser_vpn_config || ""} ${browserStatus.browser_vpn_ip || ""}` : "inactive"}</div>
        </div>
      </Card>
    </div>
  )
}

function GenerationSettings({
  ratio,
  setRatio,
  duration,
  setDuration,
  parallel,
  setParallel,
  cleanWatermark,
  setCleanWatermark,
  saveMode,
  setSaveMode,
  settings,
  browserStatus,
  submitting,
  hasActiveJob,
  onStart,
  onStop,
  onKillAllSlots,
  onSetBrowserHeadless,
  onSetDirectDolaSubmitEnabled,
}: {
  ratio: string
  setRatio: (value: string) => void
  duration: number
  setDuration: (value: number) => void
  parallel: number
  setParallel: (value: number) => void
  cleanWatermark: boolean
  setCleanWatermark: (value: boolean) => void
  saveMode: string
  setSaveMode: (value: string) => void
  settings: SettingsPayload
  browserStatus: DolaBrowserStatus | null
  submitting: boolean
  hasActiveJob: boolean
  onStart: () => void
  onStop: () => void
  onKillAllSlots: () => void
  onSetBrowserHeadless: (value: boolean) => void
  onSetDirectDolaSubmitEnabled: (value: boolean) => void
}) {
  return (
    <Card className="p-4">
      <SectionTitle icon={<Settings2 size={15} />} title="Generation Settings" />
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Model">
          <Select value="Seedance 2.0" disabled><option>Seedance 2.0</option></Select>
        </Field>
        <Field label="Duration">
          <Select value={String(duration)} onChange={(event) => setDuration(Number(event.target.value))}>
            <option value="5">5</option>
            <option value="10">10</option>
            <option value="15">15</option>
          </Select>
        </Field>
        <Field label="Aspect Ratio">
          <Select value={ratio} onChange={(event) => setRatio(event.target.value)}>
            <option>9:16</option>
            <option>16:9</option>
            <option>1:1</option>
          </Select>
        </Field>
        <Field label="Batch Size">
          <Input type="number" min={1} value={parallel} onChange={(event) => setParallel(Math.max(1, Number(event.target.value)))} />
        </Field>
        <label className="flex min-h-10 items-center gap-3 rounded-md border border-border bg-background px-3 text-xs font-black uppercase tracking-wide text-muted-foreground sm:col-span-2">
          <input type="checkbox" checked={cleanWatermark} onChange={(event) => setCleanWatermark(event.target.checked)} className="h-4 w-4 accent-[hsl(var(--primary))]" />
          Clean watermark after download
        </label>
        <div className="flex min-h-10 flex-col gap-3 rounded-md border border-border bg-background px-3 py-3 sm:col-span-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="text-xs font-black uppercase tracking-wide text-muted-foreground">Browser Mode</div>
            <div className="mt-1 text-[11px] font-semibold text-muted-foreground">Applies to all generation browser submits.</div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Button variant={!settings.browser_headless ? "default" : "secondary"} className="h-8 px-3 text-xs" onClick={() => onSetBrowserHeadless(false)}>
              Visible
            </Button>
            <Button variant={settings.browser_headless ? "default" : "secondary"} className="h-8 px-3 text-xs" onClick={() => onSetBrowserHeadless(true)}>
              Headless
            </Button>
          </div>
        </div>
        <div className="flex min-h-10 flex-col gap-3 rounded-md border border-border bg-background px-3 py-3 sm:col-span-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="text-xs font-black uppercase tracking-wide text-muted-foreground">Direct Dola HTTP Submit</div>
            <div className="mt-1 text-[11px] font-semibold text-muted-foreground">When off, generation submits through the browser.</div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Button variant={settings.direct_dola_submit_enabled ? "default" : "secondary"} className="h-8 px-3 text-xs" onClick={() => onSetDirectDolaSubmitEnabled(true)}>
              On
            </Button>
            <Button variant={!settings.direct_dola_submit_enabled ? "default" : "secondary"} className="h-8 px-3 text-xs" onClick={() => onSetDirectDolaSubmitEnabled(false)}>
              Off
            </Button>
          </div>
        </div>
        <Field label="Save output" className="sm:col-span-2">
          <Select value={saveMode} onChange={(event) => setSaveMode(event.target.value)}>
            <option value="final">Final only, raw only if cleanup fails</option>
            <option value="raw">Raw only</option>
            <option value="both">Both raw and final</option>
          </Select>
        </Field>
        <div className="rounded-md border border-border bg-background p-3 sm:col-span-2">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <div className="text-[11px] font-black uppercase tracking-wide text-muted-foreground">Network</div>
              <div className="mt-1 truncate text-xs font-bold text-foreground">
                Mode: {networkModeLabel(settings)} - Browser {!browserStatus ? "starting" : browserStatus.ok ? "healthy" : "manager unavailable"}
              </div>
              <div className="mt-1 truncate text-[11px] font-semibold text-muted-foreground">{browserStatus?.page_url || browserStatus?.error || "Browser status unavailable"}</div>
              <div className="mt-1 truncate text-[11px] font-semibold text-muted-foreground">
                Browser proxy: {browserStatus?.browser_proxy_active ? `active ${browserStatus.browser_proxy_host || ""}` : "inactive"}
                {browserStatus?.browser_ip ? ` - IP ${browserStatus.browser_ip}` : ""}
                {typeof browserStatus?.active_browser_count === "number" ? ` - active ${browserStatus.active_browser_count}/${browserStatus.max_browser_slots || 0}` : ""}
                {browserStatus?.active_cdp_ports?.length ? ` - ports ${browserStatus.active_cdp_ports.join(",")}` : ""}
              </div>
              <div className="mt-1 truncate text-[11px] font-semibold text-muted-foreground">
                Browser VPN: {browserStatus?.browser_vpn_active ? `active ${browserStatus.browser_vpn_config || ""} ${browserStatus.browser_vpn_ip || ""}` : "inactive"}
                {typeof browserStatus?.active_vpn_browser_count === "number" ? ` - isolated slots ${browserStatus.active_vpn_browser_count}` : ""}
              </div>
              {browserStatus?.vpn_slots?.length ? (
                <div className="mt-1 truncate text-[11px] font-semibold text-amber-200">
                  VPN slots: {browserStatus.vpn_slots.map((slot) => `${slot.config_name || slot.slot_id}: ${slot.stage || "starting"}`).join(" | ")}
                </div>
              ) : null}
              {settings.browser_headless && <div className="mt-1 text-[11px] font-semibold text-muted-foreground">Headless mode: generation browser windows are not visible in noVNC.</div>}
              {(browserStatus?.last_submit_endpoint || browserStatus?.last_dola_error) && (
                <div className="mt-1 truncate text-[11px] font-semibold text-muted-foreground">
                  Last submit: {browserStatus.last_submit_endpoint || "none"} {browserStatus.last_dola_error ? `- ${browserStatus.last_dola_error}` : ""}
                </div>
              )}
            </div>
            <div className="flex shrink-0 flex-wrap gap-2">
              <Button variant="secondary" className="h-9 px-3 text-xs text-red-200 hover:text-red-100" onClick={onKillAllSlots}>
                <Square size={14} />
                Kill All Slots
              </Button>
              <Button variant="secondary" className="h-9 px-3 text-xs" onClick={() => window.open(browserStatus?.manual_url || "http://localhost:6080", "_blank")}>
                <Terminal size={14} />
                Open
              </Button>
            </div>
          </div>
        </div>
      </div>
      <Button className="mt-5 h-12 w-full bg-[#ff225c] text-white hover:bg-[#ff3b6f]" onClick={hasActiveJob ? onStop : onStart} disabled={submitting}>
        {submitting ? <Loader2 className="animate-spin" size={17} /> : hasActiveJob ? <Square size={15} /> : <Play size={16} />}
        {hasActiveJob ? "Stop Generation" : "Start Generation"}
      </Button>
    </Card>
  )
}

function PromptsPanel({ promptText, setPromptText, count }: { promptText: string; setPromptText: (value: string) => void; count: number }) {
  const txtRef = useRef<HTMLInputElement>(null)
  const csvRef = useRef<HTMLInputElement>(null)

  async function handleImport(file: File) {
    try {
      const result = await api.importPrompts(file)
      const incoming = result.prompts.join("\n")
      setPromptText(promptText.trim() ? `${promptText.trim()}\n${incoming}` : incoming)
    } catch (err) {
      alert(`Import failed: ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  return (
    <Card className="p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <SectionTitle icon={<FileText size={15} />} title="Prompts" badge={`${count} prompt${count === 1 ? "" : "s"}`} />
        <div className="flex flex-wrap gap-2">
          <input ref={txtRef} type="file" accept=".txt" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) { handleImport(f); e.target.value = "" } }} />
          <input ref={csvRef} type="file" accept=".csv" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) { handleImport(f); e.target.value = "" } }} />
          <MiniAction onClick={() => txtRef.current?.click()}><Upload size={13} />TXT</MiniAction>
          <MiniAction onClick={() => csvRef.current?.click()}><Upload size={13} />CSV</MiniAction>
          <MiniAction danger onClick={() => setPromptText("")}><Trash2 size={13} />Clear</MiniAction>
        </div>
      </div>
      <Textarea
        className="mt-4 min-h-[145px] resize-y border-border/80 bg-[#11121d] font-mono text-sm"
        value={promptText}
        onChange={(event) => setPromptText(event.target.value)}
        placeholder="One prompt per line..."
      />
      <p className="mt-3 text-xs font-semibold text-muted-foreground">One prompt per line = one video.</p>
    </Card>
  )
}

function GenerationQueue({
  items,
  snapshots,
  onResumePoll,
  onForceStop,
  onRestart,
  browserUrl,
}: {
  items: QueueRow[]
  snapshots: Array<Record<string, unknown>>
  onResumePoll: (jobId: string, itemId: string) => void
  onForceStop: (jobId: string, itemId: string) => void
  onRestart: (jobId: string, itemId: string) => void
  browserUrl: string
}) {
  const [page, setPage] = useState(1)
  const [expandedItemId, setExpandedItemId] = useState<string | null>(null)
  const stableItems = useMemo(() => [...items].sort(compareQueueRows), [items])
  const pageCount = Math.max(1, Math.ceil(stableItems.length / QUEUE_PAGE_SIZE))
  const safePage = Math.min(page, pageCount)
  const start = (safePage - 1) * QUEUE_PAGE_SIZE
  const pageItems = stableItems.slice(start, start + QUEUE_PAGE_SIZE)

  useEffect(() => {
    setPage((current) => Math.min(current, Math.max(1, Math.ceil(stableItems.length / QUEUE_PAGE_SIZE))))
  }, [stableItems.length])

  return (
    <Card className="p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <SectionTitle icon={<Zap size={15} />} title="Generation Queue" badge={stableItems.length ? `${stableItems.length} total` : undefined} />
        {stableItems.length > QUEUE_PAGE_SIZE && (
          <div className="flex items-center gap-2 text-xs font-bold text-muted-foreground">
            <Button variant="secondary" className="h-8 px-2 text-xs" onClick={() => setPage((current) => Math.max(1, current - 1))} disabled={safePage <= 1}>
              Prev
            </Button>
            <span className="min-w-[92px] text-center">Page {safePage} / {pageCount}</span>
            <Button variant="secondary" className="h-8 px-2 text-xs" onClick={() => setPage((current) => Math.min(pageCount, current + 1))} disabled={safePage >= pageCount}>
              Next
            </Button>
          </div>
        )}
      </div>
      <div className="mt-4 overflow-x-auto rounded-md border border-border">
        <table className="w-full min-w-[560px] text-left text-xs">
          <thead className="bg-muted/50 text-[11px] uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="px-3 py-2">#</th>
              <th className="px-3 py-2">Prompt</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Action</th>
            </tr>
          </thead>
          <tbody>
            {pageItems.map((item, index) => {
              const canResume = hasSavedBrowserSnapshot(snapshots, item.id) && !item.artifact_id
              const canForceStop = item.status === "queued" || item.status === "running"
              const canRestart = item.status === "failed" || item.status === "cancelled" || item.status === "completed" || item.status === "running"
              const diagnostic = item.diagnostic_json ?? {}
              const hasDiagnostic = visibleDiagnosticEntries(diagnostic).length > 0
              return (
                <Fragment key={item.id}>
                  <tr className="border-t border-border">
                    <td className="px-3 py-3 text-muted-foreground">{start + index + 1}</td>
                    <td className="max-w-[320px] truncate px-3 py-3 font-bold">{item.prompt}</td>
                    <td className="px-3 py-3"><Badge tone={tone(item.status)}>{item.status}</Badge></td>
                    <td className="max-w-[520px] px-3 py-3 text-muted-foreground">
                      <div className="flex items-center gap-2">
                        <span className="min-w-0 flex-1 truncate" title={item.error || item.action || "Waiting..."}>{item.error || item.action || "Waiting..."}</span>
                        {hasDiagnostic && (
                          <Button
                            variant="secondary"
                            className="h-7 shrink-0 px-2 text-[11px]"
                            onClick={() => setExpandedItemId((current) => current === item.id ? null : item.id)}
                          >
                            {expandedItemId === item.id ? "Hide details" : "View details"}
                          </Button>
                        )}
                        {canResume && (
                          <Button variant="secondary" className="h-7 shrink-0 px-2 text-[11px]" onClick={() => onResumePoll(item.jobId, item.id)}>
                            Resume Poll
                          </Button>
                        )}
                        {canRestart && (
                          <Button variant="secondary" className="h-7 shrink-0 px-2 text-[11px]" onClick={() => onRestart(item.jobId, item.id)}>
                            Restart
                          </Button>
                        )}
                        {canForceStop && (
                          <Button variant="secondary" className="h-7 shrink-0 px-2 text-[11px] text-red-200 hover:text-red-100" onClick={() => onForceStop(item.jobId, item.id)}>
                            Force Stop
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                  {hasDiagnostic && expandedItemId === item.id && (
                    <tr className="border-t border-border bg-black/20">
                      <td colSpan={4} className="p-3">
                        <BrowserDiagnosticDetails diagnostic={diagnostic} browserUrl={browserUrl} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
            {!stableItems.length && <tr><td className="px-3 py-5 text-center text-muted-foreground" colSpan={4}>No queued videos yet.</td></tr>}
          </tbody>
        </table>
      </div>
      {stableItems.length > QUEUE_PAGE_SIZE && (
        <div className="mt-3 text-right text-xs font-semibold text-muted-foreground">
          Showing {start + 1}-{Math.min(start + QUEUE_PAGE_SIZE, stableItems.length)} of {stableItems.length}
        </div>
      )}
    </Card>
  )
}

function LiveLogConsole({ logs, search, setSearch }: { logs: LogRow[]; search: string; setSearch: (value: string) => void }) {
  return (
    <Card className="overflow-hidden border-primary/25">
      <div className="flex flex-col gap-3 border-b border-border bg-[#0d0f1c] p-4 sm:flex-row sm:items-center sm:justify-between">
        <SectionTitle icon={<Terminal size={15} />} title="Live Log Console" badge="streaming" />
        <div className="flex min-w-0 gap-2">
          <div className="relative min-w-0 flex-1 sm:w-56">
            <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" size={14} />
            <Input className="h-8 pl-8 text-xs" placeholder="Search logs..." value={search} onChange={(event) => setSearch(event.target.value)} />
          </div>
          <IconButton label="Download"><Download size={15} /></IconButton>
        </div>
      </div>
      <div className="h-[420px] overflow-auto bg-black p-4 font-mono text-xs leading-6 text-cyan-300 shadow-inner shadow-primary/10">
        {logs.map((row, index) => (
          <div key={row.id} className="grid grid-cols-[42px_82px_42px_minmax(0,1fr)] items-start gap-2">
            <span className="text-slate-500">{String(index + 1).padStart(3, "0")}</span>
            <span className="text-slate-500">[{new Date(row.created_at).toLocaleTimeString()}]</span>
            <span className={row.level === "error" ? "text-red-300" : row.level === "warn" ? "text-amber-300" : "text-emerald-300"}>{row.level.toUpperCase()}</span>
            <span className="whitespace-pre-wrap break-words">{row.message}</span>
          </div>
        ))}
        {!logs.length && <div className="text-muted-foreground">No live logs yet. Start a generation to stream worker events.</div>}
      </div>
    </Card>
  )
}

function isStudioLogVisible(row: LogRow) {
  const message = row.message.trim()
  if (row.level === "debug") return false
  if (message.startsWith("RAW ")) return false
  if (message.startsWith("Could not persist RAW ")) return false
  if (message.includes("No Dola video id found yet")) return false
  return true
}

function GalleryPage({ videos }: { videos: VideoArtifact[] }) {
  return (
    <div className="mx-auto max-w-[1760px] space-y-5">
      <div className="flex flex-col gap-3 border-b border-border/70 pb-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <h1 className="text-xl font-black tracking-tight sm:text-2xl">Video Gallery</h1>
          <p className="mt-1 text-xs font-semibold text-muted-foreground">Playable MP4 outputs from completed Seedance jobs.</p>
        </div>
      </div>
      <VideoGallery videos={videos} />
    </div>
  )
}

function VideoGallery({ videos }: { videos: VideoArtifact[] }) {
  return (
    <section className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <SectionTitle icon={<FileVideo size={15} />} title="Generated Video Outputs" badge={`${videos.length} ready`} />
        <div className="text-xs font-semibold text-muted-foreground">Playable MP4 previews from completed artifacts.</div>
      </div>

      {videos.length ? (
        <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
          {videos.map(({ artifact, job }) => (
            <Card key={artifact.id} className="overflow-hidden">
              <div className="aspect-video bg-black">
                <video className="h-full w-full object-contain" controls preload="metadata" src={artifactUrl(artifact.id)} />
              </div>
              <div className="space-y-3 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="truncate font-black">{artifact.filename}</h3>
                    <p className="mt-1 text-xs text-muted-foreground">{job.title} - {formatBytes(artifact.size_bytes)} - {new Date(artifact.created_at).toLocaleString()}</p>
                  </div>
                  <Badge tone={tone(job.status)}>{job.status}</Badge>
                </div>
                <div className="flex flex-wrap gap-2">
                  <a className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-bold text-primary-foreground hover:opacity-90" href={artifactUrl(artifact.id)} target="_blank" rel="noreferrer"><Folder size={16} />Open</a>
                  <a className="inline-flex h-9 items-center gap-2 rounded-md bg-muted px-3 text-sm font-bold text-foreground hover:bg-border" href={artifactUrl(artifact.id)} download><Download size={16} />Download</a>
                </div>
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Card className="flex min-h-[190px] flex-col items-center justify-center p-8 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary"><FileVideo size={24} /></div>
          <h3 className="mt-4 text-lg font-black">No generated videos yet.</h3>
          <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">Completed MP4 artifacts will appear here with playable previews and download actions.</p>
        </Card>
      )}
    </section>
  )
}

function PromptGenerator({
  settings,
  onSettingsSaved,
  onUsePrompts,
}: {
  settings: SettingsPayload
  onSettingsSaved: (settings: SettingsPayload) => void
  onUsePrompts: (text: string) => void
}) {
  const [sourceMode, setSourceMode] = useState<"manual" | "niches">("manual")
  const [idea, setIdea] = useState("")
  const [count, setCount] = useState(5)
  const [countMode, setCountMode] = useState<"global" | "per_niche">("global")
  const [duration, setDuration] = useState(10)
  const [apiKey, setApiKey] = useState(settings.gemini_api_key || "")
  const [baseUrl, setBaseUrl] = useState(settings.gemini_base_url || "https://generativelanguage.googleapis.com/v1beta")
  const [model, setModel] = useState(settings.gemini_model || "gemini-2.5-flash")
  const [niches, setNiches] = useState<Niche[]>([])
  const [loadingNiches, setLoadingNiches] = useState(false)
  const [nicheSearch, setNicheSearch] = useState("")
  const [selectedNicheIds, setSelectedNicheIds] = useState<string[]>([])
  const [generated, setGenerated] = useState<string[]>([])
  const [generatedGroups, setGeneratedGroups] = useState<NichePromptGroup[]>([])
  const [generating, setGenerating] = useState(false)
  const [generationTarget, setGenerationTarget] = useState(0)
  const [currentProgressLabel, setCurrentProgressLabel] = useState("")
  const [savingSettings, setSavingSettings] = useState(false)
  const [copied, setCopied] = useState<number | string | null>(null)

  const allGeneratedPrompts = useMemo(() => (generatedGroups.length ? generatedGroups.flatMap((group) => group.prompts) : generated), [generated, generatedGroups])
  const generatedText = allGeneratedPrompts.join("\n")
  const generatedDownloadText = useMemo(
    () =>
      generatedGroups.length
        ? generatedGroups
            .map((group) => [`## ${group.niche_name}`, ...group.prompts.map((prompt, index) => `${index + 1}. ${prompt}`)].join("\n"))
            .join("\n\n")
        : generated.map((prompt, index) => `${index + 1}. ${prompt}`).join("\n\n"),
    [generated, generatedGroups],
  )
  const generationProgress = generationTarget ? Math.min(100, Math.round((allGeneratedPrompts.length / generationTarget) * 100)) : 0
  const filteredNiches = useMemo(() => {
    const query = nicheSearch.trim().toLowerCase()
    if (!query) return niches
    return niches.filter((niche) => `${niche.name} ${niche.filename}`.toLowerCase().includes(query))
  }, [nicheSearch, niches])
  const selectedNiches = useMemo(() => niches.filter((niche) => selectedNicheIds.includes(niche.id)), [niches, selectedNicheIds])
  const expectedPromptTotal = sourceMode === "niches" && countMode === "per_niche" ? count * selectedNicheIds.length : count

  useEffect(() => {
    setApiKey(settings.gemini_api_key || "")
    setBaseUrl(settings.gemini_base_url || "https://generativelanguage.googleapis.com/v1beta")
    setModel(settings.gemini_model || "gemini-2.5-flash")
  }, [settings])

  useEffect(() => {
    let alive = true
    setLoadingNiches(true)
    api.niches()
      .then((rows) => {
        if (alive) setNiches(rows)
      })
      .catch((error) => toast.error(error instanceof Error ? error.message : "Failed to load niches"))
      .finally(() => {
        if (alive) setLoadingNiches(false)
      })
    return () => {
      alive = false
    }
  }, [])

  async function saveGeminiSettings() {
    setSavingSettings(true)
    try {
      const nextSettings = { ...settings, gemini_api_key: apiKey, gemini_base_url: baseUrl, gemini_model: model }
      const saved = await api.saveSettings(nextSettings)
      onSettingsSaved(saved)
      toast.success("Gemini API settings saved")
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to save Gemini settings")
    } finally {
      setSavingSettings(false)
    }
  }

  async function generate() {
    if (sourceMode === "niches") {
      await generateFromNiches()
      return
    }
    await generateFromManualIdea()
  }

  async function generateFromManualIdea() {
    if (!idea.trim()) {
      toast.error("Add a master idea first.")
      return
    }
    if (!apiKey.trim()) {
      toast.error("Add and save your Gemini API key first.")
      return
    }
    setGenerating(true)
    setGenerated([])
    setGeneratedGroups([])
    setGenerationTarget(count)
    setCurrentProgressLabel("Manual idea batches")
    try {
      const saved = await api.saveSettings({ ...settings, gemini_api_key: apiKey, gemini_base_url: baseUrl, gemini_model: model })
      onSettingsSaved(saved)
      const nextPrompts: string[] = []
      const seen = new Set<string>()
      let resultModel = model
      while (nextPrompts.length < count) {
        const remaining = count - nextPrompts.length
        const batchCount = Math.min(PROMPT_UI_BATCH_SIZE, remaining)
        const batchIdea = buildProgressivePromptRequest(idea, nextPrompts)
        const result = await api.generatePrompts({ master_prompt: batchIdea, count: batchCount, duration, ratio: "9:16", style: "cinematic realistic" })
        resultModel = result.model
        for (const prompt of result.prompts) {
          const key = prompt.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()
          if (key && !seen.has(key)) {
            seen.add(key)
            nextPrompts.push(prompt)
          }
          if (nextPrompts.length >= count) break
        }
        setGenerated([...nextPrompts])
        if (!result.prompts.length) {
          throw new Error(`Gemini returned no prompts after ${nextPrompts.length}/${count}.`)
        }
      }
      toast.success(`Generated ${nextPrompts.length} prompt${nextPrompts.length === 1 ? "" : "s"} with ${resultModel}`)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to generate prompts")
    } finally {
      setGenerating(false)
      setGenerationTarget(0)
      setCurrentProgressLabel("")
    }
  }

  async function generateFromNiches() {
    if (!selectedNicheIds.length) {
      toast.error("Select at least one niche.")
      return
    }
    if (countMode === "global" && count < selectedNicheIds.length) {
      toast.error("Global prompt count must be at least the selected niche count.")
      return
    }
    if (!apiKey.trim()) {
      toast.error("Add and save your Gemini API key first.")
      return
    }
    const counts = countMode === "per_niche" ? selectedNiches.map(() => count) : splitPromptCount(count, selectedNiches.length)
    setGenerating(true)
    setGenerated([])
    setGeneratedGroups([])
    setGenerationTarget(counts.reduce((total, item) => total + item, 0))
    try {
      const saved = await api.saveSettings({ ...settings, gemini_api_key: apiKey, gemini_base_url: baseUrl, gemini_model: model })
      onSettingsSaved(saved)
      let resultModel = model
      for (let index = 0; index < selectedNiches.length; index += 1) {
        const niche = selectedNiches[index]
        const promptCount = counts[index]
        const nichePrompts: string[] = []
        const seen = new Set<string>()
        let batchNumber = 1
        setCurrentProgressLabel(`${niche.name}: 0/${promptCount}`)
        setGeneratedGroups((current) => upsertPromptGroup(current, {
          niche_id: niche.id,
          niche_name: niche.name,
          filename: niche.filename,
          requested_count: promptCount,
          prompts: [],
          saved_path: "",
        }))
        while (nichePrompts.length < promptCount) {
          const remaining = promptCount - nichePrompts.length
          const batchCount = Math.min(PROMPT_UI_BATCH_SIZE, remaining)
          setCurrentProgressLabel(`${niche.name}: ${nichePrompts.length}/${promptCount} - batch ${batchNumber}`)
          const result = await api.generateNichePrompts({
            niche_ids: [niche.id],
            count: batchCount,
            count_mode: "per_niche",
            duration,
            style: "cinematic realistic",
            existing_prompts: nichePrompts,
            save: false,
          })
          resultModel = result.model
          const group = result.groups[0]
          let added = 0
          for (const prompt of group?.prompts ?? []) {
            const key = prompt.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()
            if (key && !seen.has(key)) {
              seen.add(key)
              nichePrompts.push(prompt)
              added += 1
            }
            if (nichePrompts.length >= promptCount) break
          }
          if (!added) {
            throw new Error(`${niche.name}: Gemini returned no new unique prompts in batch ${batchNumber}.`)
          }
          setGeneratedGroups((current) => upsertPromptGroup(current, {
            niche_id: niche.id,
            niche_name: niche.name,
            filename: niche.filename,
            requested_count: promptCount,
            prompts: [...nichePrompts],
            saved_path: "",
          }))
          setCurrentProgressLabel(`${niche.name}: ${nichePrompts.length}/${promptCount}`)
          batchNumber += 1
        }
        const saved = await api.saveNichePrompts({ niche_id: niche.id, prompts: nichePrompts })
        setGeneratedGroups((current) => upsertPromptGroup(current, {
          niche_id: niche.id,
          niche_name: niche.name,
          filename: niche.filename,
          requested_count: promptCount,
          prompts: [...nichePrompts],
          saved_path: saved.saved_path,
        }))
        if (index + 1 < selectedNiches.length) {
          setCurrentProgressLabel(`${niche.name}: saved ${nichePrompts.length}/${promptCount}`)
        }
      }
      toast.success(`Generated niche prompts with ${resultModel}`)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to generate niche prompts")
    } finally {
      setGenerating(false)
      setGenerationTarget(0)
      setCurrentProgressLabel("")
    }
  }

  async function copyText(text: string, index: number | string) {
    await navigator.clipboard.writeText(text)
    setCopied(index)
    setTimeout(() => setCopied(null), 1400)
  }

  function downloadPrompts() {
    if (!allGeneratedPrompts.length) return
    const blob = new Blob([generatedDownloadText], { type: "text/plain" })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = "auto-dola-seedance-prompts.txt"
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
  }

  function toggleNiche(id: string, checked: boolean) {
    setSelectedNicheIds((current) => (checked ? [...new Set([...current, id])] : current.filter((item) => item !== id)))
  }

  function selectVisibleNiches() {
    setSelectedNicheIds((current) => [...new Set([...current, ...filteredNiches.map((niche) => niche.id)])])
  }

  function clearSelectedNiches() {
    setSelectedNicheIds([])
  }

  return (
    <div className="mx-auto max-w-[1760px] space-y-5">
      <div className="flex flex-col gap-3 border-b border-border/70 pb-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <h1 className="text-xl font-black tracking-tight sm:text-2xl">Prompt Generator</h1>
          <p className="mt-1 text-xs font-semibold text-muted-foreground">Create polished Seedance-ready video prompts from one master idea.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={() => copyText(generatedText, "all")} disabled={!allGeneratedPrompts.length}>
            {copied === "all" ? <Check size={16} /> : <Copy size={16} />}
            Copy All
          </Button>
          <Button variant="secondary" onClick={downloadPrompts} disabled={!allGeneratedPrompts.length}>
            <Download size={16} />
            Download TXT
          </Button>
          <Button onClick={() => onUsePrompts(generatedText)} disabled={!allGeneratedPrompts.length}>
            <FileVideo size={16} />
            Use in Video Studio
          </Button>
        </div>
      </div>

      <section className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(360px,0.38fr)_minmax(560px,0.62fr)]">
        <Card className="p-4">
          <SectionTitle icon={<Wand2 size={15} />} title="Generator Settings" badge="gemini" />
          <div className="mt-4 space-y-4">
            <div className="rounded-md border border-primary/25 bg-primary/5 p-3">
              <div className="text-xs font-black uppercase tracking-wide text-primary">Gemini API Configuration</div>
              <div className="mt-3 grid grid-cols-1 gap-3">
                <Field label="Gemini API key">
                  <Input
                    type="password"
                    value={apiKey}
                    onChange={(event) => setApiKey(event.target.value)}
                    placeholder="Paste your Gemini API key here"
                  />
                </Field>
                <Field label="Gemini API host">
                  <Input
                    value={baseUrl}
                    onChange={(event) => setBaseUrl(event.target.value)}
                    placeholder="localhost:8045"
                  />
                </Field>
                <Field label="Gemini model">
                  <Select value={model} onChange={(event) => setModel(event.target.value)}>
                    {GEMINI_MODELS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </Select>
                </Field>
              </div>
              <Button variant="secondary" className="mt-3 w-full" onClick={saveGeminiSettings} disabled={savingSettings}>
                {savingSettings ? <Loader2 className="animate-spin" size={16} /> : <Settings2 size={16} />}
                Save API Configuration
              </Button>
            </div>
            <Field label="Generation source">
              <Select value={sourceMode} onChange={(event) => setSourceMode(event.target.value as "manual" | "niches")}>
                <option value="manual">Manual master idea</option>
                <option value="niches">Local niche TXT files</option>
              </Select>
            </Field>
            {sourceMode === "manual" ? (
              <Field label="Master idea">
                <Textarea
                  className="min-h-[160px] resize-y border-border/80 bg-[#11121d] font-mono text-sm"
                  value={idea}
                  onChange={(event) => setIdea(event.target.value)}
                  placeholder="Example: luxury sports car racing through neon rain at night"
                />
              </Field>
            ) : (
              <div className="rounded-md border border-border bg-[#0d0f1c] p-3">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                  <SectionTitle icon={<FileText size={14} />} title="Niche mode" badge={`${selectedNicheIds.length} selected`} />
                  <div className="flex gap-2">
                    <Button variant="secondary" className="h-8 px-2 text-xs" onClick={selectVisibleNiches} disabled={!filteredNiches.length}>Select visible</Button>
                    <Button variant="secondary" className="h-8 px-2 text-xs" onClick={clearSelectedNiches} disabled={!selectedNicheIds.length}>Clear</Button>
                  </div>
                </div>
                <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <Field label="Count mode">
                    <Select value={countMode} onChange={(event) => setCountMode(event.target.value as "global" | "per_niche")}>
                      <option value="global">Global total</option>
                      <option value="per_niche">Per niche</option>
                    </Select>
                  </Field>
                  <Field label="Expected total">
                    <Input value={String(expectedPromptTotal)} readOnly />
                  </Field>
                </div>
                <div className="relative mt-3">
                  <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" size={15} />
                  <Input className="pl-9" value={nicheSearch} onChange={(event) => setNicheSearch(event.target.value)} placeholder="Search local niches..." />
                </div>
                <div className="mt-3 max-h-[260px] space-y-2 overflow-auto rounded-md border border-border bg-background/60 p-2">
                  {filteredNiches.map((niche) => (
                    <label key={niche.id} className="flex cursor-pointer items-start gap-3 rounded-md px-2 py-2 text-sm hover:bg-muted/50">
                      <input
                        type="checkbox"
                        checked={selectedNicheIds.includes(niche.id)}
                        onChange={(event) => toggleNiche(niche.id, event.target.checked)}
                        className="mt-1 h-4 w-4 accent-[hsl(var(--primary))]"
                      />
                      <span className="min-w-0">
                        <span className="block truncate font-bold text-foreground">{niche.name}</span>
                        <span className="block truncate text-xs text-muted-foreground">{niche.filename} - {formatBytes(niche.size_bytes)}</span>
                      </span>
                    </label>
                  ))}
                  {loadingNiches && <div className="py-6 text-center text-sm text-muted-foreground">Loading local niches...</div>}
                  {!loadingNiches && !filteredNiches.length && <div className="py-6 text-center text-sm text-muted-foreground">No niche TXT files found.</div>}
                </div>
              </div>
            )}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Prompt count">
                <Input type="number" min={1} value={count} onChange={(event) => setCount(Math.max(1, Number(event.target.value)))} />
              </Field>
              <Field label="Duration">
                <Select value={String(duration)} onChange={(event) => setDuration(Number(event.target.value))}>
                  <option value="5">5</option>
                  <option value="10">10</option>
                  <option value="15">15</option>
                </Select>
              </Field>
            </div>
            <Button className="h-12 w-full bg-[#ff225c] text-white hover:bg-[#ff3b6f]" onClick={generate} disabled={generating}>
              {generating ? <Loader2 className="animate-spin" size={17} /> : <Wand2 size={17} />}
              {generating ? `Generating ${allGeneratedPrompts.length} / ${generationTarget}` : "Generate Prompts"}
            </Button>
            {generating && (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-[11px] font-black uppercase tracking-wide text-muted-foreground">
                  <span>{currentProgressLabel || "Live prompt batches"}</span>
                  <span>{generationProgress}%</span>
                </div>
                <Progress value={generationProgress} />
              </div>
            )}
          </div>
        </Card>

        <Card className="overflow-hidden">
          <div className="flex flex-col gap-3 border-b border-border bg-[#0d0f1c] p-4 sm:flex-row sm:items-center sm:justify-between">
            <SectionTitle icon={<FileText size={15} />} title="Generated Prompts" badge={generating ? `${allGeneratedPrompts.length}/${generationTarget} ready` : `${allGeneratedPrompts.length} ready`} />
            <div className="text-xs font-semibold text-muted-foreground">{generating ? `Showing completed ${sourceMode === "niches" ? "niche groups" : `batches of ${PROMPT_UI_BATCH_SIZE}`} as soon as they return.` : "One prompt per line can be sent directly to Video Studio."}</div>
          </div>
          <div className="max-h-[650px] space-y-3 overflow-auto p-4">
            {generatedGroups.length ? (
              generatedGroups.map((group) => (
                <div key={`${group.niche_id}-${group.saved_path}`} className="rounded-md border border-border bg-background/70 p-4">
                  <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Badge>{group.prompts.length} prompts</Badge>
                        <h3 className="truncate font-black">{group.niche_name}</h3>
                      </div>
                      <p className="mt-1 truncate text-xs font-semibold text-muted-foreground">Saved locally: {group.saved_path}</p>
                    </div>
                    <Button variant="secondary" className="h-8 text-xs" onClick={() => copyText(group.prompts.join("\n"), `group-${group.niche_id}`)}>
                      {copied === `group-${group.niche_id}` ? <Check size={14} /> : <Copy size={14} />}
                      {copied === `group-${group.niche_id}` ? "Copied" : "Copy group"}
                    </Button>
                  </div>
                  <div className="space-y-2">
                    {group.prompts.map((prompt, index) => (
                      <p key={`${group.niche_id}-${index}`} className="rounded border border-border/70 bg-[#080914] p-3 font-mono text-sm leading-6 text-foreground">
                        <span className="mr-2 text-primary">#{index + 1}</span>{prompt}
                      </p>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              generated.map((prompt, index) => (
                <div key={`${prompt}-${index}`} className="rounded-md border border-border bg-background/70 p-4">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <Badge>Prompt {index + 1}</Badge>
                    <Button variant="secondary" className="h-8 text-xs" onClick={() => copyText(prompt, index)}>
                      {copied === index ? <Check size={14} /> : <Copy size={14} />}
                      {copied === index ? "Copied" : "Copy"}
                    </Button>
                  </div>
                  <p className="whitespace-pre-wrap font-mono text-sm leading-6 text-foreground">{prompt}</p>
                </div>
              ))
            )}
            {!allGeneratedPrompts.length && (
              <div className="flex min-h-[360px] flex-col items-center justify-center text-center">
                <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary"><Wand2 size={24} /></div>
                <h3 className="mt-4 text-lg font-black">No prompts generated yet.</h3>
                <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">Use a master idea or select local niches, then generate prompt variations for Seedance video jobs.</p>
              </div>
            )}
          </div>
        </Card>
      </section>
    </div>
  )
}

function History({ jobs, onRefresh }: { jobs: Job[]; onRefresh: () => void }) {
  const [clearing, setClearing] = useState(false)

  async function clearHistory() {
    if (!jobs.length) return
    if (!window.confirm("Clear all video history from the app? Generated MP4 files in Downloads will not be deleted.")) return
    setClearing(true)
    try {
      const result = await api.clearVideoHistory()
      toast.success(`Cleared ${result.deleted} history item${result.deleted === 1 ? "" : "s"}`)
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to clear history")
    } finally {
      setClearing(false)
    }
  }

  return (
    <div className="mx-auto max-w-[1760px] space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-2xl font-black">Video history</h2>
          <p className="mt-1 text-sm text-muted-foreground">All Seedance batches, artifacts, failures, and downloads.</p>
        </div>
        <Button className="bg-red-500/15 text-red-200 hover:bg-red-500/25" onClick={clearHistory} disabled={!jobs.length || clearing}>
          {clearing ? <Loader2 className="animate-spin" size={16} /> : <Trash2 size={16} />}
          Clear History
        </Button>
      </div>
      <JobTable jobs={jobs} />
    </div>
  )
}

function EngineTelemetry({ stats, state }: { stats: EngineTelemetryStats; state: "READY" | "RUNNING" | "BLOCKED" }) {
  const items: Array<{ key: keyof EngineTelemetryStats; label: string; tone: "default" | "green" | "red" | "amber" | "blue" | "pink" | "violet" }> = [
    { key: "total", label: "Total", tone: "default" },
    { key: "queued", label: "Queued", tone: "blue" },
    { key: "generating", label: "Generating", tone: "amber" },
    { key: "done", label: "Done", tone: "green" },
    { key: "failed", label: "Failed", tone: "red" },
    { key: "timeoutError", label: "Timeout Error", tone: "amber" },
    { key: "captchaBlock", label: "Captcha Block", tone: "red" },
    { key: "highDemand", label: "High Demand", tone: "amber" },
    { key: "dolaPolicy", label: "Dola Policy", tone: "pink" },
    { key: "noTextbox", label: "No Textbox", tone: "violet" },
    { key: "browserEc", label: "Browser Error", tone: "red" },
    { key: "noExport", label: "Download/Export Error", tone: "amber" },
  ]
  const stateClass = state === "READY" ? "text-emerald-300" : state === "RUNNING" ? "text-amber-300" : "text-red-300"
  const hasTelemetry = Object.values(stats).some((value) => value > 0)

  return (
    <Card className="bg-[#17172a] p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="text-[10px] font-black uppercase tracking-[0.32em] text-amber-200">Engine Telemetry</div>
          {!hasTelemetry && <div className="mt-1 text-[11px] font-semibold text-muted-foreground">No telemetry yet</div>}
        </div>
        <div className={`text-[10px] font-black uppercase tracking-wide ${stateClass}`}>- {state}</div>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
        {items.map((item) => (
          <TelemetryTile key={item.key} label={item.label} value={stats[item.key]} tone={item.tone} />
        ))}
      </div>
    </Card>
  )
}

function BrowserDiagnosticDetails({ diagnostic, browserUrl }: { diagnostic: Record<string, unknown>; browserUrl: string }) {
  const entries = visibleDiagnosticEntries(diagnostic)
  const screenshotFilename = typeof diagnostic.screenshot_filename === "string" ? diagnostic.screenshot_filename : ""
  const copyText = entries.map(([key, value]) => `${diagnosticLabel(key)}: ${formatDiagnosticValue(value)}`).join("\n")
  const logUrls = typeof diagnostic.log_urls === "object" && diagnostic.log_urls !== null ? diagnostic.log_urls as Record<string, string> : {}

  async function copyDiagnostics() {
    await navigator.clipboard.writeText(copyText)
    toast.success("Diagnostics copied")
  }

  return (
    <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_360px]">
      <div className="min-w-0 rounded-md border border-border bg-background/70 p-3">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Badge tone="error">{String(diagnostic.error_type || "BROWSER_ERROR")}</Badge>
          <Button variant="secondary" className="h-7 px-2 text-[11px]" onClick={copyDiagnostics}><Copy size={13} />Copy diagnostics</Button>
          <a href={browserUrl} target="_blank" rel="noreferrer" className="inline-flex h-7 items-center rounded-md bg-muted px-2 text-[11px] font-semibold text-foreground">Open browser</a>
          {Object.entries(logUrls).map(([name, url]) => (
            <a key={name} href={`${API_BASE}${url}`} target="_blank" rel="noreferrer" className="inline-flex h-7 items-center rounded-md bg-muted px-2 text-[11px] font-semibold text-foreground">{diagnosticLabel(name)}</a>
          ))}
        </div>
        <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-[150px_minmax(0,1fr)]">
          {entries.filter(([key]) => !["screenshot_filename", "screenshot_url", "captured_request", "log_urls"].includes(key)).map(([key, value]) => (
            <Fragment key={key}>
              <dt className="font-bold text-muted-foreground">{diagnosticLabel(key)}</dt>
              <dd className="min-w-0 whitespace-pre-wrap break-words font-mono text-foreground">{formatDiagnosticValue(value)}</dd>
            </Fragment>
          ))}
        </dl>
      </div>
      {screenshotFilename && (
        <a href={browserScreenshotUrl(screenshotFilename)} target="_blank" rel="noreferrer" className="block overflow-hidden rounded-md border border-border bg-black">
          <img src={browserScreenshotUrl(screenshotFilename)} alt="Dola browser failure" className="h-auto max-h-[320px] w-full object-contain" />
        </a>
      )}
    </div>
  )
}

function visibleDiagnosticEntries(diagnostic: Record<string, unknown>) {
  return Object.entries(diagnostic).filter(([key, value]) => !key.startsWith("_") && value !== null && value !== "" && (!Array.isArray(value) || value.length > 0))
}

function diagnosticLabel(key: string) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (character: string) => character.toUpperCase())
}

function formatDiagnosticValue(value: unknown) {
  if (Array.isArray(value)) return value.join(", ")
  if (typeof value === "object" && value !== null) return JSON.stringify(value, null, 2)
  return String(value)
}

function TelemetryTile({ label, value, tone: tileTone }: { label: string; value: number; tone: "default" | "green" | "red" | "amber" | "blue" | "pink" | "violet" }) {
  const dot = tileTone === "green" ? "bg-emerald-400" : tileTone === "red" ? "bg-red-400" : tileTone === "amber" ? "bg-amber-400" : tileTone === "blue" ? "bg-sky-400" : tileTone === "pink" ? "bg-pink-400" : tileTone === "violet" ? "bg-violet-400" : "bg-indigo-300"
  return (
    <div className="flex h-10 min-w-0 items-center justify-between gap-3 rounded-md border border-border bg-[#10111d] px-3">
      <div className="flex min-w-0 items-center gap-2">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
        <span className="truncate text-xs font-semibold text-muted-foreground">{label}</span>
      </div>
      <span className="shrink-0 text-sm font-black text-foreground">{value}</span>
    </div>
  )
}

function SectionTitle({ icon, title, badge }: { icon: ReactNode; title: string; badge?: string }) {
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className="text-primary">{icon}</span>
      <h2 className="truncate text-sm font-black uppercase tracking-wide">{title}</h2>
      {badge && <span className="rounded-full bg-primary/15 px-2 py-0.5 text-[10px] font-black uppercase tracking-wide text-primary">{badge}</span>}
    </div>
  )
}

function Field({ label, className, children }: { label: string; className?: string; children: ReactNode }) {
  return (
    <label className={className}>
      <span className="mb-1.5 block text-[11px] font-black uppercase tracking-wide text-muted-foreground">{label}</span>
      {children}
    </label>
  )
}

function MiniAction({ children, danger, onClick }: { children: ReactNode; danger?: boolean; onClick?: () => void }) {
  return <button onClick={onClick} className={`inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 text-[11px] font-black uppercase tracking-wide ${danger ? "bg-red-500/10 text-red-300" : "bg-muted/60 text-primary"}`}>{children}</button>
}

function IconButton({ label, children, onClick }: { label: string; children: ReactNode; onClick?: () => void }) {
  return <button aria-label={label} title={label} onClick={onClick} className="inline-flex h-9 w-9 items-center justify-center rounded-md bg-muted/60 text-muted-foreground transition hover:bg-muted hover:text-foreground">{children}</button>
}

function promptLines(text: string) {
  return text.split("\n").map((line) => line.trim()).filter(Boolean)
}

function buildProgressivePromptRequest(masterIdea: string, previousPrompts: string[]) {
  if (!previousPrompts.length) return masterIdea
  return [
    masterIdea,
    "",
    "Already generated prompts. Do not repeat these ideas, wording, scene setups, camera moves, or subject actions:",
    ...previousPrompts.slice(-40).map((prompt, index) => `${index + 1}. ${prompt}`),
  ].join("\n")
}

function splitPromptCount(total: number, parts: number) {
  if (parts < 1) return []
  const counts = Array.from({ length: parts }, () => Math.floor(total / parts))
  const indexes = Array.from({ length: parts }, (_, index) => index)
  for (let index = indexes.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(Math.random() * (index + 1))
    ;[indexes[index], indexes[swapIndex]] = [indexes[swapIndex], indexes[index]]
  }
  for (const index of indexes.slice(0, total % parts)) {
    counts[index] += 1
  }
  return counts
}

function upsertPromptGroup(groups: NichePromptGroup[], next: NichePromptGroup) {
  const index = groups.findIndex((group) => group.niche_id === next.niche_id)
  if (index === -1) return [...groups, next]
  const copy = [...groups]
  copy[index] = next
  return copy
}

function collectVideoArtifacts(jobs: Job[]): VideoArtifact[] {
  return jobs.flatMap((job) =>
    job.artifacts
      .filter((artifact) => artifact.kind === "video" && artifact.mime_type === "video/mp4")
      .map((artifact) => ({ artifact, job })),
  )
}

function stableJobItems(items: JobItem[]): JobItem[] {
  return [...items].sort((left, right) => {
    const created = safeTimestamp(left.created_at) - safeTimestamp(right.created_at)
    if (created !== 0) return created
    return left.id.localeCompare(right.id)
  })
}

function safeTimestamp(value: string | undefined): number {
  const timestamp = value ? new Date(value).getTime() : 0
  return Number.isFinite(timestamp) ? timestamp : 0
}

function compareJobsOldestFirst(left: Job, right: Job): number {
  const created = safeTimestamp(left.created_at) - safeTimestamp(right.created_at)
  return created || left.id.localeCompare(right.id)
}

function compareQueueRows(left: QueueRow, right: QueueRow): number {
  const jobCreated = safeTimestamp(left.jobCreatedAt) - safeTimestamp(right.jobCreatedAt)
  if (jobCreated !== 0) return jobCreated
  const itemCreated = safeTimestamp(left.created_at) - safeTimestamp(right.created_at)
  return itemCreated || left.id.localeCompare(right.id)
}

function activeJobOutputLabel(job?: Job | null): string {
  const folder = String(job?.config_json?.job_output_folder_name || "")
  return folder ? `${HOST_OUTPUT_LABEL}/${folder}` : ""
}

function activeJobOutputContainer(job?: Job | null): string {
  return String(job?.config_json?.job_output_folder || "")
}

function hasSavedBrowserSnapshot(snapshots: Array<Record<string, unknown>>, itemId: string): boolean {
  return snapshots.some((snapshot) =>
    snapshot.source === "browser"
    && snapshot.item_id === itemId
    && Boolean(snapshot.conversation_type),
  )
}

function buildEngineTelemetry(jobs: Job[], logs: LogRow[]): EngineTelemetryStats {
  const items = stableJobItems(jobs.flatMap((job) => job.items))
  const stats: EngineTelemetryStats = {
    total: items.length,
    queued: items.filter((item) => item.status === "queued").length,
    generating: items.filter((item) => item.status === "running").length,
    done: items.filter((item) => item.status === "completed").length,
    failed: items.filter((item) => item.status === "failed").length,
    timeoutError: 0,
    captchaBlock: 0,
    highDemand: 0,
    dolaPolicy: 0,
    noTextbox: 0,
    browserEc: 0,
    noExport: 0,
  }
  const counted = new Set<string>()
  for (const item of items) {
    const bucket = classifyTelemetryText(`${item.error || ""} ${item.action || ""}`)
    if (bucket) {
      stats[bucket] += 1
      counted.add(`${item.id}:${bucket}`)
    }
  }
  const itemByShortId = new Map(items.map((item) => [item.id.slice(0, 8), item]))
  for (const row of logs) {
    const message = row.message || ""
    const bucket = classifyTelemetryText(message)
    if (!bucket) continue
    const shortId = message.match(/\| ([a-f0-9]{8})\]/i)?.[1]
    if (shortId) {
      const item = itemByShortId.get(shortId)
      if (item) {
        const key = `${item.id}:${bucket}`
        if (counted.has(key)) continue
        counted.add(key)
        stats[bucket] += 1
        continue
      }
    }
    const logKey = `${row.id}:${bucket}`
    if (!counted.has(logKey)) {
      counted.add(logKey)
      stats[bucket] += 1
    }
  }
  for (const item of items) {
    if (item.status !== "failed" || item.artifact_id) continue
    const text = `${item.error || ""} ${item.action || ""}`.toLowerCase()
    if (text.includes("download") || text.includes("export") || text.includes("mp4") || !classifyTelemetryText(text)) {
      stats.noExport += 1
    }
  }
  return stats
}

function classifyTelemetryText(value: string): keyof Pick<EngineTelemetryStats, "timeoutError" | "captchaBlock" | "highDemand" | "dolaPolicy" | "noTextbox" | "browserEc" | "noExport"> | null {
  const text = value.toLowerCase()
  if (!text.trim()) return null
  if (text.includes("captcha") || text.includes("manual verification") || text.includes("requires manual verification")) return "captchaBlock"
  if (text.includes("chat input not found") || text.includes("textbox")) return "noTextbox"
  if (text.includes("econnrefused") || text.includes("connect_over_cdp") || text.includes("browser manager") || text.includes("cdp") || text.includes("net::")) return "browserEc"
  if (text.includes("high demand") || text.includes("710022002")) return "highDemand"
  if (text.includes("policy") || text.includes("violate") || text.includes("content") || text.includes("flagged") || text.includes("rejected this prompt")) return "dolaPolicy"
  if (text.includes("timeout") || text.includes("timed out") || text.includes("did not return") || text.includes("no video id") || text.includes("no play_info")) return "timeoutError"
  if (text.includes("download") || text.includes("export") || text.includes("mp4")) return "noExport"
  return null
}

function tone(status: string): "default" | "success" | "warn" | "error" | "muted" {
  if (status === "completed") return "success"
  if (status === "failed") return "error"
  if (status === "running") return "default"
  if (status === "cancelled") return "warn"
  return "muted"
}

function formatBytes(bytes: number) {
  if (!bytes) return "0 B"
  const units = ["B", "KB", "MB", "GB"]
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}
