import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react"
import {
  AlertCircle,
  CheckCircle2,
  Download,
  FileVideo,
  FolderOpen,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Square,
  Trash2,
  WandSparkles,
} from "lucide-react"
import { toast } from "sonner"
import { api, artifactUrl, subscribeJobEvents } from "@/lib/api"
import type { Artifact, Job, JobItem, SettingsPayload } from "@/lib/types"
import { Layout } from "@/components/Layout"
import { JobTable } from "@/components/JobTable"
import { Badge, Button, Card, Input, Progress, Select, Textarea } from "@/components/ui"

const emptySettings: SettingsPayload = {
  dola_auth_cookies: "",
  yousmind_api_key: "",
  default_ratio: "9:16",
  default_duration: 15,
  default_parallel: 5,
  output_dir: "",
  proxy_enabled: false,
  proxy_url: "",
  tts_default_voice: "en-US-AriaNeural",
}

type Page = "video" | "history" | "logs" | "settings"

interface LogRow {
  id: string
  level: string
  message: string
  created_at: string
  job_id?: string | null
}

interface PromptRow {
  id: string
  title: string
  prompt: string
}

const starterRows: PromptRow[] = [
  {
    id: crypto.randomUUID(),
    title: "Neon sneaker reel",
    prompt: "A cinematic vertical product video of a neon sneaker rotating on wet black glass, dramatic rim light, premium ad style",
  },
  {
    id: crypto.randomUUID(),
    title: "Rainy Tokyo travel",
    prompt: "A vertical travel reel of a rainy Tokyo street at night, reflections, slow camera push, realistic people and neon signs",
  },
]

export default function App() {
  const [page, setPage] = useState<Page>("video")
  const [jobs, setJobs] = useState<Job[]>([])
  const [settings, setSettings] = useState<SettingsPayload>(emptySettings)
  const [logs, setLogs] = useState<LogRow[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    document.documentElement.classList.add("dark")
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
    const cleanups = activeJobs.map((job) =>
      subscribeJobEvents(job.id, () => refresh(), () => undefined),
    )
    return () => cleanups.forEach((cleanup) => cleanup())
  }, [jobs, refresh, runningJobIds])

  const activeJob = useMemo(() => jobs.find((job) => job.status === "running" || job.status === "queued") ?? jobs[0], [jobs])
  const recentLogs = useMemo(() => logs.filter((row) => !activeJob || row.job_id === activeJob.id).slice(0, 8), [activeJob, logs])
  const videos = useMemo(() => collectVideoArtifacts(jobs), [jobs])

  return (
    <Layout page={page} setPage={(next) => setPage(next as Page)} loading={loading}>
      {page === "video" && <VideoStudio settings={settings} jobs={jobs} activeJob={activeJob} videos={videos} logs={recentLogs} onRefresh={refresh} />}
      {page === "history" && <History jobs={jobs} />}
      {page === "logs" && <Logs rows={logs} />}
      {page === "settings" && <SettingsPage settings={settings} setSettings={setSettings} />}
    </Layout>
  )
}

