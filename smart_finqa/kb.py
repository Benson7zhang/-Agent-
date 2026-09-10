from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from .llm import LLMClient

KEYWORD_WEIGHT = 0.7
EMBEDDING_WEIGHT = 0.3
MAX_CITATION_LENGTH = 240


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}|\d{2,}", text)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", token):
            expanded.extend(token[i : i + 2] for i in range(len(token) - 1))
    return expanded


class EvidenceStatus(str, Enum):
    FOUND = "FOUND"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class EvidenceFilter:
    company: str | None = None
    industry: str | None = None
    published_at: date | str | None = None
    published_from: date | str | None = None
    published_to: date | str | None = None
    page_no: int | None = None

    def __post_init__(self) -> None:
        _validate_optional_text("company", self.company)
        _validate_optional_text("industry", self.industry)
        for field_name in ("published_at", "published_from", "published_to"):
            object.__setattr__(self, field_name, _parse_optional_date(getattr(self, field_name), field_name))
        _validate_page_no(self.page_no, required=False)
        if self.published_from and self.published_to and self.published_from > self.published_to:
            raise ValueError("published_from cannot be later than published_to")

    def matches(self, document: KnowledgeDocument) -> bool:
        if self.company is not None and document.company != self.company:
            return False
        if self.industry is not None and document.industry != self.industry:
            return False
        if self.published_at is not None and document.published_at != self.published_at:
            return False
        if self.published_from is not None and (
            document.published_at is None or document.published_at < self.published_from
        ):
            return False
        if self.published_to is not None and (
            document.published_at is None or document.published_at > self.published_to
        ):
            return False
        return self.page_no is None or document.page_no == self.page_no


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """One page of a report, with metadata used for filtering and citation."""

    title: str
    paper_path: str
    text: str
    paper_image: str = ""
    embedding: tuple[float, ...] | list[float] | None = None
    company: str | None = None
    industry: str | None = None
    published_at: date | str | None = None
    page_no: int | None = None

    def __post_init__(self) -> None:
        _validate_required_text("title", self.title)
        _validate_required_text("paper_path", self.paper_path)
        _validate_required_text("text", self.text)
        _validate_optional_text("company", self.company)
        _validate_optional_text("industry", self.industry)
        _validate_page_no(self.page_no, required=False)
        object.__setattr__(self, "published_at", _parse_optional_date(self.published_at, "published_at"))
        if self.embedding is not None:
            embedding = tuple(float(value) for value in self.embedding)
            if not embedding or any(not math.isfinite(value) for value in embedding):
                raise ValueError("embedding must contain finite numeric values")
            object.__setattr__(self, "embedding", embedding)

    @property
    def can_cite(self) -> bool:
        return self.page_no is not None


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    title: str
    paper_path: str
    page_no: int
    quote: str
    company: str | None = None
    industry: str | None = None
    published_at: date | None = None

    def verify(self, document: KnowledgeDocument) -> bool:
        """Confirm that this quote exists on the cited source page."""
        return (
            document.paper_path == self.paper_path and document.page_no == self.page_no and self.quote in document.text
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "paper_path": self.paper_path,
            "page_no": self.page_no,
            "quote": self.quote,
            "company": self.company,
            "industry": self.industry,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


@dataclass(frozen=True, slots=True)
class EvidenceHit:
    document: KnowledgeDocument
    score: float
    keyword_score: float
    embedding_score: float
    citation: EvidenceCitation


@dataclass(frozen=True, slots=True)
class EvidenceSearchResult:
    status: EvidenceStatus
    message: str
    hits: tuple[EvidenceHit, ...] = ()
    citations: tuple[EvidenceCitation, ...] = ()


class SimpleKnowledgeBase:
    def __init__(self, llm_client: LLMClient | None = None, use_embeddings: bool = False) -> None:
        self.documents: list[KnowledgeDocument] = []
        self.llm_client = llm_client
        self.use_embeddings = bool(use_embeddings)

    def add_document(
        self,
        *,
        title: str,
        paper_path: str,
        text: str,
        paper_image: str = "",
        embedding: list[float] | tuple[float, ...] | None = None,
        company: str | None = None,
        industry: str | None = None,
        published_at: date | str | None = None,
        page_no: int | None = None,
    ) -> None:
        """Add one page; documents without page_no remain non-citable."""
        if embedding is None and self.use_embeddings and self.llm_client and self.llm_client.enabled:
            embedding = self.llm_client.embedding(text[:1500])
        self.documents.append(
            KnowledgeDocument(
                title=title,
                paper_path=paper_path,
                text=text,
                paper_image=paper_image,
                embedding=embedding,
                company=company,
                industry=industry,
                published_at=published_at,
                page_no=page_no,
            )
        )

    def search_evidence(
        self,
        query: str,
        top_k: int = 3,
        *,
        filters: EvidenceFilter | None = None,
    ) -> EvidenceSearchResult:
        _validate_required_text("query", query)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError("top_k must be a positive integer")
        filters = filters or EvidenceFilter()

        query_tokens = tuple(dict.fromkeys(token.lower() for token in _tokenize(query)))
        query_embedding = self._query_embedding(query)
        scored: list[EvidenceHit] = []
        for document in self.documents:
            if not document.can_cite or not filters.matches(document):
                continue
            keyword_score = _keyword_score(query, query_tokens, document.text)
            embedding_score = _embedding_score(query_embedding, document.embedding)
            if keyword_score <= 0 and embedding_score <= 0:
                continue
            score = KEYWORD_WEIGHT * keyword_score + EMBEDDING_WEIGHT * embedding_score
            citation = _build_citation(document, query_tokens)
            scored.append(
                EvidenceHit(
                    document=document,
                    score=round(score, 8),
                    keyword_score=round(keyword_score, 8),
                    embedding_score=round(embedding_score, 8),
                    citation=citation,
                )
            )

        scored.sort(
            key=lambda hit: (
                -hit.score,
                -hit.keyword_score,
                -hit.embedding_score,
                hit.document.paper_path,
                hit.document.page_no or 0,
                hit.document.title,
            )
        )
        hits = tuple(scored[:top_k])
        if not hits:
            return EvidenceSearchResult(
                status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
                message="证据不足",
            )
        return EvidenceSearchResult(
            status=EvidenceStatus.FOUND,
            message="已找到可验证证据",
            hits=hits,
            citations=tuple(hit.citation for hit in hits),
        )

    def search(
        self,
        query: str,
        top_k: int = 3,
        *,
        filters: EvidenceFilter | None = None,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper around the typed evidence search result."""
        result = self.search_evidence(query, top_k=top_k, filters=filters)
        return [
            {
                "title": hit.document.title,
                "paper_path": hit.document.paper_path,
                "text": hit.document.text,
                "paper_image": hit.document.paper_image,
                "company": hit.document.company,
                "industry": hit.document.industry,
                "published_at": hit.document.published_at.isoformat() if hit.document.published_at else None,
                "page_no": hit.document.page_no,
                "score": hit.score,
                "keyword_score": hit.keyword_score,
                "embedding_score": hit.embedding_score,
                "citation": hit.citation.as_dict(),
            }
            for hit in result.hits
        ]

    def _query_embedding(self, query: str) -> tuple[float, ...] | None:
        if not self.use_embeddings or not self.llm_client or not self.llm_client.enabled:
            return None
        return tuple(float(value) for value in self.llm_client.embedding(query[:1500]))


def _keyword_score(query: str, query_tokens: tuple[str, ...], document_text: str) -> float:
    if not query_tokens:
        return 0.0
    lowered = document_text.lower()
    overlap = sum(token in lowered for token in query_tokens) / len(query_tokens)
    phrase_bonus = 0.15 if query.strip().lower() in lowered else 0.0
    return min(1.0, overlap + phrase_bonus)


def _embedding_score(
    query_embedding: tuple[float, ...] | None,
    document_embedding: tuple[float, ...] | list[float] | None,
) -> float:
    if query_embedding is None or document_embedding is None:
        return 0.0
    return max(0.0, _cosine_similarity(query_embedding, tuple(document_embedding)))


def _build_citation(document: KnowledgeDocument, query_tokens: tuple[str, ...]) -> EvidenceCitation:
    if document.page_no is None:
        raise ValueError("cannot cite a document without page_no")
    quote = _citation_quote(document.text, query_tokens)
    return EvidenceCitation(
        title=document.title,
        paper_path=document.paper_path,
        page_no=document.page_no,
        quote=quote,
        company=document.company,
        industry=document.industry,
        published_at=document.published_at if isinstance(document.published_at, date) else None,
    )


def _citation_quote(text: str, query_tokens: tuple[str, ...]) -> str:
    lowered = text.lower()
    first_match = next((lowered.find(token) for token in query_tokens if token in lowered), 0)
    start = max(0, first_match - MAX_CITATION_LENGTH // 3)
    end = min(len(text), start + MAX_CITATION_LENGTH)
    return text[start:end]


def _parse_optional_date(value: date | str | None, field_name: str) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date in YYYY-MM-DD format") from exc


def _validate_required_text(field_name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _validate_optional_text(field_name: str, value: object) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{field_name} must be a non-empty string when provided")


def _validate_page_no(page_no: object, *, required: bool) -> None:
    if page_no is None:
        if required:
            raise ValueError("page_no is required")
        return
    if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1:
        raise ValueError("page_no must be a positive integer")


def _cosine_similarity(v1: tuple[float, ...], v2: tuple[float, ...]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)
