from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number") from exc


@dataclass(slots=True)
class DatabaseConfig:
    backend: str = "sqlite"
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "smart_finqa"
    sqlite_path: str = ""
    sqlite_busy_timeout_ms: int = 5000

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if self.backend not in {"mysql", "sqlite"}:
            raise ValueError(f"Invalid backend: {self.backend}. Must be 'mysql' or 'sqlite'")
        if self.port < 1 or self.port > 65535:
            raise ValueError(f"Invalid port: {self.port}. Must be between 1 and 65535")
        if (
            isinstance(self.sqlite_busy_timeout_ms, bool)
            or not isinstance(self.sqlite_busy_timeout_ms, int)
            or self.sqlite_busy_timeout_ms < 1
        ):
            raise ValueError("sqlite_busy_timeout_ms must be a positive integer")

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        backend = os.getenv("DB_BACKEND", "sqlite").strip().lower()
        return cls(
            backend=backend,
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=_env_int("MYSQL_PORT", 3306),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DB", "smart_finqa"),
            sqlite_path=os.getenv("SQLITE_DB_PATH", ""),
            sqlite_busy_timeout_ms=_env_int("SQLITE_BUSY_TIMEOUT_MS", 5000),
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
        required_values = (self.base_url, self.api_key, self.model)
        if any(required_values) and not all(required_values):
            raise ValueError("LLM configuration requires base_url, api_key, and model together")
        if self.embedding_model and not all(required_values):
            raise ValueError("embedding_model requires a complete LLM configuration")

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            base_url=os.getenv("LLM_BASE_URL", "").strip(),
            api_key=os.getenv("LLM_API_KEY", "").strip(),
            model=os.getenv("LLM_MODEL", "").strip(),
            embedding_model=os.getenv("EMBEDDING_MODEL", "").strip(),
            timeout_seconds=_env_int("LLM_TIMEOUT_SECONDS", 40),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass(frozen=True, slots=True)
class OCRConfig:
    engine: str = "disabled"
    policy: str = "financial_pages_and_low_text"
    dpi: int = 220
    min_page_text_chars: int = 40
    min_confidence: float = 0.5
    max_page_pixels: int = 40_000_000

    def __post_init__(self) -> None:
        if self.engine not in {"disabled", "rapidocr"}:
            raise ValueError(f"Invalid OCR engine: {self.engine!r}")
        if self.policy not in {"when_page_text_insufficient", "financial_pages_and_low_text", "always"}:
            raise ValueError(f"Invalid OCR policy: {self.policy!r}")
        if isinstance(self.dpi, bool) or not isinstance(self.dpi, int) or not 72 <= self.dpi <= 600:
            raise ValueError("OCR dpi must be an integer between 72 and 600")
        if (
            isinstance(self.min_page_text_chars, bool)
            or not isinstance(self.min_page_text_chars, int)
            or self.min_page_text_chars < 0
        ):
            raise ValueError("OCR min_page_text_chars must be a non-negative integer")
        if (
            isinstance(self.min_confidence, bool)
            or not isinstance(self.min_confidence, int | float)
            or not 0 <= self.min_confidence <= 1
        ):
            raise ValueError("OCR min_confidence must be between 0 and 1")
        if (
            isinstance(self.max_page_pixels, bool)
            or not isinstance(self.max_page_pixels, int)
            or self.max_page_pixels < 1
        ):
            raise ValueError("OCR max_page_pixels must be a positive integer")

    @classmethod
    def from_env(cls) -> "OCRConfig":
        return cls(
            engine=os.getenv("OCR_ENGINE", "disabled").strip().lower(),
            policy=os.getenv("OCR_POLICY", "financial_pages_and_low_text").strip().lower(),
            dpi=_env_int("OCR_DPI", 220),
            min_page_text_chars=_env_int("OCR_MIN_PAGE_TEXT_CHARS", 40),
            min_confidence=_env_float("OCR_MIN_CONFIDENCE", 0.5),
            max_page_pixels=_env_int("OCR_MAX_PAGE_PIXELS", 40_000_000),
        )

    @property
    def enabled(self) -> bool:
        return self.engine != "disabled"


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
    query_timeout_seconds: float = 5.0
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)

    def __post_init__(self) -> None:
        if self.mode not in {"all", "ingest", "task2", "task3"}:
            raise ValueError(f"Invalid pipeline mode: {self.mode!r}")
        positive_fields = (
            "ingest_workers",
            "kb_max_documents",
            "kb_max_chunks_per_paper",
            "ingestion_log_limit",
            "cache_size",
        )
        for field_name in positive_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError(f"Invalid log level: {self.log_level!r}")
        if (
            isinstance(self.query_timeout_seconds, bool)
            or not isinstance(self.query_timeout_seconds, int | float)
            or not 0 < self.query_timeout_seconds <= 300
        ):
            raise ValueError("query_timeout_seconds must be a positive number no greater than 300")
        if self.kb_use_embeddings and (not self.llm.enabled or not self.llm.embedding_model):
            raise ValueError("kb_use_embeddings requires an enabled LLM configuration and embedding_model")

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            mode=os.getenv("PIPELINE_MODE", "all").strip().lower(),
            full_data=_to_bool(os.getenv("FULL_DATA"), default=False),
            ingest_workers=_env_int("INGEST_WORKERS", 4),
            incremental_ingest=_to_bool(os.getenv("INCREMENTAL_INGEST"), default=True),
            kb_max_documents=_env_int("KB_MAX_DOCUMENTS", 5000),
            kb_max_chunks_per_paper=_env_int("KB_MAX_CHUNKS_PER_PAPER", 20),
            kb_use_embeddings=_to_bool(os.getenv("KB_USE_EMBEDDINGS"), default=False),
            ingestion_log_limit=_env_int("INGESTION_LOG_LIMIT", 300),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_file=os.getenv("LOG_FILE", ""),
            enable_cache=_to_bool(os.getenv("ENABLE_CACHE"), default=True),
            cache_size=_env_int("CACHE_SIZE", 100),
            query_timeout_seconds=_env_float("QUERY_TIMEOUT_SECONDS", 5.0),
            db=DatabaseConfig.from_env(),
            llm=LLMConfig.from_env(),
            ocr=OCRConfig.from_env(),
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
            backend=db_data.get("backend", "sqlite"),
            host=db_data.get("host", "127.0.0.1"),
            port=db_data.get("port", 3306),
            user=db_data.get("user", "root"),
            password=db_data.get("password", "") or os.getenv("MYSQL_PASSWORD", ""),
            database=db_data.get("database", "smart_finqa"),
            sqlite_path=db_data.get("sqlite_path", ""),
            sqlite_busy_timeout_ms=db_data.get("sqlite_busy_timeout_ms", 5000),
        )

        # Parse LLM config
        llm_data = data.get("llm", {})
        llm_base_url = str(llm_data.get("base_url", "")).strip()
        llm_model = str(llm_data.get("model", "")).strip()
        llm_api_key = str(llm_data.get("api_key", "")).strip()
        if not llm_api_key and llm_base_url and llm_model:
            llm_api_key = os.getenv("LLM_API_KEY", "").strip()
        llm_config = LLMConfig(
            base_url=llm_base_url,
            api_key=llm_api_key,
            model=llm_model,
            embedding_model=llm_data.get("embedding_model", ""),
            timeout_seconds=llm_data.get("timeout_seconds", 40),
        )

        ocr_data = data.get("ocr", {})
        ocr_config = OCRConfig(
            engine=str(ocr_data.get("engine", "disabled")).strip().lower(),
            policy=str(ocr_data.get("policy", "financial_pages_and_low_text")).strip().lower(),
            dpi=ocr_data.get("dpi", 220),
            min_page_text_chars=ocr_data.get("min_page_text_chars", 40),
            min_confidence=ocr_data.get("min_confidence", 0.5),
            max_page_pixels=ocr_data.get("max_page_pixels", 40_000_000),
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
            query_timeout_seconds=data.get("query_timeout_seconds", 5.0),
            db=db_config,
            llm=llm_config,
            ocr=ocr_config,
        )

    @classmethod
    def from_file_or_env(cls, config_path: Path | None = None) -> "AppConfig":
        """Load project-local .env, then an optional YAML config or environment values."""
        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
        if config_path is not None:
            return cls.from_yaml(config_path)
        return cls.from_env()
