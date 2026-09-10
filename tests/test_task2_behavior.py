from __future__ import annotations

from pathlib import Path

import pytest

from smart_finqa.config import AppConfig, DatabaseConfig, LLMConfig
from smart_finqa.facts import FinancialFact, PeriodType, StatementScope, UntrustedFactError, ValidationStatus
from smart_finqa.pipeline import EXTRACTOR_VERSION, PipelinePaths, SmartFinancePipeline
from smart_finqa.quality import format_trend_analysis_answer
from smart_finqa.sql_planner import SQLPlanner
from smart_finqa.task2 import CompanyResolver, QuestionAnalyzer
from tests.helpers import create_dataset, write_company_workbook

TEST_SOURCE_SHA256 = "a" * 64


def _financial_fact(
    *,
    metric: str,
    value: float,
    period: str = "2025FY",
    period_type: PeriodType = PeriodType.DURATION,
    page_no: int | None = 1,
    row_label: str | None = "测试行",
    column_label: str | None = "本期金额",
    extractor_version: str = "test-v1",
) -> FinancialFact:
    table_name = "balance_sheet" if period_type is PeriodType.INSTANT else "income_sheet"
    return FinancialFact(
        company_id="600080",
        stock_code="600080",
        period=period,
        statement_scope=StatementScope.CONSOLIDATED,
        period_type=period_type,
        metric=metric,
        raw_value=value,
        normalized_value=value,
        source_unit="万元",
        target_unit="万元",
        currency="CNY",
        source_file=f"report-{period}.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        page_no=page_no,
        table_name=table_name,
        row_label=row_label,
        column_label=column_label,
        confidence=0.9,
        validation_status=ValidationStatus.NEEDS_REVIEW,
        validation_issues=(),
        extractor_version=extractor_version,
    )


def _validate_fact(pipeline: SmartFinancePipeline, fact: FinancialFact) -> None:
    pipeline.db.upsert_financial_facts([fact])
    fact_key = pipeline.db.query(
        "SELECT fact_key FROM financial_fact WHERE stock_code = ? AND period = ? AND metric = ?",
        (fact.stock_code, fact.period, fact.metric),
        use_cache=False,
    )[0]["fact_key"]
    pipeline.db.review_financial_fact(str(fact_key), ValidationStatus.VALIDATED)


def _build_company_resolver(tmp_path: Path) -> CompanyResolver:
    return CompanyResolver.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))


def test_company_resolver_supports_code_and_partial_name(tmp_path: Path) -> None:
    resolver = _build_company_resolver(tmp_path)
    assert resolver.resolve("999")["stock_abbr"] == "华润三九"
    assert resolver.resolve("企业名称：三金")["stock_abbr"] == "桂林三金"


def test_unknown_company_name_is_not_fabricated(tmp_path: Path) -> None:
    resolver = _build_company_resolver(tmp_path)
    assert resolver.resolve("企业名称：不存在制药") is None

    result = QuestionAnalyzer(resolver).analyze_turn("企业名称：不存在制药，2024年营业收入是多少？")

    assert result["need_clarify"] is True
    assert result["query_spec"] is None
    assert result["clarify_question"] == "请补充公司名称或股票代码。"


def test_question_analyzer_requires_period_for_single_metric(tmp_path: Path) -> None:
    analyzer = QuestionAnalyzer(company_resolver=_build_company_resolver(tmp_path))
    first = analyzer.analyze_turn("金花股份利润总额是多少", {})
    assert first["need_clarify"] is True
    assert "报告期" in first["clarify_question"]

    second = analyzer.analyze_turn("2025年第三季度的", first["context"])
    assert second["need_clarify"] is False
    assert second["query_spec"]["report_period"] == "2025Q3"
    assert second["query_spec"]["metric"] == "total_profit"


@pytest.mark.parametrize("period_text", ["2024 年", "2024 年年度"])
def test_question_analyzer_accepts_whitespace_inside_chinese_period(
    tmp_path: Path,
    period_text: str,
) -> None:
    analyzer = QuestionAnalyzer(company_resolver=_build_company_resolver(tmp_path))

    result = analyzer.analyze_turn(f"金花股份 {period_text}利润总额是多少", {})

    assert result["need_clarify"] is False
    assert result["query_spec"]["report_period"] == "2024FY"
    assert result["query_spec"]["metric"] == "total_profit"


