from smart_finqa.extraction import extract_metric_snapshot_from_text, period_sort_key


def test_extract_metric_snapshot_from_quarter_line() -> None:
    text = (
        "营业收入 142,615,399.97 -17.55 383,908,363.63 -8.36 "
        "利润总额 31,401,852.36 35.31 35,335,900.02 8.87 "
        "归属于上市公司股东的净利润 28,286,232.43 32.75 34,481,243.86 12.06"
    )
    snapshot = extract_metric_snapshot_from_text(text)
    assert snapshot["total_profit_yuan"] == 31401852.36
    assert snapshot["total_profit_yoy"] == 35.31
    assert snapshot["total_operating_revenue_yuan"] == 142615399.97
    assert snapshot["operating_revenue_yoy"] == -17.55
    assert snapshot["net_profit_yuan"] == 28286232.43
    assert snapshot["net_profit_yoy"] == 32.75


def test_period_sort_key() -> None:
    periods = ["2024Q3", "2023FY", "2024FY", "2024Q1"]
    assert sorted(periods, key=period_sort_key) == ["2023FY", "2024Q1", "2024Q3", "2024FY"]
