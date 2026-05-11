from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from smart_finqa.config import AppConfig, DatabaseConfig, LLMConfig
from smart_finqa.pipeline import PipelinePaths, SmartFinancePipeline


def _write_schema_workbook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    for sheet_name in ["核心业绩指标表", "资产负债表", "现金流量表", "利润表"]:
        ws = wb.create_sheet(sheet_name)
        ws.append(["字段名称", "中文名称", "字段类型", "字段说明"])
        ws.append(["stock_code", "股票代码", "varchar", "股票代码"])
        ws.append(["report_period", "报告期", "varchar", "报告期"])
    wb.save(path)


def _write_company_workbook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "基本信息表"
    ws.append(["序号", "股票代码", "A股简称", "公司名称"])
    ws.append([1, 600000, "测试公司", "测试公司股份有限公司"])
    wb.save(path)


def test_run_mode_ingest_only(tmp_path: Path) -> None:
    test_data = tmp_path / "数据" / "测试数据"
    _write_schema_workbook(test_data / "附件3：数据库-表名及字段说明.xlsx")
    _write_company_workbook(test_data / "附件1：上市公司基本信息.xlsx")

    paths = PipelinePaths.from_base_dir(tmp_path, full_data=True)
    cfg = AppConfig(
        mode="ingest",
        full_data=True,
        db=DatabaseConfig(backend="sqlite", sqlite_path=str(tmp_path / "finance.db")),
        llm=LLMConfig(),
    )
    pipeline = SmartFinancePipeline(paths, app_config=cfg)
    pipeline.ingest_reports = lambda: None
    pipeline.derive_missing_yoy = lambda: None
    pipeline.validate_database = lambda: {"ok": True}

    outputs = pipeline.run(mode="ingest")

    assert "db_path" in outputs
    assert outputs["result_2"] == ""
    assert outputs["result_3"] == ""