def test_question_analyzer_accepts_whitespace_inside_period_range(tmp_path: Path) -> None:
    analyzer = QuestionAnalyzer(company_resolver=_build_company_resolver(tmp_path))

    result = analyzer.analyze_turn("对比白云山 2022 年至 2025 年第三季度的营业收入", {})

    assert result["need_clarify"] is False
    assert result["query_spec"]["start_period"] == "2022Q1"
    assert result["query_spec"]["end_period"] == "2025Q3"


def test_question_analyzer_supports_formal_question_patterns(tmp_path: Path) -> None:
    analyzer = QuestionAnalyzer(company_resolver=_build_company_resolver(tmp_path))

    q1 = analyzer.analyze_turn("香雪制药2024年研发费用是多少", {})
    assert q1["query_spec"]["metric"] == "operating_expense_rnd_expenses"
    assert q1["query_spec"]["analysis_type"] == "single_metric"

    q2 = analyzer.analyze_turn(
        "2025年第三季度，资产负债率（负债总额/资产总额）超过60%的上市公司有哪些？请列出股票代码、简称、资产负债率（%）。",
        {},
    )
    assert q2["query_spec"]["analysis_type"] == "filter"
    assert q2["query_spec"]["metric"] == "asset_liability_ratio"
    assert any(f["op"] == ">" and f["value"] == 60 for f in q2["query_spec"]["filters"])

    q3 = analyzer.analyze_turn(
        "对比白云山（600332）2022年至2025年第三季度的“营业总收入同比增长率”，展示各报告期的增长率数据，并生成趋势折线图。",
        {},
    )
    assert q3["query_spec"]["analysis_type"] == "trend"
    assert q3["query_spec"]["metric"] == "operating_revenue_yoy_growth"
    assert q3["query_spec"]["chart_request"] == "line"

    q4 = analyzer.analyze_turn(
        "2025年第三季度，“资产-存货”金额排名前三的公司，其“营业总收入”与存货金额的比值分别是多少？请列出公司简称、存货（万元）、营业总收入（万元）及比值。",
        {},
    )
    assert q4["query_spec"]["metric"] == "asset_inventory"
    assert q4["query_spec"]["metrics"][:2] == ["asset_inventory", "total_operating_revenue"]


def test_sql_planner_builds_filter_and_trend_sql() -> None:
    planner = SQLPlanner()

    trend_query = planner.compile(
        {
            "intent": "trend",
            "query_spec": {
                "analysis_type": "trend",
                "table": "income_sheet",
                "metric": "operating_revenue_yoy_growth",
                "stock_abbr": "白云山",
                "start_period": "2022Q1",
                "end_period": "2025Q3",
                "select_fields": ["report_period", "operating_revenue_yoy_growth", "stock_abbr"],
            },
        }
    )
    assert "CASE" in trend_query.sql
    assert "operating_revenue_yoy_growth" in trend_query.sql
    assert "白云山" not in trend_query.sql
    assert trend_query.params == (
        "operating_revenue_yoy_growth",
        "VALIDATED",
        "consolidated",
        "duration",
        "CURRENT",
        "白云山",
        20221,
        20253,
    )

    filter_query = planner.compile(
        {
            "intent": "filter",
            "query_spec": {
                "analysis_type": "filter",
                "table": "balance_sheet",
                "metric": "asset_liability_ratio",
                "report_period": "2025Q3",
                "select_fields": ["stock_code", "stock_abbr", "asset_liability_ratio"],
                "filters": [{"field": "asset_liability_ratio", "op": ">", "value": 60}],
            },
        }
    )
    assert "FROM balance_sheet" in filter_query.sql
    assert "vf0.normalized_value > ?" in filter_query.sql
    assert filter_query.params == (
        "asset_liability_ratio",
        "VALIDATED",
        "consolidated",
        "instant",
        "CURRENT",
        "2025Q3",
        60,
    )


