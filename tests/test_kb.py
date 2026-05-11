from smart_finqa.kb import SimpleKnowledgeBase


def test_keyword_retrieval() -> None:
    kb = SimpleKnowledgeBase()
    kb.add_document(
        title="行业研报",
        paper_path="./行业研报/xx.pdf",
        text="2025 年产品目录新增 7 个重点产品，创新导向持续强化。",
    )
    kb.add_document(
        title="个股研报",
        paper_path="./个股研报/yy.pdf",
        text="公司收入增长主要来自品牌力和渠道扩张。",
    )
    results = kb.search("产品目录新增重点产品", top_k=1)
    assert len(results) == 1
    assert "产品目录" in results[0]["text"]
