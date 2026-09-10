export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export type DocumentKind = "financial_report" | "research_report";
export type DocumentStatus = "STORED" | "QUEUED" | "PROCESSING" | "NEEDS_REVIEW" | "COMPLETED" | "FAILED";
export type JobStatus = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "INTERRUPTED";
export type ValidationStatus = "VALIDATED" | "NEEDS_REVIEW" | "REJECTED";
export type DecisionAction = "VALIDATE" | "REJECT" | "CORRECT";
export type AnswerStatus = "VERIFIED" | "NEEDS_CLARIFICATION" | "INSUFFICIENT_EVIDENCE" | "FAILED";
export type TurnStatus = "COMPLETED" | "NEEDS_CLARIFICATION" | "FAILED";
export type StatementScope = "consolidated" | "parent";
export type PeriodType = "instant" | "duration";

export interface ApiList<T> {
  items: T[];
  next_cursor: string | null;
}

export interface CountedApiList<T> extends ApiList<T> {
  total: number;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: unknown;
    request_id: string;
  };
}

export class ApiError extends Error {
  readonly code: string;
  readonly details?: unknown;
  readonly requestId: string;
  readonly status: number;

  constructor(status: number, payload: ApiErrorBody) {
    super(payload.error.message);
    this.name = "ApiError";
    this.status = status;
    this.code = payload.error.code;
    this.details = payload.error.details;
    this.requestId = payload.error.request_id;
  }
}

export interface DocumentRecord {
  document_id: string;
  kind: DocumentKind;
  original_name: string;
  sha256: string;
  size_bytes: number;
  mime_type: string;
  status: DocumentStatus;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
}

export interface Job {
  job_id: string;
  document_id: string;
  kind: "document_ingestion";
  status: JobStatus;
  stage?: string | null;
  progress: number;
  attempt: number;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface Evidence {
  document_id: string;
  page_no: number;
  snippet: string;
  table_name?: string | null;
  row_label?: string | null;
  column_label?: string | null;
  bbox?: {
    x: number;
    y: number;
    width: number;
    height: number;
    coordinate_space: "normalized_top_left";
  } | null;
}

export interface AnswerTable {
  columns: Array<{ key: string; label: string; unit?: string | null }>;
  rows: Array<Record<string, string | number | null>>;
}

export interface ChartSpec {
  type: "line" | "bar" | "horizontal_bar";
  title?: string | null;
  x_axis: { label?: string; categories: string[] };
  series: Array<{ name: string; values: Array<number | null>; unit?: string | null }>;
  source_query_run_id: string;
}

export interface AnswerResult {
  status: AnswerStatus;
  text: string | null;
  company?: string | null;
  period?: string | null;
  unit?: string | null;
  currency?: string | null;
  statement_scope?: StatementScope | null;
  formula?: string | null;
  chart_spec?: ChartSpec | null;
  table?: AnswerTable | null;
  evidence?: Evidence[];
  clarification?: string | null;
  values: Array<{
    label: string;
    value: string;
    unit: string;
    currency: string;
    company: string;
    period: string;
    statement_scope: StatementScope;
    fact_keys: string[];
  }>;
}

export interface ConversationTurn {
  turn_id: string;
  conversation_id: string;
  sequence: number;
  question: string;
  status: TurnStatus;
  answer?: AnswerResult | null;
  query_run_id?: string | null;
  created_at: string;
}

export interface Conversation {
  conversation_id: string;
  title?: string | null;
  version: number;
  state: Record<string, unknown>;
  turns?: ConversationTurn[];
  created_at: string;
  updated_at: string;
}

export interface TurnResponse {
  turn: ConversationTurn;
  conversation: Conversation;
}

export interface FinancialFact {
  fact_key: string;
  company_id: string;
  stock_code: string;
  period: string;
  statement_scope: StatementScope;
  period_type: PeriodType;
  metric: string;
  raw_value: string;
  normalized_value: string;
  source_unit: string;
  target_unit: string;
  currency: string;
  document_id: string;
  page_no: number | null;
  table_name?: string | null;
  row_label?: string | null;
  column_label?: string | null;
  confidence: number;
  validation_status: ValidationStatus;
  validation_issues: Array<{ code: string; message: string; field?: string | null }>;
  version: number;
}

export interface FactReplacement {
  raw_value: string;
  normalized_value: string;
  source_unit: string;
  target_unit: string;
  currency: string;
  statement_scope: StatementScope;
  period_type: PeriodType;
  page_no: number;
  table_name: string;
  row_label: string;
  column_label: string;
}

export interface FactDecision {
  action: DecisionAction;
  reason: string;
  note?: string;
  expected_version: number;
  replacement?: FactReplacement;
}

export interface FactDecisionResponse {
  fact: FinancialFact;
  event_id: string;
  version: number;
}

export interface QueryRun {
  query_run_id: string;
  conversation_id?: string | null;
  question: string;
  query_spec: Record<string, unknown>;
  sql: string;
  parameters: unknown[] | Record<string, unknown>;
  answer_result: AnswerResult;
  status: string;
  created_at: string;
}

async function parseError(response: Response): Promise<ApiError> {
  let payload: ApiErrorBody;
  try {
    payload = (await response.json()) as ApiErrorBody;
  } catch {
    payload = {
      error: {
        code: "INVALID_ERROR_RESPONSE",
        message: `服务返回了无法解析的错误响应（HTTP ${response.status}）`,
        request_id: "unknown",
      },
    };
  }
  if (!payload?.error?.message) {
    payload = {
      error: {
        code: "INVALID_ERROR_RESPONSE",
        message: `服务请求失败（HTTP ${response.status}）`,
        details: payload,
        request_id: "unknown",
      },
    };
  }
  return new ApiError(response.status, payload);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        ...init?.headers,
      },
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : "网络连接失败";
    throw new ApiError(0, {
      error: {
        code: "SERVICE_UNREACHABLE",
        message: `无法连接财报服务：${reason}`,
        request_id: "client",
      },
    });
  }
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as T;
}

