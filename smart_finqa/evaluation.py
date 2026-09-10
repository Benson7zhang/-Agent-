"""Versioned golden-dataset contracts and deterministic evaluation metrics."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.1"
DATASET_KINDS = frozenset({"real", "synthetic"})
AVAILABILITY_VALUES = frozenset({"available", "unavailable"})


class EvaluationValidationError(ValueError):
    """Raised when an evaluation artifact violates the versioned contract."""


@dataclass(frozen=True)
class NumericTolerance:
    absolute: Decimal
    relative: Decimal


@dataclass(frozen=True)
class SourceLocation:
    source_file: str
    page_no: int
    table_name: str | None = None
    row_label: str | None = None
    column_label: str | None = None


@dataclass(frozen=True)
class FactKey:
    company_id: str
    period: str
    statement_scope: str
    period_type: str
    metric: str


@dataclass(frozen=True)
class FactAnnotation:
    record_id: str
    key: FactKey
    normalized_value: Decimal
    unit: str
    currency: str
    source: SourceLocation


@dataclass(frozen=True)
class QuestionAnnotation:
    record_id: str
    question: str
    answer_type: str
    expected_answer: str | int | float
    source: SourceLocation | None = None


@dataclass(frozen=True)
class ArtifactDigest:
    path: str
    sha256: str


@dataclass(frozen=True)
class DatasetProvenance:
    annotation_version: str
    review_status: str
    reviewers: tuple[str, ...]
    reviewed_at: str
    source_license: str
    source_artifacts: tuple[ArtifactDigest, ...]
    annotation_artifacts: tuple[ArtifactDigest, ...]


@dataclass(frozen=True)
class GoldenManifest:
    dataset_id: str
    dataset_kind: str
    availability: str
    description: str
    annotation_files: tuple[str, ...]
    tolerance: NumericTolerance | None
    unavailable_reason: str | None = None
    provenance: DatasetProvenance | None = None


@dataclass(frozen=True)
class GoldenDataset:
    manifest: GoldenManifest
    facts: tuple[FactAnnotation, ...] = ()
    questions: tuple[QuestionAnnotation, ...] = ()
    provenance_verified: bool = False


@dataclass(frozen=True)
class FactPrediction:
    prediction_id: str
    key: FactKey
    normalized_value: Decimal
    unit: str
    currency: str
    source: SourceLocation | None = None


@dataclass(frozen=True)
class AnswerPrediction:
    prediction_id: str
    question_id: str
    answer: str | int | float
    source: SourceLocation | None = None


@dataclass(frozen=True)
class EvaluationPredictions:
    dataset_id: str
    facts: tuple[FactPrediction, ...] = ()
    answers: tuple[AnswerPrediction, ...] = ()


@dataclass(frozen=True)
class MetricResult:
    numerator: int
    denominator: int

    @property
    def status(self) -> str:
        return "available" if self.denominator else "unavailable"

    @property
    def value(self) -> float | None:
        if not self.denominator:
            return None
        return self.numerator / self.denominator

    def to_dict(self) -> dict[str, int | float | str | None]:
        return {
            "status": self.status,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "value": self.value,
        }


@dataclass(frozen=True)
class EvaluationFailure:
    record_type: str
    record_id: str
    metrics: tuple[str, ...]
    reason: str
    expected: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "metrics": list(self.metrics),
            "reason": self.reason,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class EvaluationReport:
    dataset_id: str
    dataset_kind: str
    status: str
    evidence_scope: str
    message: str
    provenance_verified: bool = False
    metrics: dict[str, MetricResult] = field(default_factory=dict)
    failures: tuple[EvaluationFailure, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": self.dataset_id,
            "dataset_kind": self.dataset_kind,
            "is_synthetic": self.dataset_kind == "synthetic",
            "status": self.status,
            "evidence_scope": self.evidence_scope,
            "message": self.message,
            "provenance_verified": self.provenance_verified,
            "metrics": {name: result.to_dict() for name, result in self.metrics.items()},
            "failures": [failure.to_dict() for failure in self.failures],
        }


def load_golden_dataset(manifest_path: str | Path) -> GoldenDataset:
    """Load and validate a golden manifest plus its JSON/JSONL annotations."""
    path = Path(manifest_path)
    manifest_data = _load_json_object(path)
    manifest = _parse_manifest(manifest_data, path)
    if manifest.availability == "unavailable":
        return GoldenDataset(manifest=manifest)

    facts: list[FactAnnotation] = []
    questions: list[QuestionAnnotation] = []
    root = path.resolve().parent
    for relative_name in manifest.annotation_files:
        annotation_path = _resolve_dataset_file(root, relative_name)
        for index, record in enumerate(_load_record_collection(annotation_path), start=1):
            context = f"{annotation_path}:{index}"
            record_type = _required_string(record, "record_type", context)
            if record_type == "financial_fact":
                facts.append(_parse_fact_annotation(record, context))
            elif record_type == "question_answer":
                questions.append(_parse_question_annotation(record, context))
            else:
                raise EvaluationValidationError(f"{context}: unsupported record_type {record_type!r}")

    if not facts and not questions:
        raise EvaluationValidationError(f"{path}: available datasets must contain at least one annotation")
    dataset = GoldenDataset(manifest=manifest, facts=tuple(facts), questions=tuple(questions))
    _validate_golden_uniqueness(dataset)
    if manifest.dataset_kind == "real":
        _verify_real_dataset_provenance(dataset, root)
        dataset = GoldenDataset(
            manifest=manifest,
            facts=dataset.facts,
            questions=dataset.questions,
            provenance_verified=True,
        )
    return dataset


def load_predictions(predictions_path: str | Path) -> EvaluationPredictions:
    """Load a prediction set from a JSON object or header-first JSONL file."""
    path = Path(predictions_path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = _load_json_object(path)
        _expect_keys(
            payload,
            required={"schema_version", "record_type", "dataset_id", "predictions"},
            context=str(path),
        )
        records = payload["predictions"]
        if not isinstance(records, list):
            raise EvaluationValidationError(f"{path}: predictions must be a JSON array")
        header = payload
    elif suffix == ".jsonl":
        records = _load_record_collection(path)
        if not records:
            raise EvaluationValidationError(f"{path}: prediction JSONL must contain a header")
        header, *records = records
        _expect_keys(
            header,
            required={"schema_version", "record_type", "dataset_id"},
            context=f"{path}:1",
        )
    else:
        raise EvaluationValidationError(f"{path}: prediction file must use .json or .jsonl")

    _require_schema_version(header, str(path))
    if header["record_type"] != "prediction_set":
        raise EvaluationValidationError(f"{path}: prediction header record_type must be 'prediction_set'")
    dataset_id = _required_string(header, "dataset_id", str(path))

    facts: list[FactPrediction] = []
    answers: list[AnswerPrediction] = []
    for index, record in enumerate(records, start=1):
        context = f"{path}:prediction:{index}"
        if not isinstance(record, dict):
            raise EvaluationValidationError(f"{context}: prediction must be a JSON object")
        record_type = _required_string(record, "record_type", context)
        if record_type == "financial_fact_prediction":
            facts.append(_parse_fact_prediction(record, context))
        elif record_type == "question_answer_prediction":
            answers.append(_parse_answer_prediction(record, context))
        else:
            raise EvaluationValidationError(f"{context}: unsupported record_type {record_type!r}")

    predictions = EvaluationPredictions(dataset_id=dataset_id, facts=tuple(facts), answers=tuple(answers))
    _validate_prediction_uniqueness(predictions)
    return predictions


def evaluate(
    dataset: GoldenDataset,
    predictions: EvaluationPredictions | None = None,
) -> EvaluationReport:
    """Evaluate field coverage, fact values, answers, and file/page provenance."""
    _validate_golden_uniqueness(dataset)
    predictions = predictions or EvaluationPredictions(dataset_id=dataset.manifest.dataset_id)
    _validate_prediction_uniqueness(predictions)
    if predictions.dataset_id != dataset.manifest.dataset_id:
        raise EvaluationValidationError(
            f"prediction dataset_id {predictions.dataset_id!r} does not match {dataset.manifest.dataset_id!r}"
        )

    if dataset.manifest.availability == "unavailable":
        reason = dataset.manifest.unavailable_reason or "golden dataset is unavailable"
        return EvaluationReport(
            dataset_id=dataset.manifest.dataset_id,
            dataset_kind=dataset.manifest.dataset_kind,
            status="unavailable",
            evidence_scope="none",
            message=f"Real evaluation unavailable: {reason}",
            provenance_verified=False,
            metrics=_empty_metrics(),
            failures=(
                EvaluationFailure(
                    record_type="dataset",
                    record_id=dataset.manifest.dataset_id,
                    metrics=tuple(_empty_metrics()),
                    reason="dataset_unavailable",
                    expected={"availability": "available"},
                    actual={"availability": "unavailable", "reason": reason},
                ),
            ),
        )

    if dataset.manifest.dataset_kind == "real" and not dataset.provenance_verified:
        raise EvaluationValidationError("real dataset provenance must be verified by load_golden_dataset")

    tolerance = dataset.manifest.tolerance
    if tolerance is None:
        raise EvaluationValidationError("available dataset is missing numeric tolerance")

    failures: list[EvaluationFailure] = []
    predicted_facts = {prediction.key: prediction for prediction in predictions.facts}
    predicted_answers = {prediction.question_id: prediction for prediction in predictions.answers}
    field_hits = 0
    cell_hits = 0
    question_hits = 0
    source_hits = 0
    source_total = 0

    for annotation in dataset.facts:
        prediction = predicted_facts.get(annotation.key)
        source_total += 1
        if prediction is None:
            failures.append(
                EvaluationFailure(
                    record_type="financial_fact",
                    record_id=annotation.record_id,
                    metrics=("field_recall", "cell_accuracy", "source_location_accuracy"),
                    reason="missing_prediction",
                    expected=_fact_expected(annotation),
                )
            )
            continue

        field_hits += 1
        if _fact_value_matches(annotation, prediction, tolerance):
            cell_hits += 1
        else:
            failures.append(
                EvaluationFailure(
                    record_type="financial_fact",
                    record_id=annotation.record_id,
                    metrics=("cell_accuracy", "fact_precision"),
                    reason="value_or_dimension_mismatch",
                    expected=_fact_expected(annotation),
                    actual=_fact_actual(prediction),
                )
            )

        if _source_matches(annotation.source, prediction.source):
            source_hits += 1
        else:
            failures.append(
                EvaluationFailure(
                    record_type="financial_fact",
                    record_id=annotation.record_id,
                    metrics=("source_location_accuracy",),
                    reason="source_location_mismatch",
                    expected=_source_dict(annotation.source),
                    actual=_source_dict(prediction.source),
                )
            )

    for annotation in dataset.questions:
        prediction = predicted_answers.get(annotation.record_id)
        if annotation.source is not None:
            source_total += 1
        if prediction is None:
            affected_metrics = ["question_accuracy"]
            if annotation.source is not None:
                affected_metrics.append("source_location_accuracy")
            failures.append(
                EvaluationFailure(
                    record_type="question_answer",
                    record_id=annotation.record_id,
                    metrics=tuple(affected_metrics),
                    reason="missing_prediction",
                    expected={"answer": annotation.expected_answer, "answer_type": annotation.answer_type},
                )
            )
            continue

        if _answer_matches(annotation, prediction, tolerance):
            question_hits += 1
        else:
            failures.append(
                EvaluationFailure(
                    record_type="question_answer",
                    record_id=annotation.record_id,
                    metrics=("question_accuracy", "answer_precision"),
                    reason="answer_mismatch",
                    expected={"answer": annotation.expected_answer, "answer_type": annotation.answer_type},
                    actual={"answer": prediction.answer},
                )
            )

        if annotation.source is not None:
            if _source_matches(annotation.source, prediction.source):
                source_hits += 1
            else:
                failures.append(
                    EvaluationFailure(
                        record_type="question_answer",
                        record_id=annotation.record_id,
                        metrics=("source_location_accuracy",),
                        reason="source_location_mismatch",
                        expected=_source_dict(annotation.source),
                        actual=_source_dict(prediction.source),
                    )
                )

    failures.extend(_unexpected_prediction_failures(dataset, predictions))
    metrics = {
        "cell_accuracy": MetricResult(cell_hits, len(dataset.facts)),
        "fact_precision": MetricResult(cell_hits, len(predictions.facts)),
        "field_recall": MetricResult(field_hits, len(dataset.facts)),
        "question_accuracy": MetricResult(question_hits, len(dataset.questions)),
        "answer_precision": MetricResult(question_hits, len(predictions.answers)),
        "source_location_accuracy": MetricResult(source_hits, source_total),
    }
    synthetic = dataset.manifest.dataset_kind == "synthetic"
    return EvaluationReport(
        dataset_id=dataset.manifest.dataset_id,
        dataset_kind=dataset.manifest.dataset_kind,
        status="completed",
        evidence_scope="synthetic_contract_only" if synthetic else "verified_real_dataset",
        message=(
            "Synthetic results validate evaluator mechanics only and are not real-world accuracy evidence."
            if synthetic
            else "Metrics were computed after source-file hash verification; annotation review remains a manifest attestation."
        ),
        provenance_verified=dataset.provenance_verified,
        metrics=metrics,
        failures=tuple(failures),
    )


def evaluate_files(
    manifest_path: str | Path,
    predictions_path: str | Path | None = None,
) -> EvaluationReport:
    """Load artifacts and evaluate them through the same validated contract."""
    dataset = load_golden_dataset(manifest_path)
    predictions = load_predictions(predictions_path) if predictions_path is not None else None
    return evaluate(dataset, predictions)


def write_evaluation_report(report: EvaluationReport, output_path: str | Path) -> None:
    """Write a stable, machine-readable evaluation report."""
    Path(output_path).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_manifest(data: dict[str, Any], path: Path) -> GoldenManifest:
    context = str(path)
    _expect_keys(
        data,
        required={
            "schema_version",
            "dataset_id",
            "dataset_kind",
            "availability",
            "description",
            "annotation_files",
        },
        optional={"numeric_tolerance", "unavailable_reason", "provenance"},
        context=context,
    )
    _require_schema_version(data, context)
    dataset_id = _required_string(data, "dataset_id", context)
    dataset_kind = _required_choice(data, "dataset_kind", DATASET_KINDS, context)
    availability = _required_choice(data, "availability", AVAILABILITY_VALUES, context)
    description = _required_string(data, "description", context)
    annotation_files = _string_list(data["annotation_files"], f"{context}.annotation_files")

    tolerance_data = data.get("numeric_tolerance")
    unavailable_reason = data.get("unavailable_reason")
    provenance_data = data.get("provenance")
    if availability == "available":
        if not annotation_files:
            raise EvaluationValidationError(f"{context}: available dataset requires annotation_files")
        tolerance = _parse_tolerance(tolerance_data, context)
        if unavailable_reason is not None:
            raise EvaluationValidationError(f"{context}: available dataset cannot declare unavailable_reason")
        if dataset_kind == "real":
            provenance = _parse_provenance(provenance_data, context)
        elif provenance_data is not None:
            raise EvaluationValidationError(f"{context}: synthetic dataset cannot declare real-data provenance")
        else:
            provenance = None
    else:
        if annotation_files:
            raise EvaluationValidationError(f"{context}: unavailable dataset cannot declare annotation_files")
        if tolerance_data is not None:
            raise EvaluationValidationError(f"{context}: unavailable dataset cannot declare numeric_tolerance")
        if not isinstance(unavailable_reason, str) or not unavailable_reason.strip():
            raise EvaluationValidationError(f"{context}: unavailable dataset requires unavailable_reason")
        tolerance = None
        unavailable_reason = unavailable_reason.strip()
        if provenance_data is not None:
            raise EvaluationValidationError(f"{context}: unavailable dataset cannot declare provenance")
        provenance = None

    return GoldenManifest(
        dataset_id=dataset_id,
        dataset_kind=dataset_kind,
        availability=availability,
        description=description,
        annotation_files=tuple(annotation_files),
        tolerance=tolerance,
        unavailable_reason=unavailable_reason,
        provenance=provenance,
    )


def _parse_provenance(data: Any, context: str) -> DatasetProvenance:
    provenance_context = f"{context}.provenance"
    if not isinstance(data, dict):
        raise EvaluationValidationError(f"{context}: available real dataset requires provenance object")
    _expect_keys(
        data,
        required={
            "annotation_version",
            "review_status",
            "reviewers",
            "reviewed_at",
            "source_license",
            "source_artifacts",
            "annotation_artifacts",
        },
        context=provenance_context,
    )
    review_status = _required_string(data, "review_status", provenance_context)
    if review_status != "approved":
        raise EvaluationValidationError(f"{provenance_context}.review_status: expected 'approved'")
    reviewers = tuple(_string_list(data["reviewers"], f"{provenance_context}.reviewers"))
    if len(set(reviewers)) < 2:
        raise EvaluationValidationError(f"{provenance_context}.reviewers: at least two distinct reviewers are required")
    reviewed_at = _required_string(data, "reviewed_at", provenance_context)
    try:
        timestamp = datetime.fromisoformat(reviewed_at)
    except ValueError as exc:
        raise EvaluationValidationError(f"{provenance_context}.reviewed_at: expected ISO-8601 timestamp") from exc
    if timestamp.tzinfo is None:
        raise EvaluationValidationError(f"{provenance_context}.reviewed_at: timezone offset is required")
    return DatasetProvenance(
        annotation_version=_required_string(data, "annotation_version", provenance_context),
        review_status=review_status,
        reviewers=reviewers,
        reviewed_at=reviewed_at,
        source_license=_required_string(data, "source_license", provenance_context),
        source_artifacts=_parse_artifact_digests(data["source_artifacts"], f"{provenance_context}.source_artifacts"),
        annotation_artifacts=_parse_artifact_digests(
            data["annotation_artifacts"],
            f"{provenance_context}.annotation_artifacts",
        ),
    )


def _parse_artifact_digests(data: Any, context: str) -> tuple[ArtifactDigest, ...]:
    if not isinstance(data, list) or not data:
        raise EvaluationValidationError(f"{context}: expected a non-empty array")
    artifacts: list[ArtifactDigest] = []
    for index, item in enumerate(data, start=1):
        item_context = f"{context}:{index}"
        if not isinstance(item, dict):
            raise EvaluationValidationError(f"{item_context}: artifact must be a JSON object")
        _expect_keys(item, required={"path", "sha256"}, context=item_context)
        path = _normalize_source_file(_required_string(item, "path", item_context))
        digest = _required_string(item, "sha256", item_context).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise EvaluationValidationError(f"{item_context}.sha256: invalid SHA-256")
        artifacts.append(ArtifactDigest(path=path, sha256=digest))
    _ensure_unique((artifact.path for artifact in artifacts), f"{context} path")
    return tuple(artifacts)


def _parse_tolerance(data: Any, context: str) -> NumericTolerance:
    if not isinstance(data, dict):
        raise EvaluationValidationError(f"{context}: available dataset requires numeric_tolerance object")
    _expect_keys(data, required={"absolute", "relative"}, context=f"{context}.numeric_tolerance")
    absolute = _decimal_string(data["absolute"], f"{context}.numeric_tolerance.absolute")
    relative = _decimal_string(data["relative"], f"{context}.numeric_tolerance.relative")
    if absolute < 0 or relative < 0:
        raise EvaluationValidationError(f"{context}: numeric tolerances must be non-negative")
    return NumericTolerance(absolute=absolute, relative=relative)


def _parse_fact_annotation(data: dict[str, Any], context: str) -> FactAnnotation:
    required = {
        "schema_version",
        "record_type",
        "record_id",
        "company_id",
        "period",
        "statement_scope",
        "period_type",
        "metric",
        "normalized_value",
        "unit",
        "currency",
        "source",
    }
    _expect_keys(data, required=required, context=context)
    _require_schema_version(data, context)
    source = _parse_source(data["source"], f"{context}.source")
    return FactAnnotation(
        record_id=_required_string(data, "record_id", context),
        key=_parse_fact_key(data, context),
        normalized_value=_decimal_string(data["normalized_value"], f"{context}.normalized_value"),
        unit=_required_string(data, "unit", context),
        currency=_required_string(data, "currency", context),
        source=source,
    )


def _parse_question_annotation(data: dict[str, Any], context: str) -> QuestionAnnotation:
    _expect_keys(
        data,
        required={
            "schema_version",
            "record_type",
            "record_id",
            "question",
            "answer_type",
            "expected_answer",
        },
        optional={"source"},
        context=context,
    )
    _require_schema_version(data, context)
    answer_type = _required_choice(data, "answer_type", {"number", "text"}, context)
    expected_answer = _validate_answer(data["expected_answer"], answer_type, f"{context}.expected_answer")
    source_data = data.get("source")
    return QuestionAnnotation(
        record_id=_required_string(data, "record_id", context),
        question=_required_string(data, "question", context),
        answer_type=answer_type,
        expected_answer=expected_answer,
        source=_parse_source(source_data, f"{context}.source") if source_data is not None else None,
    )


def _parse_fact_prediction(data: dict[str, Any], context: str) -> FactPrediction:
    _expect_keys(
        data,
        required={
            "schema_version",
            "record_type",
            "prediction_id",
            "company_id",
            "period",
            "statement_scope",
            "period_type",
            "metric",
            "normalized_value",
            "unit",
            "currency",
        },
        optional={"source"},
        context=context,
    )
    _require_schema_version(data, context)
    source_data = data.get("source")
    return FactPrediction(
        prediction_id=_required_string(data, "prediction_id", context),
        key=_parse_fact_key(data, context),
        normalized_value=_decimal_string(data["normalized_value"], f"{context}.normalized_value"),
        unit=_required_string(data, "unit", context),
        currency=_required_string(data, "currency", context),
        source=_parse_source(source_data, f"{context}.source") if source_data is not None else None,
    )


def _parse_answer_prediction(data: dict[str, Any], context: str) -> AnswerPrediction:
    _expect_keys(
        data,
        required={"schema_version", "record_type", "prediction_id", "question_id", "answer"},
        optional={"source"},
        context=context,
    )
    _require_schema_version(data, context)
    answer = data["answer"]
    if isinstance(answer, bool) or not isinstance(answer, (str, int, float)):
        raise EvaluationValidationError(f"{context}.answer: answer must be a string or number")
    if isinstance(answer, float) and not math.isfinite(answer):
        raise EvaluationValidationError(f"{context}.answer: answer must be finite")
    source_data = data.get("source")
    return AnswerPrediction(
        prediction_id=_required_string(data, "prediction_id", context),
        question_id=_required_string(data, "question_id", context),
        answer=answer,
        source=_parse_source(source_data, f"{context}.source") if source_data is not None else None,
    )


def _parse_fact_key(data: dict[str, Any], context: str) -> FactKey:
    return FactKey(
        company_id=_required_string(data, "company_id", context),
        period=_required_string(data, "period", context),
        statement_scope=_required_string(data, "statement_scope", context),
        period_type=_required_string(data, "period_type", context),
        metric=_required_string(data, "metric", context),
    )


def _parse_source(data: Any, context: str) -> SourceLocation:
    if not isinstance(data, dict):
        raise EvaluationValidationError(f"{context}: source must be a JSON object")
    _expect_keys(
        data,
        required={"source_file", "page_no"},
        optional={"table_name", "row_label", "column_label"},
        context=context,
    )
    page_no = data["page_no"]
    if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1:
        raise EvaluationValidationError(f"{context}.page_no: page_no must be a positive integer")
    optional_labels = {}
    for name in ("table_name", "row_label", "column_label"):
        value = data.get(name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise EvaluationValidationError(f"{context}.{name}: value must be a non-empty string or null")
        optional_labels[name] = value.strip() if isinstance(value, str) else None
    return SourceLocation(
        source_file=_required_string(data, "source_file", context),
        page_no=page_no,
        **optional_labels,
    )


def _validate_answer(value: Any, answer_type: str, context: str) -> str | int | float:
    if answer_type == "text":
        if not isinstance(value, str) or not value.strip():
            raise EvaluationValidationError(f"{context}: text answer must be a non-empty string")
        return value
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise EvaluationValidationError(f"{context}: numeric answer must be a string or number")
    _decimal_value(value, context)
    return value


def _fact_value_matches(
    annotation: FactAnnotation,
    prediction: FactPrediction,
    tolerance: NumericTolerance,
) -> bool:
    return (
        annotation.unit == prediction.unit
        and annotation.currency == prediction.currency
        and _numbers_match(annotation.normalized_value, prediction.normalized_value, tolerance)
    )


def _answer_matches(
    annotation: QuestionAnnotation,
    prediction: AnswerPrediction,
    tolerance: NumericTolerance,
) -> bool:
    if annotation.answer_type == "number":
        try:
            expected = _decimal_value(annotation.expected_answer, "expected answer")
            actual = _decimal_value(prediction.answer, "predicted answer")
        except EvaluationValidationError:
            return False
        return _numbers_match(expected, actual, tolerance)
    return isinstance(prediction.answer, str) and " ".join(annotation.expected_answer.split()) == " ".join(
        prediction.answer.split()
    )


def _numbers_match(expected: Decimal, actual: Decimal, tolerance: NumericTolerance) -> bool:
    allowed_difference = max(tolerance.absolute, tolerance.relative * abs(expected))
    return abs(actual - expected) <= allowed_difference


def _source_matches(expected: SourceLocation, actual: SourceLocation | None) -> bool:
    if not _page_source_matches(expected, actual) or actual is None:
        return False
    return all(
        expected_value is None or expected_value == actual_value
        for expected_value, actual_value in (
            (expected.table_name, actual.table_name),
            (expected.row_label, actual.row_label),
            (expected.column_label, actual.column_label),
        )
    )


def _page_source_matches(expected: SourceLocation, actual: SourceLocation | None) -> bool:
    if actual is None:
        return False
    return _normalize_source_file(expected.source_file) == _normalize_source_file(actual.source_file) and (
        expected.page_no == actual.page_no
    )


def _normalize_source_file(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _unexpected_prediction_failures(
    dataset: GoldenDataset,
    predictions: EvaluationPredictions,
) -> list[EvaluationFailure]:
    expected_fact_keys = {annotation.key for annotation in dataset.facts}
    expected_question_ids = {annotation.record_id for annotation in dataset.questions}
    failures = [
        EvaluationFailure(
            record_type="financial_fact_prediction",
            record_id=prediction.prediction_id,
            metrics=("fact_precision",),
            reason="unexpected_prediction",
            actual=_fact_actual(prediction),
        )
        for prediction in predictions.facts
        if prediction.key not in expected_fact_keys
    ]
    failures.extend(
        EvaluationFailure(
            record_type="question_answer_prediction",
            record_id=prediction.prediction_id,
            metrics=("answer_precision",),
            reason="unexpected_prediction",
            actual={"question_id": prediction.question_id, "answer": prediction.answer},
        )
        for prediction in predictions.answers
        if prediction.question_id not in expected_question_ids
    )
    return failures


def _validate_golden_uniqueness(dataset: GoldenDataset) -> None:
    _ensure_unique((fact.record_id for fact in dataset.facts), "golden record_id")
    _ensure_unique((question.record_id for question in dataset.questions), "golden record_id")
    all_record_ids = [fact.record_id for fact in dataset.facts] + [question.record_id for question in dataset.questions]
    _ensure_unique(all_record_ids, "golden record_id")
    _ensure_unique((fact.key for fact in dataset.facts), "financial fact key")


def _validate_prediction_uniqueness(predictions: EvaluationPredictions) -> None:
    all_ids = [prediction.prediction_id for prediction in predictions.facts]
    all_ids.extend(prediction.prediction_id for prediction in predictions.answers)
    _ensure_unique(all_ids, "prediction_id")
    _ensure_unique((prediction.key for prediction in predictions.facts), "predicted financial fact key")
    _ensure_unique((prediction.question_id for prediction in predictions.answers), "predicted question_id")


def _ensure_unique(values: Iterable[Any], label: str) -> None:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise EvaluationValidationError(f"duplicate {label}: {value!r}")
        seen.add(value)


def _empty_metrics() -> dict[str, MetricResult]:
    return {
        "cell_accuracy": MetricResult(0, 0),
        "fact_precision": MetricResult(0, 0),
        "field_recall": MetricResult(0, 0),
        "question_accuracy": MetricResult(0, 0),
        "answer_precision": MetricResult(0, 0),
        "source_location_accuracy": MetricResult(0, 0),
    }


def _verify_real_dataset_provenance(dataset: GoldenDataset, root: Path) -> None:
    provenance = dataset.manifest.provenance
    if provenance is None:
        raise EvaluationValidationError("available real dataset requires provenance")
    referenced_sources = {_normalize_source_file(annotation.source.source_file) for annotation in dataset.facts}
    referenced_sources.update(
        _normalize_source_file(annotation.source.source_file)
        for annotation in dataset.questions
        if annotation.source is not None
    )
    _verify_artifact_digests(root, referenced_sources, provenance.source_artifacts, "source")
    annotation_files = {_normalize_source_file(path) for path in dataset.manifest.annotation_files}
    _verify_artifact_digests(root, annotation_files, provenance.annotation_artifacts, "annotation")


def _verify_artifact_digests(
    root: Path,
    referenced_paths: set[str],
    artifacts: tuple[ArtifactDigest, ...],
    artifact_kind: str,
) -> None:
    expected_hashes = {artifact.path: artifact.sha256 for artifact in artifacts}
    missing_hashes = sorted(referenced_paths - set(expected_hashes))
    if missing_hashes:
        raise EvaluationValidationError(f"real dataset provenance is missing {artifact_kind} hashes: {missing_hashes}")
    for relative_path in sorted(referenced_paths):
        artifact_path = _resolve_dataset_file(root, relative_path)
        if not artifact_path.is_file():
            raise EvaluationValidationError(f"real dataset {artifact_kind} file does not exist: {relative_path}")
        digest = _sha256_file(artifact_path)
        if digest != expected_hashes[relative_path]:
            raise EvaluationValidationError(f"real dataset {artifact_kind} SHA-256 mismatch: {relative_path}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fact_expected(annotation: FactAnnotation) -> dict[str, Any]:
    return {
        "key": _key_dict(annotation.key),
        "normalized_value": str(annotation.normalized_value),
        "unit": annotation.unit,
        "currency": annotation.currency,
        "source": _source_dict(annotation.source),
    }


def _fact_actual(prediction: FactPrediction) -> dict[str, Any]:
    return {
        "key": _key_dict(prediction.key),
        "normalized_value": str(prediction.normalized_value),
        "unit": prediction.unit,
        "currency": prediction.currency,
        "source": _source_dict(prediction.source),
    }


def _key_dict(key: FactKey) -> dict[str, str]:
    return {
        "company_id": key.company_id,
        "period": key.period,
        "statement_scope": key.statement_scope,
        "period_type": key.period_type,
        "metric": key.metric,
    }


def _source_dict(source: SourceLocation | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "source_file": source.source_file,
        "page_no": source.page_no,
        "table_name": source.table_name,
        "row_label": source.row_label,
        "column_label": source.column_label,
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    value = _load_json_value(path.read_text(encoding="utf-8-sig"), str(path))
    if not isinstance(value, dict):
        raise EvaluationValidationError(f"{path}: expected a JSON object")
    return value


def _load_record_collection(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        value = _load_json_value(path.read_text(encoding="utf-8-sig"), str(path))
        if not isinstance(value, list):
            raise EvaluationValidationError(f"{path}: annotation JSON must contain an array")
        records = value
    elif suffix == ".jsonl":
        records = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            if not line.strip():
                continue
            records.append(_load_json_value(line, f"{path}:{line_no}"))
    else:
        raise EvaluationValidationError(f"{path}: artifact file must use .json or .jsonl")
    if not all(isinstance(record, dict) for record in records):
        raise EvaluationValidationError(f"{path}: every record must be a JSON object")
    return records


def _load_json_value(text: str, context: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                EvaluationValidationError(f"{context}: non-finite JSON number {value!r} is not allowed")
            ),
        )
    except json.JSONDecodeError as exc:
        raise EvaluationValidationError(f"{context}: invalid JSON: {exc.msg}") from exc


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationValidationError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _resolve_dataset_file(root: Path, relative_name: str) -> Path:
    relative = Path(relative_name)
    if relative.is_absolute():
        raise EvaluationValidationError(f"annotation file must be relative to manifest: {relative_name!r}")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise EvaluationValidationError(f"annotation file escapes manifest directory: {relative_name!r}") from exc
    if not candidate.is_file():
        raise EvaluationValidationError(f"annotation file does not exist: {candidate}")
    return candidate


def _expect_keys(
    data: dict[str, Any],
    *,
    required: set[str],
    context: str,
    optional: set[str] | None = None,
) -> None:
    missing = required - data.keys()
    if missing:
        raise EvaluationValidationError(f"{context}: missing fields: {', '.join(sorted(missing))}")
    allowed = required | (optional or set())
    unknown = data.keys() - allowed
    if unknown:
        raise EvaluationValidationError(f"{context}: unknown fields: {', '.join(sorted(unknown))}")


def _require_schema_version(data: dict[str, Any], context: str) -> None:
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise EvaluationValidationError(
            f"{context}: unsupported schema_version {version!r}; expected {SCHEMA_VERSION!r}"
        )


def _required_string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvaluationValidationError(f"{context}.{key}: value must be a non-empty string")
    return value.strip()


def _required_choice(data: dict[str, Any], key: str, choices: set[str] | frozenset[str], context: str) -> str:
    value = _required_string(data, key, context)
    if value not in choices:
        raise EvaluationValidationError(f"{context}.{key}: expected one of {sorted(choices)}, got {value!r}")
    return value


def _string_list(value: Any, context: str) -> list[str]:
    if not isinstance(value, list):
        raise EvaluationValidationError(f"{context}: value must be an array")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise EvaluationValidationError(f"{context}[{index}]: value must be a non-empty string")
        result.append(item.strip())
    if len(set(result)) != len(result):
        raise EvaluationValidationError(f"{context}: duplicate annotation file")
    return result


def _decimal_string(value: Any, context: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationValidationError(f"{context}: decimal values must be non-empty strings")
    return _decimal_value(value, context)


def _decimal_value(value: str | int | float, context: str) -> Decimal:
    if isinstance(value, bool):
        raise EvaluationValidationError(f"{context}: boolean is not a number")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise EvaluationValidationError(f"{context}: invalid decimal value {value!r}") from exc
    if not decimal_value.is_finite():
        raise EvaluationValidationError(f"{context}: decimal value must be finite")
    return decimal_value
