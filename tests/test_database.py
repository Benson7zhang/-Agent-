from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from smart_finqa.database import FinanceDatabase
from smart_finqa.facts import (
    AuthoritativeSource,
    ExtractedFactCandidate,
    FinancialFact,
    IncompleteFactEvidenceError,
    PeriodType,
    SourceAuthorityStatus,
    SourceOwner,
    StatementScope,
    ValidationIssue,
    ValidationStatus,
)
from smart_finqa.safe_query import SafeQueryExecutor
from smart_finqa.schema import load_schema_from_xlsx
from smart_finqa.sql_planner import SQLPlanner
from tests.helpers import write_schema_workbook

TEST_SOURCE_SHA256 = "a" * 64
REPLACEMENT_SOURCE_SHA256 = "b" * 64
WEB_SOURCE_SHA256 = "c" * 64


def _profit_fact(
    *,
    value: float,
    source_file: str,
    source_content_sha256: str = TEST_SOURCE_SHA256,
) -> FinancialFact:
    return FinancialFact.from_candidate(
        ExtractedFactCandidate(
            metric="total_profit",
            raw_value=value,
            source_unit="万元",
            page_no=8,
            table_name="income_sheet",
            row_label="利润总额",
            column_label="本期金额",
            confidence=0.8,
        ),
        company_id="600080",
        stock_code="600080",
        period="2025Q3",
        statement_scope=StatementScope.CONSOLIDATED,
        period_type=PeriodType.DURATION,
        target_unit="万元",
        currency="CNY",
        source_file=source_file,
        source_content_sha256=source_content_sha256,
        extractor_version="test-v1",
    )


def _fact_key_for_source(db: FinanceDatabase, source_file: str) -> str:
    return str(
        db.query(
            "SELECT fact_key FROM financial_fact WHERE source_file = ?",
            (source_file,),
            use_cache=False,
        )[0]["fact_key"]
    )


def test_create_and_upsert(tmp_path) -> None:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    schema = load_schema_from_xlsx(schema_path)
    db_path = tmp_path / "finance.db"
    db = FinanceDatabase(db_path, schema)
    db.create_tables()
    db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "total_profit": 3140.0,
            "report_period": "2025Q3",
            "report_year": 2025,
        },
    )
    rows = db.query("SELECT stock_abbr, total_profit FROM income_sheet WHERE report_period='2025Q3'")
    assert len(rows) == 1
    assert rows[0]["stock_abbr"] == "金花股份"