function VideoStudio({
  settings,
  jobs,
  activeJob,
  videos,
  logs,
  onRefresh,
}: {
  settings: SettingsPayload
  jobs: Job[]
  activeJob?: Job
  videos: VideoArtifact[]
  logs: LogRow[]
  onRefresh: () => void
}) {
  const [rows, setRows] = useState<PromptRow[]>(starterRows)
  const [ratio, setRatio] = useState(settings.default_ratio || "9:16")
  const [duration, setDuration] = useState(settings.default_duration || 15)
  const [parallel, setParallel] = useState(settings.default_parallel || 5)
  const [saveFolder, setSaveFolder] = useState(settings.output_dir || "")
  const [cleanWatermark, setCleanWatermark] = useState(true)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    setRatio((current) => current || settings.default_ratio || "9:16")
    setDuration((current) => current || settings.default_duration || 15)
    setParallel((current) => current || settings.default_parallel || 5)
    setSaveFolder((current) => current || settings.output_dir || "")
  }, [settings])

  const stats = useMemo(() => {
    const running = jobs.filter((job) => job.status === "running" || job.status === "queued").length
    const completed = jobs.filter((job) => job.status === "completed").length
    const failed = jobs.filter((job) => job.status === "failed").length
    const items = jobs.flatMap((job) => job.items)
    return { running, completed, failed, totalItems: items.length, videos: videos.length }
  }, [jobs, videos])

  const failedItems = useMemo(() => jobs.flatMap((job) => job.items.map((item) => ({ job, item }))).filter(({ item }) => item.status === "failed").slice(0, 6), [jobs])

  async function submit() {
    const prompts = rows
      .map((row) => ({ title: row.title.trim() || row.prompt.trim().slice(0, 70), prompt: row.prompt.trim() }))
      .filter((row) => row.prompt)
    if (!prompts.length) {
      toast.error("Add at least one prompt.")
      return
    }
    setSubmitting(true)
    try {
      await api.createVideoJob({ prompts, ratio, duration, parallel, save_folder: saveFolder, clean_watermark: cleanWatermark })
      toast.success("Video job queued")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to queue video job")
    } finally {
      setSubmitting(false)
    }
  }

  async function cancelActive() {
    if (!activeJob || !["queued", "running"].includes(activeJob.status)) return
    try {
      await api.cancelVideoJob(activeJob.id)
      toast.success("Cancellation requested")
      onRefresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to cancel job")
    }
  }

  return (
    <div className="space-y-5">
      <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <Metric label="Active jobs" value={stats.running} />
        <Metric label="Completed jobs" value={stats.completed} />
        <Metric label="Failed jobs" value={stats.failed} tone="error" />
        <Metric label="Prompt items" value={stats.totalItems} />
        <Metric label="Videos ready" value={stats.videos} tone="success" />
      </section>

      <section className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_390px]">
        <Card className="overflow-hidden">
          <div className="border-b border-border/70 p-4 sm:p-5">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div>
                <div className="flex items-center gap-2 text-sm font-bold uppercase tracking-wide text-primary">
                  <WandSparkles size={16} />
                  Seedance Video Studio
                </div>
                <h2 className="mt-2 text-2xl font-black tracking-tight sm:text-3xl">Generate Dola videos in bulk</h2>
                <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">
                  Paste prompts, tune video settings, and watch each item move through Dola submission, polling, download, and cleanup.
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="secondary" onClick={onRefresh}><RefreshCw size={16} />Refresh</Button>
                <Button variant="destructive" onClick={cancelActive} disabled={!activeJob || !["queued", "running"].includes(activeJob.status)}><Square size={15} />Cancel</Button>
              </div>
            </div>
          </div>

          <div className="space-y-4 p-4 sm:p-5">
            <PromptEditor rows={rows} setRows={setRows} />
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-5">
              <Field label="Ratio">
                <Select value={ratio} onChange={(event) => setRatio(event.target.value)}>
                  <option>9:16</option>
                  <option>16:9</option>
                  <option>1:1</option>
                </Select>
              </Field>
              <Field label="Duration">
                <Input type="number" min={5} max={60} value={duration} onChange={(event) => setDuration(Number(event.target.value))} />
              </Field>
              <Field label="Parallel">
                <Input type="number" min={1} max={50} value={parallel} onChange={(event) => setParallel(Number(event.target.value))} />
              </Field>
              <Field label="Output folder">
                <Input placeholder="Docker output volume" value={saveFolder} onChange={(event) => setSaveFolder(event.target.value)} />
              </Field>
              <label className="flex min-h-[68px] items-center gap-3 rounded-md border border-border bg-background/70 px-3 py-2 text-sm font-semibold">
                <input type="checkbox" checked={cleanWatermark} onChange={(event) => setCleanWatermark(event.target.checked)} className="h-4 w-4 accent-[hsl(var(--primary))]" />
                Clean watermark
              </label>
            </div>
          </div>

          <div className="sticky bottom-0 flex flex-col gap-3 border-t border-border bg-card/95 p-4 backdrop-blur sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm text-muted-foreground">{rows.filter((row) => row.prompt.trim()).length} prompt(s) ready for generation</div>
            <Button className="h-11 gap-2 px-5" onClick={submit} disabled={submitting}>
              {submitting ? <Loader2 className="animate-spin" size={17} /> : <Play size={17} />}
              Start generation
            </Button>
          </div>
        </Card>

        <ActiveJobPanel job={activeJob} logs={logs} failedItems={failedItems} />
      </section>

      <VideoGallery videos={videos} failedItems={failedItems} />
    </div>
  )
}

