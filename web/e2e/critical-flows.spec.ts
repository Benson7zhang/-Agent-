import { expect, test, type Page, type Route } from "@playwright/test";

const now = "2026-09-08T10:00:00Z";

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function prepare(page: Page, handler?: (route: Route) => Promise<boolean>) {
  await page.addInitScript(() => localStorage.clear());
  await page.route("**/api/v1/**", async (route) => {
    if (handler && await handler(route)) return;
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "GET" && url.pathname === "/api/v1/jobs") {
      await json(route, { items: [], next_cursor: null });
      return;
    }
    if (request.method() === "GET" && url.pathname === "/api/v1/documents") {
      await json(route, { items: [], next_cursor: null, total: 0 });
      return;
    }
    if (request.method() === "GET" && url.pathname === "/api/v1/facts") {
      await json(route, { items: [], next_cursor: null, total: 0 });
      return;
    }
    await json(route, { error: { code: "NOT_FOUND", message: "测试路由未配置", details: url.pathname, request_id: "e2e" } }, 404);
  });
}

test("问答从澄清进入可信答案，并可回查 PDF 页码和查询", async ({ page }) => {
  let turnCount = 0;
  await prepare(page, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === "/api/v1/conversations") {
      await json(route, { conversation_id: "conv-1", title: "营业收入", version: 1, state: {}, turns: [], created_at: now, updated_at: now });
      return true;
    }
    if (request.method() === "POST" && path === "/api/v1/conversations/conv-1/turns") {
      turnCount += 1;
      const verified = turnCount === 2;
      const answer = verified ? {
        status: "VERIFIED", text: "华星制造 2025 年营业收入为 128.6 亿元。", clarification: null,
        company: "华星制造", period: "2025FY", unit: "亿元", currency: "CNY", statement_scope: "consolidated",
        formula: "营业收入", values: [], chart_spec: null,
        table: { columns: [{ key: "period", label: "期间" }, { key: "value", label: "营业收入", unit: "亿元" }], rows: [{ period: "2025FY", value: 128.6 }] },
        evidence: [{ document_id: "doc-1", page_no: 12, snippet: "营业收入 12,860,000,000.00", table_name: "合并利润表", row_label: "营业收入", column_label: "本期金额", bbox: null }],
      } : {
        status: "NEEDS_CLARIFICATION", text: "请明确需要查询的公司和报告期。", clarification: "请补充公司和报告期", values: [], chart_spec: null, table: null, evidence: [],
      };
      const turn = { turn_id: `turn-${turnCount}`, conversation_id: "conv-1", sequence: turnCount, question: verified ? "华星制造 2025 年" : "营业收入是多少？", status: verified ? "COMPLETED" : "NEEDS_CLARIFICATION", answer, query_run_id: verified ? "run-1" : null, created_at: now };
      await json(route, { turn, conversation: { conversation_id: "conv-1", title: "营业收入", version: turnCount + 1, state: {}, turns: verified ? [turn] : [turn], created_at: now, updated_at: now } });
      return true;
    }
    if (request.method() === "GET" && path === "/api/v1/query-runs/run-1") {
      await json(route, { query_run_id: "run-1", conversation_id: "conv-1", question: "华星制造 2025 年", query_spec: { metric: "revenue" }, sql: "SELECT normalized_value FROM financial_fact WHERE fact_key = ?", parameters: ["fact-1"], answer_result: { status: "VERIFIED", text: "可信答案", values: [], evidence: [] }, status: "SUCCEEDED", created_at: now });
      return true;
    }
    return false;
  });

  await page.goto("/workspace");
  await page.getByLabel("财务问题").fill("营业收入是多少？");
  await page.getByLabel("发送问题").click();
  await expect(page.getByText("需澄清")).toBeVisible();
  await page.getByLabel("财务问题").fill("华星制造 2025 年");
  await page.getByLabel("发送问题").click();
  await expect(page.getByText("华星制造 2025 年营业收入为 128.6 亿元。")).toBeVisible();
  await page.getByRole("button", { name: /第 12 页/ }).click();
  await expect(page.getByTestId("evidence-drawer")).toContainText("已定位到页码，暂无精确坐标");
  await expect(page.locator(".pdf-viewer")).toHaveAttribute("data-page-no", "12");
  await expect(page.getByTestId("pdf-highlight")).toHaveCount(0);
  await page.getByRole("dialog", { name: "原文证据" }).getByLabel("关闭原文证据").click();
  await page.getByRole("button", { name: "查看查询详情" }).click();
  await expect(page.getByTestId("query-drawer")).toContainText("SELECT normalized_value");
});

