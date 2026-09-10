import { useState } from "react";
import { BarChart, LineChart } from "echarts/charts";
import { AriaComponent, GridComponent, LegendComponent, TitleComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import ReactEChartsCore from "echarts-for-react/lib/core";
import { BarChart3, Braces, FileSearch, Table2 } from "lucide-react";
import type { AnswerResult, ChartSpec } from "../api/client";
import { useUi } from "../app/useUi";
import { formatNumber } from "../lib/format";
import { StatusBadge } from "./StatusBadge";

echarts.use([BarChart, LineChart, AriaComponent, GridComponent, LegendComponent, TitleComponent, TooltipComponent, CanvasRenderer]);

function chartOption(spec: ChartSpec, reduceMotion: boolean) {
  const horizontal = spec.type === "horizontal_bar";
  return {
    animation: !reduceMotion,
    color: ["#1f5d4e", "#356e9f", "#a66c18", "#8f3f3f"],
    aria: { enabled: true, decal: { show: true } },
    title: spec.title ? { text: spec.title, left: 8, top: 4, textStyle: { fontSize: 14, fontWeight: 600, color: "#17211e" } } : undefined,
    grid: { left: horizontal ? 92 : 48, right: 24, top: spec.title ? 48 : 24, bottom: 44, containLabel: true },
    tooltip: { trigger: "axis", valueFormatter: (value: number) => formatNumber(value) },
    legend: { top: spec.title ? 27 : 4, right: 12, textStyle: { color: "#4c5b56" } },
    xAxis: horizontal
      ? { type: "value", name: spec.series[0]?.unit || "", nameLocation: "middle", nameGap: 30, splitLine: { lineStyle: { color: "#e2e6e3" } } }
      : { type: "category", data: spec.x_axis.categories, name: spec.x_axis.label || "", axisLabel: { color: "#51605b", hideOverlap: true } },
    yAxis: horizontal
      ? { type: "category", data: spec.x_axis.categories, axisLabel: { color: "#51605b" } }
      : { type: "value", name: spec.series[0]?.unit || "", nameTextStyle: { color: "#51605b" }, splitLine: { lineStyle: { color: "#e2e6e3" } } },
    series: spec.series.map((series) => ({
      name: series.name,
      type: spec.type === "line" ? "line" : "bar",
      data: series.values,
      symbolSize: 8,
      smooth: false,
      barMaxWidth: 28,
      emphasis: { focus: "series" },
    })),
  };
}

function AnswerData({ answer, queryRunId }: { answer: AnswerResult; queryRunId?: string | null }) {
  const [view, setView] = useState<"chart" | "table">(answer.chart_spec ? "chart" : "table");
  const specMatchesRun = !answer.chart_spec || !queryRunId || answer.chart_spec.source_query_run_id === queryRunId;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const hasChart = Boolean(answer.chart_spec && specMatchesRun);
  const hasTable = Boolean(answer.table);

  if (!answer.chart_spec && !answer.table) return null;

  return (
    <section className="answer-data" aria-label="答案数据">
      <div className="answer-data__toolbar">
        <div className="segmented-control" aria-label="数据展示方式">
          <button type="button" className={view === "chart" ? "is-selected" : ""} onClick={() => setView("chart")} disabled={!hasChart} aria-pressed={view === "chart"}>
            <BarChart3 size={16} aria-hidden="true" />图表
          </button>
          <button type="button" className={view === "table" ? "is-selected" : ""} onClick={() => setView("table")} disabled={!hasTable} aria-pressed={view === "table"}>
            <Table2 size={16} aria-hidden="true" />数据表
          </button>
        </div>
        {answer.chart_spec?.series[0]?.unit ? <span>单位：{answer.chart_spec.series[0].unit}</span> : null}
      </div>
      {!specMatchesRun ? (
        <p className="inline-error" role="alert">图表来源与当前查询记录不一致，已停止渲染。</p>
      ) : null}
      {view === "chart" && answer.chart_spec && specMatchesRun ? (
        <div className="chart-wrap" aria-label={answer.chart_spec.title || "财务数据图表"}>
          <ReactEChartsCore echarts={echarts} option={chartOption(answer.chart_spec, reduceMotion)} style={{ height: 290, width: "100%" }} notMerge />
        </div>
      ) : null}
      {view === "table" && answer.table ? (
        <div className="table-frame">
          <table className="data-table answer-table">
            <thead><tr>{answer.table.columns.map((column) => <th key={column.key} scope="col">{column.label}{column.unit ? `（${column.unit}）` : ""}</th>)}</tr></thead>
            <tbody>
              {answer.table.rows.map((row, index) => (
                <tr key={index}>
                  {answer.table!.columns.map((column) => <td key={column.key} data-label={column.label}>{formatNumber(row[column.key])}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

export function AnswerView({ answer, queryRunId }: { answer: AnswerResult; queryRunId?: string | null }) {
  const { openEvidence, openQueryRun } = useUi();
  const metadata = [
    ["公司", answer.company], ["期间", answer.period], ["单位", answer.unit],
    ["币种", answer.currency], ["报表口径", answer.statement_scope],
  ].filter((item): item is [string, string] => Boolean(item[1]));

  return (
    <div className="answer-view">
      <div className="answer-view__status"><StatusBadge status={answer.status} /></div>
      <p className="answer-view__text">{answer.text || answer.clarification || "服务未返回可展示的答案文本。"}</p>
      {metadata.length > 0 ? (
        <dl className="answer-metadata">
          {metadata.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
        </dl>
      ) : null}
      {answer.formula ? (
        <div className="formula-line"><Braces size={17} aria-hidden="true" /><span>计算公式</span><code>{answer.formula}</code></div>
      ) : null}
      <AnswerData answer={answer} queryRunId={queryRunId} />
      {answer.evidence?.length ? (
        <section className="evidence-list" aria-label="答案证据">
          <h3>原文证据</h3>
          {answer.evidence.map((evidence, index) => (
            <button key={`${evidence.document_id}:${evidence.page_no}:${index}`} type="button" onClick={() => openEvidence(evidence)}>
              <FileSearch size={18} aria-hidden="true" />
              <span><strong>第 {evidence.page_no} 页</strong><small>{evidence.snippet || "打开 PDF 原页"}</small></span>
            </button>
          ))}
        </section>
      ) : null}
      {queryRunId ? (
        <button className="button button--quiet query-detail-link" type="button" onClick={() => openQueryRun(queryRunId)}>
          查看查询详情
        </button>
      ) : null}
    </div>
  );
}
