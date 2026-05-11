from __future__ import annotations

import re
from typing import Mapping, Sequence


_ILLEGAL_SQL_PATTERN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|grant|revoke|attach|detach|pragma)\b",
    flags=re.IGNORECASE,
)


def parse_report_period(file_name: str, text_head: str) -> tuple[str, int]:
    """Parse report period in canonical format YYYYQ1/Q2/Q3/FY."""
    name_text = f"{file_name}\n{text_head}"

    # Prefer explicit Chinese period labels.
    match = re.search(
        r"(?P<year>\d{4})\s*年\s*(?P<period>一季度|半年度|三季度|年度|年报|第三季度|第一季度)",
        name_text,
    )
    if match:
        year = int(match.group("year"))
        period_word = match.group("period")
        if "第一季度" in period_word or "一季度" == period_word:
            return f"{year}Q1", year
        if "半年度" in period_word:
            return f"{year}Q2", year
        if "第三季度" in period_word or "三季度" == period_word:
            return f"{year}Q3", year
        return f"{year}FY", year

    # SH file names include publish date like 600080_20251030_XXXX.pdf.
    date_match = re.search(r"_(\d{8})_", file_name)
    if date_match:
        date_str = date_match.group(1)
        year = int(date_str[:4])
        month = int(date_str[4:6])
        if month <= 4:
            # Annual reports are usually disclosed in Q1/Q2 of next year.
            return f"{year - 1}FY", year - 1
        if month <= 8:
            return f"{year}Q2", year
        if month <= 10:
            return f"{year}Q3", year
        return f"{year}FY", year

    fallback_year = 2025
    return f"{fallback_year}FY", fallback_year


def report_period_sort_key(period: str) -> tuple[int, int]:
    match = re.match(r"(\d{4})(Q1|Q2|Q3|FY)", str(period))
    if not match:
        return (0, 0)
    year = int(match.group(1))
    order = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}
    return (year, order.get(match.group(2), 0))


def report_period_order_value(period: str) -> int:
    year, order = report_period_sort_key(period)
    if year <= 0:
        return 0
    return year * 10 + order


def report_period_order_sql(column: str = "report_period") -> str:
    return (
        f"(report_year * 10 + CASE "
        f"WHEN {column} LIKE '%Q1' THEN 1 "
        f"WHEN {column} LIKE '%Q2' THEN 2 "
        f"WHEN {column} LIKE '%Q3' THEN 3 "
        f"WHEN {column} LIKE '%FY' THEN 4 "
        f"ELSE 0 END)"
    )


def normalize_numeric(value: str | float | int | None, source_unit: str = "元", target_unit: str = "万元") -> float | None:
    """Normalize numeric strings and convert between yuan and ten-thousand yuan."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        number = float(value)
    else:
        raw = str(value).strip()
        if not raw or raw in {"--", "-", "N/A", "nan", "None"}:
            return None
        negative = raw.startswith("(") and raw.endswith(")")
        cleaned = raw.strip("()").replace(",", "").replace("%", "")
        if cleaned in {"", ".", "-"}:
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
        if negative:
            number = -number

    # Validate reasonable range to prevent overflow
    if abs(number) > 1e15:
        return None

    if source_unit == target_unit:
        return round(number, 4)
    if source_unit == "元" and target_unit == "万元":
        return round(number / 10000.0, 4)
    if source_unit == "万元" and target_unit == "元":
        return round(number * 10000.0, 4)
    return round(number, 4)


def is_safe_select_sql(
    sql: str,
    *,
    allowed_tables: set[str],
    allowed_columns: Mapping[str, set[str]] | None = None,
) -> bool:
    """Validate SQL is a restricted SELECT over whitelist tables/columns."""
    if not sql or not isinstance(sql, str):
        return False
    stripped = sql.strip()
    if not re.match(r"^select\b", stripped, flags=re.IGNORECASE):
        return False
    if _ILLEGAL_SQL_PATTERN.search(stripped):
        return False
    if ";" in stripped[:-1]:
        return False
    if "--" in stripped or "/*" in stripped or "*/" in stripped:
        return False

    tables = re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", stripped, flags=re.IGNORECASE)
    if not tables:
        return False
    if any(table not in allowed_tables for table in tables):
        return False

    if allowed_columns:
        select_match = re.search(r"^select\s+(.*?)\s+from\b", stripped, flags=re.IGNORECASE | re.DOTALL)
        if not select_match:
            return False
        raw_columns = [segment.strip() for segment in select_match.group(1).split(",")]
        if "*" in raw_columns:
            return False
        known_columns = set().union(*(allowed_columns.get(table, set()) for table in tables))
        for col in raw_columns:
            # Allow aliases and functions around known columns.
            tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", col)
            tokens = [token for token in tokens if token.lower() not in {"as", "sum", "avg", "min", "max", "count"}]
            if not tokens:
                continue
            if all(token not in known_columns for token in tokens):
                return False

    return True
