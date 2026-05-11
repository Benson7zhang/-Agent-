from pathlib import Path

from smart_finqa.config import DatabaseConfig
from smart_finqa.database import FinanceDatabase
from smart_finqa.schema import load_schema_from_xlsx
from tests.helpers import write_schema_workbook


def test_upsert_many(tmp_path: Path) -> None:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    schema = load_schema_from_xlsx(schema_path)
    cfg = DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db"))
    db = FinanceDatabase(db_path=tmp_path / "finance.db", schema=schema, db_config=cfg)
    db.create_tables()
    rows = [
        {
            "serial_number": 1,
            "stock_code": "600080",
            "stock_abbr": "金花股份",
            "report_period": "2024FY",
            "report_year": 2024,
            "total_profit": 100.0,
        },
        {
            "serial_number": 2,
            "stock_code": "000999",
            "stock_abbr": "华润三九",
            "report_period": "2024FY",
            "report_year": 2024,
            "total_profit": 200.0,
        },
    ]
    db.upsert_many("income_sheet", rows)
    got = db.query("SELECT COUNT(*) AS cnt FROM income_sheet")
    assert got[0]["cnt"] == 2
