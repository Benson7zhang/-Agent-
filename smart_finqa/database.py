from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Mapping

from .config import DatabaseConfig
from .facts import (
    AuthoritativeSource,
    FinancialFact,
    SourceAuthorityStatus,
    SourceOwner,
    StatementScope,
    ValidationIssue,
    ValidationStatus,
    build_source_key,
    expected_period_type_for_table,
    require_validated_evidence,
)
from .logger import get_logger
from .schema import FieldSpec, map_to_sqlite_type

FINANCIAL_FACT_TABLE = "financial_fact"
FINANCIAL_SOURCE_TABLE = "financial_source"
PROJECTION_KEY_COLUMNS = frozenset({"serial_number", "stock_code", "stock_abbr", "report_period", "report_year"})
FINANCIAL_FACT_COLUMNS = (
    "fact_key",
    "company_id",
    "stock_code",
    "period",
    "statement_scope",
    "period_type",
    "metric",
    "raw_value",
    "normalized_value",
    "source_unit",
    "target_unit",
    "currency",
    "source_file",
    "source_key",
    "page_no",
    "table_name",
    "row_label",
    "column_label",
    "confidence",
    "validation_status",
    "validation_issues",
    "extractor_version",
)
FINANCIAL_FACT_CONTROL_COLUMNS = frozenset({"review_version", "document_id"})
FINANCIAL_SOURCE_COLUMNS = frozenset(
    {
        "source_key",
        "source_file",
        "content_sha256",
        "stock_code",
        "period",
        "source_owner",
        "authority_status",
        "current_slot",
        "document_id",
        "selection_version",
    }
)
CURRENT_SOURCE_SLOT = SourceAuthorityStatus.CURRENT.value
SOURCE_IDENTITY_COLUMNS = ("source_key", "source_file", "content_sha256", "stock_code", "period")
MYSQL_SCHEMA_MIGRATION_LOCK_NAME = "smart_finqa:schema_migrations"
MYSQL_SCHEMA_MIGRATION_LOCK_TIMEOUT_SECONDS = 30


