import { useState } from "react"
import { FileVideo, History, Loader2, Menu, Moon, ScrollText, Settings, Sparkles, X } from "lucide-react"
import { Button } from "./ui"

const nav = [
  ["video", FileVideo, "Video Studio"],
  ["history", History, "History"],
  ["logs", ScrollText, "Logs"],
  ["settings", Settings, "Settings"],
] as const

function Sidebar({ page, setPage, onNavigate }: { page: string; setPage: (page: string) => void; onNavigate?: () => void }) {
  return (
    <>
      <div className="mb-7 flex items-center gap-3 px-2">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-lg shadow-primary/20"><Sparkles size={20} /></div>
        <div className="min-w-0">
          <div className="font-black tracking-wide">AUTO-DOLA</div>
          <div className="truncate text-xs text-muted-foreground">Seedance video studio</div>
        </div>
      </div>
      <nav className="space-y-1">
        {nav.map(([id, Icon, label]) => (
          <button
            key={id}
            onClick={() => {
              setPage(id)
              onNavigate?.()
            }}
            className={`flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-sm font-semibold transition ${page === id ? "bg-primary text-primary-foreground shadow-md shadow-primary/15" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}
          >
            <Icon size={17} />
            <span className="truncate">{label}</span>
          </button>
        ))}
      </nav>
    </>
  )
}

export function Layout({ page, setPage, loading, children }: { page: string; setPage: (page: string) => void; loading?: boolean; children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <aside className="hidden w-72 shrink-0 border-r border-border bg-card/80 p-4 backdrop-blur lg:block">
        <Sidebar page={page} setPage={setPage} />
      </aside>

      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/60" onClick={() => setMobileOpen(false)} />
          <aside className="relative z-50 h-full w-[min(19rem,86vw)] border-r border-border bg-card p-4 shadow-2xl">
            <div className="mb-4 flex justify-end">
              <button onClick={() => setMobileOpen(false)} className="rounded-md p-1 text-muted-foreground hover:bg-muted"><X size={20} /></button>
            </div>
            <Sidebar page={page} setPage={setPage} onNavigate={() => setMobileOpen(false)} />
          </aside>
        </div>
      )}

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b border-border bg-background/88 px-4 py-3 backdrop-blur sm:px-5">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3">
              <button onClick={() => setMobileOpen(true)} className="rounded-md p-1.5 text-muted-foreground hover:bg-muted lg:hidden"><Menu size={22} /></button>
              <div className="min-w-0">
                <h1 className="truncate text-lg font-black sm:text-xl">Professional Dola video generation</h1>
                <p className="hidden truncate text-sm text-muted-foreground sm:block">Prompt, monitor, preview, and download Seedance MP4 outputs.</p>
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {loading && <Loader2 className="animate-spin text-muted-foreground" size={18} />}
              <Button variant="secondary" className="hidden gap-2 sm:inline-flex" onClick={() => document.documentElement.classList.toggle("dark")}><Moon size={16} />Theme</Button>
            </div>
          </div>
        </header>
        <div className="min-w-0 p-3 sm:p-5">{children}</div>
      </main>
    </div>
  )
}
