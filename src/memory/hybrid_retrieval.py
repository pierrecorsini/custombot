"""
memory.hybrid_retrieval — Combine BM25 keyword scoring with vector similarity.

Uses Reciprocal Rank Fusion (RRF) to merge lexical and semantic results
with configurable weights.  Falls back to keyword-only when vector memory
is unavailable.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Default RRF constant (same as Elasticsearch default).
DEFAULT_RRF_K = 60


class VectorSearch(Protocol):
    """Protocol for vector memory search backends."""

    async def search(self, chat_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class MemoryResult:
    """A single retrieved memory item with combined relevance score."""

    text: str
    score: float
    source: str  # "lexical", "semantic", or "hybrid"
    metadata: dict[str, Any] = field(default_factory=dict)


class HybridRetriever:
    """Hybrid retrieval combining BM25-like scoring with vector similarity."""

    def __init__(
        self,
        vector_memory: VectorSearch | None = None,
        lexical_weight: float = 0.4,
        semantic_weight: float = 0.6,
    ) -> None:
        self._vector = vector_memory
        self._lexical_weight = lexical_weight
        self._semantic_weight = semantic_weight
        # Inverted index: {term: {doc_id: term_frequency}}
        self._index: dict[str, dict[str, float]] = {}
        # Document store: {doc_id: text}
        self._docs: dict[str, str] = {}
        # Document frequency: {term: doc_count}
        self._doc_freq: dict[str, int] = {}

    def index_document(self, doc_id: str, text: str) -> None:
        """Add or update a document in the BM25 index."""
        self._docs[doc_id] = text
        terms = self._tokenize(text)
        tf: dict[str, float] = {}
        for term in terms:
            tf[term] = tf.get(term, 0.0) + 1.0

        self._index[doc_id] = tf
        for term in tf:
            self._doc_freq[term] = self._doc_freq.get(term, 0) + 1

    def _tokenize(self, text: str) -> list[str]:
        """Lowercase, strip punctuation, split into words."""
        return re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", text.lower())

    def _bm25_score(
        self,
        query_terms: list[str],
        doc_id: str,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> float:
        """Compute BM25 score for a single document."""
        doc_tf = self._index.get(doc_id, {})
        doc_len = sum(doc_tf.values())
        avg_len = sum(sum(t.values()) for t in self._index.values()) / max(len(self._index), 1)
        n_docs = max(len(self._index), 1)

        score = 0.0
        for term in query_terms:
            if term not in doc_tf:
                continue
            tf = doc_tf[term]
            df = self._doc_freq.get(term, 0)
            idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
            norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / avg_len))
            score += idf * norm
        return score

    def _lexical_search(self, query: str, top_k: int) -> list[MemoryResult]:
        """Score all indexed documents against the query using BM25."""
        query_terms = self._tokenize(query)
        if not query_terms:
            return []

        scored: list[tuple[str, float]] = []
        for doc_id in self._index:
            s = self._bm25_score(query_terms, doc_id)
            if s > 0:
                scored.append((doc_id, s))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            MemoryResult(
                text=self._docs[doc_id],
                score=score,
                source="lexical",
                metadata={"doc_id": doc_id},
            )
            for doc_id, score in scored[:top_k]
        ]

    async def _semantic_search(
        self,
        chat_id: str,
        query: str,
        top_k: int,
    ) -> list[MemoryResult]:
        """Search via vector memory when available."""
        if self._vector is None:
            return []
        try:
            results = await self._vector.search(chat_id, query, limit=top_k)
        except Exception as exc:
            log.warning("Vector search failed, skipping: %s", exc)
            return []
        return [
            MemoryResult(
                text=r.get("text", ""),
                score=1.0 - r.get("distance", 1.0),
                source="semantic",
                metadata={"id": r.get("id"), "category": r.get("category", "")},
            )
            for r in results
        ]

    @staticmethod
    def rrf_merge(
        lexical_results: list[MemoryResult],
        semantic_results: list[MemoryResult],
        k: int = DEFAULT_RRF_K,
        lexical_weight: float = 1.0,
        semantic_weight: float = 1.0,
    ) -> list[MemoryResult]:
        """Reciprocal Rank Fusion: combine ranked lists by weight/(k + rank)."""
        scores: dict[str, float] = {}
        texts: dict[str, str] = {}
        metadata: dict[str, dict[str, Any]] = {}

        for rank, result in enumerate(lexical_results, 1):
            key = result.text
            scores[key] = scores.get(key, 0.0) + lexical_weight / (k + rank)
            texts[key] = result.text
            metadata[key] = result.metadata

        for rank, result in enumerate(semantic_results, 1):
            key = result.text
            scores[key] = scores.get(key, 0.0) + semantic_weight / (k + rank)
            texts[key] = result.text
            metadata.setdefault(key, result.metadata)

        merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [
            MemoryResult(
                text=texts[key],
                score=score,
                source="hybrid",
                metadata=metadata[key],
            )
            for key, score in merged
        ]

    async def search(
        self,
        chat_id: str,
        query: str,
        top_k: int = 10,
    ) -> list[MemoryResult]:
        """Hybrid search combining BM25 and vector similarity via RRF."""
        lexical = self._lexical_search(query, top_k)
        semantic = await self._semantic_search(chat_id, query, top_k)

        if not semantic:
            return lexical[:top_k]
        if not lexical:
            return semantic[:top_k]

        merged = self.rrf_merge(
            lexical, semantic,
            lexical_weight=self._lexical_weight,
            semantic_weight=self._semantic_weight,
        )
        return merged[:top_k]
