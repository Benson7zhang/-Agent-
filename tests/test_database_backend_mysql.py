from __future__ import annotations

from pathlib import Path

import pytest

from smart_finqa.config import DatabaseConfig
from smart_finqa.database import FinanceDatabase
from smart_finqa.facts import AuthoritativeSource, SourceAuthorityStatus
from smart_finqa.schema import load_schema_from_xlsx
from tests.helpers import write_schema_workbook

TEST_SOURCE_SHA256 = "a" * 64


class _RecordingCursor:
    def __init__(self, commands, *, rows=None) -> None:
        self.commands = commands
        self.rows = rows or []

    def execute(self, sql, params=()) -> None:
        self.commands.append((sql, params))

    def fetchall(self):
        return self.rows

    def close(self) -> None:
        pass


class _RecordingConnection:
    def __init__(self) -> None:
        self.commands = []

    def cursor(self, dictionary=False):
        rows = [{"stock_code": "600080"}] if dictionary else []
        return _RecordingCursor(self.commands, rows=rows)


class _SchemaLockCursor:
    def __init__(self, connection: "_SchemaLockConnection") -> None:
        self.connection = connection
        self.operation: str | None = None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.connection.commands.append((sql, params))
        if "GET_LOCK" in sql:
            self.operation = "GET_LOCK"
        elif "RELEASE_LOCK" in sql:
            self.operation = "RELEASE_LOCK"

    def fetchone(self) -> dict[str, int] | None:
        if self.operation == "GET_LOCK":
            return {"acquired": self.connection.lock_result}
        if self.operation == "RELEASE_LOCK":
            return {"released": self.connection.release_result}
        return None

    def close(self) -> None:
        self.connection.closed_cursors += 1


class _SchemaLockConnection:
    def __init__(self, *, lock_result: int = 1, release_result: int = 1) -> None:
        self.lock_result = lock_result
        self.release_result = release_result
        self.commands: list[tuple[str, tuple[object, ...]]] = []
        self.closed_cursors = 0
        self.commits = 0

    def cursor(self, *, dictionary: bool = False) -> _SchemaLockCursor:
        del dictionary
        return _SchemaLockCursor(self)

    def commit(self) -> None:
        self.commits += 1


class _SourceUpsertCursor:
    def __init__(self, *, existing_row: dict[str, object] | None = None) -> None:
        self.existing_row = existing_row
        self.commands: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.commands.append((sql, params))

    def fetchone(self):
        if self.existing_row is not None:
            row = self.existing_row
            self.existing_row = None
            return row
        return None


def test_sqlite_backend_still_works(tmp_path: Path) -> None:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    schema = load_schema_from_xlsx(schema_path)
    cfg = DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db"))
    db = FinanceDatabase(db_path=tmp_path / "finance.db", schema=schema, db_config=cfg)
    db.create_tables()
    assert db.backend == "sqlite"


def test_mysql_create_tables_holds_one_named_lock_for_the_complete_schema_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = FinanceDatabase(tmp_path / "unused.db", {}, connect_immediately=False)
    connection = _SchemaLockConnection()
    db.backend = "mysql"
    db._conn = connection
    monkeypatch.setattr(db, "_migrate_source_identity_schema", lambda: connection.commands.append(("MIGRATE", ())))

    db.create_tables()

    sql = [statement for statement, _ in connection.commands]
    get_lock_indexes = [index for index, statement in enumerate(sql) if "GET_LOCK" in statement]
    release_lock_indexes = [index for index, statement in enumerate(sql) if "RELEASE_LOCK" in statement]
    source_table_index = next(index for index, statement in enumerate(sql) if "financial_source" in statement)
    fact_table_index = next(index for index, statement in enumerate(sql) if "financial_fact" in statement)
    assert len(get_lock_indexes) == 1
    assert len(release_lock_indexes) == 1
    assert get_lock_indexes[0] < source_table_index < fact_table_index < sql.index("MIGRATE") < release_lock_indexes[0]


def test_mysql_create_tables_releases_named_lock_when_schema_change_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = FinanceDatabase(tmp_path / "unused.db", {}, connect_immediately=False)
    connection = _SchemaLockConnection()
    db.backend = "mysql"
    db._conn = connection

    def fail_migration() -> None:
        connection.commands.append(("MIGRATE", ()))
        raise RuntimeError("base schema migration failed")

    monkeypatch.setattr(db, "_migrate_source_identity_schema", fail_migration)

    with pytest.raises(RuntimeError, match="base schema migration failed"):
        db.create_tables()

    sql = [statement for statement, _ in connection.commands]
    assert next(index for index, statement in enumerate(sql) if "GET_LOCK" in statement) < sql.index("MIGRATE")
    assert sql.index("MIGRATE") < next(index for index, statement in enumerate(sql) if "RELEASE_LOCK" in statement)
    assert connection.closed_cursors == 3


def test_mysql_create_tables_rejects_unavailable_lock_before_any_schema_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = FinanceDatabase(tmp_path / "unused.db", {}, connect_immediately=False)
    connection = _SchemaLockConnection(lock_result=0)
    db.backend = "mysql"
    db._conn = connection
    monkeypatch.setattr(db, "_migrate_source_identity_schema", lambda: connection.commands.append(("MIGRATE", ())))

    with pytest.raises(RuntimeError, match="schema migration lock"):
        db.create_tables()

    sql = [statement for statement, _ in connection.commands]
    assert sum("GET_LOCK" in statement for statement in sql) == 1
    assert not any("CREATE TABLE" in statement for statement in sql)
    assert "MIGRATE" not in sql
    assert not any("RELEASE_LOCK" in statement for statement in sql)
    assert connection.closed_cursors == 1