def test_trend_formatter_uses_chronological_order() -> None:
    rows = [
        {"report_period": "2024FY", "total_profit": 40},
        {"report_period": "2024Q1", "total_profit": 10},
        {"report_period": "2024Q2", "total_profit": 20},
        {"report_period": "2024Q3", "total_profit": 30},
    ]
    content = format_trend_analysis_answer(rows, metric_col="total_profit", metric_label="利润总额")
    assert "2024Q1 至 2024FY" in content
    assert "最近区间数据点为 2024Q2:20.00；2024Q3:30.00；2024FY:40.00" in content


def test_task2_mode_requires_existing_database(tmp_path: Path) -> None:
    create_dataset(tmp_path)
    paths = PipelinePaths.from_base_dir(tmp_path, full_data=False)
    cfg = AppConfig(
        mode="task2",
        full_data=False,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(paths, app_config=cfg)
    with pytest.raises(RuntimeError, match="database"):
        pipeline.run(mode="task2")


def test_graph_type_uses_real_chart_type(tmp_path: Path) -> None:
    create_dataset(tmp_path)
    paths = PipelinePaths.from_base_dir(tmp_path, full_data=False)
    cfg = AppConfig(
        mode="ingest",
        full_data=False,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(paths, app_config=cfg)
    assert pipeline._graph_type_from_images([{"path": "./result/B001_1.jpg", "type": "horizontal_bar"}]) == "水平柱状图"
    assert pipeline._graph_type_from_images([{"path": "./result/B001_1.jpg", "type": "bar"}]) == "柱状图"


def test_incremental_skip_requires_all_tables_present(tmp_path: Path) -> None:
    create_dataset(tmp_path)
    cfg = AppConfig(
        mode="ingest",
        full_data=False,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    paths = PipelinePaths.from_base_dir(tmp_path, full_data=False)
    pipeline = SmartFinancePipeline(paths, app_config=cfg)
    pipeline.db.create_tables()
    pipeline.db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
            "total_profit": 1.0,
        },
    )
    assert pipeline._has_complete_period_record("600080", "2025Q3") is False


def test_trusted_pipeline_paths_reject_mismatched_source_business_identity(tmp_path: Path) -> None:
    create_dataset(tmp_path)
    cfg = AppConfig(
        mode="ingest",
        full_data=False,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path, full_data=False), app_config=cfg)
    base_row = {
        "serial_number": 1,
        "stock_code": "600080",
        "stock_abbr": "金花股份",
        "report_period": "2025FY",
        "report_year": 2025,
    }
    for table in pipeline.schema:
        pipeline.db.upsert(table, base_row)
    fact = _financial_fact(metric="net_profit", value=10.0, extractor_version=EXTRACTOR_VERSION)
    _validate_fact(pipeline, fact)

    assert pipeline._fact_status_counts() == {"VALIDATED": 1}
    assert pipeline._fact_traceability()["total"] == 1
    assert pipeline._validated_projection_rows("income_sheet", ("net_profit",))[0]["net_profit"] == 10.0
    assert pipeline._has_complete_period_record("600080", "2025FY", ["income_sheet"]) is True
    assert pipeline._validated_fact_source("600080", "2025FY", "net_profit")["normalized_value"] == 10.0
    pipeline._ensure_database_ready()

    def assert_mismatched_source_is_untrusted() -> None:
        assert pipeline._fact_status_counts() == {}
        assert pipeline._fact_traceability()["total"] == 0
        assert pipeline._validated_projection_rows("income_sheet", ("net_profit",)) == []
        assert pipeline._has_complete_period_record("600080", "2025FY", ["income_sheet"]) is False
        with pytest.raises(UntrustedFactError, match="got 0"):
            pipeline._validated_fact_source("600080", "2025FY", "net_profit")
        with pytest.raises(RuntimeError, match="no VALIDATED financial facts"):
            pipeline._ensure_database_ready()

    pipeline.db.execute(
        "UPDATE financial_source SET stock_code = ?, period = ? WHERE source_key = ?",
        ("000001", "2025FY", fact.source_key),
    )
    assert_mismatched_source_is_untrusted()

    pipeline.db.execute(
        "UPDATE financial_source SET stock_code = ?, period = ? WHERE source_key = ?",
        ("600080", "2024FY", fact.source_key),
    )
    assert_mismatched_source_is_untrusted()


