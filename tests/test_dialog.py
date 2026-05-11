from smart_finqa.dialog import next_turn_action


def test_clarify_then_query() -> None:
    context = {}
    first = next_turn_action("金花股份利润总额是多少", context)
    assert first["state"] == "CLARIFY"
    assert "报告期" in first["content"]
    assert first["context"]["metric"] == "total_profit"
    assert first["context"]["stock_abbr"] == "金花股份"

    second = next_turn_action("2025年第三季度的", first["context"])
    assert second["state"] == "QUERY"
    assert second["context"]["report_period"] == "2025Q3"
