from __future__ import annotations

from pathlib import Path

from smart_finqa.config import DatabaseConfig
from smart_finqa.database import FinanceDatabase
from smart_finqa.schema import load_schema_from_xlsx
from tests.helpers import write_schema_workbook


def test_sqlite_backend_still_works(tmp_path: Path) -> None:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    schema = load_schema_from_xlsx(schema_path)
    cfg = DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db"))
    db = FinanceDatabase(db_path=tmp_path / "finance.db", schema=schema, db_config=cfg)
    db.create_tables()
    assert db.backend == "sqlite"


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
