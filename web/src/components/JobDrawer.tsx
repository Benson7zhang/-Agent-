import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, RotateCcw } from "lucide-react";
import { api, createIdempotencyKey, errorMessage, type Job } from "../api/client";
import { useUi } from "../app/useUi";
import { formatDateTime } from "../lib/format";
import { Drawer } from "./Drawer";
import { EmptyState, ErrorState, LoadingState } from "./Feedback";
import { StatusBadge } from "./StatusBadge";

function JobRow({ job }: { job: Job }) {
  const queryClient = useQueryClient();
  const retry = useMutation({
    mutationFn: () => api.retryJob(job.job_id, createIdempotencyKey(`retry:${job.job_id}`)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });

  return (
    <article className="job-row">
      <div className="job-row__heading">
        <div>
          <strong>{job.stage || "财报处理"}</strong>
          <span className="mono">#{job.job_id.slice(0, 8)}</span>
        </div>
        <StatusBadge status={job.status} />
      </div>
      <div className="progress-track" aria-label={`任务进度 ${Math.round(job.progress)}%`}>
        <span style={{ width: `${Math.max(0, Math.min(100, job.progress))}%` }} />
      </div>
      <dl className="inline-metadata">
        <div>
          <dt>尝试</dt>
          <dd>{job.attempt}</dd>
        </div>
        <div>
          <dt>创建</dt>
          <dd>{formatDateTime(job.created_at)}</dd>
        </div>
      </dl>
      {job.error_message ? (
        <p className="inline-error">
          <AlertTriangle size={15} aria-hidden="true" />
          {job.error_message}
        </p>
      ) : null}
      {retry.isError ? <p className="form-error" role="alert">{errorMessage(retry.error)}</p> : null}
      {job.status === "FAILED" || job.status === "INTERRUPTED" ? (
        <button className="button button--secondary" type="button" onClick={() => retry.mutate()} disabled={retry.isPending}>
          <RotateCcw size={16} aria-hidden="true" />
          {retry.isPending ? "正在重试" : "重试任务"}
        </button>
      ) : null}
    </article>
  );
}

export function JobDrawer() {
  const { jobsOpen, setJobsOpen } = useUi();
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: api.listJobs,
    refetchInterval: (query) =>
      query.state.data?.items.some((job) => job.status === "QUEUED" || job.status === "RUNNING") ? 3000 : false,
  });

  return (
    <Drawer open={jobsOpen} onClose={() => setJobsOpen(false)} title="任务中心" description="导入任务在服务重启后仍可追踪" testId="job-drawer">
      {jobs.isLoading ? <LoadingState label="正在读取任务" /> : null}
      {jobs.isError ? <ErrorState message={errorMessage(jobs.error)} onRetry={() => void jobs.refetch()} /> : null}
      {jobs.data?.items.length === 0 ? <EmptyState title="暂无任务" detail="上传财报后，处理进度会显示在这里。" /> : null}
      <div className="job-list">
        {jobs.data?.items.map((job) => <JobRow key={job.job_id} job={job} />)}
      </div>
    </Drawer>
  );
}
