import { type ReactNode, useMemo, useState } from "react";
import type { Evidence } from "../api/client";
import { UiContext, type UiContextValue } from "./ui-context";

export function UiProvider({ children }: { children: ReactNode }) {
  const [jobsOpen, setJobsOpen] = useState(false);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [queryRunId, setQueryRunId] = useState<string | null>(null);

  const value = useMemo<UiContextValue>(
    () => ({
      jobsOpen,
      setJobsOpen,
      evidence,
      openEvidence: setEvidence,
      closeEvidence: () => setEvidence(null),
      queryRunId,
      openQueryRun: setQueryRunId,
      closeQueryRun: () => setQueryRunId(null),
    }),
    [evidence, jobsOpen, queryRunId],
  );

  return <UiContext.Provider value={value}>{children}</UiContext.Provider>;
}
