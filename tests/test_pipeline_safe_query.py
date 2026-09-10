from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from smart_finqa.config import AppConfig, DatabaseConfig, LLMConfig
from smart_finqa.facts import (
    ExtractedFactCandidate,
    FinancialFact,
    PeriodType,
    StatementScope,
    ValidationStatus,
)
from smart_finqa.ingestion import PageText
from smart_finqa.pipeline import PipelinePaths, SmartFinancePipeline
from tests.helpers import create_dataset

TEST_SOURCE_SHA256 = "a" * 64


def _pipeline_with_profit(tmp_path: Path) -> SmartFinancePipeline:
    create_dataset(tmp_path)
    paths = PipelinePaths.from_base_dir(tmp_path, full_data=False)
    config = AppConfig(
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(paths, app_config=config)
    pipeline.db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
            "total_profit": 321.0,
        },
    )
    pipeline.db.upsert_financial_facts(
        [
            FinancialFact.from_candidate(
                ExtractedFactCandidate(
                    metric="total_profit",
                    raw_value=321.0,
                    source_unit="万元",
                    page_no=8,
                    table_name="income_sheet",
                    row_label="利润总额",
                    column_label="本期金额",
                    confidence=1.0,
                ),
                company_id="600080",
                stock_code="600080",
                period="2025Q3",
                statement_scope=StatementScope.CONSOLIDATED,
                period_type=PeriodType.DURATION,
                target_unit="万元",
                currency="CNY",
                source_file="report.pdf",
                source_content_sha256=TEST_SOURCE_SHA256,
                extractor_version="human-reviewed-test",
            )
        ]
    )
    fact_key = pipeline.db.query("SELECT fact_key FROM financial_fact")[0]["fact_key"]
    pipeline.db.review_financial_fact(fact_key, ValidationStatus.VALIDATED)
    return pipeline


def _write_questions(path: Path, identifier: str) -> None:
    turns = [{"Q": "金花股份2025年第三季度利润总额是多少？"}]
    pd.DataFrame([{"编号": identifier, "问题": json.dumps(turns, ensure_ascii=False)}]).to_excel(path, index=False)


def test_task2_uses_parameterized_safe_query_executor(tmp_path: Path) -> None:
    pipeline = _pipeline_with_profit(tmp_path)
    _write_questions(pipeline.paths.task2_xlsx, "T2-1")

    pipeline.answer_task2()

    log = pipeline.run_log["task2"][0]
    assert "金花股份" not in log["sql"][0]
    assert "金花股份" in log["params"][0]
    assert "2025Q3" in log["params"][0]
    assert log["query_audit"][0]["status"] == "success"
    assert log["query_audit"][0]["row_count"] == 1
    assert log["query_audit"][0]["fact_sources"][0]["page_no"] == 8


def test_task3_uses_same_parameterized_safe_query_executor(tmp_path: Path) -> None:
    pipeline = _pipeline_with_profit(tmp_path)
    _write_questions(pipeline.paths.task3_xlsx, "T3-1")

    pipeline.answer_task3()

    log = pipeline.run_log["task3"][0]
    assert "金花股份" not in log["sql"][0]
    assert log["params"] == [["total_profit", "VALIDATED", "consolidated", "duration", "CURRENT", "金花股份", "2025Q3"]]
    assert log["sql_check"] == [True]
    assert log["query_audit"][0]["status"] == "success"
    assert log["query_audit"][0]["row_count"] == 1
    assert log["query_audit"][0]["fact_sources"][0]["page_no"] == 8


def test_knowledge_base_indexes_and_returns_page_level_evidence(tmp_path: Path, monkeypatch) -> None:
    pipeline = _pipeline_with_profit(tmp_path)
    paper = pipeline.paths.research_dir / "渠道研究.pdf"
    paper.write_bytes(b"placeholder")
    monkeypatch.setattr(
        pipeline.pdf_page_extractor,
        "extract",
        lambda _path: (
            PageText(page_no=1, text="封面"),
            PageText(page_no=2, text="渠道扩张推动营业收入增长。"),
        ),
    )

    pipeline.build_knowledge_base()
    references = pipeline._kb_references("渠道扩张")

    assert references[0]["paper_path"].endswith("渠道研究.pdf")
    assert references[0]["page_no"] == 2
    assert "渠道扩张" in references[0]["quote"]


def test_retrieval_reason_fails_explicitly_without_page_evidence(tmp_path: Path) -> None:
    pipeline = _pipeline_with_profit(tmp_path)

    content = pipeline._reason_from_state("收入增长原因是什么？", {"s1": {"references": []}}, [])

    assert content.startswith("证据不足")