test("审核使用 expected_version 并显式展示 409 冲突", async ({ page }) => {
  let submittedDecision: Record<string, unknown> = {};
  const fact = {
    fact_key: "fact-1", company_id: "华星制造", stock_code: "600001", period: "2025FY", statement_scope: "consolidated", period_type: "duration",
    metric: "营业收入", raw_value: "12860000000", normalized_value: "128600", source_unit: "元", target_unit: "万元", currency: "CNY", document_id: "doc-1", page_no: 12,
    table_name: "合并利润表", row_label: "营业收入", column_label: "本期金额", confidence: 0.82, validation_status: "NEEDS_REVIEW", validation_issues: [{ code: "UNIT_REVIEW", message: "请核对表头单位", field: "source_unit" }], version: 4,
  };
  await prepare(page, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path === "/api/v1/facts") {
      await json(route, { items: [fact], next_cursor: null, total: 1 });
      return true;
    }
    if (request.method() === "POST" && path.endsWith("/facts/fact-1/decisions")) {
      submittedDecision = request.postDataJSON() as Record<string, unknown>;
      await json(route, { error: { code: "FACT_VERSION_CONFLICT", message: "事实已由其他审核者更新", details: { actual_version: 5 }, request_id: "req-conflict" } }, 409);
      return true;
    }
    return false;
  });

  await page.goto("/data");
  await page.getByRole("tab", { name: "事实审核" }).click();
  await page.getByRole("button", { name: "复核" }).click();
  await page.getByRole("button", { name: "纠正" }).click();
  await page.getByLabel("原始值").fill("12860050000");
  await page.getByLabel("标准化数值").fill("128600.50");
  await page.getByLabel(/审核原因/).fill("与原文表头和当前期列完成核对");
  await page.getByRole("button", { name: "提交审核" }).click();
  await expect(page.getByText("版本冲突，审核未提交")).toBeVisible();
  expect(submittedDecision).toMatchObject({
    action: "CORRECT",
    expected_version: 4,
    replacement: {
      raw_value: "12860050000",
      normalized_value: "128600.50",
      source_unit: "元",
      target_unit: "万元",
      currency: "CNY",
      statement_scope: "consolidated",
      period_type: "duration",
      page_no: 12,
      table_name: "合并利润表",
      row_label: "营业收入",
      column_label: "本期金额",
    },
  });
});

test("纠错成功时提交用户编辑后的完整事实", async ({ page }) => {
  let submittedDecision: Record<string, unknown> = {};
  const fact = {
    fact_key: "fact-correct", company_id: "华星制造", stock_code: "600001", period: "2025FY", statement_scope: "consolidated", period_type: "duration",
    metric: "营业收入", raw_value: "12860000000", normalized_value: "128600", source_unit: "元", target_unit: "万元", currency: "CNY", document_id: "doc-1", page_no: 12,
    table_name: "合并利润表", row_label: "营业收入", column_label: "本期金额", confidence: 0.82, validation_status: "NEEDS_REVIEW", validation_issues: [], version: 4,
  };
  await prepare(page, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path === "/api/v1/facts") {
      await json(route, { items: [fact], next_cursor: null, total: 1 });
      return true;
    }
    if (request.method() === "POST" && path.endsWith("/facts/fact-correct/decisions")) {
      submittedDecision = request.postDataJSON() as Record<string, unknown>;
      await json(route, { fact: { ...fact, fact_key: "fact-corrected", raw_value: "12860050000", normalized_value: "128600.50", validation_status: "VALIDATED", version: 1 }, event_id: "event-1", version: 1 });
      return true;
    }
    return false;
  });

  await page.goto("/data");
  await page.getByRole("tab", { name: "事实审核" }).click();
  await page.getByRole("button", { name: "复核" }).click();
  await page.getByRole("button", { name: "纠正" }).click();
  await page.getByLabel("原始值").fill("12860050000");
  await page.getByLabel("标准化数值").fill("128600.50");
  await page.getByLabel(/审核原因/).fill("原始值末位识别错误，已逐字核对");
  await page.getByRole("button", { name: "提交审核" }).click();

  await expect(page.getByText("审核决定已写入事件记录。")).toBeVisible();
  expect(submittedDecision).toMatchObject({
    action: "CORRECT",
    reason: "原始值末位识别错误，已逐字核对",
    expected_version: 4,
    replacement: {
      raw_value: "12860050000",
      normalized_value: "128600.50",
      source_unit: "元",
      target_unit: "万元",
      currency: "CNY",
      statement_scope: "consolidated",
      period_type: "duration",
      page_no: 12,
      table_name: "合并利润表",
      row_label: "营业收入",
      column_label: "本期金额",
    },
  });
});

