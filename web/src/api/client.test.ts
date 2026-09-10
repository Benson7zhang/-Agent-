import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";

describe("API client", () => {
  afterEach(() => vi.restoreAllMocks());

  it("preserves structured service errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      error: { code: "VERSION_CONFLICT", message: "事实版本已更新", details: { actual: 3 }, request_id: "req-1" },
    }), { status: 409, headers: { "Content-Type": "application/json" } }));

    await expect(api.decideFact("fact-1", {
      action: "VALIDATE",
      reason: "已与原文核对",
      expected_version: 2,
    })).rejects.toMatchObject({
      status: 409,
      code: "VERSION_CONFLICT",
      requestId: "req-1",
      message: "事实版本已更新",
    });
  });

  it("sends retry idempotency key in the request body", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ job_id: "job-2" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));

    await api.retryJob("job-1", "retry:job-1:key");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/jobs/job-1/retry", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ idempotency_key: "retry:job-1:key" }),
    }));
  });
});
