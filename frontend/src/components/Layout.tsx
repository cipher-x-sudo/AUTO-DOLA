import { useState } from "react"
import { Activity, FileVideo, History, Image, Menu, Mic2, ScrollText, Settings, Sparkles, X } from "lucide-react"
import { Button } from "./ui"

const nav = [
  ["dashboard", Activity, "Dashboard"],
  ["video", FileVideo, "Video Studio"],
  ["image", Image, "Image Studio"],
  ["tts", Mic2, "TTS Studio"],
  ["history", History, "History"],
  ["logs", ScrollText, "Logs"],
  ["settings", Settings, "Settings"],
] as const

function Sidebar({ page, setPage, onNavigate }: { page: string; setPage: (page: string) => void; onNavigate?: () => void }) {
  return (
    <>
      <div className="mb-6 flex items-center gap-3 px-2">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground"><Sparkles size={20} /></div>
        <div>
          <div className="font-black tracking-wide">AUTO-DOLA</div>
          <div className="text-xs text-muted-foreground">Automation control center</div>
        </div>
      </div>
      <nav className="space-y-1">
        {nav.map(([id, Icon, label]) => (
          <button key={id} onClick={() => { setPage(id); onNavigate?.() }} className={`flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-semibold transition ${page === id ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
            <Icon size={17} />
            {label}
          </button>
        ))}
      </nav>
    </>
  )
}

export function Layout({ page, setPage, children }: { page: string; setPage: (page: string) => void; children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    <div className="flex min-h-screen bg-background">
      <aside className="hidden w-72 shrink-0 border-r border-border bg-card p-4 lg:block">
        <Sidebar page={page} setPage={setPage} />
      </aside>
      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setMobileOpen(false)} />
          <aside className="relative z-50 h-full w-72 border-r border-border bg-card p-4">
            <div className="mb-4 flex justify-end">
              <button onClick={() => setMobileOpen(false)} className="rounded-md p-1 text-muted-foreground hover:bg-muted"><X size={20} /></button>
            </div>
            <Sidebar page={page} setPage={setPage} onNavigate={() => setMobileOpen(false)} />
          </aside>
        </div>
      )}
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-10 border-b border-border bg-background/90 px-5 py-3 backdrop-blur">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <button onClick={() => setMobileOpen(true)} className="rounded-md p-1 text-muted-foreground hover:bg-muted lg:hidden"><Menu size={22} /></button>
              <div>
                <h1 className="text-xl font-black">Professional Dola automation</h1>
                <p className="hidden text-sm text-muted-foreground sm:block">Seedance jobs, image runs, speech batches, logs, and outputs.</p>
              </div>
            </div>
            <Button variant="secondary" onClick={() => document.documentElement.classList.toggle("dark")}>Toggle theme</Button>
          </div>
        </header>
        <div className="p-5">{children}</div>
      </main>
    </div>
  )
}
