from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from smart_finqa.llm import ModelCapabilities, ModelResponse
from smart_finqa.llm_extraction import (
    CandidateBoundaryError,
    ExtractionStageExecutor,
    NormalizationError,
    StageResponseValidationError,
    assign_candidate_ids,
    normalize_candidates,
    parse_financial_decimal,
)
from smart_finqa.llm_extraction_contracts import (
    FinancialFactExtractionPayload,
    NormalizationFactPlan,
    RequestManifest,
)
from smart_finqa.llm_prompts import build_runtime_metric_catalog
from smart_finqa.schema import FieldSpec
from smart_finqa.task2 import MetricSpec

SOURCE_HASH = "a" * 64


class _FakeStructuredClient:
    enabled = True
    capabilities = ModelCapabilities()

    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.requests = []

    def invoke(self, model_request, *, max_attempts=3) -> ModelResponse:
        self.requests.append(model_request)
        return ModelResponse(
            response_id="response-1",
            model="fake-model",
            raw_json=self.responses.pop(0),
            usage={},
            latency_ms=1,
            finish_reason="stop",
        )


def _schema() -> dict[str, list[FieldSpec]]:
    return {
        "income_sheet": [
            FieldSpec("serial_number", "序号", "int", ""),
            FieldSpec("stock_code", "股票代码", "varchar", ""),
            FieldSpec("stock_abbr", "股票简称", "varchar", ""),
            FieldSpec("report_period", "报告期", "varchar", ""),
            FieldSpec("report_year", "报告年度", "int", ""),
            FieldSpec("net_profit", "净利润（万元）", "decimal", "净利润，万元"),
        ],
        "balance_sheet": [],
        "cash_flow_sheet": [],
        "core_performance_indicators_sheet": [],
    }


def _metrics() -> dict[str, MetricSpec]:
    return {
        "net_profit": MetricSpec(
            key="net_profit",
            label="净利润",
            table="income_sheet",
            field="net_profit",
            unit="万元",
            aliases=("净利润",),
        )
    }


def _manifest(stage: str, *, allowed_candidate_ids=()) -> RequestManifest:
    catalog = build_runtime_metric_catalog(_schema(), _metrics())
    return RequestManifest(
        request_id="request-1",
        document_id="document-1",
        source_file_name="report.pdf",
        source_content_sha256=SOURCE_HASH,
        stage=stage,
        prompt_version="1.0.0",
        input_scope_id="page-1",
        allowed_region_ids=["region-1"],
        allowed_candidate_ids=list(allowed_candidate_ids),
        extractor_version="test-model-prompt-v1",
        input_locator_type="pdf_page",
        metric_catalog_version=catalog.catalog_version,
        payload={
            "total_pages": 2,
            "confirmed_company_id": "600080",
            "confirmed_stock_code": "600080",
            "confirmed_report_period": "2025FY",
            "evidence_regions": {"region-1": "净利润 1,234.50"},
        },
    )


def _p2_payload(*, region_id: str = "region-1", raw_value: str = "1,234.50") -> dict:
    return {
        "document_id": "document-1",
        "fragment_id": "fragment-1",
        "facts": [
            {
                "document_id": "document-1",
                "document_type": "financial_report",
                "company_id": "600080",
                "company_name": "金花股份",
                "stock_code": "600080",
                "report_period": "2025FY",
                "statement_table": "income_sheet",
                "statement_scope": "consolidated",
                "period_type": "duration",
                "period_semantics": {
                    "as_of_date": None,
                    "period_start": "2025-01-01",
                    "period_end": "2025-12-31",
                    "basis": "annual",
                    "column_role": "current_period",
                },
                "metric": "net_profit",
                "raw_value": raw_value,
                "normalized_value": None,
                "source_unit": "元",
                "target_unit": "万元",
                "currency": "CNY",
                "conversion": None,
                "source": {
                    "source_file_name": "report.pdf",
                    "source_content_sha256": SOURCE_HASH,
                    "locator_type": "pdf_page",
                    "region_id": region_id,
                    "page_no": 1,
                    "paragraph_no": None,
                    "table_name": "合并利润表",
                    "table_index": 1,
                    "row_index": 3,
                    "column_index": 2,
                    "bbox": None,
                    "row_label": "净利润",
                    "column_label": "2025年度",
                    "evidence_text": f"净利润 {raw_value}",
                },
                "row_path": ["净利润"],
                "negative_interpretation": False,
                "confidence": 0.95,
                "candidate_decision": "NEEDS_REVIEW",
                "issues": [],
            }
        ],
        "comparison_values": [],
        "unresolved_rows": [],
        "issues": [],
    }


def _envelope(payload: dict) -> dict:
    return {
        "schema_version": "1.0.0",
        "request_id": "request-1",
        "document_id": "document-1",
        "source_content_sha256": SOURCE_HASH,
        "stage": "financial-fact-extraction",
        "prompt_version": "1.0.0",
        "input_scope_id": "page-1",
        "status": "OK",
        "payload": payload,
        "issues": [],
    }