def test_mysql_placeholder_generation(tmp_path: Path) -> None:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    schema = load_schema_from_xlsx(schema_path)
    cfg = DatabaseConfig(backend="mysql", host="localhost", port=3306, user="u", password="p", database="d")
    db = FinanceDatabase(db_path=tmp_path / "unused.db", schema=schema, db_config=cfg, connect_immediately=False)
    sql = db.build_upsert_sql(
        table="income_sheet",
        columns=["stock_code", "stock_abbr", "report_period", "total_profit"],
    )
    assert "%s" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert db.parameter_markers(2) == "%s, %s"


def test_sqlite_placeholder_generation(tmp_path: Path) -> None:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    schema = load_schema_from_xlsx(schema_path)
    cfg = DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db"))
    db = FinanceDatabase(db_path=tmp_path / "finance.db", schema=schema, db_config=cfg, connect_immediately=False)

    assert db.parameter_markers(2) == "?, ?"


def test_mysql_ddl_uses_controlled_types_instead_of_raw_schema_text(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    schema["income_sheet"][0].raw_type = "varchar(255)); DROP TABLE income_sheet; --"
    config = DatabaseConfig(backend="mysql", host="localhost", user="u", password="p", database="d")
    db = FinanceDatabase(tmp_path / "unused.db", schema, db_config=config, connect_immediately=False)

    ddl = db._build_create_table_sql("income_sheet", schema["income_sheet"])

    assert "DROP TABLE" not in ddl
    assert "VARCHAR(255)" in ddl
    assert ddl.rstrip().endswith("ENGINE=InnoDB")


def test_mysql_query_timeout_is_set_and_restored(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "unused.db", schema, connect_immediately=False)
    connection = _RecordingConnection()
    db.backend = "mysql"
    db._conn = connection

    rows = db.query("SELECT stock_code FROM income_sheet", timeout_seconds=0.25)

    assert rows == [{"stock_code": "600080"}]
    assert connection.commands == [
        ("SET SESSION MAX_EXECUTION_TIME = %s", (250,)),
        ("SELECT stock_code FROM income_sheet", ()),
        ("SET SESSION MAX_EXECUTION_TIME = 0", ()),
    ]


def test_mysql_source_upsert_does_not_update_on_business_unique_key_conflict(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "unused.db", schema, connect_immediately=False)
    db.backend = "mysql"
    source = AuthoritativeSource(
        source_file="report.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        stock_code="600080",
        period="2025Q3",
        selection_version="report-selection-v1",
    )
    insert_cursor = _SourceUpsertCursor()

    db._upsert_source_in_transaction(insert_cursor, source, SourceAuthorityStatus.CURRENT)

    assert "FOR UPDATE" in insert_cursor.commands[0][0]
    assert insert_cursor.commands[1][0].startswith("INSERT INTO financial_source")
    assert "ON DUPLICATE KEY" not in insert_cursor.commands[1][0]


def test_mysql_source_upsert_updates_only_the_matching_source_key(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "unused.db", schema, connect_immediately=False)
    db.backend = "mysql"
    source = AuthoritativeSource(
        source_file="report.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        stock_code="600080",
        period="2025Q3",
        selection_version="report-selection-v1",
    )
    update_cursor = _SourceUpsertCursor(
        existing_row={
            "source_key": source.source_key,
            "source_file": source.source_file,
            "content_sha256": source.source_content_sha256,
            "stock_code": source.stock_code,
            "period": source.period,
        }
    )

    db._upsert_source_in_transaction(update_cursor, source, SourceAuthorityStatus.CURRENT)

    assert len(update_cursor.commands) == 2
    assert update_cursor.commands[1][0].startswith("UPDATE financial_source SET")
    assert update_cursor.commands[1][0].endswith("WHERE source_key = %s")
    assert "source_file =" not in update_cursor.commands[1][0]
    assert "content_sha256 =" not in update_cursor.commands[1][0]
    assert "stock_code =" not in update_cursor.commands[1][0]
    assert "period =" not in update_cursor.commands[1][0]
    assert update_cursor.commands[1][1][-1] == source.source_key


def test_mysql_source_upsert_rejects_business_identity_drift_before_update(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    db = FinanceDatabase(tmp_path / "unused.db", schema, connect_immediately=False)
    db.backend = "mysql"
    source = AuthoritativeSource(
        source_file="report.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        stock_code="600080",
        period="2025FY",
        selection_version="report-selection-v1",
    )
    cursor = _SourceUpsertCursor(
        existing_row={
            "source_key": source.source_key,
            "source_file": source.source_file,
            "content_sha256": source.source_content_sha256,
            "stock_code": source.stock_code,
            "period": "2025Q3",
        }
    )

    with pytest.raises(ValueError, match="source identity is immutable"):
        db._upsert_source_in_transaction(cursor, source, SourceAuthorityStatus.CURRENT)

    assert len(cursor.commands) == 1
    select_sql, params = cursor.commands[0]
    assert "source_file, content_sha256, stock_code, period" in select_sql
    assert "FOR UPDATE" in select_sql
    assert params == (source.source_key,)