def test_field_aliases_strip_sheet_prefixes(tmp_path: Path) -> None:
    cfg = AppConfig(
        mode="ingest",
        full_data=False,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    create_dataset(tmp_path)
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path, full_data=False), app_config=cfg)
    balance_field = next(field for field in pipeline.schema["balance_sheet"] if field.field_name == "asset_inventory")
    cash_field = next(
        field for field in pipeline.schema["cash_flow_sheet"] if field.field_name == "operating_cf_net_amount"
    )
    assert "存货" in pipeline._field_aliases(balance_field)
    assert "现金流量净额" in pipeline._field_aliases(cash_field)
    assert "资产" not in pipeline._field_aliases(balance_field)


def test_extract_field_value_avoids_generic_balance_prefix_match(tmp_path: Path) -> None:
    cfg = AppConfig(
        mode="ingest",
        full_data=False,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    create_dataset(tmp_path)
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path, full_data=False), app_config=cfg)
    field = next(field for field in pipeline.schema["balance_sheet"] if field.field_name == "asset_accounts_receivable")
    section_text = (
        "合并资产负债表\n"
        "流动资产：\n"
        "交易性金融资产 1,915,071,623.85 1,860,774,958.89\n"
        "应收账款 415,658,940.82 383,881,541.26\n"
    )
    lines = [line.strip() for line in section_text.splitlines() if line.strip()]
    value = pipeline._extract_field_value(section_text, lines, field)
    assert value == pytest.approx(41565.8941, rel=1e-6)


def test_sanitize_row_values_drops_invalid_growth_fields(tmp_path: Path) -> None:
    cfg = AppConfig(
        mode="ingest",
        full_data=False,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    create_dataset(tmp_path)
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path, full_data=False), app_config=cfg)
    row = {field.field_name: None for field in pipeline.schema["balance_sheet"]}
    row.update(
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
            "asset_cash_and_cash_equivalents": 1000.0,
            "asset_accounts_receivable": 200.0,
            "asset_inventory": 300.0,
            "asset_total_assets": 5000.0,
            "liability_accounts_payable": 150.0,
            "liability_total_liabilities": 1200.0,
            "liability_short_term_loans": 50.0,
            "equity_unappropriated_profit": 400.0,
            "asset_total_assets_yoy_growth": 1915071623.85,
            "liability_total_liabilities_yoy_growth": 591314838.79,
        }
    )
    cleaned = pipeline._sanitize_row_values("balance_sheet", row)
    assert cleaned["asset_total_assets_yoy_growth"] is None
    assert cleaned["liability_total_liabilities_yoy_growth"] is None
    ok, issues = pipeline._validate_row("balance_sheet", cleaned)
    assert ok is True
    assert issues == []


def test_locate_table_section_prefers_real_statement_over_audit_mention(tmp_path: Path) -> None:
    cfg = AppConfig(
        mode="ingest",
        full_data=False,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    create_dataset(tmp_path)
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path, full_data=False), app_config=cfg)
    text = (
        "审计报告提及合并资产负债表和母公司资产负债表、合并利润表。\n"
        "这里没有任何表格正文。\n"
        "其他说明。\n"
        "合并资产负债表\n"
        "货币资金 6,021,681,418.07 5,370,034,362.27\n"
        "应收账款 415,658,940.82 383,881,541.26\n"
        "存货 530,000,000.00 420,000,000.00\n"
        "总资产 9,999,999,999.99 8,888,888,888.88\n"
        "合并利润表\n"
    )
    section = pipeline._locate_table_section(text, "balance_sheet")
    assert "货币资金" in section
    assert "应收账款" in section


def test_extract_field_value_reads_following_numeric_line(tmp_path: Path) -> None:
    cfg = AppConfig(
        mode="ingest",
        full_data=False,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    create_dataset(tmp_path)
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path, full_data=False), app_config=cfg)
    field = next(
        field for field in pipeline.schema["balance_sheet"] if field.field_name == "asset_cash_and_cash_equivalents"
    )
    section_text = "资产负债表\n货币资金\n221,123,205.57 236,120,514.71\n应收账款\n97,802,419.10 93,095,689.67\n"
    lines = [line.strip() for line in section_text.splitlines() if line.strip()]
    value = pipeline._extract_field_value(section_text, lines, field)
    assert value == pytest.approx(22112.3206, rel=1e-6)


