from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import gc
import json
import os
from pathlib import Path
import re
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from openpyxl import load_workbook
import pandas as pd

from .config import AppConfig, DatabaseConfig
from .core import is_safe_select_sql, normalize_numeric, report_period_order_sql, report_period_sort_key
from .database import FinanceDatabase
from .ingestion import CompanyIndex, ReportRecord, extract_report_record, extract_pdf_text
from .kb import SimpleKnowledgeBase
from .llm import LLMClient
from .logger import get_logger
from .monitor import ProgressTracker, ResourceMonitor
from .planner import TaskPlanner
from .quality import (
    METRIC_LABELS,
    METRIC_UNITS,
    format_reason_with_references,
    format_single_metric_answer,
    format_topn_analysis_answer,
    format_trend_analysis_answer,
)
from .schema import FieldSpec, load_schema_from_xlsx
from .sql_planner import SQLPlanner
from .task2 import CompanyResolver, QuestionAnalyzer, TASK2_METRICS


plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["font.sans-serif"] = ["PingFang SC", "SimHei", "Arial Unicode MS", "DejaVu Sans"]

TABLE_SECTION_KEYWORDS = {
    "income_sheet": ["合并利润表", "利润表"],
    "balance_sheet": ["合并资产负债表", "资产负债表"],
    "cash_flow_sheet": ["合并现金流量表", "现金流量表"],
    "core_performance_indicators_sheet": ["主要会计数据和财务指标", "核心财务指标"],
}
TABLE_SECTION_ROW_HINTS = {
    "income_sheet": ["营业总收入", "净利润", "销售费用", "研发费用"],
    "balance_sheet": ["货币资金", "应收账款", "存货", "总资产", "短期借款"],
    "cash_flow_sheet": ["经营活动产生的现金流量净额", "销售商品、提供劳务收到的现金", "投资活动产生的现金流量净额"],
    "core_performance_indicators_sheet": ["营业收入", "归属于上市公司股东的净利润", "基本每股收益", "加权平均净资产收益率"],
}

BASE_FIELDS = {"serial_number", "stock_code", "stock_abbr", "report_period", "report_year"}
INGESTION_STATE_VERSION = 2


def _period_sort_key(period: str) -> tuple[int, int]:
    return report_period_sort_key(period)


def _to_bool(text: str | bool | None) -> bool:
    if isinstance(text, bool):
        return text
    return str(text or "").strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(slots=True)
class PipelinePaths:
    base_dir: Path
    sample_dir: Path
    schema_xlsx: Path
    company_xlsx: Path
    reports_dir: Path
    task2_xlsx: Path
    task3_xlsx: Path
    research_dir: Path
    output_dir: Path
    result_dir: Path
    db_path: Path

    @classmethod
    def from_base_dir(cls, base_dir: Path, full_data: bool = False) -> "PipelinePaths":
        sample_dir = cls._resolve_dataset_dir(base_dir=base_dir, full_data=full_data)
        output_dir = base_dir / "outputs"
        result_dir = base_dir / "result"
        return cls(
            base_dir=base_dir,
            sample_dir=sample_dir,
            schema_xlsx=sample_dir / "附件3：数据库-表名及字段说明.xlsx",
            company_xlsx=cls._resolve_company_xlsx(sample_dir),
            reports_dir=sample_dir / "附件2：财务报告",
            task2_xlsx=sample_dir / "附件4：问题汇总.xlsx",
            task3_xlsx=sample_dir / "附件6：问题汇总.xlsx",
            research_dir=sample_dir / "附件5：研报数据",
            output_dir=output_dir,
            result_dir=result_dir,
            db_path=output_dir / "finance.db",
        )

    @staticmethod
    def _resolve_dataset_dir(base_dir: Path, full_data: bool) -> Path:
        sample_default = base_dir / "样例数据"
        schema_name = "附件3：数据库-表名及字段说明.xlsx"
        if not full_data:
            return sample_default if sample_default.exists() else base_dir

        direct_candidates = [
            base_dir / "数据" / "测试数据",
            base_dir / "数据" / "全量数据",
            base_dir / "测试数据",
            base_dir / "全量数据",
            sample_default,
            base_dir,
        ]
        for candidate in direct_candidates:
            if (candidate / schema_name).exists():
                return candidate

        hits: list[Path] = []
        for schema_path in base_dir.rglob(schema_name):
            hits.append(schema_path.parent)

        for label in ("测试数据", "全量数据", "样例数据"):
            label_hits = [path for path in hits if label in str(path)]
            if label_hits:
                label_hits.sort(key=lambda p: (len(p.parts), len(str(p))))
                return label_hits[0]

        if hits:
            hits.sort(key=lambda p: (len(p.parts), len(str(p))))
            return hits[0]
        return sample_default if sample_default.exists() else base_dir

    @staticmethod
    def _resolve_company_xlsx(sample_dir: Path) -> Path:
        matches = sorted(sample_dir.glob("附件1：*上市公司基本信息*.xlsx"))
        if matches:
            return matches[0]
        return sample_dir / "附件1：上市公司基本信息.xlsx"


