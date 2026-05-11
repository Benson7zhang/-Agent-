from __future__ import annotations


class QueryEngine:
    def build_total_profit_sql(self, *, stock_abbr: str, report_period: str) -> tuple[str, tuple]:
        return (
            "SELECT total_profit, report_period "
            "FROM income_sheet "
            "WHERE stock_abbr = ? AND report_period = ? "
            "ORDER BY report_period",
            (stock_abbr, report_period)
        )

    def build_profit_trend_sql(self, *, stock_abbr: str, start_period: str | None = None) -> tuple[str, tuple]:
        base = (
            "SELECT report_period, total_profit "
            "FROM income_sheet "
            "WHERE stock_abbr = ? "
        )
        params = [stock_abbr]
        if start_period:
            base += "AND report_period >= ? "
            params.append(start_period)
        base += "ORDER BY report_year, report_period"
        return base, tuple(params)

    def build_top_profit_2024_sql(self, *, limit_n: int = 10) -> tuple[str, tuple]:
        return (
            "SELECT stock_code, stock_abbr, total_profit, total_operating_revenue, operating_revenue_yoy_growth "
            "FROM income_sheet "
            "WHERE report_period = '2024FY' "
            "ORDER BY total_profit DESC "
            f"LIMIT {int(limit_n)}",
            ()
        )

    def build_revenue_3y_sql(self, *, stock_abbr: str) -> tuple[str, tuple]:
        return (
            "SELECT report_period, report_year, total_operating_revenue "
            "FROM income_sheet "
            "WHERE stock_abbr = ? "
            "ORDER BY report_year DESC, report_period DESC "
            "LIMIT 3",
            (stock_abbr,)
        )

