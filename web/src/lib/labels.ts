import type { AnswerStatus, DocumentKind, DocumentStatus, JobStatus, ValidationStatus } from "../api/client";

export const answerLabels: Record<AnswerStatus, string> = {
  VERIFIED: "已验证",
  NEEDS_CLARIFICATION: "需澄清",
  INSUFFICIENT_EVIDENCE: "证据不足",
  FAILED: "失败",
};

export const validationLabels: Record<ValidationStatus, string> = {
  VALIDATED: "已验证",
  NEEDS_REVIEW: "待复核",
  REJECTED: "已拒绝",
};

export const jobLabels: Record<JobStatus, string> = {
  QUEUED: "排队中",
  RUNNING: "处理中",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  INTERRUPTED: "已中断",
};

export const documentKindLabels: Record<DocumentKind, string> = {
  financial_report: "财务报告",
  research_report: "研究报告",
};

export const documentStatusLabels: Record<DocumentStatus, string> = {
  STORED: "已存储",
  QUEUED: "排队中",
  PROCESSING: "解析中",
  NEEDS_REVIEW: "待复核",
  COMPLETED: "已完成",
  FAILED: "失败",
};
