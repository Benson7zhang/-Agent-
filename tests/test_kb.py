from __future__ import annotations

from datetime import date

import pytest

from smart_finqa.kb import (
    EvidenceFilter,
    EvidenceStatus,
    KnowledgeDocument,
    SimpleKnowledgeBase,
)


def test_keyword_retrieval() -> None:
    kb = SimpleKnowledgeBase()
    kb.add_document(
        title="行业研报",
        paper_path="./行业研报/xx.pdf",
        text="2025 年产品目录新增 7 个重点产品，创新导向持续强化。",
        page_no=7,
    )
    kb.add_document(
        title="个股研报",
        paper_path="./个股研报/yy.pdf",
        text="公司收入增长主要来自品牌力和渠道扩张。",
        page_no=3,
    )
    results = kb.search("产品目录新增重点产品", top_k=1)
    assert len(results) == 1
    assert "产品目录" in results[0]["text"]
    assert results[0]["page_no"] == 7


def test_search_filters_company_industry_date_and_page() -> None:
    kb = SimpleKnowledgeBase()
    kb.add_document(
        title="目标研报",
        paper_path="reports/target.pdf",
        text="渠道扩张推动营业收入增长。",
        company="金花股份",
        industry="医药",
        published_at="2025-04-30",
        page_no=8,
    )
    kb.add_document(
        title="其他公司研报",
        paper_path="reports/other.pdf",
        text="渠道扩张推动营业收入增长。",
        company="其他公司",
        industry="医药",
        published_at="2025-04-30",
        page_no=8,
    )
    kb.add_document(
        title="过期目标研报",
        paper_path="reports/old.pdf",
        text="渠道扩张推动营业收入增长。",
        company="金花股份",
        industry="医药",
        published_at="2024-04-30",
        page_no=8,
    )

    result = kb.search_evidence(
        "营业收入增长",
        filters=EvidenceFilter(
            company="金花股份",
            industry="医药",
            published_at="2025-04-30",
            published_from=date(2025, 1, 1),
            published_to=date(2025, 12, 31),
            page_no=8,
        ),
    )

    assert result.status is EvidenceStatus.FOUND
    assert [hit.document.title for hit in result.hits] == ["目标研报"]


def test_missing_page_number_is_not_returned_as_evidence() -> None:
    kb = SimpleKnowledgeBase()
    kb.add_document(
        title="无法定位的研报",
        paper_path="reports/unlocated.pdf",
        text="营业收入增长主要来自渠道扩张。",
    )

    result = kb.search_evidence("营业收入增长")

    assert result.status is EvidenceStatus.INSUFFICIENT_EVIDENCE
    assert result.message == "证据不足"
    assert result.hits == ()
    assert result.citations == ()


def test_citation_can_be_verified_against_source_page() -> None:
    kb = SimpleKnowledgeBase()
    kb.add_document(
        title="个股研报",
        paper_path="reports/company.pdf",
        text="公司收入增长主要来自品牌力和渠道扩张。",
        company="金花股份",
        page_no=12,
    )

    result = kb.search_evidence("收入增长渠道扩张")

    citation = result.citations[0]
    assert citation.paper_path == "reports/company.pdf"
    assert citation.page_no == 12
    assert citation.verify(result.hits[0].document)
    assert not citation.verify(
        KnowledgeDocument(
            title="个股研报",
            paper_path="reports/company.pdf",
            text="另一页的内容。",
            page_no=13,
        )
    )


class _EmbeddingClient:
    enabled = True

    def embedding(self, text: str) -> list[float]:
        if "渠道" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]


def test_hybrid_retrieval_uses_embeddings_to_rerank_keyword_matches() -> None:
    kb = SimpleKnowledgeBase(llm_client=_EmbeddingClient(), use_embeddings=True)
    kb.add_document(
        title="弱向量匹配",
        paper_path="reports/b.pdf",
        text="收入增长来自成本改善。",
        page_no=2,
        embedding=[0.0, 1.0],
    )
    kb.add_document(
        title="强向量匹配",
        paper_path="reports/a.pdf",
        text="收入增长来自渠道深化。",
        page_no=5,
        embedding=[1.0, 0.0],
    )

    result = kb.search_evidence("收入增长渠道", top_k=2)

    assert [hit.document.title for hit in result.hits] == ["强向量匹配", "弱向量匹配"]
    assert result.hits[0].embedding_score > result.hits[1].embedding_score


def test_reranking_is_deterministic_for_equal_scores() -> None:
    kb = SimpleKnowledgeBase()
    for path, page_no in (("reports/b.pdf", 2), ("reports/a.pdf", 3), ("reports/a.pdf", 1)):
        kb.add_document(title="同分研报", paper_path=path, text="收入增长。", page_no=page_no)

    first = kb.search_evidence("收入增长", top_k=3)
    second = kb.search_evidence("收入增长", top_k=3)

    expected = [("reports/a.pdf", 1), ("reports/a.pdf", 3), ("reports/b.pdf", 2)]
    assert [(hit.document.paper_path, hit.document.page_no) for hit in first.hits] == expected
    assert first.hits == second.hits


@pytest.mark.parametrize("page_no", [0, -1])
def test_add_document_rejects_invalid_page_number(page_no: int) -> None:
    kb = SimpleKnowledgeBase()

    with pytest.raises(ValueError, match="page_no"):
        kb.add_document(title="研报", paper_path="reports/a.pdf", text="正文", page_no=page_no)


def test_add_document_rejects_invalid_published_at() -> None:
    kb = SimpleKnowledgeBase()

    with pytest.raises(ValueError, match="published_at"):
        kb.add_document(
            title="研报",
            paper_path="reports/a.pdf",
            text="正文",
            published_at="2025/04/30",
            page_no=1,
        )
