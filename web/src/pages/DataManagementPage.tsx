import { flexRender, getCoreRowModel, getSortedRowModel, useReactTable, type ColumnDef, type SortingState } from "@tanstack/react-table";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, ArrowUpDown, FileUp, RefreshCw, Search, UploadCloud } from "lucide-react";
import { type ChangeEvent, useMemo, useRef, useState } from "react";
import {
  api,
  createIdempotencyKey,
  errorMessage,
  type DocumentKind,
  type DocumentRecord,
  type FinancialFact,
} from "../api/client";
import { useUi } from "../app/useUi";
import { EmptyState, ErrorState, LoadingState } from "../components/Feedback";
import { ReviewFactDrawer } from "../components/ReviewFactDrawer";
import { StatusBadge } from "../components/StatusBadge";
import { formatDateTime, formatFileSize, formatNumber } from "../lib/format";
import { documentKindLabels } from "../lib/labels";

type Tab = "documents" | "reviews";

function SortIcon({ direction }: { direction: false | "asc" | "desc" }) {
  if (direction === "asc") return <ArrowUp size={14} aria-hidden="true" />;
  if (direction === "desc") return <ArrowDown size={14} aria-hidden="true" />;
  return <ArrowUpDown size={14} aria-hidden="true" />;
}

function DocumentsTable({ documents }: { documents: DocumentRecord[] }) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "created_at", desc: true }]);
  const columns = useMemo<ColumnDef<DocumentRecord>[]>(() => [
    { accessorKey: "original_name", header: "文件名称", cell: ({ row }) => <div className="primary-cell"><strong>{row.original.original_name}</strong><small className="mono">{row.original.sha256.slice(0, 12)}</small></div> },
    { accessorKey: "kind", header: "资料类型", cell: ({ getValue }) => documentKindLabels[getValue<DocumentKind>()] },
    { accessorKey: "status", header: "状态", cell: ({ getValue }) => <StatusBadge status={getValue<DocumentRecord["status"]>()} /> },
    { accessorKey: "size_bytes", header: "大小", cell: ({ getValue }) => formatFileSize(getValue<number>()) },
    { accessorKey: "created_at", header: "上传时间", cell: ({ getValue }) => formatDateTime(getValue<string>()) },
  ], []);
  const table = useReactTable({ data: documents, columns, state: { sorting }, onSortingChange: setSorting, getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel() });
  return (
    <div className="table-frame">
      <table className="data-table documents-table">
        <thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => {
          const direction = header.column.getIsSorted();
          return <th key={header.id} scope="col" aria-sort={direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none"}>
            <button type="button" onClick={header.column.getToggleSortingHandler()}>{flexRender(header.column.columnDef.header, header.getContext())}<SortIcon direction={direction} /></button>
          </th>;
        })}</tr>)}</thead>
        <tbody>{table.getRowModel().rows.map((row) => <tr key={row.id} className={row.original.error_message ? "has-error" : ""}>{row.getVisibleCells().map((cell) => <td key={cell.id} data-label={String(cell.column.columnDef.header)} title={cell.column.id === "status" ? row.original.error_message || undefined : undefined}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

function FactsTable({ facts, onReview }: { facts: FinancialFact[]; onReview: (fact: FinancialFact) => void }) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const columns = useMemo<ColumnDef<FinancialFact>[]>(() => [
    { accessorKey: "company_id", header: "公司", cell: ({ row }) => <div className="primary-cell"><strong>{row.original.company_id}</strong><small>{row.original.stock_code || "无证券代码"}</small></div> },
    { accessorKey: "period", header: "期间" },
    { accessorKey: "metric", header: "指标" },
    { accessorKey: "normalized_value", header: "标准化数值", cell: ({ row }) => <span className="tabular">{formatNumber(row.original.normalized_value)} {row.original.target_unit}</span> },
    { accessorKey: "confidence", header: "置信度", cell: ({ getValue }) => { const value = getValue<number | null>(); return value == null ? "—" : `${Math.round(value * 100)}%`; } },
    { accessorKey: "validation_status", header: "状态", cell: ({ getValue }) => <StatusBadge status={getValue<FinancialFact["validation_status"]>()} /> },
    { id: "action", header: "操作", enableSorting: false, cell: ({ row }) => <button className="button button--secondary table-action" type="button" onClick={() => onReview(row.original)}>复核</button> },
  ], [onReview]);
  const table = useReactTable({ data: facts, columns, state: { sorting }, onSortingChange: setSorting, getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel() });
  return (
    <div className="table-frame">
      <table className="data-table facts-table">
        <thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => {
          const direction = header.column.getIsSorted();
          return <th key={header.id} scope="col" aria-sort={header.column.getCanSort() ? (direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none") : undefined}>
            {header.column.getCanSort() ? <button type="button" onClick={header.column.getToggleSortingHandler()}>{flexRender(header.column.columnDef.header, header.getContext())}<SortIcon direction={direction} /></button> : flexRender(header.column.columnDef.header, header.getContext())}
          </th>;
        })}</tr>)}</thead>
        <tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id} data-label={String(cell.column.columnDef.header)}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

async function validatePdf(file: File): Promise<void> {
  if (!file.name.toLowerCase().endsWith(".pdf")) throw new Error("仅支持 PDF 文件。");
  if (file.size === 0) throw new Error("不能上传空文件。");
  const signature = new TextDecoder("ascii").decode(await file.slice(0, 5).arrayBuffer());
  if (signature !== "%PDF-") throw new Error("文件签名不是有效的 PDF。");
}

export function DataManagementPage() {
  const [tab, setTab] = useState<Tab>("documents");
  const [selectedFact, setSelectedFact] = useState<FinancialFact | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const { setJobsOpen } = useUi();

  const documents = useQuery({ queryKey: ["documents"], queryFn: () => api.listDocuments(), enabled: tab === "documents" });
  const facts = useQuery({ queryKey: ["facts", "NEEDS_REVIEW"], queryFn: () => api.listFacts("NEEDS_REVIEW"), enabled: tab === "reviews" });

  const upload = useMutation({
    mutationFn: async (file: File) => {
      await validatePdf(file);
      const document = await api.uploadDocument(file, "financial_report");
      const job = await api.createJob(document.document_id, createIdempotencyKey(`ingest:${document.sha256}`));
      return { document, job };
    },
    onSuccess: async () => {
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["documents"] }), queryClient.invalidateQueries({ queryKey: ["jobs"] })]);
      setNotice("文件已上传，处理任务已创建。");
      setJobsOpen(true);
      if (fileInput.current) fileInput.current.value = "";
    },
  });

  function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (file) upload.mutate(file);
  }

  async function reloadSelectedFact() {
    const result = await facts.refetch();
    const refreshed = result.data?.items.find((fact) => fact.fact_key === selectedFact?.fact_key) ?? null;
    setSelectedFact(refreshed);
  }

  async function completedReview() {
    setSelectedFact(null);
    setNotice("审核决定已写入事件记录。");
    await queryClient.invalidateQueries({ queryKey: ["facts"] });
  }

  return (
    <div className="data-page">
      <header className="page-heading data-heading">
        <div><p className="eyebrow">DATA CONTROL</p><h1>数据管理</h1></div>
        <button className="button button--quiet" type="button" onClick={() => tab === "documents" ? void documents.refetch() : void facts.refetch()}>
          <RefreshCw size={17} aria-hidden="true" />刷新
        </button>
      </header>
      {notice ? <div className="notice" role="status"><span>{notice}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示">关闭</button></div> : null}
      <div className="page-tabs" role="tablist" aria-label="数据管理分类">
        <button id="documents-tab" role="tab" aria-selected={tab === "documents"} aria-controls="documents-panel" className={tab === "documents" ? "is-selected" : ""} type="button" onClick={() => setTab("documents")}>财报资料</button>
        <button id="reviews-tab" role="tab" aria-selected={tab === "reviews"} aria-controls="reviews-panel" className={tab === "reviews" ? "is-selected" : ""} type="button" onClick={() => setTab("reviews")}>事实审核</button>
      </div>
      {tab === "documents" ? (
        <section id="documents-panel" role="tabpanel" aria-labelledby="documents-tab" className="data-panel">
          <div className="upload-band">
            <div className="upload-band__lead"><UploadCloud size={25} aria-hidden="true" /><div><h2>导入 PDF</h2><p>文件先存储，再由持久化任务解析。</p></div></div>
            <div className="upload-actions">
              <span className="upload-kind">财务报告</span>
              <label className="button button--primary file-button">
                <FileUp size={17} aria-hidden="true" />{upload.isPending ? "正在上传" : "选择 PDF"}
                <input ref={fileInput} type="file" accept="application/pdf,.pdf" onChange={selectFile} disabled={upload.isPending} />
              </label>
            </div>
            {upload.isError ? <p className="upload-error" role="alert">{errorMessage(upload.error)}</p> : null}
          </div>
          <div className="section-heading"><div><h2>资料记录</h2><p>仅显示服务端已登记的文件。</p></div><span>{formatNumber(documents.data?.total ?? 0)} 个文件</span></div>
          {documents.isLoading ? <LoadingState label="正在读取资料" /> : null}
          {documents.isError ? <ErrorState message={errorMessage(documents.error)} onRetry={() => void documents.refetch()} /> : null}
          {documents.data?.items.length === 0 ? <EmptyState title="暂无资料" detail="导入 PDF 后，文件及处理状态会显示在这里。" /> : null}
          {documents.data?.items.length ? <DocumentsTable documents={documents.data.items} /> : null}
        </section>
      ) : (
        <section id="reviews-panel" role="tabpanel" aria-labelledby="reviews-tab" className="data-panel">
          <div className="review-toolbar">
            <div><h2>待复核事实</h2><p>共 {formatNumber(facts.data?.total ?? 0)} 条，每次只提交一条决定并校验事实版本。</p></div>
            <label className="search-field"><Search size={17} aria-hidden="true" /><span className="sr-only">筛选待复核事实</span><input type="search" placeholder="筛选由服务端接口提供" disabled title="当前 API 尚未提供文本搜索参数" /></label>
          </div>
          {facts.isLoading ? <LoadingState label="正在读取审核队列" /> : null}
          {facts.isError ? <ErrorState message={errorMessage(facts.error)} onRetry={() => void facts.refetch()} /> : null}
          {facts.data?.items.length === 0 ? <EmptyState title="没有待复核事实" detail="新的抽取候选入库后会进入此队列。" /> : null}
          {facts.data?.items.length ? <FactsTable facts={facts.data.items} onReview={setSelectedFact} /> : null}
        </section>
      )}
      <ReviewFactDrawer fact={selectedFact} onClose={() => setSelectedFact(null)} onCompleted={() => void completedReview()} onReload={reloadSelectedFact} />
    </div>
  );
}