def test_source_identity_migration_is_fail_closed_and_idempotent(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.execute(
        "CREATE TABLE financial_source ("
        "source_key TEXT PRIMARY KEY, source_file TEXT NOT NULL, stock_code TEXT NOT NULL, period TEXT NOT NULL, "
        "authority_status TEXT NOT NULL, current_slot TEXT, document_id TEXT, selection_version TEXT NOT NULL, "
        "UNIQUE(stock_code, period, current_slot))"
    )
    old_fact_ddl = db._build_financial_fact_table_sql().replace("                source_key TEXT NOT NULL,\n", "")
    assert "source_key TEXT NOT NULL" not in old_fact_ddl
    db.execute(old_fact_ddl)
    db.execute("CREATE TABLE document (document_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")

    web_document_id = "00000000-0000-0000-0000-000000000001"
    db.execute("INSERT INTO document (document_id, sha256) VALUES (?, ?)", (web_document_id, WEB_SOURCE_SHA256))
    db.execute_many(
        "INSERT INTO financial_source (source_key, source_file, stock_code, period, authority_status, current_slot, "
        "document_id, selection_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "legacy-batch-key",
                "batch.pdf",
                "600080",
                "2024FY",
                SourceAuthorityStatus.CURRENT.value,
                "CURRENT",
                None,
                "report-selection-v1",
            ),
            (
                "legacy-web-key",
                "web.pdf",
                "600080",
                "2025FY",
                SourceAuthorityStatus.CURRENT.value,
                "CURRENT",
                web_document_id,
                "web-promotion-v1",
            ),
        ],
    )
    fact_columns = (
        "fact_key, company_id, stock_code, period, statement_scope, period_type, metric, raw_value, "
        "normalized_value, source_unit, target_unit, currency, source_file, page_no, table_name, row_label, "
        "column_label, confidence, validation_status, validation_issues, extractor_version, review_version, document_id"
    )
    db.execute_many(
        f"INSERT INTO financial_fact ({fact_columns}) VALUES ({', '.join('?' for _ in range(23))})",
        [
            (
                "batch-fact",
                "600080",
                "600080",
                "2024FY",
                "consolidated",
                "duration",
                "total_profit",
                "100",
                100.0,
                "万元",
                "万元",
                "CNY",
                "batch.pdf",
                8,
                "income_sheet",
                "利润总额",
                "本期金额",
                1.0,
                "VALIDATED",
                "[]",
                "legacy-v1",
                1,
                None,
            ),
            (
                "web-fact",
                "600080",
                "600080",
                "2025FY",
                "consolidated",
                "duration",
                "total_profit",
                "200",
                200.0,
                "万元",
                "万元",
                "CNY",
                "web.pdf",
                8,
                "income_sheet",
                "利润总额",
                "本期金额",
                1.0,
                "VALIDATED",
                "[]",
                "legacy-v1",
                1,
                web_document_id,
            ),
        ],
    )

    db.create_tables()
    web_source_key = db.financial_source_key("web.pdf", WEB_SOURCE_SHA256)
    expected_sources = [
        {
            "source_file": "batch.pdf",
            "content_sha256": None,
            "source_owner": SourceOwner.LEGACY.value,
            "authority_status": SourceAuthorityStatus.REMOVED.value,
            "current_slot": None,
        },
        {
            "source_file": "web.pdf",
            "content_sha256": WEB_SOURCE_SHA256,
            "source_owner": SourceOwner.WEB.value,
            "authority_status": SourceAuthorityStatus.CURRENT.value,
            "current_slot": "CURRENT",
        },
    ]
    assert (
        db.query(
            "SELECT source_file, content_sha256, source_owner, authority_status, current_slot "
            "FROM financial_source ORDER BY source_file",
            use_cache=False,
        )
        == expected_sources
    )
    assert db.query(
        "SELECT source_file, source_key, validation_status FROM financial_fact ORDER BY source_file",
        use_cache=False,
    ) == [
        {"source_file": "batch.pdf", "source_key": "legacy-batch-key", "validation_status": "VALIDATED"},
        {"source_file": "web.pdf", "source_key": web_source_key, "validation_status": "VALIDATED"},
    ]
    with pytest.raises(ValueError, match="not CURRENT"):
        db.review_financial_fact("batch-fact", ValidationStatus.VALIDATED)

    db.create_tables()
    assert (
        db.query(
            "SELECT source_file, content_sha256, source_owner, authority_status, current_slot "
            "FROM financial_source ORDER BY source_file",
            use_cache=False,
        )
        == expected_sources
    )