def _repair_envelope(repaired_response: dict, modified_fields: list[dict], *, status: str = "OK") -> dict:
    return {
        "schema_version": "1.0.0",
        "request_id": "request-1-repair",
        "document_id": "document-1",
        "source_content_sha256": SOURCE_HASH,
        "stage": "schema-repair",
        "prompt_version": "1.0.0",
        "input_scope_id": "page-1-repair",
        "status": status,
        "payload": {
            "repaired_response": repaired_response,
            "modified_fields": modified_fields,
            "rejected_records": [],
            "issues": [],
        },
        "issues": [],
    }


def test_executor_invokes_registered_prompt_and_verifies_evidence() -> None:
    client = _FakeStructuredClient([_envelope(_p2_payload())])
    executor = ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())
    manifest = _manifest("financial-fact-extraction")

    result = executor.execute(
        manifest,
        {"confirmed_metadata": {}, "table_fragment": {}},
        repair_invalid_response=False,
    )

    assert isinstance(result.envelope.payload, FinancialFactExtractionPayload)
    assert client.requests[0].prompt_id == "financial-fact-extraction"
    assert client.requests[0].output_json_schema["properties"]["stage"]["const"] == "financial-fact-extraction"
    assert len(client.requests[0].idempotency_key) == 64
    assert "evidence_regions" not in client.requests[0].input_json["request_manifest"]["payload"]
    assert "evidence_regions" not in client.requests[0].input_json["prompt_sections"]


def test_executor_rejects_region_outside_manifest() -> None:
    client = _FakeStructuredClient([_envelope(_p2_payload(region_id="invented-region"))])
    executor = ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())

    with pytest.raises(CandidateBoundaryError, match="outside the request manifest"):
        executor.execute(
            _manifest("financial-fact-extraction"),
            {"confirmed_metadata": {}, "table_fragment": {}},
            repair_invalid_response=False,
        )


@pytest.mark.parametrize("status", ["PARTIAL", "REFUSED"])
def test_executor_rejects_non_ok_stage_status(status: str) -> None:
    response = _envelope(_p2_payload())
    response["status"] = status
    response.pop("issues")
    client = _FakeStructuredClient([response, _envelope(_p2_payload())])
    executor = ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())

    with pytest.raises(StageResponseValidationError, match=f"non-OK status: {status}"):
        executor.execute(
            _manifest("financial-fact-extraction"),
            {"confirmed_metadata": {}, "table_fragment": {}},
        )
    assert len(client.requests) == 1


@pytest.mark.parametrize("status", ["PARTIAL", "REFUSED"])
def test_executor_rejects_non_ok_schema_repair_status(status: str) -> None:
    invalid = _envelope(_p2_payload())
    invalid.pop("issues")
    repaired = deepcopy(invalid)
    repaired["issues"] = []
    client = _FakeStructuredClient(
        [
            invalid,
            _repair_envelope(
                repaired,
                [
                    {
                        "json_pointer": "/issues",
                        "operation": "initialize_empty_array",
                        "reason": "补充必需问题列表",
                    }
                ],
                status=status,
            ),
        ]
    )
    executor = ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())

    with pytest.raises(StageResponseValidationError, match=f"non-OK status: {status}"):
        executor.execute(
            _manifest("financial-fact-extraction"),
            {"confirmed_metadata": {}, "table_fragment": {}},
        )