function queryString(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") search.set(key, String(value));
  });
  const result = search.toString();
  return result ? `?${result}` : "";
}

export const api = {
  listDocuments: () => request<CountedApiList<DocumentRecord>>("/documents"),
  uploadDocument: (file: File, kind: DocumentKind) => {
    const data = new FormData();
    data.append("file", file);
    data.append("kind", kind);
    return request<DocumentRecord>("/documents", { method: "POST", body: data });
  },
  documentContentUrl: (documentId: string) =>
    `${API_BASE}/documents/${encodeURIComponent(documentId)}/content`,
  createJob: (documentId: string, idempotencyKey: string) =>
    request<Job>("/jobs", {
      method: "POST",
      body: JSON.stringify({ document_id: documentId, kind: "document_ingestion", idempotency_key: idempotencyKey }),
    }),
  listJobs: () => request<ApiList<Job>>("/jobs"),
  retryJob: (jobId: string, idempotencyKey: string) =>
    request<Job>(`/jobs/${encodeURIComponent(jobId)}/retry`, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey }),
    }),
  createConversation: (title?: string) =>
    request<Conversation>("/conversations", { method: "POST", body: JSON.stringify({ title }) }),
  getConversation: (conversationId: string) =>
    request<Conversation>(`/conversations/${encodeURIComponent(conversationId)}`),
  createTurn: (conversationId: string, question: string, expectedVersion: number, idempotencyKey: string) =>
    request<TurnResponse>(`/conversations/${encodeURIComponent(conversationId)}/turns`, {
      method: "POST",
      body: JSON.stringify({ question, expected_version: expectedVersion, idempotency_key: idempotencyKey }),
    }),
  listFacts: (status: ValidationStatus = "NEEDS_REVIEW", cursor?: string) =>
    request<CountedApiList<FinancialFact>>(`/facts${queryString({ validation_status: status, cursor })}`),
  decideFact: (factKey: string, decision: FactDecision) =>
    request<FactDecisionResponse>(`/facts/${encodeURIComponent(factKey)}/decisions`, {
      method: "POST",
      body: JSON.stringify(decision),
    }),
  getQueryRun: (queryRunId: string) =>
    request<QueryRun>(`/query-runs/${encodeURIComponent(queryRunId)}`),
  async downloadQueryRun(queryRunId: string): Promise<Blob> {
    const response = await fetch(`${API_BASE}/query-runs/${encodeURIComponent(queryRunId)}/export.xlsx`);
    if (!response.ok) throw await parseError(response);
    return response.blob();
  },
};

export function createIdempotencyKey(scope: string): string {
  return `${scope}:${crypto.randomUUID()}`;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "发生未知错误";
}
