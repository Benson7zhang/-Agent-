import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FinancialFact } from "../api/client";
import { UiProvider } from "../app/UiProvider";
import { ReviewFactDrawer } from "./ReviewFactDrawer";

const incompleteFact: FinancialFact = {
  fact_key: "fact-incomplete",
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
  table_name: "income_sheet",
  row_label: "利润总额",
  column_label: null,
  confidence: 0.8,
  validation_status: "NEEDS_REVIEW",
  validation_issues: [],
  version: 1,
};

afterEach(cleanup);

function renderDrawer() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <UiProvider>
        <ReviewFactDrawer
          fact={incompleteFact}
          onClose={vi.fn()}
          onCompleted={vi.fn()}
          onReload={vi.fn(async () => undefined)}
        />
      </UiProvider>
    </QueryClientProvider>,
  );
}

describe("ReviewFactDrawer evidence gate", () => {
  it("blocks direct validation when any evidence coordinate is missing", () => {
    renderDrawer();

    expect(screen.getByRole("button", { name: "通过" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/审核原因/), { target: { value: "已核对原文" } });
    expect(screen.getByRole("button", { name: "提交审核" })).toBeDisabled();
  });

  it("requires complete table coordinates for a correction", () => {
    renderDrawer();

    fireEvent.click(screen.getByRole("button", { name: "纠正" }));
    expect(screen.getByLabelText("表格名称")).toBeRequired();
    expect(screen.getByLabelText("行标签")).toBeRequired();
    expect(screen.getByLabelText("列标签")).toBeRequired();
    fireEvent.change(screen.getByLabelText(/审核原因/), { target: { value: "补全证据" } });
    expect(screen.getByRole("button", { name: "提交审核" })).toBeDisabled();
  });
});
