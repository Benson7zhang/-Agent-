"""Executable boundaries for the versioned LLM document-extraction protocol."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from .facts import expected_period_type_for_table
from .llm import ModelAttachment, ModelRequest, ModelResponse, StructuredModelClient
from .llm_extraction_contracts import (
    FinancialFactExtractionPayload,
    IdentityMismatchError,
    LayoutRecoveryPayload,
    NormalizationFactPlan,
    P2FactCandidate,
    RequestManifest,
    SchemaRepairPayload,
    StageEnvelope,
)
from .llm_prompts import (
    PromptDefinition,
    PromptRegistryError,
    build_runtime_metric_catalog,
    get_prompt,
)
from .schema import FieldSpec
from .task2 import MetricSpec

MAX_STAGE_ATTEMPTS = 3
DISPLAY_GROUPING_PATTERN = re.compile(r"^[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?$")
PLAIN_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class ExtractionProtocolError(RuntimeError):
    """A stage response violated an extraction trust boundary."""


class StageResponseValidationError(ExtractionProtocolError):
    """A response failed its stage Schema or immutable identity checks."""


class EvidenceVerificationError(ExtractionProtocolError):
    """A model candidate cannot be found at its claimed source evidence."""


class CandidateBoundaryError(ExtractionProtocolError):
    """A candidate or region identifier escaped its request manifest."""


class NormalizationError(ExtractionProtocolError):
    """A normalization plan conflicts with deterministic conversion rules."""


@dataclass(frozen=True, slots=True)
class StageExecutionResult:
    manifest: RequestManifest
    response: ModelResponse
    envelope: StageEnvelope[Any]
    repair_response: ModelResponse | None = None


@dataclass(frozen=True, slots=True)
class AssignedFactCandidate:
    candidate_id: str
    fact: P2FactCandidate


@dataclass(frozen=True, slots=True)
class UnitConversionRule:
    rule_id: str
    source_unit: str
    target_unit: str
    factor: Decimal


@dataclass(frozen=True, slots=True)
class NormalizedFactCandidate:
    candidate_id: str
    fact: P2FactCandidate
    normalized_value: Decimal
    conversion_rule_id: str


DEFAULT_CONVERSION_RULES: Mapping[tuple[str, str], UnitConversionRule] = MappingProxyType(
    {
        ("元", "元"): UnitConversionRule("IDENTITY_CNY_YUAN_V1", "元", "元", Decimal("1")),
        ("万元", "万元"): UnitConversionRule("IDENTITY_CNY_WANYUAN_V1", "万元", "万元", Decimal("1")),
        ("元", "万元"): UnitConversionRule("CNY_YUAN_TO_WANYUAN_V1", "元", "万元", Decimal("0.0001")),
        ("万元", "元"): UnitConversionRule("CNY_WANYUAN_TO_YUAN_V1", "万元", "元", Decimal("10000")),
        ("%", "%"): UnitConversionRule("IDENTITY_PERCENT_V1", "%", "%", Decimal("1")),
    }
)


class ExtractionStageExecutor:
    """Render, invoke, validate, and audit one registered extraction stage."""

    def __init__(
        self,
        client: StructuredModelClient,
        schema: Mapping[str, Sequence[FieldSpec]],
        *,
        task2_metrics: Mapping[str, MetricSpec] | None = None,
    ) -> None:
        if not client.enabled:
            raise ValueError("structured model client must be explicitly configured")
        self.client = client
        self.metric_catalog = build_runtime_metric_catalog(schema, task2_metrics)
        self._metric_entries = {
            (table.statement_table, metric.metric): metric
            for table in self.metric_catalog.schema_tables
            for metric in table.metrics
        }
        self._allowed_metrics = frozenset(metric for _table, metric in self._metric_entries)

    def execute(
        self,
        manifest: RequestManifest,
        inputs: Mapping[str, Any],
        *,
        attachments: Sequence[ModelAttachment] = (),
        repair_invalid_response: bool = True,
        max_attempts: int = MAX_STAGE_ATTEMPTS,
    ) -> StageExecutionResult:
        definition = get_prompt(manifest.stage)
        self._validate_manifest(definition, manifest)
        rendered_inputs = dict(inputs)
        model_manifest = _model_visible_manifest(manifest)
        if "request_context" in definition.input_sections:
            if "request_context" in rendered_inputs:
                raise ValueError("request_context is controlled by the executor")
            rendered_inputs["request_context"] = model_manifest
        rendered = definition.render(
            rendered_inputs,
            metric_catalog=self.metric_catalog if definition.inject_metric_catalog else None,
        )
        request = ModelRequest(
            stage=manifest.stage,
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.prompt_version,
            system_prompt=rendered.system_prompt,
            input_json={
                "request_manifest": model_manifest,
                "prompt_sections": rendered.user_message,
            },
            output_json_schema=rendered.output_json_schema,
            attachments=tuple(attachments),
            idempotency_key=self._idempotency_key(manifest, rendered.output_json_schema),
        )
        response = self.client.invoke(request, max_attempts=max_attempts)
        if response.finish_reason in {"length", "content_filter"}:
            raise StageResponseValidationError(f"model response ended with finish_reason={response.finish_reason!r}")
        self._reject_explicit_non_ok_status(response.raw_json, "model response")
        try:
            envelope = definition.validate_response(
                response.raw_json,
                manifest,
                metric_catalog=self.metric_catalog if definition.validate_metric_outputs else None,
            )
        except IdentityMismatchError as exc:
            raise StageResponseValidationError(str(exc)) from exc
        except PromptRegistryError as exc:
            raise StageResponseValidationError(str(exc)) from exc
        except ValidationError as exc:
            if not repair_invalid_response or manifest.stage == "schema-repair":
                raise StageResponseValidationError("model response failed its registered JSON Schema") from exc
            envelope, repair_response = self._repair_once(definition, manifest, response.raw_json, exc)
            self._validate_payload_boundaries(envelope, manifest)
            return StageExecutionResult(manifest, response, envelope, repair_response)
        self._require_ok_status(envelope, "model response")
        self._validate_payload_boundaries(envelope, manifest)
        return StageExecutionResult(manifest, response, envelope)

    def _repair_once(
        self,
        definition: PromptDefinition,
        manifest: RequestManifest,
        invalid_response: Mapping[str, Any],
        validation_error: ValidationError,
    ) -> tuple[StageEnvelope[Any], ModelResponse]:
        repair_definition = get_prompt("schema-repair")
        repair_manifest = manifest.model_copy(
            update={
                "request_id": f"{manifest.request_id}-repair",
                "stage": "schema-repair",
                "prompt_version": repair_definition.version,
                "input_scope_id": f"{manifest.input_scope_id}-repair",
            }
        )
        repair_rendered = repair_definition.render(
            {
                "json_schema": definition.response_json_schema(),
                "validator_errors": validation_error.errors(include_url=False, include_input=False),
                "original_response": dict(invalid_response),
                "original_input_digest": {
                    "request_id": manifest.request_id,
                    "document_id": manifest.document_id,
                    "source_content_sha256": manifest.source_content_sha256,
                    "input_scope_id": manifest.input_scope_id,
                    "allowed_region_ids": manifest.allowed_region_ids,
                    "allowed_candidate_ids": manifest.allowed_candidate_ids,
                },
            }
        )
        repair_request = ModelRequest(
            stage="schema-repair",
            prompt_id=repair_rendered.prompt_id,
            prompt_version=repair_rendered.prompt_version,
            system_prompt=repair_rendered.system_prompt,
            input_json={
                "request_manifest": repair_manifest.model_dump(mode="json"),
                "prompt_sections": repair_rendered.user_message,
            },
            output_json_schema=repair_rendered.output_json_schema,
            idempotency_key=self._idempotency_key(repair_manifest, repair_rendered.output_json_schema),
        )
        repair_response = self.client.invoke(repair_request, max_attempts=1)
        self._reject_explicit_non_ok_status(repair_response.raw_json, "schema repair response")
        try:
            repair_envelope = repair_definition.validate_response(repair_response.raw_json, repair_manifest)
        except (IdentityMismatchError, ValidationError) as exc:
            raise StageResponseValidationError("schema repair response is invalid") from exc
        self._require_ok_status(repair_envelope, "schema repair response")
        repair_payload = repair_envelope.payload
        if not isinstance(repair_payload, SchemaRepairPayload):
            raise StageResponseValidationError("schema repair returned the wrong payload type")
        if repair_payload.rejected_records:
            raise StageResponseValidationError("schema repair rejected one or more response records")
        _validate_repair_changes(dict(invalid_response), repair_payload)
        try:
            repaired = definition.validate_response(
                repair_payload.repaired_response,
                manifest,
                metric_catalog=self.metric_catalog if definition.validate_metric_outputs else None,
            )
        except (IdentityMismatchError, PromptRegistryError, ValidationError) as exc:
            raise StageResponseValidationError("repaired response still fails its registered contract") from exc
        self._require_ok_status(repaired, "repaired model response")
        return repaired, repair_response

    @staticmethod
    def _require_ok_status(envelope: StageEnvelope[Any], response_name: str) -> None:
        if envelope.status != "OK":
            raise StageResponseValidationError(f"{response_name} returned non-OK status: {envelope.status}")

    @staticmethod
    def _reject_explicit_non_ok_status(response: Mapping[str, Any], response_name: str) -> None:
        status = response.get("status")
        if status in {"PARTIAL", "REFUSED"}:
            raise StageResponseValidationError(f"{response_name} returned non-OK status: {status}")

    def _validate_manifest(self, definition: PromptDefinition, manifest: RequestManifest) -> None:
        if manifest.stage != definition.prompt_id or manifest.prompt_version != definition.version:
            raise ValueError("request manifest does not match the registered prompt version")
        if manifest.metric_catalog_version != self.metric_catalog.catalog_version:
            raise ValueError("request manifest metric catalog version is stale or unknown")

    def _validate_payload_boundaries(self, envelope: StageEnvelope[Any], manifest: RequestManifest) -> None:
        payload = envelope.payload
        payload_document_id = getattr(payload, "document_id", manifest.document_id)
        if payload_document_id != manifest.document_id:
            raise StageResponseValidationError("stage payload document_id does not match request manifest")

        dumped = payload.model_dump(mode="json")
        region_ids = _collect_identifier_values(dumped, "region_id")
        if region_ids - set(manifest.allowed_region_ids):
            unknown_regions = sorted(region_ids - set(manifest.allowed_region_ids))
            raise CandidateBoundaryError(f"model returned region IDs outside the request manifest: {unknown_regions}")
        candidate_ids = _collect_identifier_values(
            dumped,
            "candidate_id",
            "candidate_ids",
            "selected_candidate_id",
            "related_candidate_ids",
        )
        if candidate_ids - set(manifest.allowed_candidate_ids):
            unknown = sorted(candidate_ids - set(manifest.allowed_candidate_ids))
            raise CandidateBoundaryError(f"model returned candidate IDs outside the request manifest: {unknown}")
        if isinstance(payload, LayoutRecoveryPayload):
            self._validate_layout_regions(payload, manifest)
        if isinstance(payload, FinancialFactExtractionPayload):
            self._validate_extracted_facts(payload, manifest)

    @staticmethod
    def _validate_layout_regions(payload: LayoutRecoveryPayload, manifest: RequestManifest) -> None:
        evidence_regions = _manifest_evidence_regions(manifest)
        for fragment in payload.fragments:
            for row in fragment.data_rows:
                for cell in row.cells:
                    evidence = evidence_regions.get(cell.region_id)
                    if evidence is None:
                        raise CandidateBoundaryError(f"layout cell references unknown region: {cell.region_id!r}")
                    if cell.raw_text and cell.raw_text not in evidence:
                        raise EvidenceVerificationError(
                            f"layout cell text is absent from source region: {cell.region_id}"
                        )

    def _validate_extracted_facts(
        self,
        payload: FinancialFactExtractionPayload,
        manifest: RequestManifest,
    ) -> None:
        allowed_regions = set(manifest.allowed_region_ids)
        total_pages = manifest.payload.get("total_pages")
        if total_pages is not None and (
            isinstance(total_pages, bool) or not isinstance(total_pages, int) or total_pages < 1
        ):
            raise ValueError("manifest payload total_pages must be a positive integer")
        trusted_company_id = _required_manifest_text(manifest, "confirmed_company_id")
        trusted_stock_code = _required_manifest_text(manifest, "confirmed_stock_code")
        trusted_report_period = _required_manifest_text(manifest, "confirmed_report_period")
        evidence_regions = _manifest_evidence_regions(manifest)
        for fact in payload.facts:
            metric_entry = self._metric_entries.get((fact.statement_table, fact.metric))
            if metric_entry is None:
                raise CandidateBoundaryError(
                    f"model returned metric outside its statement table catalog: {fact.statement_table}/{fact.metric}"
                )
            if fact.document_id != manifest.document_id:
                raise CandidateBoundaryError("fact document_id differs from the request manifest")
            if fact.company_id != trusted_company_id:
                raise CandidateBoundaryError("fact company_id differs from confirmed metadata")
            if fact.stock_code != trusted_stock_code:
                raise CandidateBoundaryError("fact stock_code differs from confirmed metadata")
            if fact.report_period != trusted_report_period:
                raise CandidateBoundaryError("fact report_period differs from confirmed metadata")
            expected_period_type = expected_period_type_for_table(fact.statement_table).value
            if fact.period_type != expected_period_type or fact.period_type != metric_entry.period_type:
                raise CandidateBoundaryError("fact period_type conflicts with its statement table")
            if fact.target_unit != metric_entry.target_unit:
                raise CandidateBoundaryError("fact target_unit conflicts with the runtime metric catalog")
            if fact.source_unit is not None and fact.source_unit not in metric_entry.allowed_source_units:
                raise CandidateBoundaryError("fact source_unit is not allowed by the runtime metric catalog")
            _validate_current_period_semantics(fact)
            if fact.source.region_id not in allowed_regions:
                raise CandidateBoundaryError(
                    f"model returned region ID outside the request manifest: {fact.source.region_id!r}"
                )
            if fact.source.source_content_sha256 != manifest.source_content_sha256:
                raise CandidateBoundaryError("fact source hash differs from the request manifest")
            if fact.source.source_file_name != manifest.source_file_name:
                raise CandidateBoundaryError("fact source file name differs from the request manifest")
            if total_pages is not None and fact.source.page_no is not None and fact.source.page_no > total_pages:
                raise CandidateBoundaryError("fact source page exceeds the document page count")
            if fact.raw_value not in fact.source.evidence_text:
                raise EvidenceVerificationError(
                    f"raw value is absent from its claimed evidence region: {fact.source.region_id}"
                )
            if fact.raw_value not in evidence_regions[fact.source.region_id]:
                raise EvidenceVerificationError(
                    f"raw value is absent from the immutable input region: {fact.source.region_id}"
                )
        for value in payload.comparison_values:
            if value.metric not in self._allowed_metrics:
                raise CandidateBoundaryError(f"comparison metric is outside the runtime catalog: {value.metric!r}")
            if value.region_id not in allowed_regions:
                raise CandidateBoundaryError(
                    f"comparison region ID is outside the request manifest: {value.region_id!r}"
                )
            if value.raw_value not in value.evidence_text:
                raise EvidenceVerificationError(
                    f"comparison value is absent from its claimed evidence region: {value.region_id}"
                )
            if value.raw_value not in evidence_regions[value.region_id]:
                raise EvidenceVerificationError(
                    f"comparison value is absent from the immutable input region: {value.region_id}"
                )

    @staticmethod
    def _idempotency_key(manifest: RequestManifest, output_schema: Mapping[str, Any]) -> str:
        schema_json = json.dumps(output_schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        identity = "\x1f".join(
            (
                manifest.source_content_sha256,
                manifest.stage,
                manifest.prompt_version,
                hashlib.sha256(schema_json.encode("utf-8")).hexdigest(),
                manifest.input_scope_id,
                manifest.extractor_version,
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def assign_candidate_ids(
    payload: FinancialFactExtractionPayload,
    manifest: RequestManifest,
) -> tuple[AssignedFactCandidate, ...]:
    """Assign stable service-owned IDs after P2 trust-boundary validation."""

    assigned: list[AssignedFactCandidate] = []
    seen_ids: set[str] = set()
    for fact in payload.facts:
        identity = "\x1f".join(
            (
                manifest.source_content_sha256,
                fact.source.region_id,
                fact.metric,
                fact.raw_value,
                fact.report_period or "",
                fact.statement_scope or "",
            )
        )
        candidate_id = "fact_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if candidate_id in seen_ids:
            raise CandidateBoundaryError(f"duplicate fact candidate identity: {candidate_id}")
        seen_ids.add(candidate_id)
        assigned.append(AssignedFactCandidate(candidate_id, fact))
    return tuple(assigned)


def normalize_candidates(
    candidates: Sequence[AssignedFactCandidate],
    plans: Sequence[NormalizationFactPlan],
    *,
    conversion_rules: Mapping[tuple[str, str], UnitConversionRule] = DEFAULT_CONVERSION_RULES,
) -> tuple[NormalizedFactCandidate, ...]:
    """Recompute every accepted normalization plan with Decimal exactly once."""

    candidate_by_id = {item.candidate_id: item for item in candidates}
    if len(candidate_by_id) != len(candidates):
        raise NormalizationError("candidate IDs must be unique")
    plan_by_id = {item.candidate_id: item for item in plans}
    if len(plan_by_id) != len(plans):
        raise NormalizationError("normalization plans must have unique candidate IDs")
    if set(plan_by_id) != set(candidate_by_id):
        raise NormalizationError("normalization plan IDs must exactly match candidate IDs")

    normalized: list[NormalizedFactCandidate] = []
    for candidate_id, assigned in candidate_by_id.items():
        fact = assigned.fact
        plan = plan_by_id[candidate_id]
        _require_unchanged_normalization_context(fact, plan)
        if fact.source_unit is None or fact.target_unit is None or plan.conversion is None:
            raise NormalizationError(f"candidate {candidate_id} has no complete conversion context")
        rule = conversion_rules.get((fact.source_unit, fact.target_unit))
        if rule is None:
            raise NormalizationError(
                f"unsupported unit conversion for candidate {candidate_id}: {fact.source_unit!r} to {fact.target_unit!r}"
            )
        if (
            plan.conversion.rule_id != rule.rule_id
            or plan.conversion.operation != "multiply"
            or Decimal(plan.conversion.factor) != rule.factor
        ):
            raise NormalizationError(f"model conversion plan conflicts with rule registry for {candidate_id}")
        parsed = parse_financial_decimal(fact.raw_value, negative_parentheses=fact.negative_interpretation)
        proposal = Decimal(plan.conversion.parsed_decimal_proposal)
        if proposal != parsed:
            raise NormalizationError(f"model decimal proposal conflicts with source value for {candidate_id}")
        normalized.append(
            NormalizedFactCandidate(
                candidate_id=candidate_id,
                fact=fact,
                normalized_value=parsed * rule.factor,
                conversion_rule_id=rule.rule_id,
            )
        )
    return tuple(normalized)


def parse_financial_decimal(raw_value: str, *, negative_parentheses: bool) -> Decimal:
    """Parse one evidence-backed financial display value without guessing blanks or units."""

    value = raw_value.strip()
    negative = False
    if value.startswith("(") and value.endswith(")"):
        if not negative_parentheses:
            raise NormalizationError("parenthesized value lacks an explicit negative interpretation")
        negative = True
        value = value[1:-1].strip()
    elif value.startswith("-"):
        negative = True
        value = value[1:].strip()
    if value.endswith("%"):
        value = value[:-1].strip()
    if "," in value:
        if DISPLAY_GROUPING_PATTERN.fullmatch(value) is None:
            raise NormalizationError(f"invalid thousands grouping in raw value: {raw_value!r}")
        value = value.replace(",", "")
    elif PLAIN_DECIMAL_PATTERN.fullmatch(value) is None:
        raise NormalizationError(f"raw value is not an explicit finite decimal: {raw_value!r}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise NormalizationError(f"raw value is not an explicit finite decimal: {raw_value!r}") from exc
    if not parsed.is_finite():
        raise NormalizationError(f"raw value is not finite: {raw_value!r}")
    return -parsed if negative else parsed


def _require_unchanged_normalization_context(fact: P2FactCandidate, plan: NormalizationFactPlan) -> None:
    comparisons = (
        ("raw_value", fact.raw_value, plan.raw_value),
        ("source_unit", fact.source_unit, plan.source_unit),
        ("target_unit", fact.target_unit, plan.target_unit),
        ("currency", fact.currency, plan.currency),
        ("report_period", fact.report_period, plan.report_period),
        ("statement_scope", fact.statement_scope, plan.statement_scope),
        ("period_type", fact.period_type, plan.period_type),
    )
    changed = [name for name, original, proposed in comparisons if original != proposed]
    if changed:
        raise NormalizationError(f"normalization plan changed protected candidate fields: {', '.join(changed)}")


def _collect_identifier_values(value: Any, *keys: str) -> set[str]:
    target_keys = set(keys)
    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in target_keys:
                    if isinstance(child, str):
                        found.add(child)
                    elif isinstance(child, list):
                        found.update(value for value in child if isinstance(value, str))
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _required_manifest_text(manifest: RequestManifest, field_name: str) -> str:
    value = manifest.payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"manifest payload requires non-empty {field_name}")
    return value


def _model_visible_manifest(manifest: RequestManifest) -> dict[str, Any]:
    """Remove duplicated evidence bodies while retaining locally enforced identities and allowlists."""

    visible = manifest.model_dump(mode="json")
    payload = dict(visible["payload"])
    payload.pop("evidence_regions", None)
    payload.pop("confirmed_fragment", None)
    visible["payload"] = payload
    return visible


def _manifest_evidence_regions(manifest: RequestManifest) -> dict[str, str]:
    raw_regions = manifest.payload.get("evidence_regions")
    if not isinstance(raw_regions, dict):
        raise ValueError("manifest payload requires evidence_regions")
    regions: dict[str, str] = {}
    for region_id, text in raw_regions.items():
        if not isinstance(region_id, str) or not region_id.strip() or not isinstance(text, str):
            raise ValueError("manifest evidence_regions must map non-empty IDs to strings")
        regions[region_id] = text
    if set(regions) != set(manifest.allowed_region_ids):
        raise ValueError("manifest evidence_regions must exactly match allowed_region_ids")
    return regions


def _validate_current_period_semantics(fact: P2FactCandidate) -> None:
    semantics = fact.period_semantics
    if fact.report_period is None:
        raise CandidateBoundaryError("current-period fact requires a confirmed report period")
    year = fact.report_period[:4]
    suffix = fact.report_period[4:]
    period_end = {
        "Q1": f"{year}-03-31",
        "Q2": f"{year}-06-30",
        "Q3": f"{year}-09-30",
        "FY": f"{year}-12-31",
    }[suffix]
    if fact.period_type == "instant":
        if semantics.basis != "instant" or semantics.column_role not in {"current_period", "closing_balance"}:
            raise CandidateBoundaryError("instant fact does not represent the current closing period")
        if semantics.as_of_date != period_end:
            raise CandidateBoundaryError("instant fact as_of_date conflicts with the confirmed report period")
        if semantics.period_start is not None or semantics.period_end is not None:
            raise CandidateBoundaryError("instant fact cannot claim duration boundaries")
        return
    expected_basis = "annual" if suffix == "FY" else "ytd"
    if semantics.basis != expected_basis or semantics.column_role != "current_period":
        raise CandidateBoundaryError("duration fact does not match the supported cumulative current-period semantics")
    if semantics.period_start != f"{year}-01-01" or semantics.period_end != period_end:
        raise CandidateBoundaryError("duration fact boundaries conflict with the confirmed report period")
    if semantics.as_of_date is not None:
        raise CandidateBoundaryError("duration fact cannot claim an instant as_of_date")


def _validate_repair_changes(original: dict[str, Any], repair_payload: SchemaRepairPayload) -> None:
    repaired = repair_payload.repaired_response
    original_leaves = _leaf_projection(original)
    repaired_leaves = _leaf_projection(repaired)
    for path, original_value in original_leaves.items():
        if path not in repaired_leaves or repaired_leaves[path] != original_value:
            raise StageResponseValidationError(f"schema repair changed existing response content: {path or '/'}")

    for path, repaired_value in repaired_leaves.items():
        if path not in original_leaves and repaired_value not in (None, [], {}):
            raise StageResponseValidationError(f"schema repair added non-structural response content: {path or '/'}")

    for modified in repair_payload.modified_fields:
        if _json_pointer_exists(original, modified.json_pointer):
            raise StageResponseValidationError(
                f"schema repair reported a modification to existing response content: {modified.json_pointer}"
            )


def _leaf_projection(value: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}

    def visit(item: Any, path: str) -> None:
        if isinstance(item, dict):
            if not item:
                found[path] = {}
                return
            for key in sorted(item):
                escaped_key = key.replace("~", "~0").replace("/", "~1")
                visit(item[key], f"{path}/{escaped_key}")
        elif isinstance(item, list):
            if not item:
                found[path] = []
                return
            for index, child in enumerate(item):
                visit(child, f"{path}/{index}")
        else:
            found[path] = item

    visit(value, "")
    return found


def _json_pointer_exists(value: Any, pointer: str) -> bool:
    if pointer == "":
        return True
    if not pointer.startswith("/"):
        raise StageResponseValidationError(f"schema repair returned an invalid JSON pointer: {pointer!r}")
    current = value
    for encoded_part in pointer[1:].split("/"):
        part = encoded_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                return False
            current = current[part]
            continue
        if isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                return False
            current = current[int(part)]
            continue
        return False
    return True