def _map_to_mysql_type(raw_type: str | None) -> str:
    text = (raw_type or "").strip().lower()
    if "int" in text:
        return "BIGINT"
    if any(token in text for token in ("decimal", "float", "double", "numeric")):
        return "DOUBLE"
    return "VARCHAR(255)"


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
        self._schema_migration_lock_depth = 0
        self._schema_migration_lock_cursor: Any = None

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
                self._conn.execute("PRAGMA foreign_keys = ON")
                self._conn.execute(f"PRAGMA busy_timeout = {self.db_config.sqlite_busy_timeout_ms}")
                self._conn.execute("PRAGMA journal_mode = WAL")
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
            autocommit=True,
        )
        return conn

    def create_tables(self) -> None:
        self.connect()
        with self.schema_migration_lock():
            for table, fields in self.schema.items():
                ddl = self._build_create_table_sql(table, fields)
                self.execute(ddl)
            self.execute(self._build_financial_source_table_sql())
            self.execute(self._build_financial_fact_table_sql())
            self._migrate_source_identity_schema()

    @contextmanager
    def schema_migration_lock(self) -> Iterator[Any | None]:
        """Serialize MySQL schema changes on this connection; SQLite needs no named lock."""
        self.connect()
        if self.backend != "mysql":
            yield None
            return

        if self._schema_migration_lock_depth:
            self._schema_migration_lock_depth += 1
            try:
                yield self._schema_migration_lock_cursor
            finally:
                self._schema_migration_lock_depth -= 1
            return

        cursor = self._conn.cursor(dictionary=True)
        release_lock_on_exit = False
        primary_error: BaseException | None = None
        primary_traceback: Any = None
        cleanup_errors: list[BaseException] = []
        try:
            cursor.execute(
                "SELECT GET_LOCK(%s, %s) AS acquired",
                (MYSQL_SCHEMA_MIGRATION_LOCK_NAME, MYSQL_SCHEMA_MIGRATION_LOCK_TIMEOUT_SECONDS),
            )
            release_lock_on_exit = True
            lock_row = cursor.fetchone()
            if lock_row is None or dict(lock_row).get("acquired") != 1:
                release_lock_on_exit = False
                raise RuntimeError(
                    "could not acquire MySQL schema migration lock within "
                    f"{MYSQL_SCHEMA_MIGRATION_LOCK_TIMEOUT_SECONDS} seconds"
                )
            self._schema_migration_lock_depth = 1
            self._schema_migration_lock_cursor = cursor
            try:
                yield cursor
            finally:
                self._schema_migration_lock_depth = 0
                self._schema_migration_lock_cursor = None
        except BaseException as error:
            primary_error = error
            primary_traceback = error.__traceback__
        finally:
            if release_lock_on_exit:
                try:
                    cursor.execute(
                        "SELECT RELEASE_LOCK(%s) AS released",
                        (MYSQL_SCHEMA_MIGRATION_LOCK_NAME,),
                    )
                    release_row = cursor.fetchone()
                    if release_row is None or dict(release_row).get("released") != 1:
                        raise RuntimeError("could not release MySQL schema migration lock")
                except BaseException as release_error:
                    cleanup_errors.append(release_error)
            try:
                cursor.close()
            except BaseException as cursor_error:
                cleanup_errors.append(cursor_error)

        cleanup_error: BaseException | None = None
        if len(cleanup_errors) == 1:
            cleanup_error = cleanup_errors[0]
        elif cleanup_errors:
            details = "; ".join(f"{type(error).__name__}: {error}" for error in cleanup_errors)
            cleanup_error = RuntimeError(f"multiple MySQL schema migration lock cleanup failures: {details}")
            cleanup_error.__cause__ = cleanup_errors[0]
        if primary_error is not None:
            if cleanup_error is not None:
                raise primary_error.with_traceback(primary_traceback) from cleanup_error
            raise primary_error.with_traceback(primary_traceback)
        if cleanup_error is not None:
            raise cleanup_error

    @property
    def parameter_marker(self) -> str:
        """Return the DB-API parameter marker for the configured backend."""
        return "%s" if self.backend == "mysql" else "?"

    def parameter_markers(self, count: int) -> str:
        if count < 1:
            raise ValueError("Parameter marker count must be positive")
        return ", ".join(self.parameter_marker for _ in range(count))

    def _build_financial_fact_table_sql(self) -> str:
        if self.backend == "mysql":
            return """
                CREATE TABLE IF NOT EXISTS `financial_fact` (
                    `fact_key` CHAR(64) PRIMARY KEY,
                    `company_id` VARCHAR(64) NOT NULL,
                    `stock_code` VARCHAR(16) NOT NULL,
                    `period` VARCHAR(16) NOT NULL,
                    `statement_scope` VARCHAR(32) NOT NULL,
                    `period_type` VARCHAR(32) NOT NULL,
                    `metric` VARCHAR(128) NOT NULL,
                    `raw_value` VARCHAR(128) NOT NULL,
                    `normalized_value` DOUBLE NOT NULL,
                    `source_unit` VARCHAR(32) NOT NULL,
                    `target_unit` VARCHAR(32) NOT NULL,
                    `currency` VARCHAR(16) NOT NULL,
                    `source_file` TEXT NOT NULL,
                    `source_key` CHAR(64) NOT NULL,
                    `page_no` INT NULL,
                    `table_name` VARCHAR(128) NULL,
                    `row_label` VARCHAR(255) NULL,
                    `column_label` VARCHAR(255) NULL,
                    `confidence` DOUBLE NOT NULL,
                    `validation_status` VARCHAR(32) NOT NULL,
                    `validation_issues` TEXT NOT NULL,
                    `extractor_version` VARCHAR(64) NOT NULL,
                    `review_version` BIGINT NOT NULL DEFAULT 1,
                    `document_id` VARCHAR(36) NULL,
                    INDEX `idx_fact_lookup` (`stock_code`, `period`, `metric`, `validation_status`),
                    INDEX `idx_fact_source` (`source_key`)
                ) ENGINE=InnoDB
            """
        return """
            CREATE TABLE IF NOT EXISTS financial_fact (
                fact_key TEXT PRIMARY KEY,
                company_id TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                period TEXT NOT NULL,
                statement_scope TEXT NOT NULL,
                period_type TEXT NOT NULL,
                metric TEXT NOT NULL,
                raw_value TEXT NOT NULL,
                normalized_value REAL NOT NULL,
                source_unit TEXT NOT NULL,
                target_unit TEXT NOT NULL,
                currency TEXT NOT NULL,
                source_file TEXT NOT NULL,
                source_key TEXT NOT NULL,
                page_no INTEGER,
                table_name TEXT,
                row_label TEXT,
                column_label TEXT,
                confidence REAL NOT NULL,
                validation_status TEXT NOT NULL,
                validation_issues TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                review_version INTEGER NOT NULL DEFAULT 1,
                document_id TEXT
            )
        """

    def _build_financial_source_table_sql(self) -> str:
        if self.backend == "mysql":
            return """
                CREATE TABLE IF NOT EXISTS `financial_source` (
                    `source_key` CHAR(64) PRIMARY KEY,
                    `source_file` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                    `content_sha256` CHAR(64) NULL,
                    `stock_code` VARCHAR(16) NOT NULL,
                    `period` VARCHAR(16) NOT NULL,
                    `source_owner` VARCHAR(16) NOT NULL,
                    `authority_status` VARCHAR(32) NOT NULL,
                    `current_slot` VARCHAR(16) NULL,
                    `document_id` VARCHAR(36) NULL,
                    `selection_version` VARCHAR(64) NOT NULL,
                    UNIQUE KEY `uniq_current_financial_source` (`stock_code`, `period`, `current_slot`),
                    INDEX `idx_financial_source_status` (`stock_code`, `period`, `authority_status`),
                    CONSTRAINT `chk_financial_source_owner` CHECK (
                        `source_owner` IN ('BATCH', 'WEB', 'LEGACY')
                    ),
                    CONSTRAINT `chk_financial_source_authority` CHECK (
                        `authority_status` IN ('CURRENT', 'SUPERSEDED', 'REMOVED', 'NEEDS_REVIEW')
                    ),
                    CONSTRAINT `chk_financial_source_status` CHECK (
                        (`authority_status` = 'CURRENT' AND `current_slot` = 'CURRENT' AND `content_sha256` IS NOT NULL)
                        OR (`authority_status` <> 'CURRENT' AND `current_slot` IS NULL)
                    )
                ) ENGINE=InnoDB
            """
        return """
            CREATE TABLE IF NOT EXISTS financial_source (
                source_key TEXT PRIMARY KEY,
                source_file TEXT NOT NULL,
                content_sha256 TEXT,
                stock_code TEXT NOT NULL,
                period TEXT NOT NULL,
                source_owner TEXT NOT NULL,
                authority_status TEXT NOT NULL,
                current_slot TEXT,
                document_id TEXT,
                selection_version TEXT NOT NULL,
                UNIQUE(stock_code, period, current_slot),
                CHECK (source_owner IN ('BATCH', 'WEB', 'LEGACY')),
                CHECK (authority_status IN ('CURRENT', 'SUPERSEDED', 'REMOVED', 'NEEDS_REVIEW')),
                CHECK (
                    (authority_status = 'CURRENT' AND current_slot = 'CURRENT' AND content_sha256 IS NOT NULL)
                    OR (authority_status <> 'CURRENT' AND current_slot IS NULL)
                )
            )
        """

    @staticmethod
    def financial_source_key(source_file: str, source_content_sha256: str) -> str:
        return build_source_key(source_file, source_content_sha256)

    def _migrate_source_identity_schema(self) -> None:
        """Upgrade path-only source identity without trusting current on-disk file bytes."""
        self.connect()
        cursor = self._conn.cursor(dictionary=True) if self.backend == "mysql" else self._conn.cursor()
        try:
            self._begin_transaction()
            source_columns = self._table_columns(cursor, FINANCIAL_SOURCE_TABLE)
            fact_columns = self._table_columns(cursor, FINANCIAL_FACT_TABLE)
            added_fact_source_key = "source_key" not in fact_columns

            if "content_sha256" not in source_columns:
                column_type = "CHAR(64) NULL" if self.backend == "mysql" else "TEXT"
                cursor.execute(f"ALTER TABLE financial_source ADD COLUMN content_sha256 {column_type}")
            if "source_owner" not in source_columns:
                column_type = "VARCHAR(16) NULL" if self.backend == "mysql" else "TEXT"
                cursor.execute(f"ALTER TABLE financial_source ADD COLUMN source_owner {column_type}")
            if added_fact_source_key:
                column_type = "CHAR(64) NULL" if self.backend == "mysql" else "TEXT"
                cursor.execute(f"ALTER TABLE financial_fact ADD COLUMN source_key {column_type}")

            self._backfill_source_identity_in_transaction(cursor)

            if self.backend == "sqlite":
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_fact_source ON financial_fact(source_key)")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()

    def _backfill_source_identity_in_transaction(self, cursor: Any) -> None:
        document_hashes = self._document_hashes(cursor)
        cursor.execute(
            "SELECT source_key, source_file, content_sha256, stock_code, period, source_owner, authority_status, "
            "current_slot, document_id, selection_version FROM financial_source"
        )
        sources = [dict(row) for row in cursor.fetchall()]

        if not sources:
            cursor.execute(
                "SELECT DISTINCT source_file, stock_code, period, document_id FROM financial_fact "
                "WHERE source_file IS NOT NULL"
            )
            for raw in cursor.fetchall():
                fact = dict(raw)
                source_file = str(fact["source_file"])
                stock_code = str(fact["stock_code"])
                period = str(fact["period"])
                document_id = fact.get("document_id")
                content_sha256 = document_hashes.get(str(document_id)) if document_id else None
                if content_sha256 is not None:
                    source_key = build_source_key(source_file, content_sha256)
                    source_owner = SourceOwner.WEB.value
                else:
                    source_key = self._legacy_source_key(source_file, stock_code, period)
                    source_owner = SourceOwner.LEGACY.value
                cursor.execute(
                    "INSERT INTO financial_source (source_key, source_file, content_sha256, stock_code, period, "
                    "source_owner, authority_status, current_slot, document_id, selection_version) VALUES "
                    f"({self.parameter_markers(10)})",
                    (
                        source_key,
                        source_file,
                        content_sha256,
                        stock_code,
                        period,
                        source_owner,
                        SourceAuthorityStatus.REMOVED.value,
                        None,
                        document_id,
                        "source-identity-migration-v2",
                    ),
                )
                cursor.execute(
                    "UPDATE financial_fact SET source_key = {m} WHERE source_file = {m} AND stock_code = {m} "
                    "AND period = {m}".format(m=self.parameter_marker),
                    (source_key, source_file, stock_code, period),
                )
            return

        for source in sources:
            old_key = str(source["source_key"])
            source_file = str(source["source_file"])
            stock_code = str(source["stock_code"])
            period = str(source["period"])
            document_id = source.get("document_id")
            raw_content_sha256 = source.get("content_sha256")
            content_sha256 = str(raw_content_sha256).lower() if raw_content_sha256 else None
            if content_sha256 is None and document_id is not None:
                content_sha256 = document_hashes.get(str(document_id))

            if (
                content_sha256 is not None
                and len(content_sha256) == 64
                and all(character in "0123456789abcdef" for character in content_sha256)
            ):
                new_key = build_source_key(source_file, content_sha256)
                raw_owner = source.get("source_owner")
                source_owner = str(raw_owner) if raw_owner in {item.value for item in SourceOwner} else None
                if source_owner is None:
                    source_owner = SourceOwner.WEB.value if document_id is not None else SourceOwner.BATCH.value
                authority_status = str(source["authority_status"])
                current_slot = source.get("current_slot")
            else:
                content_sha256 = None
                new_key = old_key or self._legacy_source_key(source_file, stock_code, period)
                source_owner = SourceOwner.LEGACY.value
                authority_status = SourceAuthorityStatus.REMOVED.value
                current_slot = None

            if new_key != old_key:
                cursor.execute(
                    f"SELECT source_key FROM financial_source WHERE source_key = {self.parameter_marker}",
                    (new_key,),
                )
                if cursor.fetchone() is not None:
                    raise RuntimeError(f"source identity migration collision for {source_file!r}")
                cursor.execute(
                    "UPDATE financial_source SET source_key = {m}, content_sha256 = {m}, source_owner = {m}, "
                    "authority_status = {m}, current_slot = {m} WHERE source_key = {m}".format(m=self.parameter_marker),
                    (new_key, content_sha256, source_owner, authority_status, current_slot, old_key),
                )
            else:
                cursor.execute(
                    "UPDATE financial_source SET content_sha256 = {m}, source_owner = {m}, authority_status = {m}, "
                    "current_slot = {m} WHERE source_key = {m}".format(m=self.parameter_marker),
                    (content_sha256, source_owner, authority_status, current_slot, old_key),
                )
            cursor.execute(
                "UPDATE financial_fact SET source_key = {m} WHERE source_file = {m} AND stock_code = {m} "
                "AND period = {m} AND (source_key IS NULL OR source_key = {m})".format(m=self.parameter_marker),
                (new_key, source_file, stock_code, period, old_key),
            )

    def _document_hashes(self, cursor: Any) -> dict[str, str]:
        if not self._table_exists(cursor, "document"):
            return {}
        document_columns = self._table_columns(cursor, "document")
        if not {"document_id", "sha256"}.issubset(document_columns):
            return {}
        cursor.execute("SELECT document_id, sha256 FROM document")
        hashes: dict[str, str] = {}
        for raw in cursor.fetchall():
            row = dict(raw)
            value = str(row.get("sha256") or "").lower()
            if len(value) == 64 and all(character in "0123456789abcdef" for character in value):
                hashes[str(row["document_id"])] = value
        return hashes

    def _table_exists(self, cursor: Any, table: str) -> bool:
        if self.backend == "mysql":
            cursor.execute(
                "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (table,),
            )
            return cursor.fetchone() is not None
        cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
        return cursor.fetchone() is not None

    def _table_columns(self, cursor: Any, table: str) -> set[str]:
        if self.backend == "mysql":
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (table,),
            )
            return {str(dict(row)["COLUMN_NAME"]) for row in cursor.fetchall()}
        cursor.execute(f"PRAGMA table_info({self._quote_identifier(table)})")
        return {str(dict(row)["name"]) for row in cursor.fetchall()}

    @staticmethod
    def _legacy_source_key(source_file: str, stock_code: str, period: str) -> str:
        identity = f"smart-finqa:legacy-source\x1f{source_file}\x1f{stock_code}\x1f{period}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def reconcile_batch_authoritative_sources(
        self,
        sources: list[AuthoritativeSource],
        *,
        selection_version: str,
    ) -> None:
        """Replace one batch selector's CURRENT source set without touching Web-managed sources."""
        if not selection_version.strip():
            raise ValueError("selection_version must be a non-empty string")
        by_business_key: dict[tuple[str, str], AuthoritativeSource] = {}
        for source in sources:
            if source.source_owner is not SourceOwner.BATCH:
                raise ValueError("batch reconciliation requires BATCH-owned sources")
            if source.document_id is not None:
                raise ValueError("batch authoritative sources cannot be linked to Web documents")
            if source.selection_version != selection_version:
                raise ValueError("batch source selection_version does not match reconciliation scope")
            key = (source.stock_code, source.period)
            if key in by_business_key:
                raise ValueError(f"multiple CURRENT batch sources requested for {source.stock_code}/{source.period}")
            by_business_key[key] = source

        self.connect()
        cursor = self._conn.cursor(dictionary=True) if self.backend == "mysql" else self._conn.cursor()
        try:
            self._begin_transaction()
            lock = " FOR UPDATE" if self.backend == "mysql" else ""
            cursor.execute(
                "SELECT source_key, source_file, stock_code, period, document_id FROM financial_source "
                f"WHERE authority_status = {self.parameter_marker} AND source_owner = {self.parameter_marker}{lock}",
                (SourceAuthorityStatus.CURRENT.value, SourceOwner.BATCH.value),
            )
            managed_current = [dict(row) for row in cursor.fetchall()]
            affected = {(str(row["stock_code"]), str(row["period"])) for row in managed_current}
            affected.update(by_business_key)

            for row in managed_current:
                key = (str(row["stock_code"]), str(row["period"]))
                requested = by_business_key.get(key)
                if requested is None or requested.source_key != row["source_key"]:
                    replacement_status = (
                        SourceAuthorityStatus.REMOVED.value
                        if requested is None
                        else SourceAuthorityStatus.SUPERSEDED.value
                    )
                    cursor.execute(
                        "UPDATE financial_source SET authority_status = {m}, current_slot = NULL "
                        "WHERE source_key = {m}".format(m=self.parameter_marker),
                        (replacement_status, row["source_key"]),
                    )

            for source in by_business_key.values():
                cursor.execute(
                    "SELECT source_key, source_file, document_id, source_owner, selection_version FROM financial_source "
                    "WHERE stock_code = {m} AND period = {m} AND authority_status = {m}{lock}".format(
                        m=self.parameter_marker,
                        lock=lock,
                    ),
                    (source.stock_code, source.period, SourceAuthorityStatus.CURRENT.value),
                )
                current = cursor.fetchone()
                if current is not None:
                    current_row = dict(current)
                    current_owner = SourceOwner(str(current_row["source_owner"]))
                    if current_owner is SourceOwner.WEB:
                        raise ValueError(
                            f"batch source conflicts with CURRENT Web source for {source.stock_code}/{source.period}"
                        )
                    if current_owner not in {SourceOwner.BATCH, SourceOwner.LEGACY}:
                        raise ValueError(
                            f"batch source conflicts with another owner for {source.stock_code}/{source.period}"
                        )
                source_key = source.source_key
                cursor.execute(
                    "SELECT stock_code, period, document_id, source_owner, selection_version FROM financial_source "
                    "WHERE source_key = {m}{lock}".format(m=self.parameter_marker, lock=lock),
                    (source_key,),
                )
                registered = cursor.fetchone()
                if registered is not None:
                    registered_row = dict(registered)
                    registered_owner = SourceOwner(str(registered_row["source_owner"]))
                    if registered_owner is SourceOwner.WEB:
                        raise ValueError(f"batch source conflicts with Web-managed source: {source.source_file}")
                    affected.add((str(registered_row["stock_code"]), str(registered_row["period"])))
                if current is not None and str(current["source_key"]) != source.source_key:
                    cursor.execute(
                        "UPDATE financial_source SET authority_status = {m}, current_slot = NULL "
                        "WHERE source_key = {m}".format(m=self.parameter_marker),
                        (SourceAuthorityStatus.SUPERSEDED.value, current_row["source_key"]),
                    )
                self._upsert_source_in_transaction(cursor, source, SourceAuthorityStatus.CURRENT)

            for stock_code, period in sorted(affected):
                self._rebuild_period_projections_in_transaction(cursor, stock_code, period)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()
        self.clear_cache()

    def activate_web_authoritative_source_in_transaction(
        self,
        cursor: Any,
        source: AuthoritativeSource,
    ) -> None:
        """Make a Web document globally authoritative inside its promotion transaction."""
        if source.document_id is None:
            raise ValueError("Web authoritative source requires document_id")
        lock = " FOR UPDATE" if self.backend == "mysql" else ""
        if source.source_owner is not SourceOwner.WEB:
            raise ValueError("Web activation requires a WEB-owned source")
        source_key = source.source_key
        cursor.execute(
            "SELECT stock_code, period, document_id FROM financial_source WHERE source_key = {m}{lock}".format(
                m=self.parameter_marker,
                lock=lock,
            ),
            (source_key,),
        )
        registered = cursor.fetchone()
        affected_business_keys: set[tuple[str, str]] = set()
        if registered is not None:
            registered_row = dict(registered)
            registered_document_id = registered_row.get("document_id")
            if registered_document_id is not None and str(registered_document_id) != source.document_id:
                raise ValueError("source_file is already linked to another Web document")
            affected_business_keys.add((str(registered_row["stock_code"]), str(registered_row["period"])))
        cursor.execute(
            "SELECT source_key, stock_code, period FROM financial_source WHERE source_file = {m} "
            "AND authority_status = {m}{lock}".format(m=self.parameter_marker, lock=lock),
            (source.source_file, SourceAuthorityStatus.CURRENT.value),
        )
        current_path_sources = [dict(row) for row in cursor.fetchall()]
        if len(current_path_sources) > 1:
            raise RuntimeError(f"multiple CURRENT revisions exist for source path: {source.source_file!r}")
        for path_source in current_path_sources:
            affected_business_keys.add((str(path_source["stock_code"]), str(path_source["period"])))
            if str(path_source["source_key"]) != source.source_key:
                cursor.execute(
                    "UPDATE financial_source SET authority_status = {m}, current_slot = NULL "
                    "WHERE source_key = {m}".format(m=self.parameter_marker),
                    (SourceAuthorityStatus.SUPERSEDED.value, path_source["source_key"]),
                )
        cursor.execute(
            "SELECT source_key, source_file FROM financial_source WHERE stock_code = {m} AND period = {m} "
            "AND authority_status = {m}{lock}".format(m=self.parameter_marker, lock=lock),
            (source.stock_code, source.period, SourceAuthorityStatus.CURRENT.value),
        )
        current = cursor.fetchone()
        if current is not None:
            affected_business_keys.add((source.stock_code, source.period))
        if current is not None and str(current["source_key"]) != source.source_key:
            cursor.execute(
                "UPDATE financial_source SET authority_status = {m}, current_slot = NULL WHERE source_key = {m}".format(
                    m=self.parameter_marker
                ),
                (SourceAuthorityStatus.SUPERSEDED.value, current["source_key"]),
            )
        self._upsert_source_in_transaction(cursor, source, SourceAuthorityStatus.CURRENT)
        affected_business_keys.add((source.stock_code, source.period))
        for business_key in sorted(affected_business_keys):
            self._rebuild_period_projections_in_transaction(cursor, *business_key)

    def _upsert_source_in_transaction(
        self,
        cursor: Any,
        source: AuthoritativeSource,
        status: SourceAuthorityStatus,
    ) -> None:
        source_key = source.source_key
        values = (
            source_key,
            source.source_file,
            source.source_content_sha256,
            source.stock_code,
            source.period,
            source.source_owner.value,
            status.value,
            CURRENT_SOURCE_SLOT if status is SourceAuthorityStatus.CURRENT else None,
            source.document_id,
            source.selection_version,
        )
        columns = (
            "source_key, source_file, content_sha256, stock_code, period, source_owner, authority_status, "
            "current_slot, document_id, selection_version"
        )
        lock = " FOR UPDATE" if self.backend == "mysql" else ""
        cursor.execute(
            "SELECT source_key, source_file, content_sha256, stock_code, period FROM financial_source "
            "WHERE source_key = {m}{lock}".format(
                m=self.parameter_marker,
                lock=lock,
            ),
            (source_key,),
        )
        registered = cursor.fetchone()
        if registered is None:
            cursor.execute(
                f"INSERT INTO financial_source ({columns}) VALUES ({self.parameter_markers(10)})",
                values,
            )
            return
        self._require_source_identity(dict(registered), source)
        cursor.execute(
            "UPDATE financial_source SET source_owner = {m}, authority_status = {m}, current_slot = {m}, document_id = {m}, "
            "selection_version = {m} WHERE source_key = {m}".format(m=self.parameter_marker),
            (*values[5:], source_key),
        )

    @staticmethod
    def _require_source_identity(registered: Mapping[str, Any], source: AuthoritativeSource) -> None:
        expected = (
            source.source_key,
            source.source_file,
            source.source_content_sha256,
            source.stock_code,
            source.period,
        )
        actual = tuple(str(registered.get(column) or "") for column in SOURCE_IDENTITY_COLUMNS)
        if actual != expected:
            raise ValueError(
                "source identity is immutable; "
                f"source_key {source.source_key} is registered for {actual[3]}/{actual[4]}, "
                f"not {source.stock_code}/{source.period}"
            )

    def _rebuild_period_projections_in_transaction(self, cursor: Any, stock_code: str, period: str) -> None:
        for table, fields in self.schema.items():
            metric_fields = [field.field_name for field in fields if field.field_name not in PROJECTION_KEY_COLUMNS]
            if not metric_fields:
                continue
            quoted_table = self._quote_identifier(table)
            assignments = ", ".join(f"{self._quote_identifier(metric)} = NULL" for metric in metric_fields)
            cursor.execute(
                f"UPDATE {quoted_table} SET {assignments} WHERE stock_code = {self.parameter_marker} "
                f"AND report_period = {self.parameter_marker}",
                (stock_code, period),
            )
            cursor.execute(
                "SELECT ff.metric, ff.normalized_value FROM financial_fact ff "
                "INNER JOIN financial_source fs ON fs.source_key = ff.source_key "
                "AND fs.stock_code = ff.stock_code AND fs.period = ff.period "
                "WHERE ff.stock_code = {m} AND ff.period = {m} AND ff.table_name = {m} "
                "AND ff.validation_status = {m} AND ff.statement_scope = {m} AND ff.period_type = {m} "
                "AND fs.authority_status = {m} AND ff.metric IN ({markers})".format(
                    m=self.parameter_marker,
                    markers=self.parameter_markers(len(metric_fields)),
                ),
                (
                    stock_code,
                    period,
                    table,
                    ValidationStatus.VALIDATED.value,
                    StatementScope.CONSOLIDATED.value,
                    expected_period_type_for_table(table).value,
                    SourceAuthorityStatus.CURRENT.value,
                    *metric_fields,
                ),
            )
            values: dict[str, Any] = {}
            for raw in cursor.fetchall():
                row = dict(raw)
                metric = str(row["metric"])
                if metric in values:
                    raise ValueError(f"multiple CURRENT VALIDATED facts conflict for {stock_code}/{period}/{metric}")
                values[metric] = row["normalized_value"]
            for metric, value in values.items():
                cursor.execute(
                    f"UPDATE {quoted_table} SET {self._quote_identifier(metric)} = {self.parameter_marker} "
                    f"WHERE stock_code = {self.parameter_marker} AND report_period = {self.parameter_marker}",
                    (value, stock_code, period),
                )

    def build_financial_fact_upsert_sql(self) -> str:
        markers = self.parameter_markers(len(FINANCIAL_FACT_COLUMNS))
        immutable_review_columns = {"fact_key", "validation_status", "validation_issues"}
        if self.backend == "mysql":
            columns = ", ".join(f"`{column}`" for column in FINANCIAL_FACT_COLUMNS)
            updates = ", ".join(
                f"`{column}`=VALUES(`{column}`)"
                for column in FINANCIAL_FACT_COLUMNS
                if column not in immutable_review_columns
            )
            return (
                f"INSERT INTO `{FINANCIAL_FACT_TABLE}` ({columns}) VALUES ({markers}) ON DUPLICATE KEY UPDATE {updates}"
            )
        columns = ", ".join(FINANCIAL_FACT_COLUMNS)
        updates = ", ".join(
            f"{column}=excluded.{column}" for column in FINANCIAL_FACT_COLUMNS if column not in immutable_review_columns
        )
        return (
            f"INSERT INTO {FINANCIAL_FACT_TABLE} ({columns}) VALUES ({markers}) "
            f"ON CONFLICT(fact_key) DO UPDATE SET {updates}"
        )

    def upsert_financial_facts(self, facts: list[FinancialFact]) -> None:
        if not facts:
            return
        source_business_keys: dict[str, tuple[str, str]] = {}
        for fact in facts:
            business_key = (fact.stock_code, fact.period)
            existing_key = source_business_keys.setdefault(fact.source_file, business_key)
            if existing_key != business_key:
                raise ValueError(f"one source_file cannot contain multiple company/period keys: {fact.source_file!r}")
        invalid_statuses = {
            fact.validation_status for fact in facts if fact.validation_status is not ValidationStatus.NEEDS_REVIEW
        }
        if invalid_statuses:
            values = ", ".join(sorted(status.value for status in invalid_statuses))
            raise ValueError(
                f"financial fact ingestion accepts NEEDS_REVIEW candidates only; use review_financial_fact for: {values}"
            )
        self.connect()
        cursor = self._conn.cursor(dictionary=True) if self.backend == "mysql" else self._conn.cursor()
        try:
            self._begin_transaction()
            seen_sources: set[str] = set()
            for fact in facts:
                if fact.source_key in seen_sources:
                    continue
                seen_sources.add(fact.source_key)
                source = AuthoritativeSource(
                    source_file=fact.source_file,
                    source_content_sha256=fact.source_content_sha256,
                    stock_code=fact.stock_code,
                    period=fact.period,
                    source_owner=SourceOwner.LEGACY,
                    selection_version="legacy-fact-upsert-v2",
                )
                cursor.execute(
                    "SELECT source_key, source_file, content_sha256, stock_code, period FROM financial_source "
                    f"WHERE source_key = {self.parameter_marker}",
                    (fact.source_key,),
                )
                registered = cursor.fetchone()
                if registered is not None:
                    self._require_source_identity(dict(registered), source)
                    continue
                cursor.execute(
                    "SELECT source_key FROM financial_source WHERE stock_code = {m} AND period = {m} "
                    "AND authority_status = {m}".format(m=self.parameter_marker),
                    (fact.stock_code, fact.period, SourceAuthorityStatus.CURRENT.value),
                )
                current = cursor.fetchone()
                status = (
                    SourceAuthorityStatus.CURRENT
                    if current is None or str(current["source_key"]) == fact.source_key
                    else SourceAuthorityStatus.NEEDS_REVIEW
                )
                self._upsert_source_in_transaction(cursor, source, status)
            cursor.executemany(
                self.build_financial_fact_upsert_sql(), [self._financial_fact_params(fact) for fact in facts]
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()
        self.clear_cache()

    def review_financial_fact(
        self,
        fact_key: str,
        status: ValidationStatus,
        *,
        validation_issues: tuple[ValidationIssue, ...] = (),
    ) -> None:
        """Apply an explicit human-review decision to one persisted fact."""
        if status not in {ValidationStatus.VALIDATED, ValidationStatus.REJECTED}:
            raise ValueError("review status must be VALIDATED or REJECTED")
        if status is ValidationStatus.VALIDATED and validation_issues:
            raise ValueError("a validated fact cannot retain validation issues")
        if status is ValidationStatus.REJECTED and not validation_issues:
            raise ValueError("a rejected fact must include at least one validation issue")

        issues_json = json.dumps(
            [asdict(issue) for issue in validation_issues],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.connect()
        cursor = None
        try:
            self._begin_transaction()
            cursor = self._conn.cursor(dictionary=True) if self.backend == "mysql" else self._conn.cursor()
            lock_clause = " FOR UPDATE" if self.backend == "mysql" else ""
            cursor.execute(
                f"SELECT stock_code, period, statement_scope, period_type, metric, normalized_value, page_no, source_key, "
                f"table_name, row_label, column_label, "
                f"validation_status FROM {FINANCIAL_FACT_TABLE} WHERE fact_key = {self.parameter_marker}{lock_clause}",
                (fact_key,),
            )
            selected_fact = cursor.fetchone()
            if selected_fact is None:
                raise KeyError(f"Unknown financial fact: {fact_key}")
            fact = dict(selected_fact)
            cursor.execute(
                "SELECT source_key FROM financial_source WHERE source_key = {m} AND stock_code = {m} AND period = {m} "
                "AND authority_status = {m}{lock}".format(
                    m=self.parameter_marker,
                    lock=lock_clause,
                ),
                (
                    fact["source_key"],
                    fact["stock_code"],
                    fact["period"],
                    SourceAuthorityStatus.CURRENT.value,
                ),
            )
            if cursor.fetchone() is None:
                raise ValueError("fact source is not CURRENT and cannot be reviewed")

            if status is ValidationStatus.VALIDATED:
                require_validated_evidence(
                    page_no=fact.get("page_no"),
                    table_name=fact.get("table_name"),
                    row_label=fact.get("row_label"),
                    column_label=fact.get("column_label"),
                )

            target_table = fact.get("table_name")
            if target_table not in self.schema:
                raise ValueError(f"fact references an unknown projection table: {target_table!r}")
            table_metrics = {field.field_name for field in self.schema[target_table]}
            if str(fact["metric"]) not in table_metrics:
                raise ValueError(f"fact metric {fact['metric']!r} does not belong to table {target_table!r}")
            expected_period_type = expected_period_type_for_table(target_table).value
            if fact["period_type"] != expected_period_type:
                raise ValueError(
                    f"fact period_type {fact['period_type']!r} does not match metric table: {target_table!r}"
                )
            should_project = fact["statement_scope"] == StatementScope.CONSOLIDATED.value

            cursor.execute(
                f"SELECT ff.fact_key, ff.normalized_value, ff.page_no, ff.validation_status FROM {FINANCIAL_FACT_TABLE} ff "
                "INNER JOIN financial_source fs ON fs.source_key = ff.source_key "
                "AND fs.stock_code = ff.stock_code AND fs.period = ff.period "
                f"WHERE ff.stock_code = {self.parameter_marker} AND ff.period = {self.parameter_marker} "
                f"AND ff.statement_scope = {self.parameter_marker} AND ff.period_type = {self.parameter_marker} "
                f"AND ff.metric = {self.parameter_marker} AND fs.authority_status = {self.parameter_marker} "
                f"ORDER BY ff.fact_key{lock_clause}",
                (
                    fact["stock_code"],
                    fact["period"],
                    fact["statement_scope"],
                    fact["period_type"],
                    fact["metric"],
                    SourceAuthorityStatus.CURRENT.value,
                ),
            )
            business_facts = [dict(row) for row in cursor.fetchall()]
            current = next((row for row in business_facts if row["fact_key"] == fact_key), None)
            if current is None:
                raise KeyError(f"Unknown financial fact: {fact_key}")
            if status is ValidationStatus.VALIDATED:
                if any(
                    row["fact_key"] != fact_key and row["validation_status"] == ValidationStatus.VALIDATED.value
                    for row in business_facts
                ):
                    raise ValueError("a conflicting VALIDATED fact already exists for this business key")

            projection_tables: list[str] = []
            if should_project:
                quoted_table = self._quote_identifier(target_table)
                cursor.execute(
                    f"SELECT stock_code FROM {quoted_table} WHERE stock_code = {self.parameter_marker} "
                    f"AND report_period = {self.parameter_marker}{lock_clause}",
                    (fact["stock_code"], fact["period"]),
                )
                if cursor.fetchone() is not None:
                    projection_tables.append(target_table)
                if not projection_tables:
                    raise ValueError("fact has no projection row; ingest the report before applying review")

            cursor.execute(
                f"UPDATE {FINANCIAL_FACT_TABLE} SET validation_status = {self.parameter_marker}, "
                f"validation_issues = {self.parameter_marker} WHERE fact_key = {self.parameter_marker}",
                (status.value, issues_json, fact_key),
            )
            for table in projection_tables:
                quoted_table = self._quote_identifier(table)
                quoted_metric = self._quote_identifier(str(fact["metric"]))
                if status is ValidationStatus.VALIDATED:
                    cursor.execute(
                        f"UPDATE {quoted_table} SET {quoted_metric} = {self.parameter_marker} "
                        f"WHERE stock_code = {self.parameter_marker} AND report_period = {self.parameter_marker}",
                        (current["normalized_value"], fact["stock_code"], fact["period"]),
                    )
                elif current["validation_status"] == ValidationStatus.VALIDATED.value:
                    cursor.execute(
                        f"UPDATE {quoted_table} SET {quoted_metric} = NULL "
                        f"WHERE stock_code = {self.parameter_marker} AND report_period = {self.parameter_marker}",
                        (fact["stock_code"], fact["period"]),
                    )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
        self.clear_cache()

    def refresh_fact_projection(self, table: str, source_row: dict[str, Any]) -> None:
        """Rebuild one wide compatibility row exclusively from VALIDATED facts."""
        if table not in self.schema:
            raise ValueError(f"Unknown table: {table}")
        stock_code = str(source_row.get("stock_code") or "")
        report_period = str(source_row.get("report_period") or "")
        if not stock_code or not report_period:
            raise ValueError("projection row requires stock_code and report_period")
        metric_fields = [
            field.field_name for field in self.schema[table] if field.field_name not in PROJECTION_KEY_COLUMNS
        ]
        facts: list[dict[str, Any]] = []
        if metric_fields:
            expected_period_type = expected_period_type_for_table(table).value
            facts = self.query(
                f"SELECT ff.metric, ff.normalized_value FROM {FINANCIAL_FACT_TABLE} ff "
                "INNER JOIN financial_source fs ON fs.source_key = ff.source_key "
                "AND fs.stock_code = ff.stock_code AND fs.period = ff.period "
                f"WHERE ff.stock_code = {self.parameter_marker} AND ff.period = {self.parameter_marker} "
                f"AND ff.validation_status = {self.parameter_marker} AND ff.table_name = {self.parameter_marker} "
                f"AND ff.statement_scope = {self.parameter_marker} AND ff.period_type = {self.parameter_marker} "
                f"AND fs.authority_status = {self.parameter_marker} "
                f"AND ff.metric IN ({self.parameter_markers(len(metric_fields))})",
                (
                    stock_code,
                    report_period,
                    ValidationStatus.VALIDATED.value,
                    table,
                    StatementScope.CONSOLIDATED.value,
                    expected_period_type,
                    SourceAuthorityStatus.CURRENT.value,
                    *metric_fields,
                ),
                use_cache=False,
            )
        validated_values: dict[str, Any] = {}
        for fact in facts:
            metric = str(fact["metric"])
            if metric in validated_values:
                raise ValueError(f"multiple VALIDATED facts conflict for {stock_code}/{report_period}/{metric}")
            validated_values[metric] = fact["normalized_value"]

        projection = {
            field.field_name: source_row.get(field.field_name)
            for field in self.schema[table]
            if field.field_name in PROJECTION_KEY_COLUMNS and field.field_name in source_row
        }
        projection.update(validated_values)
        columns = [field.field_name for field in self.schema[table] if field.field_name in projection]
        quoted_table = self._quote_identifier(table)
        quoted_columns = ", ".join(self._quote_identifier(column) for column in columns)
        statements = [
            (
                f"DELETE FROM {quoted_table} WHERE stock_code = {self.parameter_marker} "
                f"AND report_period = {self.parameter_marker}",
                (stock_code, report_period),
            ),
            (
                f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES ({self.parameter_markers(len(columns))})",
                tuple(projection[column] for column in columns),
            ),
        ]
        self._execute_transaction(statements)

    @staticmethod
    def _financial_fact_params(fact: Any) -> tuple[Any, ...]:
        issues = [asdict(issue) for issue in fact.validation_issues]
        identity = "\x1f".join(
            str(value or "")
            for value in (
                fact.company_id,
                fact.stock_code,
                fact.period,
                fact.statement_scope.value,
                fact.period_type.value,
                fact.metric,
                fact.raw_value,
                fact.normalized_value,
                fact.source_unit,
                fact.target_unit,
                fact.currency,
                fact.source_file,
                fact.source_key,
                fact.page_no,
                fact.table_name,
                fact.row_label,
                fact.column_label,
            )
        )
        values: dict[str, Any] = {
            "fact_key": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            "company_id": fact.company_id,
            "stock_code": fact.stock_code,
            "period": fact.period,
            "statement_scope": fact.statement_scope.value,
            "period_type": fact.period_type.value,
            "metric": fact.metric,
            "raw_value": str(fact.raw_value),
            "normalized_value": fact.normalized_value,
            "source_unit": fact.source_unit,
            "target_unit": fact.target_unit,
            "currency": fact.currency,
            "source_file": fact.source_file,
            "source_key": fact.source_key,
            "page_no": fact.page_no,
            "table_name": fact.table_name,
            "row_label": fact.row_label,
            "column_label": fact.column_label,
            "confidence": fact.confidence,
            "validation_status": fact.validation_status.value,
            "validation_issues": json.dumps(issues, ensure_ascii=False, sort_keys=True),
            "extractor_version": fact.extractor_version,
        }
        return tuple(values[column] for column in FINANCIAL_FACT_COLUMNS)

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
                + ",\nUNIQUE KEY uniq_stock_period (stock_code, report_period)\n) ENGINE=InnoDB"
            )
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            + ",\n".join(field_sql)
            + ",\nUNIQUE(stock_code, report_period)\n)"
        )

    def build_upsert_sql(self, table: str, columns: list[str]) -> str:
        if self.backend == "mysql":
            placeholders = self.parameter_markers(len(columns))
            col_names = ", ".join(f"`{col}`" for col in columns)
            update_clause = ", ".join(
                f"`{col}`=COALESCE(VALUES(`{col}`), `{table}`.`{col}`)"
                for col in columns
                if col not in {"stock_code", "report_period"}
            )
            return (
                f"INSERT INTO `{table}` ({col_names}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_clause}"
            )
        placeholders = self.parameter_markers(len(columns))
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

    def query(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
        use_cache: bool = True,
        timeout_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float) or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive number")
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

        cursor = None
        mysql_timeout_active = False
        query_error: Exception | None = None
        try:
            if self.backend == "mysql":
                if timeout_seconds is not None:
                    timeout_cursor = self._conn.cursor()
                    try:
                        timeout_cursor.execute(
                            "SET SESSION MAX_EXECUTION_TIME = %s",
                            (max(1, int(timeout_seconds * 1000)),),
                        )
                        mysql_timeout_active = True
                    finally:
                        timeout_cursor.close()
                cursor = self._conn.cursor(dictionary=True)
                cursor.execute(sql, params or ())
                rows = cursor.fetchall()
                result = [dict(row) for row in rows]
            else:
                if timeout_seconds is not None:
                    deadline = time.monotonic() + timeout_seconds
                    self._conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                cursor = self._conn.execute(sql, params or ())
                result = [dict(row) for row in cursor.fetchall()]

            # Store in cache
            if cache_key and self._cache_enabled:
                if len(self._cache) >= self._cache_size:
                    # Simple LRU: remove first item
                    self._cache.pop(next(iter(self._cache)))
                self._cache[cache_key] = result
                self.logger.debug(f"Cache MISS | key={cache_key[:16]}... | rows={len(result)}")

            return result

        except Exception as exc:
            query_error = exc
            self.logger.error(f"Query failed | sql={sql[:100]}... | error={exc}")
            if timeout_seconds is not None and self._is_query_timeout_error(exc):
                raise TimeoutError(f"Database query timed out after {timeout_seconds:g} seconds") from exc
            raise
        finally:
            if self.backend == "sqlite" and timeout_seconds is not None:
                self._conn.set_progress_handler(None, 0)
            if cursor is not None:
                cursor.close()
            if mysql_timeout_active:
                try:
                    reset_cursor = self._conn.cursor()
                    try:
                        reset_cursor.execute("SET SESSION MAX_EXECUTION_TIME = 0")
                    finally:
                        reset_cursor.close()
                except Exception:
                    if query_error is None:
                        raise
                    self.logger.exception("Failed to reset MySQL MAX_EXECUTION_TIME after query failure")

    def _is_query_timeout_error(self, exc: Exception) -> bool:
        if self.backend == "sqlite":
            return isinstance(exc, sqlite3.OperationalError) and "interrupted" in str(exc).lower()
        return getattr(exc, "errno", None) in {1317, 3024}

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

    def _execute_transaction(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.connect()
        self._begin_transaction()
        cursor = self._conn.cursor()
        try:
            for sql, params in statements:
                cursor.execute(sql, params)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()
        self.clear_cache()

    def _begin_transaction(self) -> None:
        if self.backend == "mysql":
            self._conn.start_transaction()
            return
        self._conn.execute("BEGIN IMMEDIATE")

    def _quote_identifier(self, identifier: str) -> str:
        allowed = (
            set(self.schema)
            | {field.field_name for fields in self.schema.values() for field in fields}
            | {FINANCIAL_FACT_TABLE, FINANCIAL_SOURCE_TABLE, "document"}
        )
        if identifier not in allowed:
            raise ValueError(f"Unknown schema identifier: {identifier}")
        return f"`{identifier}`" if self.backend == "mysql" else identifier

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
        return set(self.schema.keys()) | {FINANCIAL_FACT_TABLE, FINANCIAL_SOURCE_TABLE}

    @property
    def allowed_columns(self) -> dict[str, set[str]]:
        columns = {table: {field.field_name for field in fields} for table, fields in self.schema.items()}
        columns[FINANCIAL_FACT_TABLE] = set(FINANCIAL_FACT_COLUMNS) | set(FINANCIAL_FACT_CONTROL_COLUMNS)
        columns[FINANCIAL_SOURCE_TABLE] = set(FINANCIAL_SOURCE_COLUMNS)
        return columns


class FinanceDatabaseFactory:
    """Create request-scoped database objects instead of sharing mutable connections."""

    def __init__(
        self,
        db_path: Path,
        schema: dict[str, list[FieldSpec]],
        db_config: DatabaseConfig | None = None,
        *,
        enable_cache: bool = False,
        cache_size: int = 100,
    ) -> None:
        self.db_path = db_path
        self.schema = schema
        self.db_config = db_config or DatabaseConfig(backend="sqlite", sqlite_path=str(db_path))
        self.enable_cache = enable_cache
        self.cache_size = cache_size

    def create(self) -> FinanceDatabase:
        return FinanceDatabase(
            self.db_path,
            self.schema,
            db_config=self.db_config,
            enable_cache=self.enable_cache,
            cache_size=self.cache_size,
        )

    @contextmanager
    def unit_of_work(self) -> Iterator[FinanceDatabase]:
        database = self.create()
        try:
            yield database
        finally:
            database.close()
