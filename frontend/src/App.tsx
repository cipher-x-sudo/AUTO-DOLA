import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react"
import {
  AlertCircle,
  CheckCircle2,
  Download,
  FileText,
  FileVideo,
  Folder,
  FolderOpen,
  Loader2,
  Play,
  RefreshCw,
  Search,
  Settings2,
  Square,
  Terminal,
  Trash2,
  Upload,
  Zap,
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

interface VideoArtifact {
  artifact: Artifact
  job: Job
}

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
    const cleanups = activeJobs.map((job) => subscribeJobEvents(job.id, () => refresh(), () => undefined))
    return () => cleanups.forEach((cleanup) => cleanup())
  }, [jobs, refresh, runningJobIds])

  const activeJob = useMemo(() => jobs.find((job) => job.status === "running" || job.status === "queued") ?? jobs[0], [jobs])
  const recentLogs = useMemo(() => logs.filter((row) => !activeJob || row.job_id === activeJob.id).slice(0, 16), [activeJob, logs])
  const videos = useMemo(() => collectVideoArtifacts(jobs), [jobs])

  return (
    <Layout page={page} setPage={(next) => setPage(next as Page)} loading={loading}>
      {page === "video" && <VideoConsole settings={settings} jobs={jobs} activeJob={activeJob} videos={videos} logs={recentLogs} onRefresh={refresh} />}
      {page === "history" && <History jobs={jobs} />}
      {page === "logs" && <Logs rows={logs} />}
      {page === "settings" && <SettingsPage settings={settings} setSettings={setSettings} />}
    </Layout>
  )
}

