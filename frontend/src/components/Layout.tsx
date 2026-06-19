import { FileVideo, History, Loader2, Moon, Sparkles, Wand2 } from "lucide-react"
import { Button } from "./ui"

const nav = [
  ["video", "/video", FileVideo, "Video Studio"],
  ["prompts", "/prompt-generator", Wand2, "Prompt Generator"],
  ["history", "/history", History, "History"],
] as const

export function Layout({ page, setPage, loading, children }: { page: string; setPage: (page: string) => void; loading?: boolean; children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-30 border-b border-border bg-[#070812]/95 backdrop-blur">
        <div className="flex min-h-14 flex-col gap-3 px-3 py-3 lg:flex-row lg:items-center lg:justify-between lg:px-5">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/15 text-primary ring-1 ring-primary/30">
              <Sparkles size={19} />
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm font-black tracking-wide sm:text-base">AUTO-DOLA</div>
              <div className="truncate text-[11px] font-bold uppercase tracking-wide text-primary">Seedance video console</div>
            </div>
          </div>

          <nav className="flex min-w-0 gap-2 overflow-x-auto pb-1 lg:pb-0">
            {nav.map(([id, href, Icon, label]) => (
              <a
                key={id}
                href={href}
                onClick={(event) => {
                  event.preventDefault()
                  setPage(id)
                }}
                className={`inline-flex h-9 shrink-0 items-center gap-2 rounded-md px-3 text-xs font-black uppercase tracking-wide transition ${
                  page === id ? "bg-primary text-primary-foreground shadow-lg shadow-primary/20" : "bg-muted/45 text-muted-foreground hover:bg-muted hover:text-foreground"
                }`}
              >
                <Icon size={15} />
                {label}
              </a>
            ))}
          </nav>

          <div className="hidden shrink-0 items-center gap-2 lg:flex">
            {loading && <Loader2 className="animate-spin text-muted-foreground" size={18} />}
            <Button variant="secondary" className="gap-2" onClick={() => document.documentElement.classList.toggle("dark")}>
              <Moon size={16} />
              Theme
            </Button>
          </div>
        </div>
      </header>

      <main className="min-w-0 px-3 py-4 sm:px-5">{children}</main>
    </div>
  )
}
