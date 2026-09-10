import pytest
from openpyxl import load_workbook

from smart_finqa.schema import load_schema_from_xlsx, map_to_sqlite_type
from tests.helpers import write_schema_workbook


def test_load_schema_tables(tmp_path) -> None:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    schema = load_schema_from_xlsx(schema_path)
    assert "income_sheet" in schema
    assert "balance_sheet" in schema
    assert len(schema["income_sheet"]) >= 10


def test_load_schema_rejects_unsafe_field_identifier(tmp_path) -> None:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    workbook = load_workbook(schema_path)
    workbook["利润表"]["A2"] = "stock_code); DROP TABLE income_sheet; --"
    workbook.save(schema_path)

    with pytest.raises(ValueError, match="Invalid schema field name"):
        load_schema_from_xlsx(schema_path)


def test_map_to_sqlite_type() -> None:
    assert map_to_sqlite_type("decimal(20,2)") == "REAL"
    assert map_to_sqlite_type("varchar(50)") == "TEXT"
    assert map_to_sqlite_type("INT") == "INTEGER"
