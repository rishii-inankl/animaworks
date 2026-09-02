from __future__ import annotations

from pathlib import Path
import json
import logging
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from core.memory.rag.sqlite_health import SQLiteHealthResult


def test_vector_worker_shutdown_closes_cached_stores(monkeypatch) -> None:
    monkeypatch.delenv("ANIMAWORKS_VECTOR_URL", raising=False)

    from core.memory.rag.vector_worker import create_app

    with (
        patch("core.memory.rag.singleton.close_all_vector_stores") as close_all,
        TestClient(create_app()) as client,
    ):
        assert client.get("/health").json() == {"status": "ok"}

    close_all.assert_called_once()


def test_vector_worker_quick_check_endpoint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANIMAWORKS_VECTOR_URL", raising=False)

    from core.memory.rag.vector_worker import create_app

    check = MagicMock(
        return_value=SQLiteHealthResult(
            db_path=tmp_path / "chroma.sqlite3",
            ok=True,
            status="ok",
            details=("ok",),
        )
    )
    with (
        patch("core.memory.rag.sqlite_health.check_anima_vectordb_health", check),
        TestClient(create_app()) as client,
    ):
        resp = client.post(
            "/quick-check",
            json={
                "anima_name": "sora",
                "timeout_seconds": 3,
                "source": "test_quick_check",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    check.assert_called_once_with(
        "sora",
        timeout_seconds=3.0,
        source="test_quick_check",
        record_repair=True,
    )


def test_vector_worker_quiesce_closes_store_and_blocks_operations(monkeypatch) -> None:
    monkeypatch.delenv("ANIMAWORKS_VECTOR_URL", raising=False)

    from core.memory.rag.vector_worker import create_app

    with (
        patch("core.memory.rag.singleton.reset_vector_store") as reset,
        TestClient(create_app()) as client,
    ):
        response = client.post("/admin/quiesce", json={"anima_name": "sora"})
        blocked = client.post("/list-collections", json={"anima_name": "sora"})
        resumed = client.post("/admin/resume", json={"anima_name": "sora"})

    assert response.json() == {"status": "quiesce", "anima_name": "sora", "closed": True}
    assert blocked.status_code == 423
    assert resumed.json() == {"status": "resume", "anima_name": "sora"}
    reset.assert_called_once_with("sora")


def test_vector_worker_log_handler_is_rotating_structured_json(tmp_path: Path) -> None:
    from core.memory.rag.vector_worker import configure_worker_logging

    root = logging.getLogger()
    previous = list(root.handlers)
    for handler in previous:
        root.removeHandler(handler)
    handler = configure_worker_logging(tmp_path / "vector-worker.log")
    try:
        record = logging.LogRecord("vector", logging.INFO, __file__, 1, "done", (), None)
        record.anima = "sora"
        record.operation = "upsert"
        record.collection = "sora_knowledge"
        payload = json.loads(handler.format(record))

        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 5
        assert payload["ts"]
        assert payload["anima"] == "sora"
        assert payload["operation"] == "upsert"
        assert payload["collection"] == "sora_knowledge"
    finally:
        root.removeHandler(handler)
        handler.close()
        for previous_handler in previous:
            root.addHandler(previous_handler)
