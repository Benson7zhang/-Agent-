from __future__ import annotations

import os
from pathlib import Path

from smart_finqa.config import AppConfig, DatabaseConfig, LLMConfig
from smart_finqa.pipeline import PipelinePaths


def test_database_config_from_env_defaults(monkeypatch) -> None:
    monkeypatch.delenv("DB_BACKEND", raising=False)
    cfg = DatabaseConfig.from_env()
    assert cfg.backend == "mysql"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 3306


def test_database_config_sqlite_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "x.db"))
    cfg = DatabaseConfig.from_env()
    assert cfg.backend == "sqlite"
    assert cfg.sqlite_path.endswith("x.db")


def test_app_config_modes(monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_MODE", "task3")
    app = AppConfig.from_env()
    assert app.mode == "task3"
    assert isinstance(app.llm, LLMConfig)


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


def test_pipeline_paths_matches_medical_company_workbook(tmp_path: Path) -> None:
    test_data = tmp_path / "数据" / "测试数据"
    schema_name = "附件3：数据库-表名及字段说明.xlsx"
    company_name = "附件1：上市公司基本信息.xlsx"

    test_data.mkdir(parents=True)
    (test_data / schema_name).write_text("schema", encoding="utf-8")
    (test_data / company_name).write_text("company", encoding="utf-8")

    paths = PipelinePaths.from_base_dir(tmp_path, full_data=True)

    assert paths.company_xlsx == test_data / company_name
