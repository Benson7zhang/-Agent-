import json
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from smart_finqa.evaluation import (
    SCHEMA_VERSION,
    EvaluationValidationError,
    FactKey,
    evaluate,
    evaluate_files,
    load_golden_dataset,
    load_predictions,
    write_evaluation_report,
)

FIXTURES = Path(__file__).parent / "fixtures" / "evaluation"


def test_synthetic_evaluation_reports_metrics_and_scope() -> None:
    report = evaluate_files(
        FIXTURES / "synthetic_manifest.json",
        FIXTURES / "synthetic_predictions.jsonl",
    )

    assert report.status == "completed"
    assert report.dataset_kind == "synthetic"
    assert report.evidence_scope == "synthetic_contract_only"
    assert report.provenance_verified is False
    assert "not real-world accuracy evidence" in report.message
    assert report.metrics["field_recall"].value == 0.5
    assert report.metrics["cell_accuracy"].value == 0.5
    assert report.metrics["fact_precision"].value == 1.0
    assert report.metrics["question_accuracy"].value == 1.0
    assert report.metrics["source_location_accuracy"].value == pytest.approx(1 / 3)

    reasons = [failure.reason for failure in report.failures]
    assert reasons.count("missing_prediction") == 1
    assert reasons.count("source_location_mismatch") == 1


def test_report_serialization_keeps_synthetic_disclaimer(tmp_path: Path) -> None:
    report = evaluate_files(
        FIXTURES / "synthetic_manifest.json",
        FIXTURES / "synthetic_predictions.jsonl",
    )
    output_path = tmp_path / "report.json"

    write_evaluation_report(report, output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["is_synthetic"] is True
    assert payload["provenance_verified"] is False
    assert payload["evidence_scope"] == "synthetic_contract_only"
    assert payload["metrics"]["question_accuracy"]["value"] == 1.0


def test_unavailable_real_dataset_has_no_accuracy_claims(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset_id": "real-golden-v1",
                "dataset_kind": "real",
                "availability": "unavailable",
                "description": "Reserved contract for the first real golden dataset.",
                "annotation_files": [],
                "unavailable_reason": "No licensed reports have been annotated.",
            }
        ),
        encoding="utf-8",
    )

    report = evaluate(load_golden_dataset(manifest_path))

    assert report.status == "unavailable"
    assert report.evidence_scope == "none"
    assert report.provenance_verified is False
    assert all(metric.status == "unavailable" for metric in report.metrics.values())
    assert all(metric.value is None for metric in report.metrics.values())
    assert report.failures[0].reason == "dataset_unavailable"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": "2.0"}, "unsupported schema_version"),
        ({"dataset_kind": "unknown"}, "expected one of"),
        ({"availability": "available", "annotation_files": []}, "requires annotation_files"),
    ],
)
def test_manifest_validation_is_explicit(tmp_path: Path, mutation: dict[str, object], message: str) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "real-golden-v1",
        "dataset_kind": "real",
        "availability": "unavailable",
        "description": "Unavailable fixture.",
        "annotation_files": [],
        "unavailable_reason": "Not collected.",
    }
    payload.update(mutation)
    if payload["availability"] == "available":
        payload.pop("unavailable_reason")
        payload["numeric_tolerance"] = {"absolute": "0", "relative": "0"}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationValidationError, match=message):
        load_golden_dataset(manifest_path)


def test_annotation_rejects_unknown_fields(tmp_path: Path) -> None:
    annotation_path = tmp_path / "annotations.jsonl"
    annotation_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "financial_fact",
                "record_id": "fact-1",
                "company_id": "SYNTH-001",
                "period": "2024FY",
                "statement_scope": "consolidated",
                "period_type": "closing",
                "metric": "total_assets",
                "normalized_value": "1",
                "unit": "CNY_10K",
                "currency": "CNY",
                "source": {"source_file": "report.pdf", "page_no": 1},
                "unexpected": "must fail",
            }
        ),
        encoding="utf-8",
    )
    manifest_path = _write_available_manifest(tmp_path, annotation_path.name)

    with pytest.raises(EvaluationValidationError, match="unknown fields: unexpected"):
        load_golden_dataset(manifest_path)


def test_annotation_file_cannot_escape_manifest_directory(tmp_path: Path) -> None:
    manifest_path = _write_available_manifest(tmp_path, "../annotations.jsonl")

    with pytest.raises(EvaluationValidationError, match="escapes manifest directory"):
        load_golden_dataset(manifest_path)


