import * as React from "react"
import { cn } from "@/lib/utils"

export function Button({ className, variant = "default", ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "secondary" | "ghost" | "destructive" }) {
  return <button className={cn("inline-flex h-9 items-center justify-center gap-2 rounded-md px-3 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50", variant === "default" && "bg-primary text-primary-foreground hover:opacity-90", variant === "secondary" && "bg-muted text-foreground hover:bg-border", variant === "ghost" && "hover:bg-muted", variant === "destructive" && "bg-destructive text-white hover:opacity-90", className)} {...props} />
}

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("w-full min-w-0 max-w-full rounded-lg border border-border bg-card shadow-sm shadow-black/10", className)} {...props} />
}

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cn("h-10 w-full rounded-md border border-border bg-background px-3 text-sm outline-none transition placeholder:text-muted-foreground/70 focus:ring-2 focus:ring-primary", props.className)} />
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cn("min-h-[120px] w-full rounded-md border border-border bg-background px-3 py-2 text-sm leading-6 outline-none transition placeholder:text-muted-foreground/70 focus:ring-2 focus:ring-primary", props.className)} />
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cn("h-10 w-full rounded-md border border-border bg-background px-3 text-sm outline-none transition focus:ring-2 focus:ring-primary", props.className)} />
}

export function Badge({ children, tone = "default" }: { children: React.ReactNode; tone?: "default" | "success" | "warn" | "error" | "muted" }) {
  return <span className={cn("inline-flex rounded-full px-2 py-0.5 text-xs font-bold", tone === "default" && "bg-primary/10 text-primary", tone === "success" && "bg-emerald-500/15 text-emerald-600 dark:text-emerald-300", tone === "warn" && "bg-amber-500/15 text-amber-700 dark:text-amber-300", tone === "error" && "bg-red-500/15 text-red-600 dark:text-red-300", tone === "muted" && "bg-muted text-muted-foreground")}>{children}</span>
}

export function Progress({ value }: { value: number }) {
  return <div className="h-2 rounded-full bg-muted"><div className="h-full rounded-full bg-primary transition-all" style={{ width: `${Math.max(0, Math.min(100, value))}%` }} /></div>
}
