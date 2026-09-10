import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, Check, ExternalLink, FileSearch, X } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { ApiError, api, errorMessage, type DecisionAction, type FinancialFact, type PeriodType, type StatementScope } from "../api/client";
import { useUi } from "../app/useUi";
import { formatNumber } from "../lib/format";
import { Drawer } from "./Drawer";
import { StatusBadge } from "./StatusBadge";

interface ReviewFactDrawerProps {
  fact: FinancialFact | null;
  onClose: () => void;
  onCompleted: () => void;
  onReload: () => Promise<void>;
}

export function ReviewFactDrawer({ fact, onClose, onCompleted, onReload }: ReviewFactDrawerProps) {
  const { openEvidence } = useUi();
  const [action, setAction] = useState<DecisionAction>("VALIDATE");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [rawValue, setRawValue] = useState("");
  const [value, setValue] = useState("");
  const [sourceUnit, setSourceUnit] = useState("");
  const [targetUnit, setTargetUnit] = useState("");
  const [currency, setCurrency] = useState("");
  const [scope, setScope] = useState<StatementScope>("consolidated");
  const [periodType, setPeriodType] = useState<PeriodType>("duration");
  const [pageNo, setPageNo] = useState(1);
  const [tableName, setTableName] = useState("");
  const [rowLabel, setRowLabel] = useState("");
  const [columnLabel, setColumnLabel] = useState("");
  const [dirty, setDirty] = useState(false);
  const hasCompleteEvidence = Boolean(
    fact?.page_no && fact.table_name?.trim() && fact.row_label?.trim() && fact.column_label?.trim(),
  );
  const correctionHasCompleteEvidence = Boolean(
    pageNo >= 1 && tableName.trim() && rowLabel.trim() && columnLabel.trim(),
  );

  useEffect(() => {
    if (!fact) return;
    setAction("VALIDATE");
    setReason("");
    setNote("");
    setRawValue(fact.raw_value);
    setValue(String(fact.normalized_value));
    setSourceUnit(fact.source_unit);
    setTargetUnit(fact.target_unit);
    setCurrency(fact.currency);
    setScope(fact.statement_scope);
    setPeriodType(fact.period_type);
    setPageNo(fact.page_no ?? 1);
    setTableName(fact.table_name || "");
    setRowLabel(fact.row_label || "");
    setColumnLabel(fact.column_label || "");
    setDirty(false);
  }, [fact]);

  const decision = useMutation({
    mutationFn: () => {
      if (!fact) throw new Error("未选择待审核事实");
      return api.decideFact(fact.fact_key, {
        action,
        reason: reason.trim(),
        note: note.trim() || undefined,
        expected_version: fact.version,
        replacement: action === "CORRECT" ? {
          raw_value: rawValue,
          normalized_value: value,
          source_unit: sourceUnit,
          target_unit: targetUnit,
          currency,
          statement_scope: scope,
          period_type: periodType,
          page_no: pageNo,
          table_name: tableName.trim(),
          row_label: rowLabel.trim(),
          column_label: columnLabel.trim(),
        } : undefined,
      });
    },
    onSuccess: () => {
      setDirty(false);
      onCompleted();
    },
  });

  function requestClose() {
    if (dirty && !window.confirm("审核内容尚未提交，确定关闭吗？")) return;
    onClose();
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!reason.trim()) return;
    decision.mutate();
  }

  const conflict = decision.error instanceof ApiError && decision.error.status === 409;

  return (
    <Drawer open={Boolean(fact)} onClose={requestClose} title="复核事实" description={fact ? `版本 ${fact.version} · 单条审核` : undefined} width="wide" testId="review-drawer">
      {fact ? (
        <form className="review-form" onSubmit={submit} onChange={() => setDirty(true)}>
          <div className="review-form__status"><StatusBadge status={fact.validation_status} /><code>{fact.fact_key}</code></div>
          <section className="fact-comparison">
            <div>
              <span>抽取候选</span>
              <strong>{formatNumber(fact.raw_value)}</strong>
              <small>{fact.source_unit} · {fact.statement_scope}</small>
            </div>
            <div>
              <span>标准化结果</span>
              <strong>{formatNumber(fact.normalized_value)}</strong>
              <small>{fact.target_unit} · {fact.currency}</small>
            </div>
          </section>
          <dl className="metadata-grid fact-metadata">
            <div><dt>公司</dt><dd>{fact.company_id}</dd></div>
            <div><dt>期间</dt><dd>{fact.period}</dd></div>
            <div><dt>指标</dt><dd>{fact.metric}</dd></div>
            <div><dt>置信度</dt><dd>{fact.confidence == null ? "—" : `${Math.round(fact.confidence * 100)}%`}</dd></div>
          </dl>
          {fact.validation_issues.length ? (
            <div className="issue-box" role="note"><AlertTriangle size={17} aria-hidden="true" /><div><strong>校验问题</strong>{fact.validation_issues.map((issue) => <p key={`${issue.code}:${issue.field || ""}`}>{issue.message}{issue.field ? `（${issue.field}）` : ""}</p>)}</div></div>
          ) : null}
          <button className="evidence-link" type="button" disabled={fact.page_no == null} onClick={() => fact.page_no && openEvidence({
            document_id: fact.document_id,
            page_no: fact.page_no,
            snippet: "",
            table_name: fact.table_name,
            row_label: fact.row_label,
            column_label: fact.column_label,
          })}>
            <FileSearch size={18} aria-hidden="true" />{fact.page_no ? `查看第 ${fact.page_no} 页原文证据` : "缺少来源页码，不能定位原文"}<ExternalLink size={15} aria-hidden="true" />
          </button>
          <fieldset>
            <legend>审核决定</legend>
            <div className="decision-control">
              <button type="button" className={action === "VALIDATE" ? "is-selected" : ""} disabled={!hasCompleteEvidence} title={hasCompleteEvidence ? undefined : "补全页码和表格行列坐标后才能通过"} onClick={() => { setAction("VALIDATE"); setDirty(true); }} aria-pressed={action === "VALIDATE"}><Check size={16} aria-hidden="true" />通过</button>
              <button type="button" className={action === "CORRECT" ? "is-selected" : ""} onClick={() => { setAction("CORRECT"); setDirty(true); }} aria-pressed={action === "CORRECT"}><FileSearch size={16} aria-hidden="true" />纠正</button>
              <button type="button" className={action === "REJECT" ? "is-selected is-danger" : ""} onClick={() => { setAction("REJECT"); setDirty(true); }} aria-pressed={action === "REJECT"}><X size={16} aria-hidden="true" />拒绝</button>
            </div>
          </fieldset>
          {action === "CORRECT" ? (
            <fieldset className="correction-fields">
              <legend>纠正后的事实</legend>
              <label><span>原始值</span><input value={rawValue} onChange={(event) => setRawValue(event.target.value)} required inputMode="decimal" /></label>
              <label><span>标准化数值</span><input value={value} onChange={(event) => setValue(event.target.value)} required inputMode="decimal" /></label>
              <label><span>原始单位</span><input value={sourceUnit} onChange={(event) => setSourceUnit(event.target.value)} required /></label>
              <label><span>目标单位</span><input value={targetUnit} onChange={(event) => setTargetUnit(event.target.value)} required /></label>
              <label><span>币种</span><input value={currency} onChange={(event) => setCurrency(event.target.value)} required /></label>
              <label><span>报表口径</span><select value={scope} onChange={(event) => setScope(event.target.value as StatementScope)} required><option value="consolidated">合并口径</option><option value="parent">母公司口径</option></select></label>
              <label><span>期间类型</span><select value={periodType} onChange={(event) => setPeriodType(event.target.value as PeriodType)} required><option value="duration">期间值</option><option value="instant">时点值</option></select></label>
              <label><span>PDF 页码</span><input type="number" min="1" value={pageNo} onChange={(event) => setPageNo(Number(event.target.value))} required /></label>
              <label><span>表格名称</span><input value={tableName} onChange={(event) => setTableName(event.target.value)} required /></label>
              <label><span>行标签</span><input value={rowLabel} onChange={(event) => setRowLabel(event.target.value)} required /></label>
              <label><span>列标签</span><input value={columnLabel} onChange={(event) => setColumnLabel(event.target.value)} required /></label>
            </fieldset>
          ) : null}
          <label className="field"><span>审核原因 <b aria-hidden="true">*</b></span><textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={3} required aria-describedby="reason-help" /></label>
          <p id="reason-help" className="field-help">原因将写入不可变审核事件。</p>
          <label className="field"><span>补充备注</span><textarea value={note} onChange={(event) => setNote(event.target.value)} rows={2} /></label>
          {decision.isError ? (
            <div className="submit-error" role="alert">
              <strong>{conflict ? "版本冲突，审核未提交" : "审核提交失败"}</strong>
              <p>{errorMessage(decision.error)}</p>
              {conflict ? <button type="button" className="button button--secondary" onClick={() => void onReload()}>刷新最新版本</button> : null}
            </div>
          ) : null}
          <div className="drawer-actions">
            <button className="button button--quiet" type="button" onClick={requestClose}>取消</button>
            <button className={action === "REJECT" ? "button button--danger" : "button button--primary"} type="submit" disabled={!reason.trim() || decision.isPending || (action === "VALIDATE" && !hasCompleteEvidence) || (action === "CORRECT" && !correctionHasCompleteEvidence)}>
              {decision.isPending ? "正在提交" : "提交审核"}
            </button>
          </div>
        </form>
      ) : null}
    </Drawer>
  );
}