def test_predictions_must_target_the_same_dataset(tmp_path: Path) -> None:
    dataset = load_golden_dataset(FIXTURES / "synthetic_manifest.json")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "prediction_set",
        "dataset_id": "different-dataset",
        "predictions": [],
    }
    predictions_path = tmp_path / "predictions.json"
    predictions_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationValidationError, match="does not match"):
        evaluate(dataset, load_predictions(predictions_path))


def test_duplicate_fact_prediction_keys_fail_instead_of_selecting_one(tmp_path: Path) -> None:
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "financial_fact_prediction",
        "prediction_id": "prediction-1",
        "company_id": "SYNTH-001",
        "period": "2024FY",
        "statement_scope": "consolidated",
        "period_type": "current",
        "metric": "operating_revenue",
        "normalized_value": "1250",
        "unit": "CNY_10K",
        "currency": "CNY",
    }
    duplicate = {**record, "prediction_id": "prediction-2"}
    predictions_path = tmp_path / "predictions.json"
    predictions_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "prediction_set",
                "dataset_id": "synthetic-evaluator-contract-v1",
                "predictions": [record, duplicate],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationValidationError, match="duplicate predicted financial fact key"):
        load_predictions(predictions_path)


def test_real_dataset_requires_verified_provenance(tmp_path: Path) -> None:
    annotation_path = _write_annotation_copy(tmp_path / "annotations.jsonl", source_file="source/report.pdf")
    payload = _available_manifest_payload(annotation_path.name, dataset_kind="real")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationValidationError, match="provenance"):
        load_golden_dataset(manifest_path)


def test_verified_real_dataset_checks_source_and_annotation_hashes(tmp_path: Path) -> None:
    source_path = _write_source_fixture(tmp_path)
    annotation_path = _write_annotation_copy(tmp_path / "annotations.jsonl", source_file="source/report.pdf")
    manifest_path = _write_real_manifest(tmp_path, source_path, annotation_path)

    dataset = load_golden_dataset(manifest_path)
    report = evaluate(dataset)

    assert dataset.provenance_verified is True
    assert report.provenance_verified is True
    assert report.evidence_scope == "verified_real_dataset"

    source_path.write_bytes(b"tampered report")
    with pytest.raises(EvaluationValidationError, match="SHA-256 mismatch"):
        load_golden_dataset(manifest_path)

    source_path.write_bytes(b"licensed annual report fixture")
    annotation_path.write_text(annotation_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(EvaluationValidationError, match="SHA-256 mismatch"):
        load_golden_dataset(manifest_path)


def test_unapproved_real_dataset_is_rejected(tmp_path: Path) -> None:
    source_path = _write_source_fixture(tmp_path)
    annotation_path = _write_annotation_copy(tmp_path / "annotations.jsonl", source_file="source/report.pdf")
    manifest_path = _write_real_manifest(tmp_path, source_path, annotation_path, review_status="pending")

    with pytest.raises(EvaluationValidationError, match="review_status"):
        load_golden_dataset(manifest_path)


def test_extra_fact_prediction_reduces_precision() -> None:
    dataset = load_golden_dataset(FIXTURES / "synthetic_manifest.json")
    predictions = load_predictions(FIXTURES / "synthetic_predictions.jsonl")
    extra = replace(
        predictions.facts[0],
        prediction_id="pred-extra",
        key=FactKey("SYNTH-002", "2024FY", "consolidated", "current", "operating_revenue"),
    )

    report = evaluate(dataset, replace(predictions, facts=(*predictions.facts, extra)))

    assert report.metrics["fact_precision"].value == 0.5
    unexpected = next(failure for failure in report.failures if failure.record_id == "pred-extra")
    assert unexpected.metrics == ("fact_precision",)


def test_source_location_checks_table_row_and_column_labels() -> None:
    dataset = load_golden_dataset(FIXTURES / "synthetic_manifest.json")
    predictions = load_predictions(FIXTURES / "synthetic_predictions.jsonl")
    fact = predictions.facts[0]
    wrong_source = replace(fact.source, row_label="Another revenue row")

    report = evaluate(dataset, replace(predictions, facts=(replace(fact, source=wrong_source),)))

    assert report.metrics["source_location_accuracy"].value == 0.0
    assert any(
        failure.record_id == "fact-revenue" and failure.reason == "source_location_mismatch"
        for failure in report.failures
    )


def test_value_and_answer_mismatches_are_reported_outside_tolerance() -> None:
    dataset = load_golden_dataset(FIXTURES / "synthetic_manifest.json")
    predictions = load_predictions(FIXTURES / "synthetic_predictions.jsonl")
    fact = replace(predictions.facts[0], normalized_value=Decimal("1250.0101"))
    answer = replace(predictions.answers[0], answer="1250.0101")

    report = evaluate(dataset, replace(predictions, facts=(fact,), answers=(answer,)))

    assert report.metrics["cell_accuracy"].value == 0.0
    assert report.metrics["question_accuracy"].value == 0.0
    assert {failure.reason for failure in report.failures} >= {
        "value_or_dimension_mismatch",
        "answer_mismatch",
    }


def test_absolute_and_relative_tolerance_boundaries_are_inclusive(tmp_path: Path) -> None:
    manifest_payload = json.loads((FIXTURES / "synthetic_manifest.json").read_text(encoding="utf-8"))
    manifest_payload["numeric_tolerance"] = {"absolute": "0.01", "relative": "0.001"}
    manifest_payload["annotation_files"] = ["annotations.jsonl"]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
    _write_annotation_copy(tmp_path / "annotations.jsonl", source_file="synthetic/report.pdf")
    dataset = load_golden_dataset(tmp_path / "manifest.json")
    predictions = load_predictions(FIXTURES / "synthetic_predictions.jsonl")
    fact = replace(predictions.facts[0], normalized_value=Decimal("1251.25"))

    report = evaluate(dataset, replace(predictions, facts=(fact,)))

    assert report.metrics["cell_accuracy"].value == 0.5


def test_json_annotation_array_is_supported(tmp_path: Path) -> None:
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(json.dumps(_fixture_annotation_records()), encoding="utf-8")
    manifest_path = _write_available_manifest(tmp_path, annotation_path.name)

    dataset = load_golden_dataset(manifest_path)

    assert len(dataset.facts) == 2
    assert len(dataset.questions) == 1


def test_non_finite_json_number_is_rejected(tmp_path: Path) -> None:
    records = _fixture_annotation_records()
    records[-1]["expected_answer"] = float("nan")
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(json.dumps(records), encoding="utf-8")
    manifest_path = _write_available_manifest(tmp_path, annotation_path.name)

    with pytest.raises(EvaluationValidationError, match="non-finite JSON number"):
        load_golden_dataset(manifest_path)


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        f'{{"schema_version":"{SCHEMA_VERSION}","dataset_id":"one","dataset_id":"two"}}',
        encoding="utf-8",
    )

    with pytest.raises(EvaluationValidationError, match="duplicate JSON key"):
        load_golden_dataset(manifest_path)


