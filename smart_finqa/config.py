from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(slots=True)
class DatabaseConfig:
    backend: str = "mysql"
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "smart_finqa"
    sqlite_path: str = ""

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if self.backend not in {"mysql", "sqlite"}:
            raise ValueError(f"Invalid backend: {self.backend}. Must be 'mysql' or 'sqlite'")
        if self.port < 1 or self.port > 65535:
            raise ValueError(f"Invalid port: {self.port}. Must be between 1 and 65535")

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        backend = os.getenv("DB_BACKEND", "mysql").strip().lower()
        try:
            port = int(os.getenv("MYSQL_PORT", "3306"))
        except ValueError:
            port = 3306
        return cls(
            backend=backend,
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=port,
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DB", "smart_finqa"),
            sqlite_path=os.getenv("SQLITE_DB_PATH", ""),
        )


@dataclass(slots=True)
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    embedding_model: str = ""
    timeout_seconds: int = 40

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if self.timeout_seconds < 1 or self.timeout_seconds > 300:
            raise ValueError(f"Invalid timeout: {self.timeout_seconds}. Must be between 1 and 300 seconds")

    @classmethod
    def from_env(cls) -> "LLMConfig":
        try:
            timeout = int(os.getenv("LLM_TIMEOUT_SECONDS", "40"))
        except ValueError:
            timeout = 40
        return cls(
            base_url=os.getenv("LLM_BASE_URL", "").strip(),
            api_key=os.getenv("LLM_API_KEY", "").strip(),
            model=os.getenv("LLM_MODEL", "").strip(),
            embedding_model=os.getenv("EMBEDDING_MODEL", "").strip(),
            timeout_seconds=timeout,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass(slots=True)
class AppConfig:
    mode: str = "all"
    full_data: bool = False
    ingest_workers: int = 4
    incremental_ingest: bool = True
    kb_max_documents: int = 5000
    kb_max_chunks_per_paper: int = 20
    kb_use_embeddings: bool = False
    ingestion_log_limit: int = 300
    log_level: str = "INFO"
    log_file: str = ""
    enable_cache: bool = True
    cache_size: int = 100
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            mode=os.getenv("PIPELINE_MODE", "all").strip().lower(),
            full_data=_to_bool(os.getenv("FULL_DATA"), default=False),
            ingest_workers=max(1, int(os.getenv("INGEST_WORKERS", "4"))),
            incremental_ingest=_to_bool(os.getenv("INCREMENTAL_INGEST"), default=True),
            kb_max_documents=max(200, int(os.getenv("KB_MAX_DOCUMENTS", "5000"))),
            kb_max_chunks_per_paper=max(3, int(os.getenv("KB_MAX_CHUNKS_PER_PAPER", "20"))),
            kb_use_embeddings=_to_bool(os.getenv("KB_USE_EMBEDDINGS"), default=False),
            ingestion_log_limit=max(50, int(os.getenv("INGESTION_LOG_LIMIT", "300"))),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_file=os.getenv("LOG_FILE", ""),
            enable_cache=_to_bool(os.getenv("ENABLE_CACHE"), default=True),
            cache_size=max(10, int(os.getenv("CACHE_SIZE", "100"))),
            db=DatabaseConfig.from_env(),
            llm=LLMConfig.from_env(),
        )

    @classmethod
    def from_yaml(cls, config_path: Path) -> "AppConfig":
        """Load configuration from YAML file."""
        if not YAML_AVAILABLE:
            raise RuntimeError("PyYAML is required for YAML config support. Install with: pip install pyyaml")

        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        # Parse database config
        db_data = data.get("database", {})
        db_config = DatabaseConfig(
            backend=db_data.get("backend", "mysql"),
            host=db_data.get("host", "127.0.0.1"),
            port=db_data.get("port", 3306),
            user=db_data.get("user", "root"),
            password=db_data.get("password", ""),
            database=db_data.get("database", "smart_finqa"),
            sqlite_path=db_data.get("sqlite_path", ""),
        )

        # Parse LLM config
        llm_data = data.get("llm", {})
        llm_config = LLMConfig(
            base_url=llm_data.get("base_url", ""),
            api_key=llm_data.get("api_key", ""),
            model=llm_data.get("model", ""),
            embedding_model=llm_data.get("embedding_model", ""),
            timeout_seconds=llm_data.get("timeout_seconds", 40),
        )

        return cls(
            mode=data.get("mode", "all"),
            full_data=data.get("full_data", False),
            ingest_workers=data.get("ingest_workers", 4),
            incremental_ingest=data.get("incremental_ingest", True),
            kb_max_documents=data.get("kb_max_documents", 5000),
            kb_max_chunks_per_paper=data.get("kb_max_chunks_per_paper", 20),
            kb_use_embeddings=data.get("kb_use_embeddings", False),
            ingestion_log_limit=data.get("ingestion_log_limit", 300),
            log_level=data.get("log_level", "INFO"),
            log_file=data.get("log_file", ""),
            enable_cache=data.get("enable_cache", True),
            cache_size=data.get("cache_size", 100),
            db=db_config,
            llm=llm_config,
        )

    @classmethod
    def from_file_or_env(cls, config_path: Path | None = None) -> "AppConfig":
        """Load config from file if exists, otherwise from environment."""
        if config_path and config_path.exists():
            return cls.from_yaml(config_path)
        return cls.from_env()