def test_schema_repair_can_only_add_missing_empty_structure() -> None:
    invalid = _envelope(_p2_payload())
    invalid.pop("issues")
    repaired = deepcopy(invalid)
    repaired["issues"] = []
    client = _FakeStructuredClient(
        [
            invalid,
            _repair_envelope(
                repaired,
                [
                    {
                        "json_pointer": "/issues",
                        "operation": "initialize_empty_array",
                        "reason": "补充必需问题列表",
                    }
                ],
            ),
        ]
    )
    executor = ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())

    result = executor.execute(
        _manifest("financial-fact-extraction"),
        {"confirmed_metadata": {}, "table_fragment": {}},
    )

    assert result.envelope.status == "OK"
    assert result.repair_response is not None


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("payload", "facts", 0, "negative_interpretation"), True),
        (("payload", "facts", 0, "statement_table"), "balance_sheet"),
        (("payload", "facts", 0, "period_semantics", "basis"), "ytd"),
        (("payload", "facts", 0, "period_semantics", "period_start"), "2025-04-01"),
        (("payload", "facts", 0, "source", "table_name"), "母公司利润表"),
        (("payload", "facts", 0, "source", "row_label"), "利润总额"),
        (("payload", "facts", 0, "source", "column_label"), "2024年度"),
        (
            ("payload", "facts", 0, "issues"),
            [
                {
                    "code": "VALUE_CONFLICT",
                    "severity": "error",
                    "field": "raw_value",
                    "message": "候选值冲突",
                    "evidence_locator": "pdf:p1:table1",
                }
            ],
        ),
    ],
)
def test_schema_repair_cannot_change_existing_financial_or_evidence_content(
    path: tuple[str | int, ...], replacement: object
) -> None:
    invalid = _envelope(_p2_payload())
    invalid.pop("issues")
    repaired = deepcopy(invalid)
    repaired["issues"] = []
    target = repaired
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    json_pointer = "/" + "/".join(str(part) for part in path)
    client = _FakeStructuredClient(
        [
            invalid,
            _repair_envelope(
                repaired,
                [
                    {
                        "json_pointer": json_pointer,
                        "operation": "replace",
                        "reason": "尝试改写现有业务内容",
                    },
                    {
                        "json_pointer": "/issues",
                        "operation": "initialize_empty_array",
                        "reason": "补充必需问题列表",
                    },
                ],
            ),
        ]
    )
    executor = ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())

    with pytest.raises(StageResponseValidationError, match="changed existing response content"):
        executor.execute(
            _manifest("financial-fact-extraction"),
            {"confirmed_metadata": {}, "table_fragment": {}},
        )


def test_executor_rejects_period_boundaries_outside_confirmed_report_period() -> None:
    payload = _p2_payload()
    payload["facts"][0]["period_semantics"]["period_end"] = "2024-12-31"
    client = _FakeStructuredClient([_envelope(payload)])
    executor = ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())

    with pytest.raises(CandidateBoundaryError, match="boundaries conflict"):
        executor.execute(
            _manifest("financial-fact-extraction"),
            {"confirmed_metadata": {}, "table_fragment": {}},
            repair_invalid_response=False,
        )


def test_server_assigns_stable_candidate_id_and_normalizes_once() -> None:
    payload = FinancialFactExtractionPayload.model_validate(_p2_payload())
    manifest = _manifest("financial-fact-extraction")
    assigned = assign_candidate_ids(payload, manifest)
    candidate_id = assigned[0].candidate_id
    plan = NormalizationFactPlan(
        candidate_id=candidate_id,
        raw_value="1,234.50",
        normalized_value=None,
        source_unit="元",
        target_unit="万元",
        currency="CNY",
        report_period="2025FY",
        statement_scope="consolidated",
        period_type="duration",
        conversion={
            "parsed_decimal_proposal": "1234.50",
            "operation": "multiply",
            "factor": "0.0001",
            "rule_id": "CNY_YUAN_TO_WANYUAN_V1",
        },
        candidate_decision="NEEDS_REVIEW",
        issues=[],
    )

    normalized = normalize_candidates(assigned, [plan])

    assert normalized[0].normalized_value == Decimal("0.123450")
    assert normalized[0].conversion_rule_id == "CNY_YUAN_TO_WANYUAN_V1"
    assert candidate_id == assign_candidate_ids(payload, manifest)[0].candidate_id


def test_normalization_rejects_model_factor_change() -> None:
    payload = FinancialFactExtractionPayload.model_validate(_p2_payload())
    assigned = assign_candidate_ids(payload, _manifest("financial-fact-extraction"))
    plan = NormalizationFactPlan(
        candidate_id=assigned[0].candidate_id,
        raw_value="1,234.50",
        normalized_value=None,
        source_unit="元",
        target_unit="万元",
        currency="CNY",
        report_period="2025FY",
        statement_scope="consolidated",
        period_type="duration",
        conversion={
            "parsed_decimal_proposal": "1234.50",
            "operation": "multiply",
            "factor": "1",
            "rule_id": "CNY_YUAN_TO_WANYUAN_V1",
        },
        candidate_decision="NEEDS_REVIEW",
        issues=[],
    )

    with pytest.raises(NormalizationError, match="conflicts with rule registry"):
        normalize_candidates(assigned, [plan])


@pytest.mark.parametrize(
    ("raw", "negative_parentheses", "expected"),
    [
        ("12.3%", False, Decimal("12.3")),
        ("(1,234.50)", True, Decimal("-1234.50")),
        ("-0.25", False, Decimal("-0.25")),
    ],
)
def test_parse_financial_decimal_preserves_financial_display_semantics(
    raw: str, negative_parentheses: bool, expected: Decimal
) -> None:
    assert parse_financial_decimal(raw, negative_parentheses=negative_parentheses) == expected


@pytest.mark.parametrize("raw", ["—", "1,23", "NaN", "inf", "1 234"])
def test_parse_financial_decimal_rejects_ambiguous_or_non_finite_values(raw: str) -> None:
    with pytest.raises(NormalizationError):
        parse_financial_decimal(raw, negative_parentheses=False)