def _write_available_manifest(directory: Path, annotation_file: str) -> Path:
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(_available_manifest_payload(annotation_file)), encoding="utf-8")
    return manifest_path


def _available_manifest_payload(annotation_file: str, *, dataset_kind: str = "synthetic") -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "dataset-v1",
        "dataset_kind": dataset_kind,
        "availability": "available",
        "description": "Validation fixture.",
        "annotation_files": [annotation_file],
        "numeric_tolerance": {"absolute": "0", "relative": "0"},
    }


def _fixture_annotation_records() -> list[dict[str, object]]:
    return [
        json.loads(line) for line in (FIXTURES / "synthetic_annotations.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _write_annotation_copy(path: Path, *, source_file: str) -> Path:
    records = _fixture_annotation_records()
    for record in records:
        source = record["source"]
        assert isinstance(source, dict)
        source["source_file"] = source_file
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def _write_source_fixture(directory: Path) -> Path:
    source_path = directory / "source" / "report.pdf"
    source_path.parent.mkdir()
    source_path.write_bytes(b"licensed annual report fixture")
    return source_path


def _write_real_manifest(
    directory: Path,
    source_path: Path,
    annotation_path: Path,
    *,
    review_status: str = "approved",
) -> Path:
    payload = _available_manifest_payload(annotation_path.name, dataset_kind="real")
    payload["provenance"] = {
        "annotation_version": "1.0.0",
        "review_status": review_status,
        "reviewers": ["reviewer-a", "reviewer-b"],
        "reviewed_at": "2026-09-06T12:00:00+08:00",
        "source_license": "internal-evaluation-only",
        "source_artifacts": [
            {
                "path": source_path.relative_to(directory).as_posix(),
                "sha256": sha256(source_path.read_bytes()).hexdigest(),
            }
        ],
        "annotation_artifacts": [
            {
                "path": annotation_path.relative_to(directory).as_posix(),
                "sha256": sha256(annotation_path.read_bytes()).hexdigest(),
            }
        ],
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path
