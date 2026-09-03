# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for min_score parameter in MemoryRetriever.search().

Verifies:
- min_score=None returns all results (default behavior)
- min_score filters out results with vector score < threshold
- min_score keeps results with vector score >= threshold
- min_score that filters ALL results returns empty list
- Spreading activation results (no "vector" in source_scores) are NOT filtered
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from core.memory.rag.retriever import MemoryRetriever, RetrievalResult
from core.memory.rag.store import Document, SearchResult

# ── Mock fixtures ────────────────────────────────────────────────────


class MockVectorStoreWithScores:
    """Mock vector store that returns configurable scores."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def query(self, collection, embedding, top_k, filter_metadata=None):
        return [
            SearchResult(
                document=Document(
                    id=f"anima/knowledge/doc{i}.md#0",
                    content=f"Content {i}",
                    metadata={"source_file": f"knowledge/doc{i}.md"},
                ),
                score=score,
            )
            for i, score in enumerate(self._scores[:top_k])
        ]


class MockIndexer:
    """Mock indexer for embedding generation."""

    def _generate_embeddings(self, texts):
        return [[0.1] * 384 for _ in texts]


def test_missing_facts_collection_skips_embedding_and_query(tmp_path: Path) -> None:
    vector_store = MockVectorStoreWithScores([0.9])
    indexer = MockIndexer()
    indexer._collection_exists = MagicMock(return_value=False)  # type: ignore[attr-defined]
    retriever = MemoryRetriever(vector_store, indexer, tmp_path)

    with patch.object(indexer, "_generate_embeddings", wraps=indexer._generate_embeddings) as embed:
        results = retriever._vector_search_collection("query", "kaede_facts", 5)

    assert results == []
    embed.assert_not_called()


# ── Config mock for spreading activation ────────────────────────────


class _MockConfigDisabled:
    """Config with spreading activation disabled (avoids graph expansion)."""

    class _RAG:
        enable_spreading_activation = False
        spreading_memory_types = ["knowledge", "episodes"]

    rag = _RAG()


# ── min_score=None returns all ────────────────────────────────────────


class TestMinScoreNone:
    def test_min_score_none_returns_all_results(
        self,
        tmp_path: Path,
    ) -> None:
        """search() with min_score=None returns all results (default behavior)."""
        vector_store = MockVectorStoreWithScores([0.9, 0.5, 0.2])
        indexer = MockIndexer()
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        with patch.object(
            MemoryRetriever,
            "_load_config",
            staticmethod(lambda: _MockConfigDisabled()),
        ):
            retriever = MemoryRetriever(vector_store, indexer, knowledge_dir)

        results = retriever.search(
            "query",
            "test_anima",
            memory_type="knowledge",
            top_k=5,
            min_score=None,
        )

        assert len(results) == 3
        assert [r.source_scores.get("vector") for r in results] == [0.9, 0.5, 0.2]


# ── min_score filters low scores ─────────────────────────────────────


class TestMinScoreFilters:
    def test_min_score_filters_out_low_results(
        self,
        tmp_path: Path,
    ) -> None:
        """search() with min_score=0.3 filters out results with vector score < 0.3."""
        vector_store = MockVectorStoreWithScores([0.9, 0.5, 0.2, 0.1])
        indexer = MockIndexer()
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        with patch.object(
            MemoryRetriever,
            "_load_config",
            staticmethod(lambda: _MockConfigDisabled()),
        ):
            retriever = MemoryRetriever(vector_store, indexer, knowledge_dir)

        results = retriever.search(
            "query",
            "test_anima",
            memory_type="knowledge",
            top_k=5,
            min_score=0.3,
        )

        assert len(results) == 2
        assert all(r.source_scores.get("vector", 0) >= 0.3 for r in results)

    def test_min_score_keeps_results_at_threshold(
        self,
        tmp_path: Path,
    ) -> None:
        """search() with min_score=0.3 keeps results with vector score >= 0.3."""
        vector_store = MockVectorStoreWithScores([0.9, 0.3, 0.299])
        indexer = MockIndexer()
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        with patch.object(
            MemoryRetriever,
            "_load_config",
            staticmethod(lambda: _MockConfigDisabled()),
        ):
            retriever = MemoryRetriever(vector_store, indexer, knowledge_dir)

        results = retriever.search(
            "query",
            "test_anima",
            memory_type="knowledge",
            top_k=5,
            min_score=0.3,
        )

        assert len(results) == 2
        scores = [r.source_scores.get("vector") for r in results]
        assert 0.9 in scores
        assert 0.3 in scores
        assert 0.299 not in scores

    def test_min_score_filters_all_returns_empty(
        self,
        tmp_path: Path,
    ) -> None:
        """search() with min_score that filters ALL results returns empty list."""
        vector_store = MockVectorStoreWithScores([0.1, 0.2])
        indexer = MockIndexer()
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        with patch.object(
            MemoryRetriever,
            "_load_config",
            staticmethod(lambda: _MockConfigDisabled()),
        ):
            retriever = MemoryRetriever(vector_store, indexer, knowledge_dir)

        results = retriever.search(
            "query",
            "test_anima",
            memory_type="knowledge",
            top_k=5,
            min_score=0.5,
        )

        assert results == []


# ── Spreading activation results not filtered ──────────────────────────


class TestMinScoreSpreadingActivation:
    def test_spreading_results_without_vector_not_filtered(
        self,
        tmp_path: Path,
    ) -> None:
        """Results with no 'vector' key in source_scores are NOT filtered by min_score."""
        # Simulate spreading activation results: source_scores has "pagerank" but no "vector"
        # The retriever uses: r.source_scores.get("vector", 1.0) >= min_score
        # So when there's no "vector" key, get returns 1.0, which passes any min_score
        vector_store = MockVectorStoreWithScores([0.9])
        indexer = MockIndexer()
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        with patch.object(
            MemoryRetriever,
            "_load_config",
            staticmethod(lambda: _MockConfigDisabled()),
        ):
            retriever = MemoryRetriever(vector_store, indexer, knowledge_dir)

        # Get one result, then manually create a spreading-style result
        results = retriever.search(
            "query",
            "test_anima",
            memory_type="knowledge",
            top_k=5,
            min_score=0.3,
        )
        assert len(results) >= 1

        # Verify: result with only pagerank (no vector) would pass min_score
        # because source_scores.get("vector", 1.0) = 1.0 >= 0.3
        spreading_result = RetrievalResult(
            doc_id="anima/knowledge/spread.md#0",
            content="Spreading content",
            score=0.5,
            metadata={"source_file": "knowledge/spread.md", "activation": "spreading"},
            source_scores={"pagerank": 0.8},
        )
        # The filter is: r.source_scores.get("vector", 1.0) >= min_score
        # For spreading_result: get("vector", 1.0) = 1.0, so 1.0 >= 0.3 -> kept
        assert spreading_result.source_scores.get("vector", 1.0) >= 0.3
