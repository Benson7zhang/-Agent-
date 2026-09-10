import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type DocumentRecord, type FinancialFact } from "../api/client";
import { UiProvider } from "../app/UiProvider";
import { DataManagementPage } from "./DataManagementPage";

const document: DocumentRecord = {
  document_id: "document-1",
  kind: "financial_report",
  original_name: "report.pdf",
  sha256: "0".repeat(64),
  size_bytes: 1024,
  mime_type: "application/pdf",
  status: "NEEDS_REVIEW",
  created_at: "2026-09-09T00:00:00Z",
};

const fact: FinancialFact = {
  fact_key: "fact-1",
  company_id: "600080",
  stock_code: "600080",
  period: "2025Q3",
  statement_scope: "consolidated",
  period_type: "duration",
  metric: "total_profit",
  raw_value: "100",
  normalized_value: "100",
  source_unit: "万元",
  target_unit: "万元",
  currency: "CNY",
  document_id: "document-1",
  page_no: 8,
  table_name: "合并利润表",
  row_label: "利润总额",
  column_label: "本期金额",
  confidence: 0.9,
  validation_status: "NEEDS_REVIEW",
  validation_issues: [],
  version: 1,
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <UiProvider>
        <DataManagementPage />
      </UiProvider>
    </QueryClientProvider>,
  );
}

describe("DataManagementPage collection totals", () => {
  it("shows server totals instead of the current page lengths", async () => {
    vi.spyOn(api, "listDocuments").mockResolvedValue({
      items: [document],
      next_cursor: "50",
      total: 785,
    });
    vi.spyOn(api, "listFacts").mockResolvedValue({
      items: [fact],
      next_cursor: "50",
      total: 31_110,
    });

    renderPage();

    expect(await screen.findByText("785 个文件")).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "事实审核" }));
    expect(await screen.findByText(/共 31,110 条/)).toBeVisible();
  });
});
