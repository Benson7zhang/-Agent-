from smart_finqa.schema import load_schema_from_xlsx, map_to_sqlite_type
from tests.helpers import write_schema_workbook


def test_load_schema_tables(tmp_path) -> None:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    schema = load_schema_from_xlsx(schema_path)
    assert "income_sheet" in schema
    assert "balance_sheet" in schema
    assert len(schema["income_sheet"]) >= 10


def test_map_to_sqlite_type() -> None:
    assert map_to_sqlite_type("decimal(20,2)") == "REAL"
    assert map_to_sqlite_type("varchar(50)") == "TEXT"
    assert map_to_sqlite_type("INT") == "INTEGER"