def test_upsert_financial_fact_preserves_provenance(tmp_path) -> None:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    schema = load_schema_from_xlsx(schema_path)
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    fact = FinancialFact.from_candidate(
        ExtractedFactCandidate(
            metric="total_profit",
            raw_value="10000",
            source_unit="元",
            page_no=8,
            table_name="合并利润表",
            row_label="利润总额",
            column_label="本期金额",
            confidence=0.8,
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
        extractor_version="test-v1",
    )

    db.upsert_financial_facts([fact])

    rows = db.query("SELECT * FROM financial_fact")
    assert rows[0]["normalized_value"] == 1.0
    assert rows[0]["page_no"] == 8
    assert rows[0]["validation_status"] == "NEEDS_REVIEW"


def test_review_financial_fact_requires_an_explicit_valid_decision(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
        },
    )
    fact = FinancialFact.from_candidate(
        ExtractedFactCandidate(
            metric="total_profit",
            raw_value="10000",
            source_unit="元",
            page_no=8,
            table_name="income_sheet",
            row_label="利润总额",
            column_label="本期金额",
            confidence=0.8,
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
        extractor_version="test-v1",
    )
    db.upsert_financial_facts([fact])
    fact_key = db.query("SELECT fact_key FROM financial_fact")[0]["fact_key"]

    db.review_financial_fact(fact_key, ValidationStatus.VALIDATED)

    reviewed = db.query("SELECT validation_status, validation_issues FROM financial_fact")[0]
    assert reviewed == {"validation_status": "VALIDATED", "validation_issues": "[]"}

    db.upsert_financial_facts([fact])
    assert db.query("SELECT validation_status FROM financial_fact")[0]["validation_status"] == "VALIDATED"

    issue = ValidationIssue(code="wrong_column", message="选中了上期列")
    db.review_financial_fact(fact_key, ValidationStatus.REJECTED, validation_issues=(issue,))
    rejected = db.query("SELECT validation_status, validation_issues FROM financial_fact")[0]
    assert rejected["validation_status"] == "REJECTED"
    assert "wrong_column" in rejected["validation_issues"]
    assert db.query("SELECT total_profit FROM income_sheet")[0]["total_profit"] is None


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("page_no", None),
        ("table_name", None),
        ("table_name", "  "),
        ("row_label", None),
        ("row_label", "  "),
        ("column_label", None),
        ("column_label", "  "),
    ],
)
def test_review_financial_fact_rejects_incomplete_evidence_without_updating_projection(
    tmp_path, field_name: str, value: object
) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
        },
    )
    fact = replace(_profit_fact(value=100.0, source_file="report.pdf"), **{field_name: value})
    db.upsert_financial_facts([fact])
    fact_key = _fact_key_for_source(db, "report.pdf")

    with pytest.raises(IncompleteFactEvidenceError, match=field_name):
        db.review_financial_fact(fact_key, ValidationStatus.VALIDATED)

    assert db.query("SELECT validation_status FROM financial_fact", use_cache=False) == [
        {"validation_status": "NEEDS_REVIEW"}
    ]
    assert db.query("SELECT total_profit FROM income_sheet", use_cache=False)[0]["total_profit"] is None


def test_review_rejects_fact_with_unknown_projection_table(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    db.upsert_financial_facts([replace(_profit_fact(value=100.0, source_file="report.pdf"), table_name="合并利润表")])

    with pytest.raises(ValueError, match="unknown projection table"):
        db.review_financial_fact(_fact_key_for_source(db, "report.pdf"), ValidationStatus.VALIDATED)


def test_candidate_upsert_cannot_bypass_human_review(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    fact = replace(_profit_fact(value=100.0, source_file="report.pdf"), validation_status=ValidationStatus.VALIDATED)

    with pytest.raises(ValueError, match="NEEDS_REVIEW"):
        db.upsert_financial_facts([fact])


def test_fact_upsert_rejects_one_source_with_multiple_business_keys(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    first = _profit_fact(value=100.0, source_file="report.pdf")
    second = replace(first, period="2024FY", raw_value=90.0, normalized_value=90.0)

    with pytest.raises(ValueError, match="one source_file"):
        db.upsert_financial_facts([first, second])

    assert db.query("SELECT * FROM financial_fact", use_cache=False) == []
    assert db.query("SELECT * FROM financial_source", use_cache=False) == []


def test_review_rolls_back_status_when_projection_update_fails(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
        },
    )
    db.upsert_financial_facts([_profit_fact(value=100.0, source_file="report.pdf")])
    fact_key = db.query("SELECT fact_key FROM financial_fact", use_cache=False)[0]["fact_key"]
    db.execute(
        "CREATE TRIGGER reject_profit_update BEFORE UPDATE OF total_profit ON income_sheet "
        "BEGIN SELECT RAISE(ABORT, 'projection failed'); END"
    )

    with pytest.raises(Exception, match="projection failed"):
        db.review_financial_fact(fact_key, ValidationStatus.VALIDATED)

    fact = db.query("SELECT validation_status FROM financial_fact", use_cache=False)[0]
    assert fact["validation_status"] == ValidationStatus.NEEDS_REVIEW.value


def test_concurrent_reviews_allow_only_one_validated_business_fact(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db_path = tmp_path / "finance.db"
    setup_db = FinanceDatabase(db_path, schema)
    setup_db.create_tables()
    setup_db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
        },
    )
    setup_db.upsert_financial_facts(
        [
            _profit_fact(value=100.0, source_file="first.pdf"),
            _profit_fact(value=200.0, source_file="second.pdf"),
        ]
    )
    fact_keys = [
        row["fact_key"]
        for row in setup_db.query("SELECT fact_key FROM financial_fact ORDER BY source_file", use_cache=False)
    ]
    setup_db.close()

    def review(fact_key: str) -> str:
        database = FinanceDatabase(db_path, schema)
        try:
            database.review_financial_fact(fact_key, ValidationStatus.VALIDATED)
        except ValueError:
            return "conflict"
        finally:
            database.close()
        return "validated"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(review, fact_keys))

    verification_db = FinanceDatabase(db_path, schema)
    validated = verification_db.query(
        "SELECT COUNT(*) AS cnt FROM financial_fact WHERE validation_status = 'VALIDATED'",
        use_cache=False,
    )[0]["cnt"]
    assert sorted(outcomes) == ["conflict", "validated"]
    assert validated == 1


