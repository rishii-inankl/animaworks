"""Reranking must respect the RAG device policy in both retrieval paths."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.memory.retrieval import reranker as reranker_module


@pytest.fixture
def cross_encoder(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMAWORKS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reranker_module, "_reranker", None)
    model = MagicMock()
    model.device = "cpu"
    model.predict.return_value = [0.1, 0.9]
    constructor = MagicMock(return_value=model)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=constructor))
    return constructor


@pytest.mark.parametrize("use_gpu,expected_device", [(None, "cpu"), (False, "cpu"), (True, None)])
def test_device_policy_from_runtime_config(tmp_path, cross_encoder, use_gpu, expected_device):
    # An absent config must also respect RAGConfig's default CPU policy.
    if use_gpu is not None:
        (tmp_path / "config.json").write_text(json.dumps({"rag": {"use_gpu": use_gpu}}))

    reranker = reranker_module.CrossEncoderReranker("custom/model")
    items = [{"content": "first", "origin": "local"}, {"content": "second", "origin": "local"}]
    result = reranker.rerank_sync("query", items)

    cross_encoder.assert_called_once_with("custom/model", device=expected_device)
    assert [row["content"] for row in result] == ["second", "first"]
    assert all(row["search_method"] == "cross_encoder" and row["origin"] == "local" for row in result)
    assert all("ce_score" not in row for row in items)


def test_invalid_config_logs_failure_without_auto_selecting_gpu(tmp_path, cross_encoder, caplog):
    (tmp_path / "config.json").write_text("{invalid json")
    reranker = reranker_module.CrossEncoderReranker()
    items = [{"content": "first", "score": 0.4}, {"content": "second", "score": 0.3}]

    assert reranker.rerank_sync("query", items) == items
    cross_encoder.assert_not_called()
    assert "Cross-encoder unavailable" in caplog.text
    assert not reranker._available


async def test_graph_and_legacy_paths_share_one_cpu_model(cross_encoder, caplog):
    from core.memory.graph.reranker import get_reranker as get_graph_reranker

    legacy = reranker_module.get_reranker()
    graph = get_graph_reranker()
    assert graph is legacy
    cross_encoder.assert_not_called()

    items = [{"fact": "first"}, {"fact": "second"}]
    with caplog.at_level("INFO", logger=reranker_module.__name__):
        sync_result = legacy.rerank_sync("query", items, text_field="fact")
        async_result = await graph.rerank("query", items)

    cross_encoder.assert_called_once_with(reranker_module._DEFAULT_MODEL, device="cpu")
    assert sync_result == async_result
    assert sync_result[0]["fact"] == "second"
    assert f"Cross-encoder loaded: {reranker_module._DEFAULT_MODEL} on cpu" in caplog.text
