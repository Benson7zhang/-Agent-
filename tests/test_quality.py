from smart_finqa.quality import (
    format_single_metric_answer,
    format_topn_analysis_answer,
    format_trend_analysis_answer,
    format_reason_with_references,
)


def test_format_single_metric_answer() -> None:
    slots = {"stock_abbr": "金花股份", "report_period": "2025Q3", "metric": "total_profit"}
    row = {"total_profit": 3140.1852}
    content = format_single_metric_answer(slots, row)
    assert "金花股份" in content
    assert "2025Q3" in content
    assert "3140.19" in content
    assert "利润总额" in content


def test_format_trend_analysis_answer() -> None:
    rows = [
        {"report_period": "2023FY", "total_profit": -4105.46},
        {"report_period": "2024FY", "total_profit": 4203.93},
        {"report_period": "2025Q3", "total_profit": 3140.19},
    ]
    content = format_trend_analysis_answer(rows, metric_col="total_profit", metric_label="利润总额")
    assert "趋势" in content
    assert "2023FY" in content
    assert "2025Q3" in content
    assert "最大值" in content
    assert "最小值" in content


def test_format_topn_analysis_answer() -> None:
    rows = [
        {"stock_abbr": "华润三九", "total_profit": 459361.07, "operating_revenue_yoy_growth": 11.63},
        {"stock_abbr": "华润三九", "total_profit": 459361.07, "operating_revenue_yoy_growth": 11.63},
        {"stock_abbr": "金花股份", "total_profit": 4203.93, "operating_revenue_yoy_growth": 3.55},
    ]
    content = format_topn_analysis_answer(rows, metric_col="total_profit", metric_label="利润总额")
    assert "华润三九" in content
    assert "同比" in content
    assert "涨幅最大" in content
    # duplicated stock should not be listed twice
    assert content.count("华润三九：") == 1


def test_format_reason_with_references() -> None:
    refs = [
        {"paper_path": "./x.pdf", "text": "公司收入增长主要来自渠道扩张和品牌势能。", "paper_image": ""},
        {"paper_path": "./y.pdf", "text": "医保目录调整带动产品放量。", "paper_image": ""},
    ]
    content = format_reason_with_references("主营业务收入上升的原因是什么", sql_rows_count=12, references=refs)
    assert "12 条" in content
    assert "2 条" in content
    assert "渠道扩张" in content or "医保目录" in content