test("轮次执行失败后同步服务端版本并可继续提问", async ({ page }) => {
  let failedTurnPersisted = false;
  let secondTurnExpectedVersion: number | undefined;
  const failedTurn = {
    turn_id: "turn-failed", conversation_id: "conv-failed", sequence: 1, question: "查询营业收入", status: "FAILED",
    answer: { status: "FAILED", text: "查询执行失败。", clarification: null, values: [], chart_spec: null, table: null, evidence: [] },
    query_run_id: null, created_at: now,
  };
  await prepare(page, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === "/api/v1/conversations") {
      await json(route, { conversation_id: "conv-failed", title: "查询营业收入", version: 1, state: {}, turns: [], created_at: now, updated_at: now });
      return true;
    }
    if (request.method() === "GET" && path === "/api/v1/conversations/conv-failed") {
      await json(route, { conversation_id: "conv-failed", title: "查询营业收入", version: failedTurnPersisted ? 2 : 1, state: {}, turns: failedTurnPersisted ? [failedTurn] : [], created_at: now, updated_at: now });
      return true;
    }
    if (request.method() === "POST" && path === "/api/v1/conversations/conv-failed/turns") {
      const body = request.postDataJSON() as { expected_version: number; question: string };
      if (!failedTurnPersisted) {
        failedTurnPersisted = true;
        await json(route, { error: { code: "QUERY_EXECUTION_FAILED", message: "数据库查询超时", details: null, request_id: "req-failed" } }, 500);
        return true;
      }
      secondTurnExpectedVersion = body.expected_version;
      const completedTurn = {
        turn_id: "turn-completed", conversation_id: "conv-failed", sequence: 2, question: body.question, status: "COMPLETED",
        answer: { status: "VERIFIED", text: "已使用同步后的会话继续回答。", clarification: null, values: [], chart_spec: null, table: null, evidence: [] },
        query_run_id: "run-after-failure", created_at: now,
      };
      await json(route, { turn: completedTurn, conversation: { conversation_id: "conv-failed", title: "查询营业收入", version: 3, state: {}, turns: [failedTurn, completedTurn], created_at: now, updated_at: now } });
      return true;
    }
    return false;
  });

  await page.goto("/workspace");
  await page.getByLabel("财务问题").fill("查询营业收入");
  await page.getByLabel("发送问题").click();
  await expect(page.getByText(/已同步服务端会话状态/)).toBeVisible();
  await expect(page.getByText("查询执行失败。")).toBeVisible();
  await page.getByLabel("财务问题").fill("改查 2025 年营业收入");
  await page.getByLabel("发送问题").click();
  await expect(page.getByText("已使用同步后的会话继续回答。")).toBeVisible();
  expect(secondTurnExpectedVersion).toBe(2);
});

test("上传创建失败任务后可显式重试", async ({ page }) => {
  let jobCreated = false;
  let retryBody: { idempotency_key?: string } = {};
  const failedJob = { job_id: "job-1", document_id: "doc-1", kind: "document_ingestion", status: "FAILED", stage: "表格解析", progress: 42, attempt: 1, error_code: "PARSE_FAILED", error_message: "无法恢复跨页表格", created_at: now, started_at: now, finished_at: now };
  await prepare(page, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === "/api/v1/documents") {
      await json(route, { document_id: "doc-1", kind: "financial_report", original_name: "report.pdf", sha256: "abc123", size_bytes: 20, mime_type: "application/pdf", status: "STORED", created_at: now });
      return true;
    }
    if (request.method() === "POST" && path === "/api/v1/jobs") {
      jobCreated = true;
      await json(route, failedJob);
      return true;
    }
    if (request.method() === "GET" && path === "/api/v1/jobs") {
      await json(route, { items: jobCreated ? [failedJob] : [], next_cursor: null });
      return true;
    }
    if (request.method() === "POST" && path === "/api/v1/jobs/job-1/retry") {
      retryBody = request.postDataJSON();
      await json(route, { ...failedJob, job_id: "job-2", status: "QUEUED", progress: 0, attempt: 2, error_code: null, error_message: null });
      return true;
    }
    return false;
  });

  await page.goto("/data");
  await page.locator('input[type="file"]').setInputFiles({ name: "report.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n%%EOF") });
  await expect(page.getByTestId("job-drawer")).toContainText("无法恢复跨页表格");
  await page.getByRole("button", { name: "重试任务" }).click();
  expect(retryBody.idempotency_key).toMatch(/^retry:job-1:/);
});

test("当前视口没有页面级横向溢出", async ({ page }) => {
  await prepare(page);
  await page.goto("/data");
  const dimensions = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.client);
});
