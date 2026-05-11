from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import DatabaseConfig
from .logger import get_logger
from .schema import FieldSpec, map_to_sqlite_type


def _map_to_mysql_type(raw_type: str | None) -> str:
    text = (raw_type or "").strip()
    if not text:
        return "VARCHAR(255)"
    return text.upper()


class FinanceDatabase:
    def __init__(
        self,
        db_path: Path,
        schema: dict[str, list[FieldSpec]],
        db_config: DatabaseConfig | None = None,
        *,
        connect_immediately: bool = True,
        enable_cache: bool = True,
        cache_size: int = 100,
    ) -> None:
        self.db_path = db_path
        self.schema = schema
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_config = db_config or DatabaseConfig(backend="sqlite", sqlite_path=str(db_path))
        self.backend = self.db_config.backend
        self._conn: Any = None
        self.logger = get_logger()

        # Query cache
        self._cache_enabled = enable_cache
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._cache_size = cache_size
        self._cache_hits = 0
        self._cache_misses = 0

        if connect_immediately:
            self.connect()

    def __enter__(self) -> "FinanceDatabase":
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()

    def __del__(self) -> None:
        """Ensure connection is closed on deletion."""
        try:
            self.close()
        except Exception:
            pass

    def connect(self) -> None:
        if self._conn is not None:
            return
        try:
            if self.backend == "mysql":
                self._conn = self._connect_mysql()
                self.logger.info(f"Connected to MySQL | host={self.db_config.host} | db={self.db_config.database}")
            else:
                sqlite_target = self.db_config.sqlite_path or str(self.db_path)
                self._conn = sqlite3.connect(sqlite_target)
                self._conn.row_factory = sqlite3.Row
                self.logger.info(f"Connected to SQLite | path={sqlite_target}")
        except Exception as exc:
            self.logger.error(f"Database connection failed | backend={self.backend} | error={exc}")
            raise

    def _connect_mysql(self) -> Any:
        try:
            import mysql.connector  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on runtime env
            raise RuntimeError("mysql-connector-python is required for MySQL backend") from exc
        conn = mysql.connector.connect(
            host=self.db_config.host,
            port=self.db_config.port,
            user=self.db_config.user,
            password=self.db_config.password,
            database=self.db_config.database,
            autocommit=False,
        )
        return conn

    def create_tables(self) -> None:
        self.connect()
        for table, fields in self.schema.items():
            ddl = self._build_create_table_sql(table, fields)
            self.execute(ddl)

    def _build_create_table_sql(self, table: str, fields: list[FieldSpec]) -> str:
        field_sql: list[str] = []
        for field in fields:
            if self.backend == "mysql":
                field_sql.append(f"`{field.field_name}` {_map_to_mysql_type(field.raw_type)}")
            else:
                field_sql.append(f"{field.field_name} {map_to_sqlite_type(field.raw_type)}")

        if self.backend == "mysql":
            return (
                f"CREATE TABLE IF NOT EXISTS `{table}` (\n"
                + ",\n".join(field_sql)
                + ",\nUNIQUE KEY uniq_stock_period (stock_code, report_period)\n)"
            )
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            + ",\n".join(field_sql)
            + ",\nUNIQUE(stock_code, report_period)\n)"
        )

    def build_upsert_sql(self, table: str, columns: list[str]) -> str:
        if self.backend == "mysql":
            placeholders = ", ".join("%s" for _ in columns)
            col_names = ", ".join(f"`{col}`" for col in columns)
            update_clause = ", ".join(
                f"`{col}`=COALESCE(VALUES(`{col}`), `{table}`.`{col}`)"
                for col in columns
                if col not in {"stock_code", "report_period"}
            )
            return (
                f"INSERT INTO `{table}` ({col_names}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_clause}"
            )
        placeholders = ", ".join("?" for _ in columns)
        update_clause = ", ".join(
            f"{col}=COALESCE(excluded.{col}, {table}.{col})"
            for col in columns
            if col not in {"stock_code", "report_period"}
        )
        return (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(stock_code, report_period) DO UPDATE SET {update_clause}"
        )

    def upsert(self, table: str, row: dict[str, Any]) -> None:
        self.connect()
        if table not in self.schema:
            raise ValueError(f"Unknown table: {table}")
        columns = [field.field_name for field in self.schema[table] if field.field_name in row]
        if not columns:
            return
        sql = self.build_upsert_sql(table, columns)
        values = [row.get(col) for col in columns]
        self.execute(sql, tuple(values))

    def upsert_many(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.connect()
        if table not in self.schema:
            raise ValueError(f"Unknown table: {table}")
        if not rows:
            return
        columns = [field.field_name for field in self.schema[table] if field.field_name in rows[0]]
        if not columns:
            return
        sql = self.build_upsert_sql(table, columns)
        params = [tuple(row.get(col) for col in columns) for row in rows]
        self.execute_many(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] | None = None, use_cache: bool = True) -> list[dict[str, Any]]:
        self.connect()

        # Check cache
        cache_key = None
        if self._cache_enabled and use_cache and sql.strip().upper().startswith("SELECT"):
            cache_key = self._make_cache_key(sql, params)
            if cache_key in self._cache:
                self._cache_hits += 1
                self.logger.debug(f"Cache HIT | key={cache_key[:16]}...")
                return self._cache[cache_key]
            self._cache_misses += 1

        try:
            if self.backend == "mysql":
                cur = self._conn.cursor(dictionary=True)
                cur.execute(sql, params or ())
                rows = cur.fetchall()
                cur.close()
                result = [dict(row) for row in rows]
            else:
                cur = self._conn.execute(sql, params or ())
                result = [dict(row) for row in cur.fetchall()]

            # Store in cache
            if cache_key and self._cache_enabled:
                if len(self._cache) >= self._cache_size:
                    # Simple LRU: remove first item
                    self._cache.pop(next(iter(self._cache)))
                self._cache[cache_key] = result
                self.logger.debug(f"Cache MISS | key={cache_key[:16]}... | rows={len(result)}")

            return result

        except Exception as exc:
            self.logger.error(f"Query failed | sql={sql[:100]}... | error={exc}")
            raise

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.connect()
        if self.backend == "mysql":
            cur = self._conn.cursor()
            cur.execute(sql, params or ())
            self._conn.commit()
            cur.close()
            self.clear_cache()
            return
        self._conn.execute(sql, params or ())
        self._conn.commit()
        self.clear_cache()

    def execute_many(self, sql: str, params_list: list[tuple[Any, ...]]) -> None:
        self.connect()
        if not params_list:
            return
        if self.backend == "mysql":
            cur = self._conn.cursor()
            cur.executemany(sql, params_list)
            self._conn.commit()
            cur.close()
            self.clear_cache()
            return
        self._conn.executemany(sql, params_list)
        self._conn.commit()
        self.clear_cache()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self.logger.info(f"Database connection closed | backend={self.backend}")

    def clear_cache(self) -> None:
        """Clear query cache."""
        self._cache.clear()
        self._cache_hits = 0
        self._cache_misses = 0
        self.logger.info("Query cache cleared")

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        total = self._cache_hits + self._cache_misses
        hit_rate = round(self._cache_hits / max(total, 1) * 100, 2)
        return {
            "enabled": self._cache_enabled,
            "size": len(self._cache),
            "max_size": self._cache_size,
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate": hit_rate,
        }

    @staticmethod
    def _make_cache_key(sql: str, params: tuple[Any, ...] | None) -> str:
        """Generate cache key from SQL and parameters."""
        key_data = sql + str(params or ())
        return hashlib.md5(key_data.encode()).hexdigest()

    @property
    def allowed_tables(self) -> set[str]:
        return set(self.schema.keys())

    @property
    def allowed_columns(self) -> dict[str, set[str]]:
        return {table: {field.field_name for field in fields} for table, fields in self.schema.items()}
