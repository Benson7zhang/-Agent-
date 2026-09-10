from __future__ import annotations

from pathlib import Path

import pytest

from smart_finqa.config import AppConfig, DatabaseConfig, LLMConfig, OCRConfig
from smart_finqa.pipeline import PipelinePaths


def test_database_config_from_env_defaults(monkeypatch) -> None:
    monkeypatch.delenv("DB_BACKEND", raising=False)
    cfg = DatabaseConfig.from_env()
    assert cfg.backend == "sqlite"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 3306


def test_database_config_sqlite_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "x.db"))
    cfg = DatabaseConfig.from_env()
    assert cfg.backend == "sqlite"
    assert cfg.sqlite_path.endswith("x.db")


def test_invalid_environment_values_fail_explicitly(monkeypatch) -> None:
    monkeypatch.setenv("MYSQL_PORT", "not-an-integer")
    with pytest.raises(ValueError, match="MYSQL_PORT"):
        DatabaseConfig.from_env()

    monkeypatch.delenv("MYSQL_PORT")
    monkeypatch.setenv("FULL_DATA", "maybe")
    with pytest.raises(ValueError, match="boolean"):
        AppConfig.from_env()

    monkeypatch.delenv("FULL_DATA")
    monkeypatch.setenv("QUERY_TIMEOUT_SECONDS", "not-a-number")
    with pytest.raises(ValueError, match="QUERY_TIMEOUT_SECONDS"):
        AppConfig.from_env()


def test_partial_llm_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires base_url, api_key, and model"):
        LLMConfig(base_url="https://example.invalid")


def test_embedding_mode_requires_enabled_embedding_service() -> None:
    with pytest.raises(ValueError, match="kb_use_embeddings requires"):
        AppConfig(kb_use_embeddings=True)

    with pytest.raises(ValueError, match="kb_use_embeddings requires"):
        AppConfig(
            kb_use_embeddings=True,
            llm=LLMConfig(
                base_url="https://example.invalid/v1",
                api_key="test-key",
                model="test-model",
            ),
        )


def test_app_config_modes(monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_MODE", "task3")
    app = AppConfig.from_env()
    assert app.mode == "task3"
    assert isinstance(app.llm, LLMConfig)


def test_ocr_config_loads_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("OCR_ENGINE", "rapidocr")
    monkeypatch.setenv("OCR_POLICY", "always")
    monkeypatch.setenv("OCR_DPI", "240")
    monkeypatch.setenv("OCR_MIN_PAGE_TEXT_CHARS", "32")
    monkeypatch.setenv("OCR_MIN_CONFIDENCE", "0.65")
    monkeypatch.setenv("OCR_MAX_PAGE_PIXELS", "30000000")

    config = OCRConfig.from_env()

    assert config.engine == "rapidocr"
    assert config.policy == "always"
    assert config.dpi == 240
    assert config.min_page_text_chars == 32
    assert config.min_confidence == 0.65
    assert config.max_page_pixels == 30_000_000


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"engine": "unknown"}, "engine"),
        ({"policy": "sometimes"}, "policy"),
        ({"dpi": 71}, "dpi"),
        ({"min_page_text_chars": -1}, "min_page_text_chars"),
        ({"min_confidence": 1.1}, "min_confidence"),
        ({"max_page_pixels": 0}, "max_page_pixels"),
    ],
)
def test_ocr_config_rejects_invalid_values(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        OCRConfig(**kwargs)


def test_explicit_missing_config_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        AppConfig.from_file_or_env(tmp_path / "missing.yaml")


def test_from_file_or_env_loads_dotenv_without_overriding_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "LLM_BASE_URL=https://dotenv.example/v1\nLLM_API_KEY=dotenv-secret\nLLM_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_MODEL", "process-model")

    config = AppConfig.from_file_or_env()

    assert config.llm.base_url == "https://dotenv.example/v1"
    assert config.llm.api_key == "dotenv-secret"
    assert config.llm.model == "process-model"


def test_yaml_reads_secrets_from_environment_when_values_are_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MYSQL_PASSWORD", "db-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "database:\n  backend: mysql\n  password: ''\n"
        "llm:\n  base_url: 'https://example.invalid/v1'\n  api_key: ''\n  model: 'test-model'\n",
        encoding="utf-8",
    )

    config = AppConfig.from_yaml(config_path)

    assert config.db.password == "db-secret"
    assert config.llm.api_key == "llm-secret"


def test_yaml_loads_ocr_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "ocr:\n  engine: rapidocr\n  policy: always\n  dpi: 240\n  min_page_text_chars: 25\n"
        "  min_confidence: 0.7\n  max_page_pixels: 30000000\n",
        encoding="utf-8",
    )

    config = AppConfig.from_yaml(config_path)

    assert config.ocr == OCRConfig(
        engine="rapidocr",
        policy="always",
        dpi=240,
        min_page_text_chars=25,
        min_confidence=0.7,
        max_page_pixels=30_000_000,
    )


def test_pipeline_paths_full_data_flag(tmp_path: Path) -> None:
    sample_data = tmp_path / "样例数据"
    sample_data.mkdir()

    p = PipelinePaths.from_base_dir(tmp_path, full_data=False)

    assert p.sample_dir == sample_data


def test_pipeline_paths_prefers_test_dataset_when_full_data(tmp_path: Path) -> None:
    test_data = tmp_path / "数据" / "测试数据"
    formal_data = tmp_path / "数据" / "全量数据"
    schema_name = "附件3：数据库-表名及字段说明.xlsx"
    company_name = "附件1：上市公司基本信息.xlsx"

    (test_data / schema_name).parent.mkdir(parents=True)
    (test_data / schema_name).write_text("schema", encoding="utf-8")
    (test_data / company_name).write_text("company", encoding="utf-8")
    (formal_data / schema_name).parent.mkdir(parents=True)
    (formal_data / schema_name).write_text("schema", encoding="utf-8")

    paths = PipelinePaths.from_base_dir(tmp_path, full_data=True)

    assert paths.sample_dir == test_data


def test_pipeline_paths_prefers_root_formal_dataset_over_generated_samples(tmp_path: Path) -> None:
    schema_name = "附件3：数据库-表名及字段说明.xlsx"
    formal_data = tmp_path / "正式数据"
    generated_sample = tmp_path / "outputs" / "web_smoke" / "样例数据"
    formal_data.mkdir(parents=True)
    generated_sample.mkdir(parents=True)
    (formal_data / schema_name).write_text("formal-schema", encoding="utf-8")
    (generated_sample / schema_name).write_text("sample-schema", encoding="utf-8")

    paths = PipelinePaths.from_base_dir(tmp_path, full_data=True)

    assert paths.sample_dir == formal_data


def test_pipeline_paths_matches_medical_company_workbook(tmp_path: Path) -> None:
    test_data = tmp_path / "数据" / "测试数据"
    schema_name = "附件3：数据库-表名及字段说明.xlsx"
    company_name = "附件1：上市公司基本信息.xlsx"

    test_data.mkdir(parents=True)
    (test_data / schema_name).write_text("schema", encoding="utf-8")
    (test_data / company_name).write_text("company", encoding="utf-8")

    paths = PipelinePaths.from_base_dir(tmp_path, full_data=True)

    assert paths.company_xlsx == test_data / company_name