function VideoConsole({
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
  const [promptText, setPromptText] = useState("car")
  const [ratio, setRatio] = useState(settings.default_ratio || "9:16")
  const [duration, setDuration] = useState(settings.default_duration || 15)
  const [parallel, setParallel] = useState(settings.default_parallel || 30)
  const [saveFolder, setSaveFolder] = useState(settings.output_dir || "C:\\Users\\Muhammad Huzaifa\\Videos")
  const [cleanWatermark, setCleanWatermark] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [logSearch, setLogSearch] = useState("")

  useEffect(() => {
    setRatio((current) => current || settings.default_ratio || "9:16")
    setDuration((current) => current || settings.default_duration || 15)
    setParallel((current) => current || settings.default_parallel || 30)
    setSaveFolder((current) => current || settings.output_dir || "C:\\Users\\Muhammad Huzaifa\\Videos")
  }, [settings])

  const stats = useMemo(() => {
    const items = jobs.flatMap((job) => job.items)
    return {
      total: items.length || promptLines(promptText).length,
      queued: items.filter((item) => item.status === "queued").length,
      generating: items.filter((item) => item.status === "running").length,
      done: items.filter((item) => item.status === "completed").length,
      failed: items.filter((item) => item.status === "failed").length,
      skipped: items.filter((item) => item.status === "cancelled").length,
      videos: videos.length,
    }
  }, [jobs, promptText, videos])

  const progressTotal = activeJob?.total || stats.total || 0
  const progressDone = activeJob ? activeJob.done + activeJob.failed : stats.done + stats.failed
  const progress = progressTotal ? Math.round((progressDone / progressTotal) * 100) : 0
  const queueItems = activeJob?.items ?? jobs.flatMap((job) => job.items).slice(0, 8)
  const failedItems = jobs.flatMap((job) => job.items.map((item) => ({ job, item }))).filter(({ item }) => item.status === "failed").slice(0, 6)
  const filteredLogs = logs.filter((row) => row.message.toLowerCase().includes(logSearch.toLowerCase()) || row.level.toLowerCase().includes(logSearch.toLowerCase()))

  async function submit() {
    const prompts = promptLines(promptText).map((prompt) => ({ title: prompt.slice(0, 70), prompt }))
    if (!prompts.length) {
      toast.error("Add at least one prompt.")
      return
    }
    setSubmitting(true)
    try {
      await api.createVideoJob({ prompts, ratio, duration, parallel, save_folder: saveFolder, clean_watermark: cleanWatermark })
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
        <div className="flex flex-wrap gap-2">
          <IconButton label="Refresh" onClick={onRefresh}><RefreshCw size={16} /></IconButton>
          <IconButton label="Status"><Zap size={16} /></IconButton>
          <IconButton label="Theme"><Settings2 size={16} /></IconButton>
        </div>
      </div>

      <OutputLocation path={saveFolder} setPath={setSaveFolder} />

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatBox label="Total" value={stats.total} />
        <StatBox label="Queued" value={stats.queued} tone="amber" />
        <StatBox label="Generating" value={stats.generating} tone="blue" />
        <StatBox label="Done" value={stats.done} tone="green" />
        <StatBox label="Failed" value={stats.failed} tone="red" />
        <StatBox label="Videos" value={stats.videos || stats.skipped} tone="amber" />
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

      <VideoGallery videos={videos} failedItems={failedItems} />
    </div>
  )
}

function OutputLocation({ path, setPath }: { path: string; setPath: (path: string) => void }) {
  return (
    <Card className="p-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-primary/15 text-primary">
            <Folder size={22} />
          </div>
          <div className="min-w-0">
            <div className="text-xs font-black uppercase tracking-[0.16em] text-muted-foreground">Video output location</div>
            <Input className="mt-2 h-9 max-w-3xl border-transparent bg-transparent px-0 font-mono text-xs focus:ring-0" value={path} onChange={(event) => setPath(event.target.value)} />
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Button variant="ghost" className="text-primary"><FolderOpen size={15} />Open Folder</Button>
          <Button><Folder size={15} />Change Folder</Button>
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
          <Input type="number" min={1} max={50} value={parallel} onChange={(event) => setParallel(Number(event.target.value))} />
        </Field>
        <label className="flex min-h-10 items-center gap-3 rounded-md border border-border bg-background px-3 text-xs font-black uppercase tracking-wide text-muted-foreground sm:col-span-2">
          <input type="checkbox" checked={cleanWatermark} onChange={(event) => setCleanWatermark(event.target.checked)} className="h-4 w-4 accent-[hsl(var(--primary))]" />
          Clean watermark after download
        </label>
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
  return (
    <Card className="p-4">
      <SectionTitle icon={<Zap size={15} />} title="Generation Queue" />
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
            {items.map((item, index) => (
              <tr key={item.id} className="border-t border-border">
                <td className="px-3 py-3 text-muted-foreground">{index + 1}</td>
                <td className="max-w-[260px] truncate px-3 py-3 font-bold">{item.prompt}</td>
                <td className="px-3 py-3"><Badge tone={tone(item.status)}>{item.status}</Badge></td>
                <td className="max-w-[240px] truncate px-3 py-3 text-muted-foreground">{item.error || item.action || "Waiting..."}</td>
              </tr>
            ))}
            {!items.length && <tr><td className="px-3 py-5 text-center text-muted-foreground" colSpan={4}>No queued videos yet.</td></tr>}
          </tbody>
        </table>
      </div>
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

function VideoGallery({ videos, failedItems }: { videos: VideoArtifact[]; failedItems: Array<{ job: Job; item: JobItem }> }) {
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
                  <a className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-bold text-primary-foreground hover:opacity-90" href={artifactUrl(artifact.id)} target="_blank" rel="noreferrer"><FolderOpen size={16} />Open</a>
                  <a className="inline-flex h-9 items-center gap-2 rounded-md bg-muted px-3 text-sm font-bold text-foreground hover:bg-border" href={artifactUrl(artifact.id)} download><Download size={16} />Download</a>
                </div>
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Card className="flex min-h-[190px] flex-col items-center justify-center p-8 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary"><FileVideo size={24} /></div>
          <h3 className="mt-4 text-lg font-black">No videos generated yet.</h3>
          <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">Completed MP4 artifacts will appear here with playable previews and download actions.</p>
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
    <div className="mx-auto max-w-[1760px] space-y-4">
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
    <Card className="mx-auto max-w-[1760px] p-4 sm:p-5">
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
      <div className="mt-5 grid grid-cols-1 gap-4 md:grid-cols-2">
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

function EmptySmall({ text }: { text: string }) {
  return <div className="rounded-md border border-dashed border-border p-4 text-center text-sm text-muted-foreground">{text}</div>
}

function promptLines(text: string) {
  return text.split("\n").map((line) => line.trim()).filter(Boolean)
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