function PromptEditor({ rows, setRows }: { rows: PromptRow[]; setRows: (rows: PromptRow[]) => void }) {
  function update(id: string, patch: Partial<PromptRow>) {
    setRows(rows.map((row) => (row.id === id ? { ...row, ...patch } : row)))
  }

  function remove(id: string) {
    setRows(rows.length === 1 ? [{ id: crypto.randomUUID(), title: "", prompt: "" }] : rows.filter((row) => row.id !== id))
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-black uppercase tracking-wide text-muted-foreground">Prompt queue</h3>
        <Button variant="secondary" onClick={() => setRows([...rows, { id: crypto.randomUUID(), title: "", prompt: "" }])}><Plus size={16} />Add row</Button>
      </div>
      <div className="space-y-3">
        {rows.map((row, index) => (
          <div key={row.id} className="grid grid-cols-1 gap-2 rounded-md border border-border bg-background/60 p-3 lg:grid-cols-[190px_minmax(0,1fr)_42px]">
            <Input aria-label={`Title ${index + 1}`} placeholder="Title" value={row.title} onChange={(event) => update(row.id, { title: event.target.value })} />
            <Textarea aria-label={`Prompt ${index + 1}`} className="min-h-[82px] resize-y" placeholder="Describe the video..." value={row.prompt} onChange={(event) => update(row.id, { prompt: event.target.value })} />
            <button aria-label="Remove prompt" onClick={() => remove(row.id)} className="flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground">
              <Trash2 size={17} />
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

function ActiveJobPanel({ job, logs, failedItems }: { job?: Job; logs: LogRow[]; failedItems: Array<{ job: Job; item: JobItem }> }) {
  const progress = job?.total ? Math.round(((job.done + job.failed) / job.total) * 100) : 0
  return (
    <aside className="space-y-4">
      <Card className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="text-lg font-black">Live job</h3>
            <p className="mt-1 text-sm text-muted-foreground">{job ? job.title : "No active job yet"}</p>
          </div>
          {job && <Badge tone={tone(job.status)}>{job.status}</Badge>}
        </div>
        <div className="mt-4">
          <Progress value={progress} />
          <div className="mt-2 flex items-center justify-between text-xs text-muted-foreground">
            <span>{job ? `${job.done}/${job.total} completed` : "Waiting for a job"}</span>
            <span>{progress}%</span>
          </div>
        </div>
        <div className="mt-4 space-y-2">
          {(job?.items ?? []).slice(0, 8).map((item) => (
            <div key={item.id} className="rounded-md border border-border bg-background/70 p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-bold">{item.title || item.prompt}</div>
                  <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">{item.action || item.prompt}</div>
                </div>
                <Badge tone={tone(item.status)}>{item.status}</Badge>
              </div>
              {item.error && <div className="mt-2 rounded-md bg-red-500/10 p-2 text-xs font-semibold text-red-300">{item.error}</div>}
            </div>
          ))}
          {!job?.items.length && <EmptySmall text="Start a video job to see item-level progress here." />}
        </div>
      </Card>

      <Card className="p-4">
        <h3 className="text-lg font-black">Recent logs</h3>
        <div className="mt-3 max-h-[290px] space-y-2 overflow-auto pr-1">
          {logs.map((row) => (
            <div key={row.id} className="rounded-md bg-background/70 p-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <Badge tone={row.level === "error" ? "error" : row.level === "warn" ? "warn" : row.level === "success" ? "success" : "muted"}>{row.level}</Badge>
                <span className="text-xs text-muted-foreground">{new Date(row.created_at).toLocaleTimeString()}</span>
              </div>
              <div className="mt-2 break-words font-medium">{row.message}</div>
            </div>
          ))}
          {!logs.length && <EmptySmall text="No logs for the selected job yet." />}
        </div>
      </Card>

      {!!failedItems.length && (
        <Card className="border-red-500/30 p-4">
          <div className="flex items-center gap-2 text-red-300"><AlertCircle size={17} /><h3 className="font-black">Recent failures</h3></div>
          <div className="mt-3 space-y-2">
            {failedItems.slice(0, 3).map(({ item }) => (
              <div key={item.id} className="rounded-md bg-red-500/10 p-3 text-sm">
                <div className="font-bold">{item.title || item.prompt}</div>
                <div className="mt-1 break-words text-xs text-red-200">{item.error || "Failed without an error message."}</div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </aside>
  )
}

interface VideoArtifact {
  artifact: Artifact
  job: Job
}

function VideoGallery({ videos, failedItems }: { videos: VideoArtifact[]; failedItems: Array<{ job: Job; item: JobItem }> }) {
  return (
    <section className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm font-bold uppercase tracking-wide text-primary"><FileVideo size={16} />Outputs</div>
          <h2 className="mt-1 text-2xl font-black">Generated videos</h2>
        </div>
        <div className="text-sm text-muted-foreground">{videos.length} playable video artifact(s)</div>
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
                    <p className="mt-1 text-xs text-muted-foreground">{job.title} • {formatBytes(artifact.size_bytes)} • {new Date(artifact.created_at).toLocaleString()}</p>
                  </div>
                  <Badge tone={tone(job.status)}>{job.status}</Badge>
                </div>
                <div className="flex flex-wrap gap-2">
                  <a className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-bold text-primary-foreground hover:opacity-90" href={artifactUrl(artifact.id)} target="_blank" rel="noreferrer"><FolderOpen size={16} />Open</a>
                  <a className="inline-flex h-9 items-center gap-2 rounded-md bg-muted px-3 text-sm font-bold text-foreground hover:bg-border" href={artifactUrl(artifact.id)} download><Download size={16} />Download</a>
                </div>
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Card className="flex min-h-[260px] flex-col items-center justify-center p-8 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10 text-primary"><FileVideo size={26} /></div>
          <h3 className="mt-4 text-xl font-black">No videos generated yet.</h3>
          <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">Start a Seedance job above. Completed MP4 artifacts will appear here with playable previews and download actions.</p>
        </Card>
      )}

      {!!failedItems.length && (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {failedItems.map(({ item }) => (
            <Card key={item.id} className="border-red-500/25 p-4">
              <div className="flex items-center gap-2 text-red-300"><AlertCircle size={17} /><span className="font-black">Failed item</span></div>
              <div className="mt-2 font-bold">{item.title || item.prompt}</div>
              <p className="mt-1 break-words text-sm text-muted-foreground">{item.error || "No error message was returned."}</p>
            </Card>
          ))}
        </div>
      )}
    </section>
  )
}

function History({ jobs }: { jobs: Job[] }) {
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-2xl font-black">Video history</h2>
        <p className="mt-1 text-sm text-muted-foreground">All Seedance batches, artifacts, failures, and downloads.</p>
      </div>
      <JobTable jobs={jobs} />
    </div>
  )
}

function Logs({ rows }: { rows: LogRow[] }) {
  return (
    <Card className="p-4 sm:p-5">
      <h2 className="text-2xl font-black">System logs</h2>
      <div className="mt-4 max-h-[720px] space-y-2 overflow-auto">
        {rows.map((row) => (
          <div key={row.id} className="rounded-md bg-background/70 p-3 text-sm">
            <Badge tone={row.level === "error" ? "error" : row.level === "warn" ? "warn" : row.level === "success" ? "success" : "muted"}>{row.level}</Badge>
            <span className="ml-3 text-muted-foreground">{new Date(row.created_at).toLocaleString()}</span>
            <div className="mt-2 break-words font-medium">{row.message}</div>
          </div>
        ))}
        {!rows.length && <EmptySmall text="No logs yet." />}
      </div>
    </Card>
  )
}

function SettingsPage({ settings, setSettings }: { settings: SettingsPayload; setSettings: (value: SettingsPayload) => void }) {
  async function save() {
    const next = await api.saveSettings(settings)
    setSettings(next)
    toast.success("Settings saved")
  }
  return (
    <Card className="mx-auto max-w-5xl p-4 sm:p-5">
      <h2 className="text-2xl font-black">Video settings</h2>
      <p className="mt-1 text-sm text-muted-foreground">Configure Dola session cookies, output paths, proxies, and video defaults.</p>
      <div className="mt-5 grid gap-4 md:grid-cols-2">
        <Field label="Dola auth cookies" className="md:col-span-2">
          <Textarea className="min-h-[150px]" placeholder="Paste current Dola cookies/session values" value={settings.dola_auth_cookies} onChange={(event) => setSettings({ ...settings, dola_auth_cookies: event.target.value })} />
        </Field>
        <Field label="Output directory">
          <Input value={settings.output_dir} onChange={(event) => setSettings({ ...settings, output_dir: event.target.value })} />
        </Field>
        <Field label="Proxy URL">
          <Input value={settings.proxy_url} onChange={(event) => setSettings({ ...settings, proxy_url: event.target.value })} />
        </Field>
        <Field label="Default ratio">
          <Select value={settings.default_ratio} onChange={(event) => setSettings({ ...settings, default_ratio: event.target.value })}><option>9:16</option><option>16:9</option><option>1:1</option></Select>
        </Field>
        <Field label="Default duration">
          <Input type="number" value={settings.default_duration} onChange={(event) => setSettings({ ...settings, default_duration: Number(event.target.value) })} />
        </Field>
        <Field label="Default parallel">
          <Input type="number" value={settings.default_parallel} onChange={(event) => setSettings({ ...settings, default_parallel: Number(event.target.value) })} />
        </Field>
      </div>
      <Button className="mt-5" onClick={save}><CheckCircle2 size={16} />Save settings</Button>
    </Card>
  )
}

function Metric({ label, value, tone: metricTone = "default" }: { label: string; value: number; tone?: "default" | "success" | "error" }) {
  return (
    <Card className="p-4">
      <div className="text-xs font-bold uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={metricTone === "success" ? "mt-2 text-3xl font-black text-emerald-300" : metricTone === "error" ? "mt-2 text-3xl font-black text-red-300" : "mt-2 text-3xl font-black"}>{value}</div>
    </Card>
  )
}

function Field({ label, className, children }: { label: string; className?: string; children: ReactNode }) {
  return (
    <label className={className}>
      <span className="mb-1.5 block text-xs font-bold uppercase tracking-wide text-muted-foreground">{label}</span>
      {children}
    </label>
  )
}

function EmptySmall({ text }: { text: string }) {
  return <div className="rounded-md border border-dashed border-border p-4 text-center text-sm text-muted-foreground">{text}</div>
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
