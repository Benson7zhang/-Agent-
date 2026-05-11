from smart_finqa.ingestion import CompanyIndex, _infer_stock_abbr, _infer_stock_code
from tests.helpers import write_company_workbook


def test_infer_company_from_file_name_and_workbook(tmp_path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))
    stock_code = _infer_stock_code("report-600080-20251030-Q3.pdf", "", company_index)
    stock_abbr = _infer_stock_abbr("report-600080-20251030-Q3.pdf", "", stock_code, company_index)
    assert stock_code == "600080"
    assert stock_abbr == "金花股份"
