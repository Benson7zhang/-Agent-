from smart_finqa.query_engine import QueryEngine


def test_build_total_profit_sql() -> None:
    engine = QueryEngine()
    sql, params = engine.build_total_profit_sql(stock_abbr="金花股份", report_period="2025Q3")
    assert "FROM income_sheet" in sql
    assert "total_profit" in sql
    assert params == ("金花股份", "2025Q3")


def test_build_top_profit_sql() -> None:
    engine = QueryEngine()
    sql, params = engine.build_top_profit_2024_sql(limit_n=10)
    assert "ORDER BY total_profit DESC" in sql
    assert "LIMIT 10" in sql
    assert params == ()
