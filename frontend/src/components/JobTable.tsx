import { Download, ExternalLink } from "lucide-react"
import { artifactUrl } from "@/lib/api"
import type { Job } from "@/lib/types"
import { Badge, Card, Progress } from "./ui"

function tone(status: string): "default" | "success" | "warn" | "error" | "muted" {
  if (status === "completed") return "success"
  if (status === "failed") return "error"
  if (status === "running") return "default"
  if (status === "cancelled") return "warn"
  return "muted"
}

export function JobTable({ jobs }: { jobs: Job[] }) {
  return (
    <Card className="overflow-hidden">
      <div className="hidden overflow-x-auto lg:block">
        <table className="w-full min-w-[780px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs uppercase text-muted-foreground">
            <tr>
              <th className="p-3">Job</th>
              <th className="p-3">Status</th>
              <th className="p-3">Progress</th>
              <th className="p-3">Latest item</th>
              <th className="p-3">Videos</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => {
              const progress = job.total ? ((job.done + job.failed) / job.total) * 100 : 0
              const latestItem = job.items[0]
              const videos = job.artifacts.filter((artifact) => artifact.kind === "video")
              return (
                <tr key={job.id} className="border-b border-border last:border-0">
                  <td className="p-3">
                    <div className="font-bold">{job.title}</div>
                    <div className="text-xs text-muted-foreground">{new Date(job.created_at).toLocaleString()}</div>
                  </td>
                  <td className="p-3"><Badge tone={tone(job.status)}>{job.status}</Badge></td>
                  <td className="p-3">
                    <Progress value={progress} />
                    <div className="mt-1 text-xs text-muted-foreground">{job.done}/{job.total} done, {job.failed} failed</div>
                  </td>
                  <td className="max-w-[280px] p-3">
                    {latestItem ? (
                      <div>
                        <div className="truncate font-semibold">{latestItem.title || latestItem.prompt}</div>
                        <div className="truncate text-xs text-muted-foreground">{latestItem.error || latestItem.action || latestItem.prompt}</div>
                      </div>
                    ) : <span className="text-muted-foreground">No items</span>}
                  </td>
                  <td className="p-3">
                    <div className="flex flex-wrap gap-2">
                      {videos.map((artifact) => (
                        <a key={artifact.id} className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs font-bold text-foreground hover:bg-border" href={artifactUrl(artifact.id)}>
                          <Download size={13} />
                          {artifact.filename}
                        </a>
                      ))}
                      {!videos.length && <span className="text-xs text-muted-foreground">No video output</span>}
                    </div>
                  </td>
                </tr>
              )
            })}
            {!jobs.length && <tr><td className="p-8 text-center text-muted-foreground" colSpan={5}>No video jobs yet.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="space-y-3 p-3 lg:hidden">
        {jobs.map((job) => {
          const progress = job.total ? ((job.done + job.failed) / job.total) * 100 : 0
          const videos = job.artifacts.filter((artifact) => artifact.kind === "video")
          return (
            <div key={job.id} className="rounded-md border border-border bg-background/70 p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate font-bold">{job.title}</div>
                  <div className="text-xs text-muted-foreground">{new Date(job.created_at).toLocaleString()}</div>
                </div>
                <Badge tone={tone(job.status)}>{job.status}</Badge>
              </div>
              <div className="mt-3">
                <Progress value={progress} />
                <div className="mt-1 text-xs text-muted-foreground">{job.done}/{job.total} done, {job.failed} failed</div>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                {videos.map((artifact) => (
                  <a key={artifact.id} className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs font-bold" href={artifactUrl(artifact.id)}>
                    <ExternalLink size={13} />
                    {artifact.filename}
                  </a>
                ))}
              </div>
            </div>
          )
        })}
        {!jobs.length && <div className="p-8 text-center text-muted-foreground">No video jobs yet.</div>}
      </div>
    </Card>
  )
}
