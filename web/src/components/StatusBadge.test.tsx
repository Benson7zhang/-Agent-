import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it("uses text as well as color for answer status", () => {
    render(<StatusBadge status="INSUFFICIENT_EVIDENCE" />);
    expect(screen.getByText("证据不足")).toBeVisible();
  });

  it("renders the stored document state", () => {
    render(<StatusBadge status="STORED" />);
    expect(screen.getByText("已存储")).toBeVisible();
  });
});
