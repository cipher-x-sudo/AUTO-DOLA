import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react"
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
import { api, artifactUrl, subscribeJobEvents } from "@/lib/api"
import type { Artifact, Job, JobItem, SettingsPayload } from "@/lib/types"
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
  default_duration: 15,
  default_parallel: 5,
  output_dir: "",
  proxy_enabled: false,
  proxy_url: "",
  tts_default_voice: "en-US-AriaNeural",
}

type Page = "video" | "prompts" | "gallery" | "history"

const pageRoutes: Record<Page, string> = {
  video: "/video",
  prompts: "/prompt-generator",
  gallery: "/gallery",
  history: "/history",
}

function pageFromPath(pathname: string): Page {
  if (pathname === "/prompt-generator" || pathname === "/prompts") return "prompts"
  if (pathname === "/gallery") return "gallery"
  if (pathname === "/history") return "history"
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

export default function App() {
  const [page, setPage] = useState<Page>(() => pageFromPath(window.location.pathname))
  const [jobs, setJobs] = useState<Job[]>([])
  const [settings, setSettings] = useState<SettingsPayload>(emptySettings)
  const [logs, setLogs] = useState<LogRow[]>([])
  const [loading, setLoading] = useState(true)
  const [studioPromptText, setStudioPromptText] = useState("")

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

  const refresh = useCallback(async () => {
    try {
      const [videoJobs, appSettings, logRows] = await Promise.all([api.videoJobs(), api.settings(), api.logs()])
      setJobs(videoJobs)
      setSettings(appSettings)
      setLogs(logRows)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to refresh")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 5000)
    return () => clearInterval(id)
  }, [refresh])

  const runningJobIds = useMemo(() => jobs.filter((job) => job.status === "running" || job.status === "queued").map((job) => job.id).join(","), [jobs])

  useEffect(() => {
    const activeJobs = jobs.filter((job) => job.status === "running" || job.status === "queued")
    if (!activeJobs.length) return
    const cleanups = activeJobs.map((job) => subscribeJobEvents(job.id, () => refresh(), () => undefined))
    return () => cleanups.forEach((cleanup) => cleanup())
  }, [jobs, refresh, runningJobIds])

  const activeJob = useMemo(() => jobs.find((job) => job.status === "running" || job.status === "queued") ?? jobs[0], [jobs])
  const recentLogs = useMemo(
    () =>
      logs
        .filter((row) => !activeJob || row.job_id === activeJob.id)
        .filter(isStudioLogVisible)
        .slice(0, 16),
    [activeJob, logs],
  )
  const videos = useMemo(() => collectVideoArtifacts(jobs), [jobs])

  return (
    <Layout page={page} setPage={(next) => navigate(next as Page)} loading={loading}>
      {page === "video" && (
        <VideoConsole
          settings={settings}
          jobs={jobs}
          activeJob={activeJob}
          logs={recentLogs}
          promptText={studioPromptText}
          setPromptText={setStudioPromptText}
          onRefresh={refresh}
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
    </Layout>
  )
}

function VideoConsole({
  settings,
  jobs,
  activeJob,
  logs,
  promptText,
  setPromptText,
  onRefresh,
}: {
  settings: SettingsPayload
  jobs: Job[]
  activeJob?: Job
  logs: LogRow[]
  promptText: string
  setPromptText: (value: string) => void
  onRefresh: () => void
}) {
  const [ratio, setRatio] = useState(settings.default_ratio || "9:16")
  const [duration, setDuration] = useState(settings.default_duration || 15)
  const [parallel, setParallel] = useState(settings.default_parallel || 30)
  const [cleanWatermark, setCleanWatermark] = useState(true)
  const [saveMode, setSaveMode] = useState("final")
  const [submitting, setSubmitting] = useState(false)
  const [logSearch, setLogSearch] = useState("")

  useEffect(() => {
    setRatio((current) => current || settings.default_ratio || "9:16")
    setDuration((current) => current || settings.default_duration || 15)
    setParallel((current) => current || settings.default_parallel || 30)
  }, [settings])

  const stats = useMemo(() => {
    const items = jobs.flatMap((job) => job.items)
    return {
      total: items.length,
      queued: items.filter((item) => item.status === "queued").length,
      generating: items.filter((item) => item.status === "running").length,
      done: items.filter((item) => item.status === "completed").length,
      failed: items.filter((item) => item.status === "failed").length,
      videos: collectVideoArtifacts(jobs).length,
    }
  }, [jobs])

  const progressTotal = activeJob?.total || stats.total || 0
  const progressDone = activeJob ? activeJob.done + activeJob.failed : stats.done + stats.failed
  const progress = progressTotal ? Math.round((progressDone / progressTotal) * 100) : 0
  const queueItems = activeJob?.items ?? jobs.flatMap((job) => job.items)
  const filteredLogs = logs
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
    if (!activeJob || !["queued", "running"].includes(activeJob.status)) return
    try {
      await api.cancelVideoJob(activeJob.id)
      toast.success("Stop requested")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to stop job")
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

      <OutputLocation path={HOST_OUTPUT_LABEL} containerPath={DOCKER_OUTPUT_DIR} />

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatBox label="Total" value={stats.total} />
        <StatBox label="Queued" value={stats.queued} tone="amber" />
        <StatBox label="Generating" value={stats.generating} tone="blue" />
        <StatBox label="Done" value={stats.done} tone="green" />
        <StatBox label="Failed" value={stats.failed} tone="red" />
        <StatBox label="Videos" value={stats.videos} tone="amber" />
      </section>

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
            submitting={submitting}
            hasActiveJob={!!activeJob && ["queued", "running"].includes(activeJob.status)}
            onStart={submit}
            onStop={stopGeneration}
          />
          <GenerationQueue items={queueItems} />
        </div>

        <div className="space-y-4">
          <PromptsPanel promptText={promptText} setPromptText={setPromptText} count={promptLines(promptText).length} />
          <LiveLogConsole logs={filteredLogs} search={logSearch} setSearch={setLogSearch} />
        </div>
      </section>

    </div>
  )
}

function OutputLocation({ path, containerPath }: { path: string; containerPath: string }) {
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
              New MP4s are written directly by Docker to the host Downloads folder. Container path: {containerPath}
            </div>
          </div>
        </div>
      </div>
    </Card>
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
  submitting,
  hasActiveJob,
  onStart,
  onStop,
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
  submitting: boolean
  hasActiveJob: boolean
  onStart: () => void
  onStop: () => void
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
            <option value="30">30</option>
            <option value="60">60</option>
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
        <Field label="Save output" className="sm:col-span-2">
          <Select value={saveMode} onChange={(event) => setSaveMode(event.target.value)}>
            <option value="final">Final only, raw only if cleanup fails</option>
            <option value="raw">Raw only</option>
            <option value="both">Both raw and final</option>
          </Select>
        </Field>
      </div>
      <Button className="mt-5 h-12 w-full bg-[#ff225c] text-white hover:bg-[#ff3b6f]" onClick={hasActiveJob ? onStop : onStart} disabled={submitting}>
        {submitting ? <Loader2 className="animate-spin" size={17} /> : hasActiveJob ? <Square size={15} /> : <Play size={16} />}
        {hasActiveJob ? "Stop Generation" : "Start Generation"}
      </Button>
    </Card>
  )
}

function PromptsPanel({ promptText, setPromptText, count }: { promptText: string; setPromptText: (value: string) => void; count: number }) {
  return (
    <Card className="p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <SectionTitle icon={<FileText size={15} />} title="Prompts" badge={`${count} prompt${count === 1 ? "" : "s"}`} />
        <div className="flex flex-wrap gap-2">
          <MiniAction><Upload size={13} />TXT</MiniAction>
          <MiniAction><Upload size={13} />CSV</MiniAction>
          <MiniAction danger onClick={() => setPromptText("")}><Trash2 size={13} />Clear</MiniAction>
        </div>
      </div>
      <Textarea
        className="mt-4 min-h-[145px] resize-y border-border/80 bg-[#11121d] font-mono text-sm"
        value={promptText}
        onChange={(event) => setPromptText(event.target.value)}
        placeholder="One prompt per line..."
      />
      <p className="mt-3 text-xs font-semibold text-muted-foreground">One prompt per line = one video. Import controls are visual placeholders for the next pass.</p>
    </Card>
  )
}

function GenerationQueue({ items }: { items: JobItem[] }) {
  const [page, setPage] = useState(1)
  const pageCount = Math.max(1, Math.ceil(items.length / QUEUE_PAGE_SIZE))
  const safePage = Math.min(page, pageCount)
  const start = (safePage - 1) * QUEUE_PAGE_SIZE
  const pageItems = items.slice(start, start + QUEUE_PAGE_SIZE)

  useEffect(() => {
    setPage((current) => Math.min(current, Math.max(1, Math.ceil(items.length / QUEUE_PAGE_SIZE))))
  }, [items.length])

  return (
    <Card className="p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <SectionTitle icon={<Zap size={15} />} title="Generation Queue" badge={items.length ? `${items.length} total` : undefined} />
        {items.length > QUEUE_PAGE_SIZE && (
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
            {pageItems.map((item, index) => (
              <tr key={item.id} className="border-t border-border">
                <td className="px-3 py-3 text-muted-foreground">{start + index + 1}</td>
                <td className="max-w-[260px] truncate px-3 py-3 font-bold">{item.prompt}</td>
                <td className="px-3 py-3"><Badge tone={tone(item.status)}>{item.status}</Badge></td>
                <td className="max-w-[240px] truncate px-3 py-3 text-muted-foreground">{item.error || item.action || "Waiting..."}</td>
              </tr>
            ))}
            {!items.length && <tr><td className="px-3 py-5 text-center text-muted-foreground" colSpan={4}>No queued videos yet.</td></tr>}
          </tbody>
        </table>
      </div>
      {items.length > QUEUE_PAGE_SIZE && (
        <div className="mt-3 text-right text-xs font-semibold text-muted-foreground">
          Showing {start + 1}-{Math.min(start + QUEUE_PAGE_SIZE, items.length)} of {items.length}
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
      <div className="h-[235px] overflow-auto bg-black p-4 font-mono text-xs leading-7 text-cyan-300 shadow-inner shadow-primary/10">
        {logs.map((row, index) => (
          <div key={row.id} className="grid grid-cols-[42px_82px_42px_minmax(0,1fr)] gap-2">
            <span className="text-slate-500">{String(index + 1).padStart(3, "0")}</span>
            <span className="text-slate-500">[{new Date(row.created_at).toLocaleTimeString()}]</span>
            <span className={row.level === "error" ? "text-red-300" : row.level === "warn" ? "text-amber-300" : "text-emerald-300"}>{row.level.toUpperCase()}</span>
            <span className="break-words">{row.message}</span>
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
  const [idea, setIdea] = useState("")
  const [count, setCount] = useState(5)
  const [duration, setDuration] = useState(15)
  const [apiKey, setApiKey] = useState(settings.gemini_api_key || "")
  const [baseUrl, setBaseUrl] = useState(settings.gemini_base_url || "https://generativelanguage.googleapis.com/v1beta")
  const [model, setModel] = useState(settings.gemini_model || "gemini-2.5-flash")
  const [generated, setGenerated] = useState<string[]>([])
  const [generating, setGenerating] = useState(false)
  const [generationTarget, setGenerationTarget] = useState(0)
  const [savingSettings, setSavingSettings] = useState(false)
  const [copied, setCopied] = useState<number | "all" | null>(null)

  const generatedText = generated.join("\n")
  const generationProgress = generationTarget ? Math.min(100, Math.round((generated.length / generationTarget) * 100)) : 0

  useEffect(() => {
    setApiKey(settings.gemini_api_key || "")
    setBaseUrl(settings.gemini_base_url || "https://generativelanguage.googleapis.com/v1beta")
    setModel(settings.gemini_model || "gemini-2.5-flash")
  }, [settings])

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
    setGenerationTarget(count)
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
    }
  }

  async function copyText(text: string, index: number | "all") {
    await navigator.clipboard.writeText(text)
    setCopied(index)
    setTimeout(() => setCopied(null), 1400)
  }

  function downloadPrompts() {
    if (!generated.length) return
    const blob = new Blob([generated.map((prompt, index) => `${index + 1}. ${prompt}`).join("\n\n")], { type: "text/plain" })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = "auto-dola-seedance-prompts.txt"
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
  }

  return (
    <div className="mx-auto max-w-[1760px] space-y-5">
      <div className="flex flex-col gap-3 border-b border-border/70 pb-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <h1 className="text-xl font-black tracking-tight sm:text-2xl">Prompt Generator</h1>
          <p className="mt-1 text-xs font-semibold text-muted-foreground">Create polished Seedance-ready video prompts from one master idea.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={() => copyText(generatedText, "all")} disabled={!generated.length}>
            {copied === "all" ? <Check size={16} /> : <Copy size={16} />}
            Copy All
          </Button>
          <Button variant="secondary" onClick={downloadPrompts} disabled={!generated.length}>
            <Download size={16} />
            Download TXT
          </Button>
          <Button onClick={() => onUsePrompts(generatedText)} disabled={!generated.length}>
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
            <Field label="Master idea">
              <Textarea
                className="min-h-[160px] resize-y border-border/80 bg-[#11121d] font-mono text-sm"
                value={idea}
                onChange={(event) => setIdea(event.target.value)}
                placeholder="Example: luxury sports car racing through neon rain at night"
              />
            </Field>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Prompt count">
                <Input type="number" min={1} value={count} onChange={(event) => setCount(Math.max(1, Number(event.target.value)))} />
              </Field>
              <Field label="Duration">
                <Select value={String(duration)} onChange={(event) => setDuration(Number(event.target.value))}>
                  <option value="5">5</option>
                  <option value="10">10</option>
                  <option value="15">15</option>
                  <option value="30">30</option>
                  <option value="60">60</option>
                </Select>
              </Field>
            </div>
            <Button className="h-12 w-full bg-[#ff225c] text-white hover:bg-[#ff3b6f]" onClick={generate} disabled={generating}>
              {generating ? <Loader2 className="animate-spin" size={17} /> : <Wand2 size={17} />}
              {generating ? `Generating ${generated.length} / ${generationTarget}` : "Generate Prompts"}
            </Button>
            {generating && (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-[11px] font-black uppercase tracking-wide text-muted-foreground">
                  <span>Live prompt batches</span>
                  <span>{generationProgress}%</span>
                </div>
                <Progress value={generationProgress} />
              </div>
            )}
          </div>
        </Card>

        <Card className="overflow-hidden">
          <div className="flex flex-col gap-3 border-b border-border bg-[#0d0f1c] p-4 sm:flex-row sm:items-center sm:justify-between">
            <SectionTitle icon={<FileText size={15} />} title="Generated Prompts" badge={generating ? `${generated.length}/${generationTarget} ready` : `${generated.length} ready`} />
            <div className="text-xs font-semibold text-muted-foreground">{generating ? `Showing each completed batch of ${PROMPT_UI_BATCH_SIZE} as soon as it returns.` : "One prompt per line can be sent directly to Video Studio."}</div>
          </div>
          <div className="max-h-[650px] space-y-3 overflow-auto p-4">
            {generated.map((prompt, index) => (
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
            ))}
            {!generated.length && (
              <div className="flex min-h-[360px] flex-col items-center justify-center text-center">
                <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary"><Wand2 size={24} /></div>
                <h3 className="mt-4 text-lg font-black">No prompts generated yet.</h3>
                <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">Enter a master idea and generate prompt variations for Seedance video jobs.</p>
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

function StatBox({ label, value, tone: statTone = "default" }: { label: string; value: number; tone?: "default" | "green" | "red" | "amber" | "blue" }) {
  const color = statTone === "green" ? "text-emerald-300" : statTone === "red" ? "text-[#ff225c]" : statTone === "amber" ? "text-amber-300" : statTone === "blue" ? "text-indigo-300" : "text-foreground"
  return (
    <Card className="flex min-h-[70px] flex-col items-center justify-center p-3">
      <div className={`text-2xl font-black ${color}`}>{value}</div>
      <div className="mt-1 text-[10px] font-black uppercase tracking-[0.16em] text-muted-foreground">{label}</div>
    </Card>
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

function collectVideoArtifacts(jobs: Job[]): VideoArtifact[] {
  return jobs.flatMap((job) =>
    job.artifacts
      .filter((artifact) => artifact.kind === "video" && artifact.mime_type === "video/mp4")
      .map((artifact) => ({ artifact, job })),
  )
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
