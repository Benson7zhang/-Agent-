import pytest

from smart_finqa.core import MetadataRecognitionError, normalize_numeric, parse_report_period


def test_parse_report_period_from_sz_name() -> None:
    period, year = parse_report_period("华润三九：2024年三季度报告.pdf", "")
    assert period == "2024Q3"
    assert year == 2024


def test_parse_report_period_from_sh_text() -> None:
    period, year = parse_report_period("600080_20251030_IVCB.pdf", "2025 年第三季度报告")
    assert period == "2025Q3"
    assert year == 2025


def test_parse_report_period_rejects_unknown_period() -> None:
    with pytest.raises(MetadataRecognitionError, match="report_period") as exc_info:
        parse_report_period("annual-report.pdf", "董事会报告")

    assert exc_info.value.field == "report_period"


def test_parse_report_period_does_not_guess_from_publish_date() -> None:
    with pytest.raises(MetadataRecognitionError, match="report_period"):
        parse_report_period("600080_20251030_IVCB.pdf", "董事会公告")


def test_normalize_numeric_parentheses_and_unit() -> None:
    assert normalize_numeric("(1,234.50)", source_unit="元", target_unit="元") == -1234.5
    assert normalize_numeric("31,401,852.36", source_unit="元", target_unit="万元") == 3140.1852