def test_validate_database_uses_source_available_tables(tmp_path: Path) -> None:
    base = tmp_path
    sample_dir = create_dataset(tmp_path)
    output_dir = tmp_path / "outputs"
    result_dir = tmp_path / "result"
    paths = PipelinePaths(
        base_dir=base,
        sample_dir=sample_dir,
        schema_xlsx=sample_dir / "附件3：数据库-表名及字段说明.xlsx",
        company_xlsx=sample_dir / "附件1：上市公司基本信息.xlsx",
        reports_dir=sample_dir / "附件2：财务报告",
        task2_xlsx=sample_dir / "附件4：问题汇总.xlsx",
        task3_xlsx=sample_dir / "附件6：问题汇总.xlsx",
        research_dir=sample_dir / "附件5：研报数据",
        output_dir=output_dir,
        result_dir=result_dir,
        db_path=tmp_path / "finance.db",
    )
    cfg = AppConfig(
        mode="ingest",
        full_data=False,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(paths, app_config=cfg)
    pipeline.db.create_tables()
    pipeline.db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
            "total_profit": 1.0,
        },
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "ingestion_state.json").write_text(
        '{"version": 4, "files": {"/fake.pdf": {"status": "parsed", "stock_code": "600080", "report_period": "2025Q3", "extractor_version": "legacy-regex-v1", "available_tables": ["income_sheet"]}}}',
        encoding="utf-8",
    )
    validation = pipeline.validate_database()
    assert validation["cross_table"] == []
    assert validation["validation_coverage"]["cash_net_change"]["status"] == "unavailable"


def test_validation_report_blocks_unreviewed_facts_and_reports_traceability(tmp_path: Path) -> None:
    create_dataset(tmp_path)
    config = AppConfig(
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path), app_config=config)
    pipeline.db.upsert_financial_facts(
        [
            _financial_fact(metric="total_operating_revenue", value=100.0),
            _financial_fact(
                metric="net_profit",
                value=10.0,
                page_no=None,
                column_label=None,
            ),
        ]
    )

    validation = pipeline.validate_database()

    assert validation["query_readiness"]["status"] == "BLOCKED"
    assert validation["query_readiness"]["validated_fact_count"] == 0
    assert "VALIDATED" in validation["query_readiness"]["reason"]
    assert validation["fact_traceability"] == {
        "total": 2,
        "with_source_file": 2,
        "with_page_no": 1,
        "with_row_label": 2,
        "with_column_label": 1,
        "fully_traceable": 1,
        "source_file_coverage": 1.0,
        "page_no_coverage": 0.5,
        "row_label_coverage": 1.0,
        "column_label_coverage": 0.5,
        "fully_traceable_coverage": 0.5,
    }
    balance_coverage = validation["validation_coverage"]["balance_equation"]
    assert balance_coverage["schema_ready"] is True
    assert balance_coverage["eligible_records"] == 0
    assert balance_coverage["status"] == "unavailable"
    assert "VALIDATED" in balance_coverage["reason"]
    yoy_coverage = validation["validation_coverage"]["revenue_yoy_recalculation"]
    assert yoy_coverage["schema_ready"] is True
    assert yoy_coverage["eligible_records"] == 0
    assert yoy_coverage["status"] == "unavailable"
    assert "VALIDATED" in yoy_coverage["reason"]


def test_validation_status_is_unavailable_when_no_consistency_rule_can_run(tmp_path: Path) -> None:
    create_dataset(tmp_path)
    config = AppConfig(
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path), app_config=config)
    base_row = {
        "serial_number": 1,
        "stock_code": "600080",
        "stock_abbr": "金花股份",
        "report_period": "2025FY",
        "report_year": 2025,
    }
    for table in pipeline.schema:
        pipeline.db.upsert(table, base_row)
    _validate_fact(pipeline, _financial_fact(metric="net_profit", value=10.0))

    validation = pipeline.validate_database()

    assert validation["query_readiness"]["status"] == "READY"
    assert validation["validation_execution"] == {
        "eligible_records": 0,
        "rules_with_eligible_records": 0,
    }
    assert validation["status"] == "VALIDATION_UNAVAILABLE"


