"""Shared application service for CLI/API financial question answering."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .calculations import apply_post_compute
from .database import FinanceDatabase, FinanceDatabaseFactory
from .facts import SourceAuthorityStatus, UntrustedFactError, expected_period_type_for_table
from .safe_query import QuerySpec, SafeQueryExecutor, SessionState
from .sql_planner import SQLPlanner
from .task2 import TASK2_METRICS, QuestionAnalyzer
from .web_store import MIGRATION_VERSION, TurnRecord, WebStore

APPLICATION_CODE_VERSION = "application-v1"


class StoredQueryFailure(RuntimeError):
    """An idempotent replay refers to a query turn that already failed."""


@dataclass(frozen=True, slots=True)
class ChartSeries:
    name: str
    field: str
    unit: str
    values: tuple[int | float | None, ...]


@dataclass(frozen=True, slots=True)
class ChartSpec:
    chart_type: str
    title: str
    x_field: str
    x_values: tuple[str, ...]
    series: tuple[ChartSeries, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chart_type": self.chart_type,
            "title": self.title,
            "x_field": self.x_field,
            "x_values": list(self.x_values),
            "series": [asdict(item) | {"values": list(item.values)} for item in self.series],
        }


@dataclass(frozen=True, slots=True)
class AnswerResult:
    status: str
    content: str
    rows: tuple[dict[str, Any], ...] = ()
    sources: tuple[dict[str, Any], ...] = ()
    chart: ChartSpec | None = None
    query_spec: dict[str, Any] | None = None
    calculation: dict[str, Any] | None = None
    formula: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "content": self.content,
            "rows": [dict(row) for row in self.rows],
            "sources": [dict(source) for source in self.sources],
            "chart": None if self.chart is None else self.chart.to_dict(),
            "query_spec": self.query_spec,
            "calculation": self.calculation,
            "formula": self.formula,
        }


@dataclass(frozen=True, slots=True)
class ApplicationTurnResult:
    turn_id: str
    conversation_id: str
    conversation_version: int
    query_run_id: str | None
    answer: AnswerResult


class ApplicationService:
    """Execute one question through the repository's only trusted query chain."""

    def __init__(
        self,
        store: WebStore,
        database_factory: FinanceDatabaseFactory,
        analyzer: QuestionAnalyzer,
        *,
        sql_planner: SQLPlanner | None = None,
        max_rows: int = 100,
        timeout_seconds: float = 5.0,
        code_version: str = APPLICATION_CODE_VERSION,
    ) -> None:
        self.store = store
        self.database_factory = database_factory
        self.analyzer = analyzer
        self.sql_planner = sql_planner or SQLPlanner()
        self.max_rows = max_rows
        self.timeout_seconds = timeout_seconds
        self.code_version = code_version

    def ask(
        self,
        conversation_id: str,
        question: str,
        *,
        expected_version: int,
        idempotency_key: str,
        request_id: str,
    ) -> ApplicationTurnResult:
        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question must be non-empty")
        if not request_id.strip():
            raise ValueError("request_id must be non-empty")
        existing = self.store.find_turn_by_idempotency(conversation_id, idempotency_key)
        if existing is not None:
            if existing.question != normalized_question:
                raise ValueError("idempotency key is already bound to a different question")
            if existing.answer_payload.get("status") == "FAILED":
                raise StoredQueryFailure(
                    f"query turn previously failed; query_run_id={existing.query_run_id or 'unavailable'}"
                )
            if existing.answer_payload.get("status") == "VERIFIED":
                self._require_verified_source_content(existing.answer_payload.get("sources"))
            return self._result_from_turn(existing)

        conversation = self.store.get_conversation(conversation_id)
        session_state = SessionState(
            slots=conversation.session_state.get("slots", {}),
            context=conversation.session_state.get("context", {}),
        )
        analysis = self.analyzer.analyze_turn(normalized_question, session_state)
        updated_session = analysis.get("session_state")
        if not isinstance(updated_session, SessionState):
            raise RuntimeError("QuestionAnalyzer did not return a SessionState")

        if analysis.get("need_clarify"):
            answer = AnswerResult(
                status="NEEDS_CLARIFICATION",
                content=str(analysis.get("clarify_question") or "请补充关键信息。"),
            )
            turn = self.store.append_turn(
                conversation_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                question=normalized_question,
                answer_payload=answer.to_dict(),
                session_state=updated_session.to_dict(),
            )
            return self._application_result(turn, answer)

        raw_query_spec = analysis.get("query_spec")
        if not isinstance(raw_query_spec, Mapping):
            raise RuntimeError("QuestionAnalyzer returned no query specification")
        query_spec_payload = dict(raw_query_spec)
        compiled = None
        try:
            query_spec = QuerySpec.from_mapping(query_spec_payload)
            compiled = self.sql_planner.compile(
                {"intent": analysis.get("intent"), "query_spec": query_spec_payload},
                backend=self.database_factory.db_config.backend,
            )
            with self.database_factory.unit_of_work() as database:
                executor = SafeQueryExecutor(
                    database,
                    self.sql_planner.registry,
                    max_rows=self.max_rows,
                    timeout_seconds=self.timeout_seconds,
                )
                raw_rows = executor.execute(compiled, use_cache=False)
                calculation = apply_post_compute(query_spec, raw_rows)
                rows = [dict(row) for row in calculation.rows]
                sources = self._validated_fact_sources(database, query_spec, rows)
            if rows:
                self._require_verified_source_content(sources)
        except Exception as exc:
            failed_answer = AnswerResult(
                status="FAILED",
                content="查询执行失败，错误详情已记录。",
                query_spec=query_spec_payload,
            )
            error_message = f"{type(exc).__name__}: {exc}"
            failed_query_run = {
                "normalized_question": normalized_question,
                "query_spec": query_spec_payload,
                "sql": "" if compiled is None else compiled.sql,
                "params": [] if compiled is None else list(compiled.params),
                "fact_sources": [],
                "calculation": {},
                "answer_payload": failed_answer.to_dict(),
                "request_id": request_id,
                "schema_version": str(MIGRATION_VERSION),
                "code_version": self.code_version,
                "status": "FAILED",
                "error_message": error_message,
            }
            self.store.append_turn(
                conversation_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                question=normalized_question,
                answer_payload=failed_answer.to_dict(),
                session_state=updated_session.to_dict(),
                query_run=failed_query_run,
            )
            raise

        status = "VERIFIED" if rows else "INSUFFICIENT_EVIDENCE"
        content = self._compose_content(query_spec, rows)
        chart = self._chart_spec(query_spec_payload, query_spec, rows)
        answer = AnswerResult(
            status=status,
            content=content,
            rows=tuple(rows),
            sources=tuple(sources),
            chart=chart,
            query_spec=query_spec_payload,
            calculation=calculation.metadata,
            formula=self._formula(calculation.metadata),
        )
        query_run = {
            "normalized_question": normalized_question,
            "query_spec": query_spec_payload,
            "sql": compiled.sql,
            "params": list(compiled.params),
            "fact_sources": sources,
            "calculation": calculation.metadata,
            "answer_payload": answer.to_dict(),
            "request_id": request_id,
            "schema_version": str(MIGRATION_VERSION),
            "code_version": self.code_version,
            "status": "SUCCEEDED",
        }
        turn = self.store.append_turn(
            conversation_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            question=normalized_question,
            answer_payload=answer.to_dict(),
            session_state=updated_session.to_dict(),
            query_run=query_run,
        )
        return self._application_result(turn, answer)

    def _result_from_turn(self, turn: TurnRecord) -> ApplicationTurnResult:
        payload = turn.answer_payload
        chart_payload = payload.get("chart")
        chart = None
        if isinstance(chart_payload, Mapping):
            chart = ChartSpec(
                chart_type=str(chart_payload["chart_type"]),
                title=str(chart_payload["title"]),
                x_field=str(chart_payload["x_field"]),
                x_values=tuple(str(item) for item in chart_payload.get("x_values", [])),
                series=tuple(
                    ChartSeries(
                        name=str(item["name"]),
                        field=str(item["field"]),
                        unit=str(item["unit"]),
                        values=tuple(item.get("values", [])),
                    )
                    for item in chart_payload.get("series", [])
                ),
            )
        answer = AnswerResult(
            status=str(payload["status"]),
            content=str(payload["content"]),
            rows=tuple(dict(row) for row in payload.get("rows", [])),
            sources=tuple(dict(source) for source in payload.get("sources", [])),
            chart=chart,
            query_spec=payload.get("query_spec"),
            calculation=payload.get("calculation"),
            formula=payload.get("formula"),
        )
        return self._application_result(turn, answer)

    @staticmethod
    def _application_result(turn: TurnRecord, answer: AnswerResult) -> ApplicationTurnResult:
        return ApplicationTurnResult(
            turn_id=turn.turn_id,
            conversation_id=turn.conversation_id,
            conversation_version=turn.sequence_no,
            query_run_id=turn.query_run_id,
            answer=answer,
        )

    def _validated_fact_sources(
        self,
        database: FinanceDatabase,
        query_spec: QuerySpec,
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        metric_keys = query_spec.metrics or (query_spec.metric,)
        metric_fields = [self.sql_planner.registry.resolve_metric(metric).field for metric in metric_keys]
        sources: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            stock_code = str(row.get("stock_code") or query_spec.stock_code)
            period = str(row.get("report_period") or query_spec.report_period)
            if not stock_code:
                resolved = self.analyzer.company_resolver.resolve(str(row.get("stock_abbr") or ""))
                stock_code = str(resolved.get("stock_code") or "") if resolved else ""
            if not stock_code or not period:
                raise UntrustedFactError("query result is missing stock_code or report_period provenance keys")
            for metric in metric_fields:
                if row.get(metric) is None:
                    raise UntrustedFactError(f"query result has no reviewable value for {stock_code}/{period}/{metric}")
                target = self.sql_planner.registry.resolve_metric(metric)
                period_type = expected_period_type_for_table(target.table).value
                marker = database.parameter_marker
                facts = database.query(
                    "SELECT ff.fact_key, ff.document_id, ff.normalized_value, ff.target_unit, ff.currency, "
                    "ff.statement_scope, ff.page_no, ff.table_name, ff.row_label, ff.column_label, ff.review_version "
                    "FROM financial_fact ff INNER JOIN financial_source fs ON fs.source_key = ff.source_key "
                    "AND fs.stock_code = ff.stock_code AND fs.period = ff.period "
                    "WHERE ff.stock_code = {m} AND ff.period = {m} AND ff.metric = {m} "
                    "AND ff.validation_status = 'VALIDATED' AND ff.statement_scope = 'consolidated' "
                    "AND ff.period_type = {m} AND fs.authority_status = {m}".format(m=marker),
                    (stock_code, period, metric, period_type, SourceAuthorityStatus.CURRENT.value),
                    use_cache=False,
                )
                if len(facts) != 1:
                    raise UntrustedFactError(
                        f"expected one consolidated VALIDATED fact for {stock_code}/{period}/{metric}, got {len(facts)}"
                    )
                fact = facts[0]
                if not fact["document_id"]:
                    raise UntrustedFactError(
                        f"validated fact {fact['fact_key']} is not linked to a document_id and cannot be exposed via Web"
                    )
                if str(fact["fact_key"]) in seen:
                    continue
                seen.add(str(fact["fact_key"]))
                sources.append(
                    {
                        "fact_key": fact["fact_key"],
                        "document_id": fact["document_id"],
                        "stock_code": stock_code,
                        "period": period,
                        "metric": metric,
                        "normalized_value": fact["normalized_value"],
                        "unit": fact["target_unit"],
                        "currency": fact["currency"],
                        "statement_scope": fact["statement_scope"],
                        "page_no": fact["page_no"],
                        "table_name": fact["table_name"],
                        "row_label": fact["row_label"],
                        "column_label": fact["column_label"],
                        "review_version": fact["review_version"],
                    }
                )
        return sources

    def _require_verified_source_content(self, sources: object) -> None:
        if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
            raise UntrustedFactError("verified answer has no valid source evidence")
        document_ids: list[str] = []
        for source in sources:
            if not isinstance(source, Mapping):
                raise UntrustedFactError("verified answer contains invalid source evidence")
            document_id = str(source.get("document_id") or "").strip()
            if not document_id:
                raise UntrustedFactError("verified answer source is missing document_id")
            document_ids.append(document_id)
        self.store.require_current_document_content(document_ids)

    @staticmethod
    def _compose_content(query_spec: QuerySpec, rows: Sequence[Mapping[str, Any]]) -> str:
        if not rows:
            return "未查询到可由已验证事实支持的数据。"
        metric_keys = query_spec.metrics or (query_spec.metric,)
        fields = [(TASK2_METRICS[key].field, TASK2_METRICS[key]) for key in metric_keys]
        parts: list[str] = []
        for row in rows:
            company = str(row.get("stock_abbr") or row.get("stock_code") or "公司")
            period = str(row.get("report_period") or query_spec.report_period)
            values = "，".join(
                f"{metric.label}={row.get(field)}{metric.unit}"
                for field, metric in fields
                if row.get(field) is not None
            )
            parts.append(f"{company} {period}：{values}")
        return "；".join(parts)

    @staticmethod
    def _chart_spec(
        query_spec_payload: Mapping[str, Any],
        query_spec: QuerySpec,
        rows: Sequence[Mapping[str, Any]],
    ) -> ChartSpec | None:
        chart_type = str(query_spec_payload.get("chart_request") or "")
        if not chart_type:
            return None
        if chart_type not in {"line", "bar", "horizontal_bar", "pie"}:
            raise ValueError(f"unsupported chart type: {chart_type!r}")
        x_field = "report_period" if all(row.get("report_period") is not None for row in rows) else "stock_abbr"
        metric_keys = query_spec.metrics or (query_spec.metric,)
        series = tuple(
            ChartSeries(
                name=TASK2_METRICS[key].label,
                field=TASK2_METRICS[key].field,
                unit=TASK2_METRICS[key].unit,
                values=tuple(row.get(TASK2_METRICS[key].field) for row in rows),
            )
            for key in metric_keys
        )
        return ChartSpec(
            chart_type=chart_type,
            title=" / ".join(item.name for item in series),
            x_field=x_field,
            x_values=tuple(str(row.get(x_field) or row.get("stock_code") or "") for row in rows),
            series=series,
        )

    @staticmethod
    def _formula(calculation: Mapping[str, Any]) -> str | None:
        if calculation.get("operator") != "ratio":
            return None
        return f"{calculation['numerator']} / {calculation['denominator']}"


class WebApplicationFacade:
    """Adapt the persistent domain service to the versioned HTTP DTO contract."""

    def __init__(self, store: WebStore, question_service: ApplicationService) -> None:
        self.store = store
        self.question_service = question_service

    def register_document(self, document: Any) -> Any:
        try:
            stored = self.store.create_document(
                document_id=document.document_id,
                kind=document.kind,
                original_name=document.original_name,
                storage_key=document.storage_key,
                sha256=document.sha256,
                size_bytes=document.size_bytes,
                mime_type=document.mime_type,
                status=document.status,
            )
        except Exception as exc:
            self._raise_service_error(exc)
        return self._api_document_record(stored)

    def list_documents(self, *, cursor: str | None, limit: int) -> tuple[list[Any], str | None, int]:
        offset = self._offset(cursor)
        records, total = self.store.list_documents(limit=limit + 1, offset=offset)
        has_more = len(records) > limit
        return (
            [self._api_document_record(item) for item in records[:limit]],
            str(offset + limit) if has_more else None,
            total,
        )

    def get_document(self, document_id: str) -> Any:
        try:
            return self._api_document_record(self.store.get_document(document_id))
        except Exception as exc:
            self._raise_service_error(exc)

    def create_job(self, request: Any) -> Any:
        try:
            job = self.store.enqueue_document_job(request.document_id, idempotency_key=request.idempotency_key)
            return self._job_response(job)
        except Exception as exc:
            self._raise_service_error(exc)

    def list_jobs(self, *, cursor: str | None, limit: int) -> tuple[list[Any], str | None]:
        offset = self._offset(cursor)
        jobs = self.store.list_jobs(limit=limit + 1, offset=offset)
        has_more = len(jobs) > limit
        return [self._job_response(item) for item in jobs[:limit]], str(offset + limit) if has_more else None

    def get_job(self, job_id: str) -> Any:
        try:
            return self._job_response(self.store.get_job(job_id))
        except Exception as exc:
            self._raise_service_error(exc)

    def retry_job(self, job_id: str, *, idempotency_key: str) -> Any:
        try:
            return self._job_response(self.store.retry_job(job_id, idempotency_key=idempotency_key))
        except Exception as exc:
            self._raise_service_error(exc)

    def create_conversation(self, request: Any) -> Any:
        return self._conversation_response(self.store.create_conversation(title=request.title or "新会话"))

    def get_conversation(self, conversation_id: str) -> Any:
        try:
            return self._conversation_response(self.store.get_conversation(conversation_id))
        except Exception as exc:
            self._raise_service_error(exc)

    def create_turn(self, conversation_id: str, request: Any, *, request_id: str) -> Any:
        from .api_models import ConversationTurnResponse, TurnResponse

        try:
            result = self.question_service.ask(
                conversation_id,
                request.question,
                expected_version=request.expected_version,
                idempotency_key=request.idempotency_key,
                request_id=request_id,
            )
            turn = self.store.get_turn(result.turn_id)
            answer = self._api_answer(result.answer, result.query_run_id)
            status = "NEEDS_CLARIFICATION" if answer.status == "NEEDS_CLARIFICATION" else "COMPLETED"
            return ConversationTurnResponse(
                turn=TurnResponse(
                    turn_id=turn.turn_id,
                    conversation_id=turn.conversation_id,
                    sequence=turn.sequence_no,
                    question=turn.question,
                    status=status,
                    answer=answer,
                    query_run_id=turn.query_run_id,
                    created_at=turn.created_at,
                ),
                conversation=self.get_conversation(conversation_id),
            )
        except Exception as exc:
            self._raise_service_error(exc)

    def list_facts(
        self,
        *,
        validation_status: str | None,
        company_id: str | None,
        period: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[Any], str | None, int]:
        offset = self._offset(cursor)
        try:
            rows, total = self.store.list_facts(
                validation_status=validation_status,
                company_id=company_id,
                period=period,
                limit=limit + 1,
                offset=offset,
            )
            has_more = len(rows) > limit
            return (
                [self._fact_response(row) for row in rows[:limit]],
                str(offset + limit) if has_more else None,
                total,
            )
        except Exception as exc:
            self._raise_service_error(exc)

    def get_fact(self, fact_key: str) -> Any:
        try:
            return self._fact_response(self.store.get_fact(fact_key))
        except Exception as exc:
            self._raise_service_error(exc)

    def decide_fact(self, fact_key: str, request: Any) -> Any:
        from .api_models import FactDecisionResponse

        corrections = request.replacement.model_dump(exclude_none=True) if request.replacement is not None else None
        try:
            result = self.store.review_fact(
                fact_key,
                action=request.action,
                expected_version=request.expected_version,
                actor_label="未认证的本地操作标识",
                reason=request.reason,
                note=request.note or "",
                corrections=corrections,
            )
            fact = self._fact_response(self.store.get_fact(result.fact_key))
            return FactDecisionResponse(fact=fact, event_id=result.event_id, version=fact.version)
        except Exception as exc:
            self._raise_service_error(exc)

    def get_query_run(self, query_run_id: str) -> Any:
        from .api_models import QueryRunResponse

        try:
            row = self.store.get_query_run(query_run_id)
            return QueryRunResponse(
                query_run_id=row["query_run_id"],
                conversation_id=row["conversation_id"],
                question=row["normalized_question"],
                query_spec=row["query_spec"],
                sql=row["sql"],
                parameters=row["params"],
                answer_result=self._api_answer_from_payload(row["answer_payload"], query_run_id),
                status=row["status"],
                created_at=row["created_at"],
            )
        except Exception as exc:
            self._raise_service_error(exc)

    def export_query_run(self, query_run_id: str) -> tuple[bytes, str]:
        from io import BytesIO

        from openpyxl import Workbook

        run = self.store.get_query_run(query_run_id)
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("查询结果")
        rows = list(run["answer_payload"].get("rows", []))
        if rows:
            columns = list(rows[0])
            sheet.append(columns)
            for row in rows:
                sheet.append([row.get(column) for column in columns])
        else:
            sheet.append(["status", "message"])
            sheet.append([run["answer_payload"].get("status"), run["answer_payload"].get("content")])
        output = BytesIO()
        workbook.save(output)
        return output.getvalue(), f"query-{query_run_id}.xlsx"

    @staticmethod
    def _offset(cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            offset = int(cursor)
        except ValueError as exc:
            raise ValueError("cursor must be an integer offset") from exc
        if offset < 0:
            raise ValueError("cursor must be non-negative")
        return offset

    @staticmethod
    def _job_response(job: Any) -> Any:
        from .api_models import JobResponse

        return JobResponse(
            job_id=job.job_id,
            document_id=str(job.input_payload["document_id"]),
            kind=job.job_type,
            status=job.status,
            stage=job.stage,
            progress=round(job.progress * 100),
            attempt=job.attempt,
            error_code=job.error_code,
            error_message=job.error_message,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )

    @staticmethod
    def _api_document_record(document: Any) -> Any:
        from .api_models import DocumentRecord

        payload = asdict(document)
        payload.pop("updated_at", None)
        payload.pop("page_count", None)
        payload.pop("active_ingestion_job_id", None)
        return DocumentRecord.model_validate(payload)

    def _conversation_response(self, conversation: Any) -> Any:
        from .api_models import ConversationResponse, TurnResponse

        turns = []
        for turn in self.store.list_turns(conversation.conversation_id):
            answer = self._api_answer_from_payload(turn.answer_payload, turn.query_run_id)
            turn_status = "NEEDS_CLARIFICATION" if answer.status == "NEEDS_CLARIFICATION" else "COMPLETED"
            if answer.status == "FAILED":
                turn_status = "FAILED"
            turns.append(
                TurnResponse(
                    turn_id=turn.turn_id,
                    conversation_id=turn.conversation_id,
                    sequence=turn.sequence_no,
                    question=turn.question,
                    status=turn_status,
                    answer=answer,
                    query_run_id=turn.query_run_id,
                    created_at=turn.created_at,
                )
            )

        return ConversationResponse(
            conversation_id=conversation.conversation_id,
            title=conversation.title,
            version=conversation.version,
            state=conversation.session_state,
            turns=turns,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )

    @staticmethod
    def _fact_response(row: Mapping[str, Any]) -> Any:
        from .api_models import FactResponse

        issues = row.get("validation_issues")
        if isinstance(issues, str):
            import json

            issues = json.loads(issues)
        return FactResponse(
            fact_key=row["fact_key"],
            company_id=row["company_id"],
            stock_code=row["stock_code"],
            period=row["period"],
            statement_scope=row["statement_scope"],
            period_type=row["period_type"],
            metric=row["metric"],
            raw_value=str(row["raw_value"]),
            normalized_value=str(row["normalized_value"]),
            source_unit=row["source_unit"],
            target_unit=row["target_unit"],
            currency=row["currency"],
            document_id=WebApplicationFacade._required_document_id(row),
            page_no=row["page_no"],
            table_name=row["table_name"],
            row_label=row["row_label"],
            column_label=row["column_label"],
            confidence=row["confidence"],
            validation_status=row["validation_status"],
            validation_issues=issues or [],
            version=max(1, int(row["review_version"] or 1)),
        )

    def _api_answer(self, answer: AnswerResult, query_run_id: str | None) -> Any:
        return self._api_answer_from_payload(answer.to_dict(), query_run_id)

    @staticmethod
    def _required_document_id(row: Mapping[str, Any]) -> str:
        document_id = row.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            from .api_models import ServiceUnavailableError

            raise ServiceUnavailableError(
                "fact_document_unlinked",
                f"Fact {row.get('fact_key')} is not linked to a registered document",
            )
        return document_id

    @staticmethod
    def _api_answer_from_payload(payload: Mapping[str, Any], query_run_id: str | None) -> Any:
        from .api_models import AnswerResult as ApiAnswerResult
        from .api_models import AnswerValue, ChartAxis, EvidenceResponse, TableColumn, TableSpec
        from .api_models import ChartSeries as ApiChartSeries
        from .api_models import ChartSpec as ApiChartSpec

        sources = list(payload.get("sources", []))
        evidence = [
            EvidenceResponse(
                document_id=WebApplicationFacade._required_document_id(source),
                page_no=source["page_no"],
                snippet=source.get("row_label") or source.get("metric") or "",
                table_name=source.get("table_name"),
                row_label=source.get("row_label"),
                column_label=source.get("column_label"),
            )
            for source in sources
            if source.get("page_no") is not None
        ]
        rows = list(payload.get("rows", []))
        metric_sources = {source.get("metric"): source for source in sources}
        values = [
            AnswerValue(
                label=str(source.get("metric")),
                value=str(source.get("normalized_value")),
                unit=str(source.get("unit") or ""),
                currency=str(source.get("currency") or "CNY"),
                company=str(source.get("stock_code") or ""),
                period=str(source.get("period") or ""),
                statement_scope=source.get("statement_scope") or "consolidated",
                fact_keys=[str(source["fact_key"])],
            )
            for source in sources
        ]
        table = None
        if rows:
            table = TableSpec(
                columns=[
                    TableColumn(
                        key=key,
                        label=TASK2_METRICS.get(key, type("Metric", (), {"label": key})()).label,
                        unit=metric_sources.get(key, {}).get("unit"),
                    )
                    for key in rows[0]
                ],
                rows=rows,
            )
        chart = None
        raw_chart = payload.get("chart")
        if raw_chart and query_run_id:
            chart_type = raw_chart["chart_type"]
            if chart_type != "pie":
                chart = ApiChartSpec(
                    type=chart_type,
                    title=raw_chart.get("title"),
                    x_axis=ChartAxis(label=raw_chart.get("x_field"), categories=raw_chart.get("x_values", [])),
                    series=[
                        ApiChartSeries(name=item["name"], values=item["values"], unit=item.get("unit"))
                        for item in raw_chart.get("series", [])
                    ],
                    source_query_run_id=query_run_id,
                )
        status = str(payload["status"])
        return ApiAnswerResult(
            status=status,
            text=payload.get("content") if status != "NEEDS_CLARIFICATION" else None,
            clarification=payload.get("content") if status == "NEEDS_CLARIFICATION" else None,
            formula=payload.get("formula"),
            values=values,
            chart_spec=chart,
            table=table,
            evidence=evidence,
        )

    @staticmethod
    def _raise_service_error(exc: Exception) -> None:
        from .api_models import ServiceConflictError, ServiceNotFoundError, ServiceValidationError
        from .facts import IncompleteFactEvidenceError
        from .web_store import ConflictError, InvalidStateError, SourceContentMismatchError

        if isinstance(exc, KeyError):
            raise ServiceNotFoundError("not_found", str(exc)) from exc
        if isinstance(exc, IncompleteFactEvidenceError):
            raise ServiceConflictError("incomplete_fact_evidence", str(exc)) from exc
        if isinstance(exc, SourceContentMismatchError):
            raise ServiceConflictError("source_content_mismatch", str(exc)) from exc
        if isinstance(exc, (ConflictError, InvalidStateError)):
            raise ServiceConflictError("conflict", str(exc)) from exc
        if isinstance(exc, (ValueError, TypeError)):
            raise ServiceValidationError("validation_failed", str(exc)) from exc
        raise exc