class SmartFinancePipeline:
    def __init__(self, paths: PipelinePaths, app_config: AppConfig | None = None, llm_client: LLMClient | None = None) -> None:
        self.paths = paths
        self.app_config = app_config or AppConfig.from_env()
        self.paths.output_dir.mkdir(parents=True, exist_ok=True)
        self.paths.result_dir.mkdir(parents=True, exist_ok=True)

        self.logger = get_logger()
        self.resource_monitor = ResourceMonitor(memory_limit_percent=85.0)

        self.schema = load_schema_from_xlsx(self.paths.schema_xlsx)
        self.company_index = CompanyIndex.from_xlsx(self.paths.company_xlsx)
        self.company_resolver = CompanyResolver.from_xlsx(self.paths.company_xlsx)

        db_cfg = self.app_config.db
        if db_cfg.backend == "sqlite" and not db_cfg.sqlite_path:
            db_cfg = DatabaseConfig(backend="sqlite", sqlite_path=str(self.paths.db_path))
        self.db = FinanceDatabase(
            self.paths.db_path,
            self.schema,
            db_config=db_cfg,
            connect_immediately=False,
            enable_cache=self.app_config.enable_cache,
            cache_size=self.app_config.cache_size,
        )
        try:
            self.db.create_tables()
        except RuntimeError:
            # Fallback for environments without MySQL driver/server.
            fallback_cfg = DatabaseConfig(backend="sqlite", sqlite_path=str(self.paths.db_path))
            self.db = FinanceDatabase(
                self.paths.db_path,
                self.schema,
                db_config=fallback_cfg,
                connect_immediately=False,
                enable_cache=self.app_config.enable_cache,
                cache_size=self.app_config.cache_size,
            )
            self.db.create_tables()

        self.llm_client = llm_client or LLMClient(self.app_config.llm)
        self.planner = TaskPlanner(self.llm_client if self.llm_client.enabled else None)
        self.sql_planner = SQLPlanner()
        self.task2_analyzer = QuestionAnalyzer(company_resolver=self.company_resolver)
        self.kb = SimpleKnowledgeBase(
            self.llm_client if self.llm_client.enabled else None,
            use_embeddings=self.app_config.kb_use_embeddings,
        )
        self._serial_counter = {table: 1 for table in self.schema}
        self.run_log: dict[str, Any] = {
            "ingestion": [],
            "task2": [],
            "task3": [],
            "validation": {},
            "config": {
                "mode": self.app_config.mode,
                "full_data": self.app_config.full_data,
                "db_backend": self.db.backend,
                "llm_enabled": self.llm_client.enabled,
                "ingest_workers": self.app_config.ingest_workers,
                "incremental_ingest": self.app_config.incremental_ingest,
                "kb_max_documents": self.app_config.kb_max_documents,
                "kb_max_chunks_per_paper": self.app_config.kb_max_chunks_per_paper,
                "kb_use_embeddings": self.app_config.kb_use_embeddings,
            },
        }

    def run(self, mode: str | None = None) -> dict[str, str]:
        selected_mode = (mode or self.app_config.mode or "all").strip().lower()
        outputs = {"db_path": str(self.paths.db_path), "result_2": "", "result_3": "", "run_log": ""}

        if selected_mode in {"all", "ingest"}:
            self.ingest_reports()
            self.derive_missing_yoy()
            self.run_log["validation"] = self.validate_database()

        if selected_mode in {"task2", "task3"}:
            self._ensure_database_ready()

        if selected_mode in {"all", "task3"}:
            self.build_knowledge_base()

        if selected_mode in {"all", "task2"}:
            outputs["result_2"] = str(self.answer_task2())
        if selected_mode in {"all", "task3"}:
            outputs["result_3"] = str(self.answer_task3())

        log_path = self.paths.output_dir / "run_log.json"
        log_path.write_text(json.dumps(self.run_log, ensure_ascii=False, indent=2), encoding="utf-8")
        outputs["run_log"] = str(log_path)
        return outputs

    # ---------- Task 1 ----------
    def ingest_reports(self) -> None:
        report_files = sorted(self.paths.reports_dir.rglob("*.pdf"))
        total_files = len(report_files)

        self.logger.info(f"Found {total_files} PDF files to process")

        best_rows: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
        state_path = self.paths.output_dir / "ingestion_state.json"
        state = self._load_ingestion_state(state_path)

        parse_candidates: list[Path] = []
        skipped_unchanged = 0
        for pdf_path in report_files:
            signature = self._file_signature(pdf_path)
            prev = state.get(str(pdf_path))
            if (
                self.app_config.incremental_ingest
                and prev
                and prev.get("size") == signature["size"]
                and prev.get("mtime_ns") == signature["mtime_ns"]
                and prev.get("status") == "parsed"
                and self._has_complete_period_record(
                    str(prev.get("stock_code", "")),
                    str(prev.get("report_period", "")),
                    prev.get("available_tables") if "available_tables" in prev else None,
                )
            ):
                skipped_unchanged += 1
                continue
            parse_candidates.append(pdf_path)

        self.logger.info(f"Skipped {skipped_unchanged} unchanged files, processing {len(parse_candidates)} files")

        if not parse_candidates:
            self.logger.info("No files to process")
            return

        # Process in batches to manage memory
        batch_size = max(10, min(100, len(parse_candidates) // 10))
        workers = max(1, min(self.app_config.ingest_workers, os.cpu_count() or 4))

        progress = ProgressTracker(len(parse_candidates), "Parsing PDFs")
        parsed_records: list[tuple[Path, ReportRecord]] = []

        for batch_start in range(0, len(parse_candidates), batch_size):
            batch = parse_candidates[batch_start:batch_start + batch_size]

            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {pool.submit(extract_report_record, path, self.company_index): path for path in batch}
                for fut in as_completed(future_map):
                    pdf_path = future_map[fut]
                    try:
                        record = fut.result()
                        parsed_records.append((pdf_path, record))
                        progress.update()
                    except Exception as exc:  # pragma: no cover
                        state[str(pdf_path)] = {
                            **self._file_signature(pdf_path),
                            "status": "error",
                            "error": str(exc),
                        }
                        self._append_ingestion_log({"pdf": str(pdf_path), "error": str(exc), "status": "error"})
                        self.logger.error(f"Failed to parse {pdf_path.name}: {exc}")
                        progress.update()

            # Check memory and force GC if needed
            if self.resource_monitor.should_gc():
                gc_stats = self.resource_monitor.force_gc()
                self.logger.info(f"Memory cleanup: freed {gc_stats['freed_mb']:.1f}MB ({gc_stats['freed_percent']:.1f}%)")

        progress.finish()

        # Process records and build rows
        self.logger.info(f"Building database rows from {len(parsed_records)} parsed records")
        progress_build = ProgressTracker(len(parsed_records), "Building rows")

        for pdf_path, record in parsed_records:
            try:
                table_rows = self._build_rows_from_record(record)
                for table, row in table_rows.items():
                    row = self._sanitize_row_values(table, row)
                    ok, issues = self._validate_row(table, row)
                    if not ok:
                        self._delete_period_row(table, record.stock_code, record.report_period)
                        self._append_ingestion_log(
                            {
                                "pdf": str(pdf_path),
                                "table": table,
                                "stock_code": record.stock_code,
                                "report_period": record.report_period,
                                "status": "skip_invalid",
                                "issues": issues,
                            }
                        )
                        continue
                    key = (table, str(row.get("stock_code")), str(row.get("report_period")))
                    quality = self._row_quality(table, row)
                    if key not in best_rows or quality > best_rows[key][0]:
                        best_rows[key] = (quality, row)
                state[str(pdf_path)] = {
                    **self._file_signature(pdf_path),
                    "status": "parsed",
                    "stock_code": record.stock_code,
                    "stock_abbr": record.stock_abbr,
                    "report_period": record.report_period,
                    "report_year": record.report_year,
                    "available_tables": self._available_tables_in_text(record.text),
                }
                self._append_ingestion_log(
                    {
                        "pdf": str(pdf_path),
                        "stock_code": record.stock_code,
                        "stock_abbr": record.stock_abbr,
                        "report_period": record.report_period,
                        "status": "parsed",
                    }
                )
            except Exception as exc:
                self.logger.error(f"Failed to build rows for {pdf_path.name}: {exc}")
                state[str(pdf_path)] = {
                    **self._file_signature(pdf_path),
                    "status": "error",
                    "error": f"Row building failed: {exc}",
                }

            progress_build.update()

        progress_build.finish()

        # Batch insert to database
        grouped_rows: dict[str, list[dict[str, Any]]] = {}
        for (table, _stock, _period), (_quality, row) in best_rows.items():
            grouped_rows.setdefault(table, []).append(row)

        self.logger.info(f"Inserting {sum(len(rows) for rows in grouped_rows.values())} rows to database")
        for table, rows in grouped_rows.items():
            try:
                # Insert in smaller batches to avoid memory issues
                batch_size = 100
                for i in range(0, len(rows), batch_size):
                    batch = rows[i:i + batch_size]
                    self.db.upsert_many(table, batch)
                self.logger.info(f"Inserted {len(rows)} rows to {table}")
            except Exception as exc:
                self.logger.error(f"Failed to insert rows to {table}: {exc}")

        # Save state after each batch
        self._save_ingestion_state(state_path, state)

        # Final resource check
        resource_summary = self.resource_monitor.get_summary()
        self.logger.info(f"Resource usage: {resource_summary}")

        self.run_log["ingestion_summary"] = {
            "total_files": len(report_files),
            "parsed_files": len(parsed_records),
            "skipped_unchanged": skipped_unchanged,
            "written_rows": {table: len(rows) for table, rows in grouped_rows.items()},
            "resource_usage": resource_summary,
        }

    def _build_rows_from_record(self, record: ReportRecord) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for table, fields in self.schema.items():
            row: dict[str, Any] = {f.field_name: None for f in fields}
            row["serial_number"] = self._serial_counter[table]
            self._serial_counter[table] += 1
            row["stock_code"] = record.stock_code
            row["stock_abbr"] = record.stock_abbr
            row["report_period"] = record.report_period
            row["report_year"] = record.report_year

            section_text = self._locate_table_section(record.text, table)
            section_lines = [ln.strip() for ln in section_text.splitlines() if ln.strip()]
            for field in fields:
                if field.field_name in BASE_FIELDS:
                    continue
                value = self._extract_field_value(section_text, section_lines, field)
                row[field.field_name] = value

            self._overlay_snapshot_values(table, row, record.snapshot)
            rows[table] = row
        return rows

    def _locate_table_section(self, text: str, table: str) -> str:
        candidates = TABLE_SECTION_KEYWORDS.get(table, [])
        positions: list[tuple[int, str]] = []
        for marker in candidates:
            positions.extend((match.start(), marker) for match in re.finditer(re.escape(marker), text))
        if not positions:
            return text

        start = max(
            (idx for idx, _marker in positions),
            key=lambda idx: (self._score_table_section_candidate(text, table, idx), idx),
        )
        next_cut = len(text)
        for other_table, markers in TABLE_SECTION_KEYWORDS.items():
            if other_table == table:
                continue
            for marker in markers:
                idx = text.find(marker, start + 1)
                if idx > start:
                    next_cut = min(next_cut, idx)
        return text[start:next_cut]

    def _score_table_section_candidate(self, text: str, table: str, start: int) -> int:
        window = text[start : start + 8000]
        preview = window[:300]
        hints = TABLE_SECTION_ROW_HINTS.get(table, [])
        score = 0
        score += sum(120 for hint in hints if hint in window)
        score += min(len(re.findall(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+\.\d+|-?\d+", window[:4000])), 80)
        score += 40 if "编制单位" in preview else 0
        score += 40 if "单位" in preview else 0
        score += 40 if "项目" in preview else 0
        score -= 160 if "和母公司" in preview else 0
        score -= 120 if "审计报告" in preview else 0
        score -= 80 if "关键审计事项" in preview else 0
        return score

    def _extract_field_value(self, text: str, lines: list[str], field: FieldSpec) -> float | int | None:
        aliases = self._field_aliases(field)
        candidates: list[tuple[float, str]] = []
        for alias in aliases:
            # Fast path: line-level scan avoids repeatedly scanning whole section.
            matched = False
            for idx, line in enumerate(lines):
                if alias not in line:
                    continue
                matched = True
                snippet = line
                start_at = snippet.find(alias) + len(alias)
                nums = re.findall(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+\.\d+|-?\d+", snippet[start_at:])
                if not nums and idx + 1 < len(lines):
                    extended = " ".join(lines[idx : min(idx + 3, len(lines))])
                    extended_start = extended.find(alias) + len(alias)
                    extended_nums = re.findall(
                        r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+\.\d+|-?\d+",
                        extended[extended_start:],
                    )
                    if extended_nums:
                        snippet = extended
                        nums = extended_nums
                if nums:
                    try:
                        candidates.append((float(nums[0].replace(",", "")), snippet[:120]))
                    except ValueError:
                        pass
            if matched:
                continue

            # Fallback to section-level regex when line segmentation misses merged rows.
            for match in re.finditer(re.escape(alias), text):
                snippet = text[match.start() : match.start() + 220]
                nums = re.findall(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+\.\d+|-?\d+", snippet)
                for token in nums[:4]:
                    try:
                        val = float(token.replace(",", ""))
                    except ValueError:
                        continue
                    candidates.append((val, snippet[:120]))

        if not candidates:
            return None
        chosen = self._pick_best_candidate(field, candidates)
        return self._normalize_field_value(field, chosen)

    def _field_aliases(self, field: FieldSpec) -> list[str]:
        cn = field.cn_name or ""
        aliases = {cn}
        stripped = re.sub(r"（.*?）|\(.*?\)", "", cn).strip()
        aliases.add(stripped)
        aliases.add(cn.replace("同比增长", "同比").replace("季度环比增长", "环比"))
        aliases.add(stripped.replace("同比增长", "同比").replace("季度环比增长", "环比"))
        aliases.add(cn.replace("营业总支出", "营业总成本"))
        aliases.add(cn.replace("净利润", "归属于上市公司股东的净利润"))
        for token in (cn, stripped):
            for sep in ("-", "—", "–"):
                if sep in token:
                    _left, right = token.split(sep, 1)
                    aliases.add(right.strip())
        if "经营性现金流" in stripped:
            aliases.add("经营活动产生的现金流量净额")
        if "融资性现金流" in stripped:
            aliases.add("融资活动产生的现金流量净额")
        if "投资性现金流" in stripped:
            aliases.add("投资活动产生的现金流量净额")
        aliases = {x for x in aliases if x}
        return sorted(aliases, key=len, reverse=True)

    def _pick_best_candidate(self, field: FieldSpec, candidates: list[tuple[float, str]]) -> float:
        # Use LLM disambiguation when available and multiple strong candidates exist.
        if self.llm_client.enabled and len(candidates) > 1:
            try:
                prompt = {
                    "field_name": field.field_name,
                    "cn_name": field.cn_name,
                    "candidates": [{"value": c[0], "context": c[1]} for c in candidates[:6]],
                }
                ans = self.llm_client.complete_json(
                    "你是财报字段数值裁决器。返回JSON: {\"value\": 数值}。",
                    json.dumps(prompt, ensure_ascii=False),
                )
                if isinstance(ans, dict) and "value" in ans:
                    return float(ans["value"])
            except Exception:
                pass

        values = [c[0] for c in candidates]
        if "yoy" in field.field_name or "qoq" in field.field_name or "%" in field.cn_name:
            valid = [v for v in values if -1000 <= v <= 1000]
            return valid[0] if valid else values[0]
        if "ratio" in field.field_name:
            valid = [v for v in values if 0 <= v <= 100]
            return valid[0] if valid else values[0]
        # For amounts pick the largest absolute among nearby candidates.
        return sorted(values, key=lambda v: abs(v), reverse=True)[0]

    def _normalize_field_value(self, field: FieldSpec, value: float) -> float | int | None:
        if value is None:
            return None
        field_type = field.raw_type.lower()
        if "int" in field_type:
            return int(round(value))
        cn = field.cn_name
        if "万元" in cn:
            return normalize_numeric(value, source_unit="元", target_unit="万元")
        if "元" in cn:
            return normalize_numeric(value, source_unit="元", target_unit="元")
        if "%" in cn:
            return round(float(value), 4)
        return round(float(value), 4)

    def _overlay_snapshot_values(self, table: str, row: dict[str, Any], snapshot: dict[str, float | None]) -> None:
        if table == "income_sheet":
            if snapshot.get("total_profit_yuan") is not None:
                row["total_profit"] = normalize_numeric(snapshot["total_profit_yuan"], "元", "万元")
            if snapshot.get("net_profit_yuan") is not None:
                row["net_profit"] = normalize_numeric(snapshot["net_profit_yuan"], "元", "万元")
            if snapshot.get("total_operating_revenue_yuan") is not None:
                row["total_operating_revenue"] = normalize_numeric(snapshot["total_operating_revenue_yuan"], "元", "万元")
            if snapshot.get("operating_revenue_yoy") is not None:
                row["operating_revenue_yoy_growth"] = snapshot["operating_revenue_yoy"]
            if snapshot.get("net_profit_yoy") is not None:
                row["net_profit_yoy_growth"] = snapshot["net_profit_yoy"]
        elif table == "core_performance_indicators_sheet":
            if snapshot.get("eps") is not None:
                row["eps"] = snapshot["eps"]
            if snapshot.get("total_operating_revenue_yuan") is not None:
                row["total_operating_revenue"] = normalize_numeric(snapshot["total_operating_revenue_yuan"], "元", "万元")
            if snapshot.get("net_profit_yuan") is not None:
                row["net_profit_10k_yuan"] = normalize_numeric(snapshot["net_profit_yuan"], "元", "万元")
            if snapshot.get("net_profit_yoy") is not None:
                row["net_profit_yoy_growth"] = snapshot["net_profit_yoy"]
            if snapshot.get("operating_revenue_yoy") is not None:
                row["operating_revenue_yoy_growth"] = snapshot["operating_revenue_yoy"]
        elif table == "cash_flow_sheet":
            if snapshot.get("operating_cf_net_yuan") is not None:
                row["operating_cf_net_amount"] = normalize_numeric(snapshot["operating_cf_net_yuan"], "元", "万元")
                row["net_cash_flow"] = snapshot["operating_cf_net_yuan"]
            if snapshot.get("operating_cf_yoy") is not None:
                row["net_cash_flow_yoy_growth"] = snapshot["operating_cf_yoy"]
        elif table == "balance_sheet":
            if snapshot.get("asset_liability_ratio") is not None:
                row["asset_liability_ratio"] = snapshot["asset_liability_ratio"]

    def _sanitize_row_values(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        cleaned = dict(row)
        for key, value in row.items():
            if value is None:
                continue
            if ("yoy" in key or "qoq" in key) and (not isinstance(value, (int, float)) or value < -1000 or value > 1000):
                cleaned[key] = None
            if key == "asset_liability_ratio" and (not isinstance(value, (int, float)) or value < 0 or value > 100):
                cleaned[key] = None
        return cleaned

    @staticmethod
    def _available_tables_in_text(text: str) -> list[str]:
        return [
            table
            for table, markers in TABLE_SECTION_KEYWORDS.items()
            if any(marker in text for marker in markers)
        ]

    def _validate_row(self, table: str, row: dict[str, Any]) -> tuple[bool, list[str]]:
        issues: list[str] = []
        business_fields = [k for k in row if k not in BASE_FIELDS]
        non_null = sum(1 for k in business_fields if row.get(k) is not None)
        ratio = non_null / max(len(business_fields), 1)
        if ratio < 0.12:
            issues.append(f"non_null_ratio_too_low:{ratio:.3f}")

        for key, value in row.items():
            if value is None:
                continue
            if ("yoy" in key or "qoq" in key) and (not isinstance(value, (int, float)) or value < -1000 or value > 1000):
                issues.append(f"invalid_growth:{key}")
            if key == "asset_liability_ratio" and (not isinstance(value, (int, float)) or value < 0 or value > 100):
                issues.append("invalid_asset_liability_ratio")

        return (len(issues) == 0, issues)

    def _row_quality(self, table: str, row: dict[str, Any]) -> float:
        score = float(sum(1 for value in row.values() if value is not None))
        if table == "income_sheet":
            for field in ("total_operating_revenue", "total_profit", "net_profit"):
                value = row.get(field)
                if isinstance(value, (int, float)):
                    score += min(abs(float(value)) / 1000.0, 1000.0)
        return score

    def derive_missing_yoy(self) -> None:
        rows = self.db.query(
            "SELECT stock_code, report_period, report_year, total_operating_revenue, operating_revenue_yoy_growth "
            "FROM income_sheet WHERE total_operating_revenue IS NOT NULL"
        )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            suffix = row["report_period"][-2:] if row.get("report_period") else ""
            grouped.setdefault((row["stock_code"], suffix), []).append(row)
        for (_stock, _suffix), items in grouped.items():
            items.sort(key=lambda item: (int(item["report_year"]), _period_sort_key(item["report_period"])))
            prev = None
            updates: list[tuple[float, str, str]] = []
            for item in items:
                if prev and item["operating_revenue_yoy_growth"] is None:
                    prev_val = prev["total_operating_revenue"]
                    curr_val = item["total_operating_revenue"]
                    if prev_val not in (None, 0) and curr_val is not None:
                        yoy = round((curr_val - prev_val) / prev_val * 100.0, 4)
                        updates.append((yoy, item["stock_code"], item["report_period"]))
                prev = item
            if updates:
                sql = (
                    "UPDATE income_sheet SET operating_revenue_yoy_growth=%s WHERE stock_code=%s AND report_period=%s"
                    if self.db.backend == "mysql"
                    else "UPDATE income_sheet SET operating_revenue_yoy_growth=? WHERE stock_code=? AND report_period=?"
                )
                self.db.execute_many(sql, updates)

    def validate_database(self) -> dict[str, Any]:
        result: dict[str, Any] = {"unique": {}, "cross_table": []}
        for table in self.schema:
            dup = self.db.query(
                f"SELECT stock_code, report_period, COUNT(*) as cnt FROM {table} "
                "GROUP BY stock_code, report_period HAVING cnt > 1"
            )
            result["unique"][table] = len(dup) == 0

        presence_rows = self.db.query(
            "SELECT stock_code, report_period, 'income_sheet' AS table_name FROM income_sheet "
            "UNION ALL "
            "SELECT stock_code, report_period, 'balance_sheet' AS table_name FROM balance_sheet "
            "UNION ALL "
            "SELECT stock_code, report_period, 'cash_flow_sheet' AS table_name FROM cash_flow_sheet "
            "UNION ALL "
            "SELECT stock_code, report_period, 'core_performance_indicators_sheet' AS table_name FROM core_performance_indicators_sheet"
        )
        presence: dict[tuple[str, str], set[str]] = {}
        for row in presence_rows:
            key = (str(row["stock_code"]), str(row["report_period"]))
            presence.setdefault(key, set()).add(str(row["table_name"]))

        required_by_period = self._required_tables_by_period()
        for key in sorted(set(presence) | set(required_by_period)):
            stock_code, report_period = key
            required = required_by_period.get(key)
            if required is None:
                required = set(self.schema.keys())
            missing = sorted(required - presence.get(key, set()))
            if missing:
                result["cross_table"].append(
                    {
                        "stock_code": stock_code,
                        "report_period": report_period,
                        "missing_tables": missing,
                    }
                )
        return result

    def _ensure_database_ready(self) -> None:
        ready = True
        table_counts: dict[str, int] = {}
        for table in self.schema:
            rows = self.db.query(f"SELECT COUNT(*) AS cnt FROM {table}")
            count = int(rows[0]["cnt"]) if rows else 0
            table_counts[table] = count
            if count <= 0:
                ready = False
        if not ready:
            raise RuntimeError(f"database is not ready for task mode: {table_counts}")

    def _required_tables_by_period(self) -> dict[tuple[str, str], set[str]]:
        required: dict[tuple[str, str], set[str]] = {}
        state_path = self.paths.output_dir / "ingestion_state.json"
        for meta in self._load_ingestion_state(state_path).values():
            if meta.get("status") != "parsed":
                continue
            stock_code = str(meta.get("stock_code", ""))
            report_period = str(meta.get("report_period", ""))
            if not stock_code or not report_period:
                continue
            key = (stock_code, report_period)
            available = meta.get("available_tables")
            if available is None:
                required.setdefault(key, set(self.schema.keys()))
                continue
            required.setdefault(key, set()).update(
                str(table_name) for table_name in available if str(table_name) in self.schema
            )
        return required

    def _has_complete_period_record(
        self,
        stock_code: str,
        report_period: str,
        required_tables: list[str] | None = None,
    ) -> bool:
        tables = list(self.schema.keys()) if required_tables is None else [
            table_name for table_name in required_tables if table_name in self.schema
        ]
        if not tables:
            return True
        for table in tables:
            rows = self.db.query(
                f"SELECT COUNT(*) AS cnt FROM {table} WHERE stock_code = ? AND report_period = ?",
                (stock_code, report_period),
            )
            count = int(rows[0]["cnt"]) if rows else 0
            if count <= 0:
                return False
        return True

    def _delete_period_row(self, table: str, stock_code: str, report_period: str) -> None:
        sql = (
            f"DELETE FROM `{table}` WHERE stock_code=%s AND report_period=%s"
            if self.db.backend == "mysql"
            else f"DELETE FROM {table} WHERE stock_code=? AND report_period=?"
        )
        self.db.execute(sql, (stock_code, report_period))

    def _append_ingestion_log(self, item: dict[str, Any]) -> None:
        logs = self.run_log.setdefault("ingestion", [])
        limit = self.app_config.ingestion_log_limit
        if len(logs) < limit:
            logs.append(item)
        else:
            self.run_log["ingestion_overflow"] = int(self.run_log.get("ingestion_overflow", 0)) + 1

    @staticmethod
    def _load_ingestion_state(path: Path) -> dict[str, dict[str, Any]]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("version") == INGESTION_STATE_VERSION and isinstance(raw.get("files"), dict):
                return raw["files"]
            if isinstance(raw, dict) and "version" not in raw:
                return {}
        except Exception:
            return {}
        return {}

    @staticmethod
    def _save_ingestion_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
        payload = {"version": INGESTION_STATE_VERSION, "files": state}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _file_signature(path: Path) -> dict[str, int]:
        st = path.stat()
        return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}

    # ---------- Task 2 ----------
    def answer_task2(self) -> Path:
        df = pd.read_excel(self.paths.task2_xlsx)
        rows_out: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            qid = str(row["编号"]).strip()
            turns = json.loads(str(row["问题"]).strip())
            context: dict[str, Any] = {}
            answers: list[dict[str, Any]] = []
            sql_history: list[str] = []
            chart_paths: list[dict[str, str]] = []
            turn_logs: list[dict[str, Any]] = []

            for idx, turn in enumerate(turns, start=1):
                question = str(turn["Q"])
                analysis = self.task2_analyzer.analyze_turn(question, context)
                context = analysis.get("context", context)
                if analysis.get("need_clarify"):
                    content = analysis.get("clarify_question") or "请补充关键信息。"
                    answers.append({"Q": question, "A": {"content": content}})
                    turn_logs.append({"Q": question, "intent": analysis, "clarify": True})
                    continue

                query_spec = dict(analysis.get("query_spec") or {})
                sql = self.sql_planner.build_sql({"intent": analysis.get("intent"), "query_spec": query_spec})
                result_rows = self.db.query(sql)
                result_rows = self._apply_task2_post_compute(query_spec, result_rows)
                sql_history.append(sql)
                images = self._render_chart_for_query_spec(qid, idx, query_spec, result_rows)
                chart_paths.extend(images)
                image_paths = [item["path"] for item in images]
                content = self._compose_task2_content(question, query_spec, result_rows, images)
                answers.append({"Q": question, "A": {"content": content, **({"image": image_paths} if image_paths else {})}})
                turn_logs.append(
                    {
                        "Q": question,
                        "intent": analysis.get("intent"),
                        "query_spec": query_spec,
                        "sql": sql,
                        "rows": len(result_rows),
                        "chart_path": image_paths,
                    }
                )

            rows_out.append(
                {
                    "编号": qid,
                    "问题": json.dumps(turns, ensure_ascii=False),
                    "SQL 查询语句": "\n".join(sql_history) if sql_history else "无",
                    "图形格式": self._graph_type_from_images(chart_paths),
                    "回答": json.dumps(answers, ensure_ascii=False),
                }
            )
            self.run_log["task2"].append(
                {
                    "id": qid,
                    "intent": [log.get("intent") for log in turn_logs],
                    "query_spec": [log.get("query_spec") for log in turn_logs if log.get("query_spec")],
                    "sql": [log.get("sql") for log in turn_logs if log.get("sql")],
                    "rows": [log.get("rows") for log in turn_logs if "rows" in log],
                    "chart_path": [log.get("chart_path") for log in turn_logs if log.get("chart_path")],
                    "turn_logs": turn_logs,
                }
            )

        result_path = self.paths.base_dir / "result_2.xlsx"
        pd.DataFrame(rows_out).to_excel(result_path, index=False)
        return result_path

    def _apply_task2_post_compute(self, query_spec: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        post_compute = query_spec.get("post_compute")
        if not rows or not post_compute:
            return rows
        if post_compute == "intersection_topn":
            metrics = [str(metric) for metric in query_spec.get("metrics", [])]
            if len(metrics) < 2:
                return rows
            ranked_sets: list[list[dict[str, Any]]] = []
            for metric in metrics:
                spec = TASK2_METRICS.get(metric)
                if not spec:
                    continue
                ranked = sorted(rows, key=lambda item: float(item.get(spec.field, 0) or 0), reverse=True)
                ranked_sets.append(ranked[: int(query_spec.get("top_n", 5))])
            if len(ranked_sets) < 2:
                return rows
            common_names = set(
                str(item.get("stock_code"))
                for item in ranked_sets[0]
            )
            for ranked in ranked_sets[1:]:
                common_names &= {str(item.get("stock_code")) for item in ranked}
            return [item for item in rows if str(item.get("stock_code")) in common_names]
        return rows

    def _compose_task2_content(
        self,
        question: str,
        query_spec: dict[str, Any],
        rows: list[dict[str, Any]],
        images: list[dict[str, str]],
    ) -> str:
        if self.llm_client.enabled:
            try:
                prompt = {
                    "question": question,
                    "query_spec": query_spec,
                    "rows": rows[:20],
                    "images": images,
                }
                ans = self.llm_client.complete_json(
                    "你是财报分析助手。返回JSON: {\"content\":\"...\"}，content必须严格基于查询结果，不得编造。",
                    json.dumps(prompt, ensure_ascii=False),
                )
                if ans.get("content"):
                    return str(ans["content"])
            except Exception:
                pass

        if not rows:
            return "未查询到符合条件的数据。"

        analysis_type = str(query_spec.get("analysis_type", "single_metric"))
        metric_key = str(query_spec.get("metric", "total_profit"))
        metric_spec = TASK2_METRICS.get(metric_key)
        metric_col = metric_spec.field if metric_spec else metric_key
        metric_label = METRIC_LABELS.get(metric_col, metric_spec.label if metric_spec else metric_key)
        unit = METRIC_UNITS.get(metric_col, metric_spec.unit if metric_spec else "")

        if analysis_type == "single_metric":
            slots = {
                "stock_abbr": query_spec.get("stock_abbr", ""),
                "report_period": query_spec.get("report_period", ""),
                "metric": metric_col,
            }
            return format_single_metric_answer(slots, rows[0])

        if analysis_type in {"trend", "comparison"}:
            return format_trend_analysis_answer(rows, metric_col=metric_col, metric_label=metric_label)

        if analysis_type == "topn_metric":
            return format_topn_analysis_answer(rows, metric_col=metric_col, metric_label=metric_label)

        if analysis_type == "intersection_topn":
            joined = "；".join(
                f"{item.get('stock_abbr', item.get('stock_code', '未知公司'))}"
                f" {metric_label}={item.get(metric_col)}{unit}"
                for item in rows
            )
            return f"同时满足排名条件的公司有：{joined}" if joined else "未查询到符合条件的数据。"

        parts: list[str] = []
        for item in rows[:10]:
            company = str(item.get("stock_abbr") or item.get("stock_code") or "未知公司")
            values = []
            for field in query_spec.get("select_fields", []):
                if field in {"stock_code", "stock_abbr", "report_period", "report_year"}:
                    continue
                if field not in item:
                    continue
                label = METRIC_LABELS.get(field, field)
                suffix = METRIC_UNITS.get(field, "")
                values.append(f"{label}={item.get(field)}{suffix}")
            parts.append(f"{company}（{'，'.join(values)}）")
        return "；".join(parts) if parts else "未查询到符合条件的数据。"

    # ---------- Task 3 ----------
    def build_knowledge_base(self) -> None:
        metadata = self._load_research_metadata()
        pdf_files = sorted(self.paths.research_dir.rglob("*.pdf"))
        max_docs = self.app_config.kb_max_documents
        max_chunks_per_paper = self.app_config.kb_max_chunks_per_paper
        failed_papers = 0
        for pdf_path in pdf_files:
            title = pdf_path.stem
            meta = metadata.get(title, {})
            try:
                text = extract_pdf_text(pdf_path)
            except Exception as exc:  # pragma: no cover
                failed_papers += 1
                self.run_log.setdefault("kb_errors", []).append({"paper_path": str(pdf_path), "error": str(exc)})
                continue
            chunks = self._chunk_text(text)
            chunks = chunks[:max_chunks_per_paper]
            paper_path = f"./{pdf_path.relative_to(self.paths.base_dir)}"
            paper_image = self._extract_paper_image_hint(text)
            for chunk in chunks:
                self.kb.add_document(title=title, paper_path=paper_path, text=chunk, paper_image=paper_image)
                if len(self.kb.documents) >= max_docs:
                    self.run_log["kb_summary"] = {
                        "papers_seen": str(pdf_path),
                        "docs_indexed": len(self.kb.documents),
                        "failed_papers": failed_papers,
                        "capped": True,
                    }
                    return
            if meta:
                summary = "；".join(f"{k}={v}" for k, v in meta.items() if v)
                self.kb.add_document(title=title, paper_path=paper_path, text=summary, paper_image=paper_image)
                if len(self.kb.documents) >= max_docs:
                    self.run_log["kb_summary"] = {
                        "papers_seen": str(pdf_path),
                        "docs_indexed": len(self.kb.documents),
                        "failed_papers": failed_papers,
                        "capped": True,
                    }
                    return
        self.run_log["kb_summary"] = {
            "papers_total": len(pdf_files),
            "docs_indexed": len(self.kb.documents),
            "failed_papers": failed_papers,
            "capped": False,
        }

    def answer_task3(self) -> Path:
        df = pd.read_excel(self.paths.task3_xlsx)
        rows_out: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            qid = str(row["编号"]).strip()
            turns = json.loads(str(row["问题"]).strip())
            answers: list[dict[str, Any]] = []
            sql_history: list[str] = []
            log_item = {
                "id": qid,
                "task_graph": [],
                "references": [],
                "sql": [],
                "sql_check": [],
                "rows": [],
                "chart_path": [],
                "intent": [],
                "slots": [],
            }
            context: dict[str, Any] = {"slots": {}}

            for idx, turn in enumerate(turns, start=1):
                question = str(turn["Q"])
                task_graph = self.planner.plan_subtasks(question, context)
                exec_state: dict[str, Any] = {}
                refs_collected: list[dict[str, str]] = []
                reason_text = ""
                image_paths: list[str] = []
                for task in task_graph.get("subtasks", []):
                    kind = str(task.get("kind", "sql"))
                    goal = str(task.get("goal", question))
                    if kind == "sql":
                        intent_payload = self.planner.parse_intent(goal, context)
                        context["slots"].update(intent_payload.get("slots", {}))
                        intent_payload["slots"] = context["slots"]
                        log_item["intent"].append(intent_payload.get("intent"))
                        log_item["slots"].append(dict(context["slots"]))
                        sql = self.sql_planner.build_sql(intent_payload)
                        sql_history.append(sql)
                        safe = is_safe_select_sql(
                            sql, allowed_tables=self.db.allowed_tables, allowed_columns=self.db.allowed_columns
                        )
                        rows = self.db.query(sql) if safe else []
                        log_item["sql_check"].append(safe)
                        log_item["rows"].append(len(rows))
                        exec_state[task["id"]] = {"sql": sql, "rows": rows, "sql_check": safe}
                        log_item["sql"].append(sql)
                        images = self._render_chart_for_intent(qid, idx, intent_payload, rows)
                        image_paths.extend(images)
                        if images:
                            log_item["chart_path"].append(images)
                    elif kind == "retrieval":
                        refs = self._kb_references(goal, top_k=3)
                        exec_state[task["id"]] = {"references": refs}
                        refs_collected.extend(refs)
                    else:  # reason
                        reason_text = self._reason_from_state(question, exec_state, refs_collected)
                        exec_state[task["id"]] = {"reason": reason_text}

                if not reason_text:
                    reason_text = self._reason_from_state(question, exec_state, refs_collected)

                answer_payload: dict[str, Any] = {"content": reason_text}
                if image_paths:
                    answer_payload["image"] = image_paths
                if refs_collected:
                    answer_payload["references"] = refs_collected
                answers.append({"Q": question, "A": answer_payload})
                log_item["task_graph"].append(task_graph)
                log_item["references"].append(refs_collected)

            rows_out.append(
                {
                    "编号": qid,
                    "问题": json.dumps(turns, ensure_ascii=False),
                    "SQL 查询语法": "\n".join(sql_history) if sql_history else "无",
                    "回答": json.dumps(answers, ensure_ascii=False),
                }
            )
            self.run_log["task3"].append(log_item)

        result_path = self.paths.base_dir / "result_3.xlsx"
        pd.DataFrame(rows_out).to_excel(result_path, index=False)
        return result_path

    # ---------- Helpers ----------
    def _graph_type_from_images(self, images: list[Any]) -> str:
        if not images:
            return "无"
        first = images[0]
        if isinstance(first, dict):
            chart_type = str(first.get("type", ""))
            return {
                "line": "折线图",
                "bar": "柱状图",
                "horizontal_bar": "水平柱状图",
                "pie": "饼图",
                "combo": "组合图",
            }.get(chart_type, "无")
        if len(images) == 1:
            return "折线图"
        return "组合图"

    def _compose_content(self, question: str, intent_payload: dict[str, Any], rows: list[dict[str, Any]], images: list[str]) -> str:
        if self.llm_client.enabled:
            try:
                prompt = {
                    "question": question,
                    "intent": intent_payload,
                    "rows": rows[:20],
                    "images": images,
                }
                ans = self.llm_client.complete_json(
                    "你是财报分析助手。返回JSON: {\"content\":\"...\"}，content需基于数据给出结论。",
                    json.dumps(prompt, ensure_ascii=False),
                )
                if ans.get("content"):
                    return str(ans["content"])
            except Exception:
                pass
        if not rows:
            return "未查询到符合条件的数据。"
        intent = intent_payload.get("intent")
        slots = intent_payload.get("slots", {})
        if intent == "single_metric":
            return format_single_metric_answer(slots, rows[0])
        if intent == "trend":
            metric_col = slots.get("metric", "total_profit")
            metric_label = METRIC_LABELS.get(str(metric_col), str(metric_col))
            return format_trend_analysis_answer(rows, metric_col=str(metric_col), metric_label=metric_label)
        if intent == "topn_metric":
            metric_col = str(slots.get("metric", "total_profit"))
            metric_label = METRIC_LABELS.get(metric_col, metric_col)
            return format_topn_analysis_answer(rows, metric_col=metric_col, metric_label=metric_label)
        return f"共检索到 {len(rows)} 条记录。"

    def _render_chart_for_intent(
        self,
        qid: str,
        seq: int,
        intent_payload: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> list[str]:
        if not rows:
            return []
        intent = intent_payload.get("intent")
        if intent == "single_metric":
            return []
        if intent in {"trend"}:
            x_vals: list[str] = []
            y_vals: list[float] = []
            metric_col = None
            for row in rows:
                if metric_col is None:
                    metric_col = next((k for k, v in row.items() if k != "report_period" and isinstance(v, (int, float))), None)
                if not metric_col:
                    continue
                value = row.get(metric_col)
                if value is None:
                    continue
                try:
                    y = float(value)
                except (TypeError, ValueError):
                    continue
                x_vals.append(str(row.get("report_period", "")))
                y_vals.append(y)
            if x_vals and y_vals:
                return [self._plot_line_chart(qid, seq, x_vals, y_vals, title=f"{metric_col}趋势", y_label=str(metric_col))]
            return []
        # topn/comparison -> bar
        metric_col = next(
            (k for k, v in rows[0].items() if k not in {"stock_abbr", "stock_code", "report_period"} and isinstance(v, (int, float))),
            None,
        )
        if not metric_col:
            return []
        names: list[str] = []
        values: list[float] = []
        for r in rows[:20]:
            value = r.get(metric_col)
            if value is None:
                continue
            try:
                fv = float(value)
            except (TypeError, ValueError):
                continue
            names.append(str(r.get("stock_abbr", r.get("stock_code", ""))))
            values.append(fv)
        if not names or not values:
            return []
        return [self._plot_bar_chart(qid, seq, names, values, title=f"{metric_col}对比", y_label=str(metric_col))]

    def _render_chart_for_query_spec(
        self,
        qid: str,
        seq: int,
        query_spec: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        if not rows:
            return []
        chart_request = str(query_spec.get("chart_request") or "")
        analysis_type = str(query_spec.get("analysis_type") or "")
        metric_key = str(query_spec.get("metric") or "")
        metric_spec = TASK2_METRICS.get(metric_key)
        metric_col = metric_spec.field if metric_spec else metric_key
        title = f"{METRIC_LABELS.get(metric_col, metric_key)}分析"
        y_label = METRIC_LABELS.get(metric_col, metric_col)

        if chart_request == "line" or analysis_type in {"trend", "comparison"}:
            ordered = sorted(rows, key=lambda item: _period_sort_key(str(item.get("report_period", ""))))
            x_vals = [str(item.get("report_period", "")) for item in ordered]
            y_vals = [float(item.get(metric_col, 0) or 0) for item in ordered if item.get(metric_col) is not None]
            if x_vals and y_vals and len(x_vals) == len(y_vals):
                path = self._plot_line_chart(qid, seq, x_vals, y_vals, title=title, y_label=y_label)
                return [{"path": path, "type": "line"}]
            return []

        if chart_request == "horizontal_bar":
            names = [str(item.get("stock_abbr", item.get("stock_code", ""))) for item in rows[:20] if item.get(metric_col) is not None]
            values = [float(item.get(metric_col, 0) or 0) for item in rows[:20] if item.get(metric_col) is not None]
            if names and values:
                path = self._plot_bar_chart(qid, seq, names, values, title=title, y_label=y_label, horizontal=True)
                return [{"path": path, "type": "horizontal_bar"}]
            return []

        if chart_request == "pie":
            names = [str(item.get("stock_abbr", item.get("stock_code", ""))) for item in rows[:8] if item.get(metric_col) is not None]
            values = [float(item.get(metric_col, 0) or 0) for item in rows[:8] if item.get(metric_col) is not None]
            if names and values:
                path = self._plot_pie_chart(qid, seq, names, values, title=title)
                return [{"path": path, "type": "pie"}]
            return []

        if chart_request == "bar" or analysis_type in {"topn_metric", "filter", "intersection_topn"}:
            names = [str(item.get("stock_abbr", item.get("stock_code", ""))) for item in rows[:20] if item.get(metric_col) is not None]
            values = [float(item.get(metric_col, 0) or 0) for item in rows[:20] if item.get(metric_col) is not None]
            if names and values:
                path = self._plot_bar_chart(qid, seq, names, values, title=title, y_label=y_label)
                return [{"path": path, "type": "bar"}]
        return []

    def _kb_references(self, query: str, top_k: int = 3) -> list[dict[str, str]]:
        refs = []
        for item in self.kb.search(query, top_k=top_k):
            refs.append(
                {
                    "paper_path": item["paper_path"],
                    "text": item["text"][:220],
                    "paper_image": item.get("paper_image", ""),
                }
            )
        return refs

    def _reason_from_state(self, question: str, exec_state: dict[str, Any], refs: list[dict[str, str]]) -> str:
        if self.llm_client.enabled:
            try:
                payload = {"question": question, "exec_state": exec_state, "references": refs}
                ans = self.llm_client.complete_json(
                    "你是财务分析归因助手。返回JSON: {\"content\":\"...\"}，要求包含结论、依据和可靠性说明。",
                    json.dumps(payload, ensure_ascii=False),
                )
                if ans.get("content"):
                    return str(ans["content"])
            except Exception:
                pass
        sql_rows = 0
        topn_rows: list[dict[str, Any]] = []
        for item in exec_state.values():
            if isinstance(item, dict) and isinstance(item.get("rows"), list):
                sql_rows += len(item["rows"])
                topn_rows.extend(item.get("rows", []))
        if topn_rows:
            metric_col = "total_profit" if any("total_profit" in r for r in topn_rows) else None
            if metric_col:
                return format_topn_analysis_answer(topn_rows, metric_col=metric_col, metric_label=METRIC_LABELS.get(metric_col, metric_col))
            # Trend-like rows in task3 SQL path.
            if all("report_period" in r for r in topn_rows):
                metric_col = next(
                    (k for k in topn_rows[0].keys() if k not in {"stock_abbr", "stock_code", "report_period", "report_year"} and isinstance(topn_rows[0].get(k), (int, float))),
                    None,
                )
                if metric_col:
                    return format_trend_analysis_answer(topn_rows, metric_col=metric_col, metric_label=METRIC_LABELS.get(metric_col, metric_col))
        if refs:
            return format_reason_with_references(question, sql_rows_count=sql_rows, references=refs)
        return f"基于结构化数据分析：共检索到 {sql_rows} 条记录。"

    def _load_research_metadata(self) -> dict[str, dict[str, Any]]:
        metadata: dict[str, dict[str, Any]] = {}
        xlsx_files = [
            self.paths.research_dir / "个股_研报信息.xlsx",
            self.paths.research_dir / "行业_研报信息.xlsx",
        ]
        for xlsx_path in xlsx_files:
            if not xlsx_path.exists():
                continue
            wb = load_workbook(xlsx_path, read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            rows = ws.iter_rows(min_row=1, values_only=True)
            header = [str(item) for item in next(rows)]
            for row in rows:
                values = {header[i]: row[i] for i in range(len(header))}
                title = str(values.get("title") or "").strip()
                if title:
                    metadata[title] = values
        return metadata

    @staticmethod
    def _chunk_text(text: str, max_chunk_len: int = 280) -> list[str]:
        cleaned = re.sub(r"\s+", " ", text)
        segments = re.split(r"[。！？；]\s*", cleaned)
        chunks: list[str] = []
        buff = ""
        for segment in segments:
            segment = segment.strip()
            if not segment:
                continue
            if len(buff) + len(segment) + 1 <= max_chunk_len:
                buff = f"{buff}。{segment}" if buff else segment
            else:
                if buff:
                    chunks.append(buff)
                buff = segment
        if buff:
            chunks.append(buff)
        return chunks

    @staticmethod
    def _extract_paper_image_hint(text: str) -> str:
        match = re.search(r"(图表\s*\d+[：:][^\n。]{0,40})", text)
        return match.group(1) if match else ""

    def _plot_line_chart(
        self,
        qid: str,
        seq: int,
        x_values: list[str],
        y_values: list[float],
        *,
        title: str,
        y_label: str,
    ) -> str:
        file_name = f"{qid}_{seq}.jpg"
        out_path = self.paths.result_dir / file_name
        plt.figure(figsize=(8, 4.8))
        plt.plot(x_values, y_values, marker="o")
        plt.title(title)
        plt.xlabel("报告期")
        plt.ylabel(y_label)
        plt.grid(alpha=0.3)
        plt.tight_layout()
        plt.savefig(out_path, dpi=160)
        plt.close()
        return f"./result/{file_name}"

    def _plot_bar_chart(
        self,
        qid: str,
        seq: int,
        x_values: list[str],
        y_values: list[float],
        *,
        title: str,
        y_label: str,
        horizontal: bool = False,
    ) -> str:
        file_name = f"{qid}_{seq}.jpg"
        out_path = self.paths.result_dir / file_name
        plt.figure(figsize=(8, 4.8))
        if horizontal:
            plt.barh(x_values, y_values, color="#2a7fff")
            plt.xlabel(y_label)
        else:
            plt.bar(x_values, y_values, color="#2a7fff")
            plt.ylabel(y_label)
        plt.title(title)
        if not horizontal:
            plt.xticks(rotation=20)
        plt.tight_layout()
        plt.savefig(out_path, dpi=160)
        plt.close()
        return f"./result/{file_name}"

    def _plot_pie_chart(
        self,
        qid: str,
        seq: int,
        labels: list[str],
        values: list[float],
        *,
        title: str,
    ) -> str:
        file_name = f"{qid}_{seq}.jpg"
        out_path = self.paths.result_dir / file_name
        plt.figure(figsize=(7.2, 5.2))
        plt.pie(values, labels=labels, autopct="%1.1f%%")
        plt.title(title)
        plt.tight_layout()
        plt.savefig(out_path, dpi=160)
        plt.close()
        return f"./result/{file_name}"
