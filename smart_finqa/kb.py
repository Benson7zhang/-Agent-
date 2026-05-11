from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

from .llm import LLMClient

def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}|\d{2,}", text)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", token):
            expanded.extend(token[i : i + 2] for i in range(len(token) - 1))
    return expanded


@dataclass(slots=True)
class KnowledgeDocument:
    title: str
    paper_path: str
    text: str
    paper_image: str = ""
    embedding: list[float] | None = None


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
        embedding: list[float] | None = None,
    ) -> None:
        if embedding is None and self.use_embeddings and self.llm_client and self.llm_client.enabled:
            try:
                embedding = self.llm_client.embedding(text[:1500])
            except Exception:
                embedding = None
        self.documents.append(
            KnowledgeDocument(title=title, paper_path=paper_path, text=text, paper_image=paper_image, embedding=embedding)
        )

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        query_tokens = _tokenize(query)
        query_embedding: list[float] | None = None
        if self.use_embeddings and self.llm_client and self.llm_client.enabled:
            try:
                query_embedding = self.llm_client.embedding(query[:1500])
            except Exception:
                query_embedding = None

        scored: list[tuple[float, KnowledgeDocument]] = []
        for doc in self.documents:
            lowered = doc.text.lower()
            score = 0.0
            for token in query_tokens:
                if token.lower() in lowered:
                    score += 1.0
            if query_embedding and doc.embedding:
                score += 2.0 * _cosine_similarity(query_embedding, doc.embedding)
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda item: item[0], reverse=True)
        result = []
        for score, doc in scored[:top_k]:
            result.append(
                {
                    "title": doc.title,
                    "paper_path": doc.paper_path,
                    "text": doc.text,
                    "paper_image": doc.paper_image,
                    "score": score,
                }
            )
        return result


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)
