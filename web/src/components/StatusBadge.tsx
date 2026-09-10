import { AlertCircle, CheckCircle2, CircleEllipsis, Clock3, Info, XCircle } from "lucide-react";
import type { AnswerStatus, DocumentStatus, JobStatus, ValidationStatus } from "../api/client";
import { answerLabels, documentStatusLabels, jobLabels, validationLabels } from "../lib/labels";

type Status = AnswerStatus | DocumentStatus | JobStatus | ValidationStatus;

const toneByStatus: Record<Status, string> = {
  VERIFIED: "success",
  VALIDATED: "success",
  SUCCEEDED: "success",
  NEEDS_CLARIFICATION: "warning",
  NEEDS_REVIEW: "warning",
  INSUFFICIENT_EVIDENCE: "neutral",
  FAILED: "danger",
  INTERRUPTED: "danger",
  REJECTED: "danger",
  QUEUED: "info",
  RUNNING: "info",
  PROCESSING: "info",
  STORED: "info",
  COMPLETED: "success",
};

function label(status: Status): string {
  if (status in answerLabels) return answerLabels[status as AnswerStatus];
  if (status in jobLabels) return jobLabels[status as JobStatus];
  if (status in validationLabels) return validationLabels[status as ValidationStatus];
  if (status in documentStatusLabels) return documentStatusLabels[status as DocumentStatus];
  return status === "PROCESSING" ? "处理中" : status;
}

function StatusIcon({ status }: { status: Status }) {
  const props = { size: 14, "aria-hidden": true as const };
  if (["VERIFIED", "VALIDATED", "SUCCEEDED"].includes(status)) return <CheckCircle2 {...props} />;
  if (["FAILED", "INTERRUPTED", "REJECTED"].includes(status)) return <XCircle {...props} />;
  if (["NEEDS_CLARIFICATION", "NEEDS_REVIEW"].includes(status)) return <AlertCircle {...props} />;
  if (["QUEUED", "PROCESSING", "STORED"].includes(status)) return <Clock3 {...props} />;
  if (status === "RUNNING") return <CircleEllipsis {...props} />;
  return <Info {...props} />;
}

export function StatusBadge({ status }: { status: Status }) {
  return (
    <span className={`status-badge status-badge--${toneByStatus[status]}`}>
      <StatusIcon status={status} />
      {label(status)}
    </span>
  );
}
