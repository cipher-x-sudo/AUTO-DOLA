import { useCallback, useEffect, useMemo, useState } from "react"
import { toast } from "sonner"
import { api, subscribeJobEvents } from "@/lib/api"
import type { Job, SettingsPayload } from "@/lib/types"
import { Layout } from "@/components/Layout"
import { JobTable } from "@/components/JobTable"
import { Badge, Button, Card, Input, Select, Textarea } from "@/components/ui"

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

export default function App() {
  const [page, setPage] = useState("dashboard")
  const [jobs, setJobs] = useState<Job[]>([])
  const [settings, setSettings] = useState<SettingsPayload>(emptySettings)
  const [logs, setLogs] = useState<Array<{ id: string; level: string; message: string; created_at: string }>>([])

  const refresh = useCallback(async () => {
    try {
      const [videoJobs, appSettings, logRows] = await Promise.all([api.videoJobs(), api.settings(), api.logs()])
      setJobs(videoJobs)
      setSettings(appSettings)
      setLogs(logRows)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to refresh")
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 6000)
    return () => clearInterval(id)
  }, [refresh])

  const runningJobIds = useMemo(() => jobs.filter((j) => j.status === "running").map((j) => j.id).join(","), [jobs])

  useEffect(() => {
    const runningJobs = jobs.filter((j) => j.status === "running")
    if (!runningJobs.length) return
    const cleanups = runningJobs.map((job) =>
      subscribeJobEvents(
        job.id,
        () => refresh(),
        () => {},
      ),
    )
    return () => cleanups.forEach((c) => c())
  }, [jobs, refresh, runningJobIds])

  const content = useMemo(() => {
    if (page === "video") return <VideoStudio settings={settings} onCreated={refresh} />
    if (page === "image") return <ImageStudio onCreated={refresh} />
    if (page === "tts") return <TtsStudio onCreated={refresh} />
    if (page === "history") return <History jobs={jobs} />
    if (page === "logs") return <Logs rows={logs} />
    if (page === "settings") return <SettingsPage settings={settings} setSettings={setSettings} />
    return <Dashboard jobs={jobs} />
  }, [page, jobs, settings, logs, refresh])

  return <Layout page={page} setPage={setPage}>{content}</Layout>
}

