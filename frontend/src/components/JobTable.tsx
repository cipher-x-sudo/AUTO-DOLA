import { artifactUrl } from "@/lib/api"
import type { Job } from "@/lib/types"
import { Badge, Card, Progress } from "./ui"

function tone(status: string) {
  if (status === "completed") return "success"
  if (status === "failed") return "error"
  if (status === "running") return "default"
  if (status === "cancelled") return "warn"
  return "muted"
}

export function JobTable({ jobs }: { jobs: Job[] }) {
  return (
    <Card className="overflow-hidden">
      <table className="w-full text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs uppercase text-muted-foreground">
          <tr><th className="p-3">Job</th><th className="p-3">Status</th><th className="p-3">Progress</th><th className="p-3">Artifacts</th></tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id} className="border-b border-border last:border-0">
              <td className="p-3"><div className="font-bold">{job.title}</div><div className="text-xs text-muted-foreground">{new Date(job.created_at).toLocaleString()}</div></td>
              <td className="p-3"><Badge tone={tone(job.status) as never}>{job.status}</Badge></td>
              <td className="p-3"><Progress value={job.total ? (job.done / job.total) * 100 : 0} /><div className="mt-1 text-xs text-muted-foreground">{job.done}/{job.total} done, {job.failed} failed</div></td>
              <td className="p-3">
                <div className="flex flex-wrap gap-2">
                  {job.artifacts.map((artifact) => <a key={artifact.id} className="text-xs font-bold text-primary hover:underline" href={artifactUrl(artifact.id)}>{artifact.filename}</a>)}
                </div>
              </td>
            </tr>
          ))}
          {!jobs.length && <tr><td className="p-6 text-center text-muted-foreground" colSpan={4}>No jobs yet.</td></tr>}
        </tbody>
      </table>
    </Card>
  )
}
