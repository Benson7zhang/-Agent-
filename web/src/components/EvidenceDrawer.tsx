import { ExternalLink, FileWarning, LocateFixed } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Evidence } from "../api/client";
import { useUi } from "../app/useUi";
import { Drawer } from "./Drawer";

function PdfPage({ evidence }: { evidence: Evidence }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [attempt, setAttempt] = useState(0);

  const retry = useCallback(() => {
    setStatus("loading");
    setAttempt((value) => value + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let loadingTask: { destroy: () => Promise<void> } | undefined;

    async function renderPage() {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();
        const task = pdfjs.getDocument(api.documentContentUrl(evidence.document_id));
        loadingTask = task;
        const pdf = await task.promise;
        const page = await pdf.getPage(evidence.page_no);
        const viewport = page.getViewport({ scale: 1.4 });
        const canvas = canvasRef.current;
        const context = canvas?.getContext("2d");
        if (!canvas || !context || cancelled) return;
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        await page.render({ canvasContext: context, viewport }).promise;
        if (!cancelled) setStatus("ready");
      } catch {
        if (!cancelled) setStatus("error");
      }
    }

    void renderPage();
    return () => {
      cancelled = true;
      void loadingTask?.destroy();
    };
  }, [attempt, evidence.document_id, evidence.page_no]);

  return (
    <div className="pdf-viewer" data-page-no={evidence.page_no}>
      {status === "loading" ? <p className="pdf-viewer__status">正在加载第 {evidence.page_no} 页…</p> : null}
      {status === "error" ? (
        <div className="pdf-viewer__error" role="alert">
          <FileWarning size={22} aria-hidden="true" />
          <p>PDF 页面加载失败。请确认文档仍可访问。</p>
          <button className="button button--secondary" type="button" onClick={retry}>重新加载</button>
        </div>
      ) : null}
      <div className="pdf-stage" hidden={status === "error"}>
        <canvas ref={canvasRef} aria-label={`PDF 第 ${evidence.page_no} 页`} />
        {evidence.bbox ? (
          <span
            className="pdf-highlight"
            data-testid="pdf-highlight"
            style={{
              left: `${evidence.bbox.x * 100}%`,
              top: `${evidence.bbox.y * 100}%`,
              width: `${evidence.bbox.width * 100}%`,
              height: `${evidence.bbox.height * 100}%`,
            }}
          />
        ) : null}
      </div>
    </div>
  );
}

export function EvidenceDrawer() {
  const { evidence, closeEvidence } = useUi();
  return (
    <Drawer
      open={Boolean(evidence)}
      onClose={closeEvidence}
      title="原文证据"
      description={evidence ? `已定位到 PDF 第 ${evidence.page_no} 页` : undefined}
      width="wide"
      testId="evidence-drawer"
    >
      {evidence ? (
        <>
          <section className="evidence-summary" aria-label="证据详情">
            <div className="evidence-summary__location">
              <LocateFixed size={18} aria-hidden="true" />
              <strong>第 {evidence.page_no} 页</strong>
              <span>{evidence.bbox ? "已定位原文坐标" : "已定位到页码，暂无精确坐标"}</span>
            </div>
            <blockquote>{evidence.snippet || "该证据未提供文本片段。"}</blockquote>
            <dl className="metadata-grid">
              <div><dt>表格</dt><dd>{evidence.table_name || "—"}</dd></div>
              <div><dt>行</dt><dd>{evidence.row_label || "—"}</dd></div>
              <div><dt>列</dt><dd>{evidence.column_label || "—"}</dd></div>
            </dl>
          </section>
          <div className="pdf-toolbar">
            <span>PDF 原页</span>
            <a className="button button--quiet" href={api.documentContentUrl(evidence.document_id)} target="_blank" rel="noreferrer">
              <ExternalLink size={16} aria-hidden="true" />
              新窗口打开
            </a>
          </div>
          <PdfPage evidence={evidence} />
        </>
      ) : null}
    </Drawer>
  );
}
