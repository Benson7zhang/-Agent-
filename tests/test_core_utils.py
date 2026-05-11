from smart_finqa.core import is_safe_select_sql, normalize_numeric, parse_report_period


def test_parse_report_period_from_sz_name() -> None:
    period, year = parse_report_period("华润三九：2024年三季度报告.pdf", "")
    assert period == "2024Q3"
    assert year == 2024


def test_parse_report_period_from_sh_text() -> None:
    period, year = parse_report_period("600080_20251030_IVCB.pdf", "2025 年第三季度报告")
    assert period == "2025Q3"
    assert year == 2025


def test_normalize_numeric_parentheses_and_unit() -> None:
    assert normalize_numeric("(1,234.50)", source_unit="元", target_unit="元") == -1234.5
    assert normalize_numeric("31,401,852.36", source_unit="元", target_unit="万元") == 3140.1852


def test_sql_safety() -> None:
    allowed_tables = {"income_sheet", "balance_sheet"}
    allowed_columns = {"income_sheet": {"stock_abbr", "total_profit", "report_period"}}
    assert is_safe_select_sql(
        "SELECT total_profit FROM income_sheet WHERE stock_abbr='金花股份' AND report_period='2025Q3'",
        allowed_tables=allowed_tables,
        allowed_columns=allowed_columns,
    )
    assert not is_safe_select_sql(
        "INSERT INTO income_sheet(total_profit) VALUES (1)",
        allowed_tables=allowed_tables,
        allowed_columns=allowed_columns,
    )
    assert not is_safe_select_sql(
        "SELECT * FROM other_table",
        allowed_tables=allowed_tables,
        allowed_columns=allowed_columns,
    )