def test_projection_metric_counts_exclude_identity_only_seed_rows(tmp_path: Path) -> None:
    create_dataset(tmp_path)
    config = AppConfig(
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path), app_config=config)
    seed = {
        "serial_number": 1,
        "stock_code": "600080",
        "stock_abbr": "金花股份",
        "report_period": "2025FY",
        "report_year": 2025,
    }
    pipeline.db.refresh_fact_projection("income_sheet", seed)
    grouped_rows = {"income_sheet": [seed]}

    rows_by_table, cells_by_table = pipeline._projection_validated_metric_counts(grouped_rows)

    assert rows_by_table == {"income_sheet": 0}
    assert cells_by_table == {"income_sheet": 0}

    _validate_fact(pipeline, _financial_fact(metric="net_profit", value=10.0))
    rows_by_table, cells_by_table = pipeline._projection_validated_metric_counts(grouped_rows)

    assert rows_by_table == {"income_sheet": 1}
    assert cells_by_table == {"income_sheet": 1}


def test_validation_coverage_requires_complete_validated_consistency_inputs(tmp_path: Path) -> None:
    create_dataset(tmp_path)
    config = AppConfig(
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path), app_config=config)
    pipeline.db.upsert(
        "balance_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025FY",
            "report_year": 2025,
        },
    )
    for metric, value in (
        ("asset_total_assets", 1000.0),
        ("liability_total_liabilities", 400.0),
        ("equity_total_equity", 600.0),
    ):
        _validate_fact(
            pipeline,
            _financial_fact(metric=metric, value=value, period_type=PeriodType.INSTANT),
        )

    for serial_number, period, year in ((1, "2024FY", 2024), (2, "2025FY", 2025)):
        for table in pipeline.schema:
            pipeline.db.upsert(
                table,
                {
                    "serial_number": serial_number,
                    "stock_code": "600080",
                    "stock_abbr": "金花股份",
                    "report_period": period,
                    "report_year": year,
                },
            )
    for fact in (
        _financial_fact(metric="total_operating_revenue", value=100.0, period="2024FY"),
        _financial_fact(metric="total_operating_revenue", value=120.0),
        _financial_fact(metric="operating_revenue_yoy_growth", value=20.0),
    ):
        _validate_fact(pipeline, fact)

    validation = pipeline.validate_database()

    assert validation["validation_execution"] == {
        "eligible_records": 2,
        "rules_with_eligible_records": 2,
    }
    assert validation["status"] == "PASS"
    assert validation["query_readiness"] == {
        "status": "READY",
        "validated_fact_count": 6,
        "reason": "VALIDATED financial facts are available for controlled queries",
    }
    balance_coverage = validation["validation_coverage"]["balance_equation"]
    assert balance_coverage == {
        "status": "available",
        "schema_ready": True,
        "eligible_records": 1,
        "reason": "1 record(s) have complete VALIDATED inputs",
    }
    yoy_coverage = validation["validation_coverage"]["revenue_yoy_recalculation"]
    assert yoy_coverage == {
        "status": "available",
        "schema_ready": True,
        "eligible_records": 1,
        "reason": "1 record(s) have complete VALIDATED current/prior-period inputs",
    }


def test_financial_consistency_reports_balance_equation_mismatch(tmp_path: Path) -> None:
    create_dataset(tmp_path)
    config = AppConfig(
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(PipelinePaths.from_base_dir(tmp_path), app_config=config)
    pipeline.db.upsert(
        "balance_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
        },
    )
    for metric, value in (
        ("asset_total_assets", 1000.0),
        ("liability_total_liabilities", 400.0),
        ("equity_total_equity", 500.0),
    ):
        _validate_fact(
            pipeline,
            _financial_fact(
                metric=metric,
                value=value,
                period="2025Q3",
                period_type=PeriodType.INSTANT,
            ),
        )

    issues = pipeline._financial_consistency_issues()

    assert issues[0]["code"] == "balance_equation_mismatch"
    assert issues[0]["difference"] == 100.0
