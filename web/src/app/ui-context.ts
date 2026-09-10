import { createContext } from "react";
import type { Evidence } from "../api/client";

export interface UiContextValue {
  jobsOpen: boolean;
  setJobsOpen: (open: boolean) => void;
  evidence: Evidence | null;
  openEvidence: (evidence: Evidence) => void;
  closeEvidence: () => void;
  queryRunId: string | null;
  openQueryRun: (queryRunId: string) => void;
  closeQueryRun: () => void;
}

export const UiContext = createContext<UiContextValue | null>(null);