def test_sqlite_query_timeout_is_enforced(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)

    with pytest.raises(TimeoutError, match="timed out"):
        db.query(
            "WITH RECURSIVE counter(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 10000000) "
            "SELECT SUM(value) AS total FROM counter",
            timeout_seconds=1e-9,
        )


class _EmptyMySQLCursor:
    def execute(self, sql, params) -> None:
        pass

    def fetchone(self):
        return None

    def close(self) -> None:
        pass


class _RecordingMySQLConnection:
    def __init__(self) -> None:
        self.started = 0
        self.rolled_back = 0

    def start_transaction(self) -> None:
        self.started += 1

    def cursor(self, dictionary: bool = False):
        return _EmptyMySQLCursor()

    def rollback(self) -> None:
        self.rolled_back += 1

    def commit(self) -> None:
        pass


def test_mysql_missing_fact_rolls_back_review_transaction(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "unused.db", schema, connect_immediately=False)
    connection = _RecordingMySQLConnection()
    db.backend = "mysql"
    db._conn = connection

    with pytest.raises(KeyError, match="Unknown financial fact"):
        db.review_financial_fact("missing", ValidationStatus.VALIDATED)

    assert connection.started == 1
    assert connection.rolled_back == 1


def test_parent_scope_never_overwrites_consolidated_projection(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    source_row = {
        "serial_number": 1,
        "stock_code": "600080",
        "stock_abbr": "金花股份",
        "report_period": "2025Q3",
        "report_year": 2025,
    }
    db.upsert("income_sheet", source_row)

    facts = []
    for scope, value in (
        (StatementScope.PARENT, 999.0),
        (StatementScope.CONSOLIDATED, 100.0),
    ):
        facts.append(
            FinancialFact.from_candidate(
                ExtractedFactCandidate(
                    metric="total_profit",
                    raw_value=value,
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
                statement_scope=scope,
                period_type=PeriodType.DURATION,
                target_unit="万元",
                currency="CNY",
                source_file="report.pdf",
                source_content_sha256=TEST_SOURCE_SHA256,
                extractor_version="test-v1",
            )
        )
    db.upsert_financial_facts(facts)
    keys = {
        row["statement_scope"]: row["fact_key"]
        for row in db.query("SELECT fact_key, statement_scope FROM financial_fact")
    }

    db.review_financial_fact(keys[StatementScope.PARENT.value], ValidationStatus.VALIDATED)
    assert db.query("SELECT total_profit FROM income_sheet")[0]["total_profit"] is None

    db.review_financial_fact(keys[StatementScope.CONSOLIDATED.value], ValidationStatus.VALIDATED)
    assert db.query("SELECT total_profit FROM income_sheet")[0]["total_profit"] == 100.0

    db.refresh_fact_projection("income_sheet", source_row)
    assert db.query("SELECT total_profit FROM income_sheet")[0]["total_profit"] == 100.0


def test_batch_authority_switch_removes_old_source_from_query_projection_and_review(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
        },
    )
    selection_version = "report-selection-v1"
    source_a = AuthoritativeSource(
        source_file="report-a.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        stock_code="600080",
        period="2025Q3",
        selection_version=selection_version,
    )
    source_b = AuthoritativeSource(
        source_file="report-b-corrected.pdf",
        source_content_sha256=REPLACEMENT_SOURCE_SHA256,
        stock_code="600080",
        period="2025Q3",
        selection_version=selection_version,
    )
    planner = SQLPlanner()
    executor = SafeQueryExecutor(db, planner.registry)
    compiled = planner.compile(
        {
            "intent": "single_metric",
            "slots": {"metric": "total_profit", "stock_abbr": "金花股份", "report_period": "2025Q3"},
        }
    )

    db.reconcile_batch_authoritative_sources([source_a], selection_version=selection_version)
    fact_a = _profit_fact(value=100.0, source_file=source_a.source_file)
    db.upsert_financial_facts([fact_a])
    fact_a_key = _fact_key_for_source(db, source_a.source_file)
    assert db.query("SELECT selection_version FROM financial_source")[0]["selection_version"] == selection_version
    db.review_financial_fact(fact_a_key, ValidationStatus.VALIDATED)
    assert executor.execute(compiled)[0]["total_profit"] == 100.0
    assert db.query("SELECT total_profit FROM income_sheet")[0]["total_profit"] == 100.0

    db.reconcile_batch_authoritative_sources([source_b], selection_version=selection_version)

    source_rows = {
        row["source_file"]: row
        for row in db.query(
            "SELECT source_file, authority_status, selection_version FROM financial_source",
            use_cache=False,
        )
    }
    assert source_rows[source_a.source_file]["authority_status"] == SourceAuthorityStatus.SUPERSEDED.value
    assert db.query("SELECT validation_status FROM financial_fact")[0]["validation_status"] == "VALIDATED"
    assert executor.execute(compiled) == []
    assert db.query("SELECT total_profit FROM income_sheet")[0]["total_profit"] is None
    with pytest.raises(ValueError, match="not CURRENT"):
        db.review_financial_fact(fact_a_key, ValidationStatus.VALIDATED)

    fact_b = _profit_fact(
        value=200.0,
        source_file=source_b.source_file,
        source_content_sha256=source_b.source_content_sha256,
    )
    db.upsert_financial_facts([fact_b])
    fact_b_key = _fact_key_for_source(db, source_b.source_file)
    assert (
        db.query(
            "SELECT selection_version FROM financial_source WHERE source_file = ?",
            (source_b.source_file,),
            use_cache=False,
        )[0]["selection_version"]
        == selection_version
    )
    db.reconcile_batch_authoritative_sources([source_b], selection_version=selection_version)
    assert (
        db.query(
            "SELECT authority_status FROM financial_source WHERE source_file = ?",
            (source_b.source_file,),
            use_cache=False,
        )[0]["authority_status"]
        == SourceAuthorityStatus.CURRENT.value
    )

    db.reconcile_batch_authoritative_sources([], selection_version=selection_version)

    assert (
        db.query(
            "SELECT authority_status FROM financial_source WHERE source_file = ?",
            (source_b.source_file,),
            use_cache=False,
        )[0]["authority_status"]
        == SourceAuthorityStatus.REMOVED.value
    )
    assert db.query("SELECT total_profit FROM income_sheet")[0]["total_profit"] is None
    with pytest.raises(ValueError, match="not CURRENT"):
        db.review_financial_fact(fact_b_key, ValidationStatus.VALIDATED)


@pytest.mark.parametrize(
    ("stock_code", "period"),
    [("000001", "2025Q3"), ("600080", "2025FY")],
)
def test_batch_reconciliation_rejects_source_identity_drift_without_mutation(
    tmp_path,
    stock_code: str,
    period: str,
) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    source = AuthoritativeSource(
        source_file="immutable-source.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        stock_code="600080",
        period="2025Q3",
        selection_version="report-selection-v1",
    )
    db.reconcile_batch_authoritative_sources([source], selection_version=source.selection_version)
    db.upsert_financial_facts([_profit_fact(value=100.0, source_file=source.source_file)])
    source_before = db.query("SELECT * FROM financial_source", use_cache=False)
    facts_before = db.query("SELECT * FROM financial_fact", use_cache=False)

    conflicting_source = replace(source, stock_code=stock_code, period=period)
    with pytest.raises(ValueError, match="source identity is immutable"):
        db.reconcile_batch_authoritative_sources(
            [conflicting_source],
            selection_version=conflicting_source.selection_version,
        )

    assert db.query("SELECT * FROM financial_source", use_cache=False) == source_before
    assert db.query("SELECT * FROM financial_fact", use_cache=False) == facts_before


def test_trusted_query_and_review_reject_corrupted_source_business_identity(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    projection = {
        "serial_number": 1,
        "stock_code": "600080",
        "stock_abbr": "金花股份",
        "report_period": "2025Q3",
        "report_year": 2025,
    }
    db.upsert("income_sheet", projection)
    source = AuthoritativeSource(
        source_file="corrupted-source.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        stock_code="600080",
        period="2025Q3",
        selection_version="report-selection-v1",
    )
    db.reconcile_batch_authoritative_sources([source], selection_version=source.selection_version)
    db.upsert_financial_facts([_profit_fact(value=100.0, source_file=source.source_file)])
    fact_key = _fact_key_for_source(db, source.source_file)
    db.review_financial_fact(fact_key, ValidationStatus.VALIDATED)
    planner = SQLPlanner()
    executor = SafeQueryExecutor(db, planner.registry)
    compiled = planner.compile(
        {
            "intent": "single_metric",
            "slots": {"metric": "total_profit", "stock_abbr": "金花股份", "report_period": "2025Q3"},
        }
    )
    assert executor.execute(compiled)[0]["total_profit"] == 100.0

    db.execute("UPDATE financial_source SET period = ? WHERE source_key = ?", ("2025FY", source.source_key))

    assert executor.execute(compiled, use_cache=False) == []
    with pytest.raises(ValueError, match="not CURRENT"):
        db.review_financial_fact(fact_key, ValidationStatus.VALIDATED)
    db.refresh_fact_projection("income_sheet", projection)
    assert db.query("SELECT total_profit FROM income_sheet", use_cache=False)[0]["total_profit"] is None


def test_web_activation_supersedes_batch_source_and_batch_cannot_take_over_web_source(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
        },
    )
    batch_source = AuthoritativeSource(
        source_file="batch.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        stock_code="600080",
        period="2025Q3",
        selection_version="report-selection-v1",
    )
    web_source = AuthoritativeSource(
        source_file="web-upload.pdf",
        source_content_sha256=WEB_SOURCE_SHA256,
        stock_code="600080",
        period="2025Q3",
        source_owner=SourceOwner.WEB,
        document_id="00000000-0000-0000-0000-000000000001",
        selection_version="web-promotion-v1",
    )
    db.reconcile_batch_authoritative_sources([batch_source], selection_version=batch_source.selection_version)
    batch_fact = _profit_fact(value=100.0, source_file=batch_source.source_file)
    db.upsert_financial_facts([batch_fact])
    db.review_financial_fact(_fact_key_for_source(db, batch_source.source_file), ValidationStatus.VALIDATED)

    db._begin_transaction()
    cursor = db._conn.cursor()
    try:
        db.activate_web_authoritative_source_in_transaction(cursor, web_source)
        db._conn.commit()
    except Exception:
        db._conn.rollback()
        raise
    finally:
        cursor.close()

    statuses = {
        row["source_file"]: row["authority_status"]
        for row in db.query("SELECT source_file, authority_status FROM financial_source", use_cache=False)
    }
    assert statuses == {
        batch_source.source_file: SourceAuthorityStatus.SUPERSEDED.value,
        web_source.source_file: SourceAuthorityStatus.CURRENT.value,
    }
    assert db.query("SELECT total_profit FROM income_sheet")[0]["total_profit"] is None

    web_fact = _profit_fact(
        value=200.0,
        source_file=web_source.source_file,
        source_content_sha256=web_source.source_content_sha256,
    )
    db.upsert_financial_facts([web_fact])
    db.review_financial_fact(_fact_key_for_source(db, web_source.source_file), ValidationStatus.VALIDATED)
    assert db.query("SELECT total_profit FROM income_sheet")[0]["total_profit"] == 200.0

    conflicting_batch_source = AuthoritativeSource(
        source_file=web_source.source_file,
        source_content_sha256=web_source.source_content_sha256,
        stock_code=web_source.stock_code,
        period=web_source.period,
        selection_version=batch_source.selection_version,
    )
    with pytest.raises(ValueError, match="CURRENT Web source"):
        db.reconcile_batch_authoritative_sources(
            [conflicting_batch_source],
            selection_version=batch_source.selection_version,
        )

    web_row = db.query(
        "SELECT authority_status, current_slot, document_id, selection_version FROM financial_source "
        "WHERE source_file = ?",
        (web_source.source_file,),
        use_cache=False,
    )[0]
    assert web_row == {
        "authority_status": SourceAuthorityStatus.CURRENT.value,
        "current_slot": "CURRENT",
        "document_id": web_source.document_id,
        "selection_version": web_source.selection_version,
    }
    assert db.query("SELECT total_profit FROM income_sheet")[0]["total_profit"] == 200.0


def test_web_activation_rebuilds_previous_period_when_source_metadata_changes(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    for serial_number, period, report_year in ((1, "2025Q3", 2025), (2, "2025FY", 2025)):
        db.upsert(
            "income_sheet",
            {
                "serial_number": serial_number,
                "stock_code": "600080",
                "stock_abbr": "金花股份",
                "report_period": period,
                "report_year": report_year,
            },
        )
    batch_source = AuthoritativeSource(
        source_file="same-upload.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        stock_code="600080",
        period="2025Q3",
        selection_version="report-selection-v1",
    )
    db.reconcile_batch_authoritative_sources([batch_source], selection_version=batch_source.selection_version)
    old_fact = _profit_fact(value=100.0, source_file=batch_source.source_file)
    db.upsert_financial_facts([old_fact])
    db.review_financial_fact(_fact_key_for_source(db, batch_source.source_file), ValidationStatus.VALIDATED)
    assert (
        db.query(
            "SELECT total_profit FROM income_sheet WHERE report_period = ?",
            (batch_source.period,),
        )[0]["total_profit"]
        == 100.0
    )

    corrected_source = AuthoritativeSource(
        source_file=batch_source.source_file,
        source_content_sha256=WEB_SOURCE_SHA256,
        stock_code=batch_source.stock_code,
        period="2025FY",
        source_owner=SourceOwner.WEB,
        document_id="00000000-0000-0000-0000-000000000002",
        selection_version="web-promotion-v1",
    )
    db._begin_transaction()
    cursor = db._conn.cursor()
    try:
        db.activate_web_authoritative_source_in_transaction(cursor, corrected_source)
        db._conn.commit()
    except Exception:
        db._conn.rollback()
        raise
    finally:
        cursor.close()

    assert (
        db.query(
            "SELECT total_profit FROM income_sheet WHERE report_period = ?",
            (batch_source.period,),
            use_cache=False,
        )[0]["total_profit"]
        is None
    )
    assert db.query(
        "SELECT stock_code, period, authority_status, document_id FROM financial_source ORDER BY period DESC",
        use_cache=False,
    ) == [
        {
            "stock_code": batch_source.stock_code,
            "period": batch_source.period,
            "authority_status": SourceAuthorityStatus.SUPERSEDED.value,
            "document_id": None,
        },
        {
            "stock_code": corrected_source.stock_code,
            "period": corrected_source.period,
            "authority_status": SourceAuthorityStatus.CURRENT.value,
            "document_id": corrected_source.document_id,
        },
    ]


def test_same_path_content_revision_hides_old_validated_fact_until_re_review(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    db.upsert(
        "income_sheet",
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2025Q3",
            "report_year": 2025,
        },
    )
    selection_version = "report-selection-v1"
    source_a = AuthoritativeSource(
        source_file="overwritten.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        stock_code="600080",
        period="2025Q3",
        selection_version=selection_version,
    )
    source_b = replace(source_a, source_content_sha256=REPLACEMENT_SOURCE_SHA256)
    planner = SQLPlanner()
    executor = SafeQueryExecutor(db, planner.registry)
    compiled = planner.compile(
        {
            "intent": "single_metric",
            "slots": {"metric": "total_profit", "stock_abbr": "金花股份", "report_period": "2025Q3"},
        }
    )

    db.reconcile_batch_authoritative_sources([source_a], selection_version=selection_version)
    fact_a = _profit_fact(
        value=100.0,
        source_file=source_a.source_file,
        source_content_sha256=source_a.source_content_sha256,
    )
    db.upsert_financial_facts([fact_a])
    fact_a_key = db.query(
        "SELECT fact_key FROM financial_fact WHERE source_key = ?",
        (source_a.source_key,),
        use_cache=False,
    )[0]["fact_key"]
    db.review_financial_fact(str(fact_a_key), ValidationStatus.VALIDATED)
    assert executor.execute(compiled)[0]["total_profit"] == 100.0

    db.reconcile_batch_authoritative_sources([source_b], selection_version=selection_version)
    fact_b = _profit_fact(
        value=200.0,
        source_file=source_b.source_file,
        source_content_sha256=source_b.source_content_sha256,
    )
    db.upsert_financial_facts([fact_b])

    sources = db.query(
        "SELECT content_sha256, authority_status FROM financial_source ORDER BY content_sha256",
        use_cache=False,
    )
    assert sources == [
        {
            "content_sha256": TEST_SOURCE_SHA256,
            "authority_status": SourceAuthorityStatus.SUPERSEDED.value,
        },
        {
            "content_sha256": REPLACEMENT_SOURCE_SHA256,
            "authority_status": SourceAuthorityStatus.CURRENT.value,
        },
    ]
    assert executor.execute(compiled) == []
    assert db.query("SELECT total_profit FROM income_sheet", use_cache=False)[0]["total_profit"] is None
    with pytest.raises(ValueError, match="not CURRENT"):
        db.review_financial_fact(str(fact_a_key), ValidationStatus.VALIDATED)

    fact_b_key = db.query(
        "SELECT fact_key FROM financial_fact WHERE source_key = ?",
        (source_b.source_key,),
        use_cache=False,
    )[0]["fact_key"]
    db.review_financial_fact(str(fact_b_key), ValidationStatus.VALIDATED)
    assert executor.execute(compiled)[0]["total_profit"] == 200.0


def test_unreviewed_high_value_cannot_affect_topn_or_filter_projection(tmp_path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "finance.db", schema)
    db.create_tables()
    candidates = []
    for stock_code, stock_abbr, value in (
        ("000001", "待复核公司", 999.0),
        ("000002", "已复核公司", 100.0),
    ):
        row = {
            "serial_number": int(stock_code),
            "stock_code": stock_code,
            "stock_abbr": stock_abbr,
            "report_period": "2025Q3",
            "report_year": 2025,
        }
        db.upsert("income_sheet", row)
        candidates.append(
            FinancialFact.from_candidate(
                ExtractedFactCandidate(
                    metric="total_profit",
                    raw_value=value,
                    source_unit="万元",
                    page_no=8,
                    table_name="income_sheet",
                    row_label="利润总额",
                    column_label="本期金额",
                    confidence=1.0 if stock_code == "000002" else 0.5,
                ),
                company_id=stock_code,
                stock_code=stock_code,
                period="2025Q3",
                statement_scope=StatementScope.CONSOLIDATED,
                period_type=PeriodType.DURATION,
                target_unit="万元",
                currency="CNY",
                source_file=f"{stock_code}.pdf",
                source_content_sha256=TEST_SOURCE_SHA256,
                extractor_version="test-v1",
            )
        )
    db.upsert_financial_facts(candidates)
    validated_key = db.query(
        "SELECT fact_key FROM financial_fact WHERE stock_code = ?",
        ("000002",),
    )[0]["fact_key"]
    db.review_financial_fact(validated_key, ValidationStatus.VALIDATED)
    for row in db.query("SELECT serial_number, stock_code, stock_abbr, report_period, report_year FROM income_sheet"):
        db.refresh_fact_projection("income_sheet", row)

    planner = SQLPlanner()
    executor = SafeQueryExecutor(db, planner.registry)
    topn = planner.compile(
        {
            "query_spec": {
                "analysis_type": "topn_metric",
                "metric": "total_profit",
                "report_period": "2025Q3",
                "select_fields": ["stock_code", "stock_abbr", "report_period", "total_profit"],
                "top_n": 1,
            }
        }
    )
    filtered = planner.compile(
        {
            "query_spec": {
                "analysis_type": "filter",
                "metric": "total_profit",
                "report_period": "2025Q3",
                "select_fields": ["stock_code", "stock_abbr", "report_period", "total_profit"],
                "filters": [{"field": "total_profit", "op": ">", "value": 50}],
            }
        }
    )

    assert [row["stock_code"] for row in executor.execute(topn)] == ["000002"]
    assert [row["stock_code"] for row in executor.execute(filtered)] == ["000002"]
