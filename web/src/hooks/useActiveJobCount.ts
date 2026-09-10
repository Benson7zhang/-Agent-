import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export function useActiveJobCount(): number {
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: api.listJobs, refetchInterval: 5000 });
  return jobs.data?.items.filter((job) => job.status === "QUEUED" || job.status === "RUNNING").length ?? 0;
}
