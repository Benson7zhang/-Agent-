from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

SCHEMA_FIELDS = {
    "核心业绩指标表": [
        ("serial_number", "序号", "int", "序号"),
        ("stock_code", "股票代码", "varchar", "股票代码"),
        ("stock_abbr", "股票简称", "varchar", "股票简称"),
        ("report_period", "报告期", "varchar", "报告期"),
        ("report_year", "报告年份", "int", "报告年份"),
        ("operating_revenue_qoq_growth", "营业总收入环比增长率", "decimal", "营业总收入环比增长率"),
        ("gross_profit_margin", "销售毛利率", "decimal", "销售毛利率"),
        ("net_profit_margin", "销售净利率", "decimal", "销售净利率"),
        ("roe_weighted_excl_non_recurring", "加权平均净资产收益率（扣非）", "decimal", "加权平均净资产收益率"),
        ("roe", "净资产收益率", "decimal", "净资产收益率"),
        ("net_profit_yoy_growth", "净利润同比增长率", "decimal", "净利润同比增长率"),
    ],
    "资产负债表": [
        ("serial_number", "序号", "int", "序号"),
        ("stock_code", "股票代码", "varchar", "股票代码"),
        ("stock_abbr", "股票简称", "varchar", "股票简称"),
        ("report_period", "报告期", "varchar", "报告期"),
        ("report_year", "报告年份", "int", "报告年份"),
        ("asset_liability_ratio", "资产负债率", "decimal", "资产负债率"),
        ("asset_inventory", "资产-存货（万元）", "decimal", "存货"),
        ("asset_accounts_receivable", "资产-应收账款（万元）", "decimal", "应收账款"),
        ("equity_unappropriated_profit", "股东权益-未分配利润（万元）", "decimal", "未分配利润"),
        ("asset_cash_and_cash_equivalents", "资产-货币资金（万元）", "decimal", "货币资金"),
        ("asset_total_assets", "资产-总资产（万元）", "decimal", "总资产"),
        ("liability_total_liabilities", "负债-总负债（万元）", "decimal", "总负债"),
        ("liability_short_term_loans", "负债-短期借款（万元）", "decimal", "短期借款"),
        ("liability_accounts_payable", "负债-应付账款（万元）", "decimal", "应付账款"),
        ("equity_total_equity", "股东权益-所有者权益合计（万元）", "decimal", "所有者权益合计"),
        ("asset_total_assets_yoy_growth", "总资产同比增长率", "decimal", "总资产同比增长率"),
        ("liability_total_liabilities_yoy_growth", "总负债同比增长率", "decimal", "总负债同比增长率"),
    ],
    "现金流量表": [
        ("serial_number", "序号", "int", "序号"),
        ("stock_code", "股票代码", "varchar", "股票代码"),
        ("stock_abbr", "股票简称", "varchar", "股票简称"),
        ("report_period", "报告期", "varchar", "报告期"),
        ("report_year", "报告年份", "int", "报告年份"),
        ("operating_cf_net_amount", "经营性现金流-现金流量净额（万元）", "decimal", "现金流量净额"),
        ("investing_cf_net_amount", "投资性现金流-现金流量净额（万元）", "decimal", "投资活动现金流量净额"),
    ],
    "利润表": [
        ("serial_number", "序号", "int", "序号"),
        ("stock_code", "股票代码", "varchar", "股票代码"),
        ("stock_abbr", "股票简称", "varchar", "股票简称"),
        ("report_period", "报告期", "varchar", "报告期"),
        ("report_year", "报告年份", "int", "报告年份"),
        ("total_profit", "利润总额（万元）", "decimal", "利润总额"),
        ("net_profit", "净利润（万元）", "decimal", "净利润"),
        ("total_operating_revenue", "营业总收入（万元）", "decimal", "营业总收入"),
        ("operating_revenue_yoy_growth", "营业总收入同比增长率", "decimal", "营业总收入同比增长率"),
        ("operating_expense_rnd_expenses", "营业总支出-研发费用（万元）", "decimal", "研发费用"),
        ("operating_expense_selling_expenses", "营业总支出-销售费用（万元）", "decimal", "销售费用"),
    ],
}


def write_schema_workbook(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    for sheet_name, fields in SCHEMA_FIELDS.items():
        ws = wb.create_sheet(sheet_name)
        ws.append(["字段名称", "中文名称", "字段类型", "字段说明"])
        for field in fields:
            ws.append(list(field))
    wb.save(path)
    return path


def write_company_workbook(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "基本信息表"
    ws.append(["序号", "股票代码", "A股简称", "公司名称"])
    ws.append([1, 600080, "金花股份", "金花股份有限公司"])
    ws.append([2, "000999", "华润三九", "华润三九股份有限公司"])
    ws.append([3, 600332, "白云山", "广州白云山集团股份有限公司"])
    ws.append([4, "002275", "桂林三金", "桂林三金药业股份有限公司"])
    ws.append([5, 300147, "香雪制药", "广州市香雪制药股份有限公司"])
    wb.save(path)
    return path


def create_dataset(base_dir: Path, *, name: str = "样例数据") -> Path:
    data_dir = base_dir / name
    write_schema_workbook(data_dir / "附件3：数据库-表名及字段说明.xlsx")
    write_company_workbook(data_dir / "附件1：上市公司基本信息.xlsx")
    (data_dir / "附件2：财务报告").mkdir(parents=True, exist_ok=True)
    (data_dir / "附件5：研报数据").mkdir(parents=True, exist_ok=True)
    return data_dir
