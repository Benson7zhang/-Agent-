from smart_finqa.database import FinanceDatabase
from smart_finqa.schema import load_schema_from_xlsx
from tests.helpers import write_schema_workbook


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