function Dashboard({ jobs }: { jobs: Job[] }) {
  const running = jobs.filter((j) => j.status === "running").length
  const completed = jobs.filter((j) => j.status === "completed").length
  const failed = jobs.filter((j) => j.status === "failed").length
  return (
    <div className="space-y-5">
      <div className="grid gap-4 md:grid-cols-4">
        <Stat label="Total jobs" value={jobs.length} />
        <Stat label="Running" value={running} />
        <Stat label="Completed" value={completed} />
        <Stat label="Failed" value={failed} />
      </div>
      <JobTable jobs={jobs.slice(0, 8)} />
    </div>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return <Card className="p-4"><div className="text-sm text-muted-foreground">{label}</div><div className="mt-2 text-3xl font-black">{value}</div></Card>
}

function VideoStudio({ settings, onCreated }: { settings: SettingsPayload; onCreated: () => void }) {
  const [prompts, setPrompts] = useState("A cinematic product shot of a neon sneaker\nA vertical travel reel of a rainy Tokyo street")
  const [ratio, setRatio] = useState(settings.default_ratio || "9:16")
  const [duration, setDuration] = useState(settings.default_duration || 15)
  const [parallel, setParallel] = useState(settings.default_parallel || 5)
  const [saveFolder, setSaveFolder] = useState(settings.output_dir || "")

  async function submit() {
    const rows = prompts.split("\n").map((line) => line.trim()).filter(Boolean).map((prompt) => ({ prompt, title: prompt.slice(0, 70) }))
    if (!rows.length) return toast.error("Add at least one prompt.")
    await api.createVideoJob({ prompts: rows, ratio, duration, parallel, save_folder: saveFolder, clean_watermark: true })
    toast.success("Video job queued")
    onCreated()
  }

  return <StudioShell title="Video Studio" description="Bulk Seedance generation through Dola sessions.">
    <Textarea value={prompts} onChange={(e) => setPrompts(e.target.value)} className="min-h-[240px]" />
    <div className="grid gap-3 md:grid-cols-4">
      <Select value={ratio} onChange={(e) => setRatio(e.target.value)}><option>9:16</option><option>16:9</option><option>1:1</option></Select>
      <Input type="number" value={duration} onChange={(e) => setDuration(Number(e.target.value))} />
      <Input type="number" value={parallel} onChange={(e) => setParallel(Number(e.target.value))} />
      <Input placeholder="Output folder" value={saveFolder} onChange={(e) => setSaveFolder(e.target.value)} />
    </div>
    <Button onClick={submit}>Start generation</Button>
  </StudioShell>
}

function ImageStudio({ onCreated }: { onCreated: () => void }) {
  const [prompts, setPrompts] = useState("Futuristic SaaS dashboard hero image")
  async function submit() {
    await api.createImageJob({ prompts: prompts.split("\n").filter(Boolean), aspect_ratio: "1:1" })
    toast.success("Image job queued")
    onCreated()
  }
  return <StudioShell title="Image Studio" description="Bulk image generation with persisted outputs."><Textarea value={prompts} onChange={(e) => setPrompts(e.target.value)} /><Button onClick={submit}>Start image batch</Button></StudioShell>
}

function TtsStudio({ onCreated }: { onCreated: () => void }) {
  const [lines, setLines] = useState("Welcome to AUTO-DOLA.")
  async function submit() {
    await api.createTtsJob({ lines: lines.split("\n").filter(Boolean), voice: "en-US-AriaNeural" })
    toast.success("TTS job queued")
    onCreated()
  }
  return <StudioShell title="TTS Studio" description="Microsoft neural voice batch rendering."><Textarea value={lines} onChange={(e) => setLines(e.target.value)} /><Button onClick={submit}>Generate speech</Button></StudioShell>
}

function StudioShell({ title, description, children }: { title: string; description: string; children: React.ReactNode }) {
  return <Card className="mx-auto max-w-5xl p-5"><div className="mb-5"><h2 className="text-2xl font-black">{title}</h2><p className="text-sm text-muted-foreground">{description}</p></div><div className="space-y-4">{children}</div></Card>
}

function History({ jobs }: { jobs: Job[] }) {
  return <div className="space-y-4"><div className="flex items-center justify-between"><h2 className="text-2xl font-black">History</h2><Badge tone="muted">{jobs.length} jobs</Badge></div><JobTable jobs={jobs} /></div>
}

function Logs({ rows }: { rows: Array<{ id: string; level: string; message: string; created_at: string }> }) {
  return <Card className="p-4"><h2 className="mb-4 text-2xl font-black">Logs</h2><div className="max-h-[650px] space-y-2 overflow-auto">{rows.map((row) => <div key={row.id} className="rounded-md bg-muted p-3 text-sm"><Badge tone={row.level === "error" ? "error" : row.level === "warn" ? "warn" : "default"}>{row.level}</Badge><span className="ml-3 text-muted-foreground">{new Date(row.created_at).toLocaleString()}</span><div className="mt-2 font-medium">{row.message}</div></div>)}</div></Card>
}

function SettingsPage({ settings, setSettings }: { settings: SettingsPayload; setSettings: (value: SettingsPayload) => void }) {
  async function save() {
    const next = await api.saveSettings(settings)
    setSettings(next)
    toast.success("Settings saved")
  }
  return <Card className="mx-auto max-w-5xl p-5"><h2 className="mb-4 text-2xl font-black">Settings</h2><div className="grid gap-4 md:grid-cols-2">
    <Textarea className="md:col-span-2" placeholder="Dola auth cookies" value={settings.dola_auth_cookies} onChange={(e) => setSettings({ ...settings, dola_auth_cookies: e.target.value })} />
    <Input placeholder="YousMind API key" value={settings.yousmind_api_key} onChange={(e) => setSettings({ ...settings, yousmind_api_key: e.target.value })} />
    <Input placeholder="Output directory" value={settings.output_dir} onChange={(e) => setSettings({ ...settings, output_dir: e.target.value })} />
    <Input placeholder="Proxy URL" value={settings.proxy_url} onChange={(e) => setSettings({ ...settings, proxy_url: e.target.value })} />
    <Input placeholder="TTS voice" value={settings.tts_default_voice} onChange={(e) => setSettings({ ...settings, tts_default_voice: e.target.value })} />
  </div><Button className="mt-4" onClick={save}>Save settings</Button></Card>
}
