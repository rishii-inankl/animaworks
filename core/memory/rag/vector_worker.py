from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

"""Isolated HTTP worker for native ChromaDB vector operations."""

import argparse
import asyncio
import concurrent.futures
import functools
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("animaworks.rag.vector_worker")

_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5


class _VectorJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        return json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                "anima": getattr(record, "anima", None),
                "operation": getattr(record, "operation", None),
                "collection": getattr(record, "collection", None),
            },
            ensure_ascii=False,
        )


def configure_worker_logging(log_file: Path) -> RotatingFileHandler:
    """Configure the dedicated worker log as bounded structured JSON."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(_VectorJSONFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return handler

_native_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="vector-worker-native",
)


class VectorQueryRequest(BaseModel):
    anima_name: str | None = None
    collection: str
    embedding: list[float]
    top_k: int = 10
    filter_metadata: dict[str, str | int | float] | None = None


class VectorUpsertRequest(BaseModel):
    anima_name: str | None = None
    collection: str
    documents: list[dict[str, Any]]


class VectorUpdateMetadataRequest(BaseModel):
    anima_name: str | None = None
    collection: str
    ids: list[str]
    metadatas: list[dict[str, str | int | float]]


class VectorDeleteDocumentsRequest(BaseModel):
    anima_name: str | None = None
    collection: str
    ids: list[str]


class VectorGetByMetadataRequest(BaseModel):
    anima_name: str | None = None
    collection: str
    where: dict[str, str | int | float] = {}
    limit: int = 20


class VectorGetByIdsRequest(BaseModel):
    anima_name: str | None = None
    collection: str
    ids: list[str]


class VectorCollectionRequest(BaseModel):
    anima_name: str | None = None
    collection: str


class VectorListCollectionsRequest(BaseModel):
    anima_name: str | None = None


class VectorQuickCheckRequest(BaseModel):
    anima_name: str
    timeout_seconds: float = 10.0
    source: str = "worker_quick_check"
    record_repair: bool = True


class VectorAdminRequest(BaseModel):
    anima_name: str


async def _run_native(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    call = functools.partial(fn, *args, **kwargs)
    return await loop.run_in_executor(_native_executor, call)


def _log_vector_operation(anima_name: str | None, operation: str, collection: str | None = None) -> None:
    logger.info(
        "Vector operation",
        extra={"anima": anima_name, "operation": operation, "collection": collection},
    )


def _vector_write_failed(anima_name: str | None, operation: str, collection: str) -> JSONResponse:
    logger.warning(
        "Vector operation failed",
        extra={"anima": anima_name, "operation": operation, "collection": collection},
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"Vector {operation} failed",
            "collection": collection,
        },
    )


def _search_results_payload(results) -> dict[str, Any]:
    return {
        "results": [
            {
                "id": r.document.id,
                "content": r.document.content,
                "score": r.score,
                "metadata": r.document.metadata,
            }
            for r in results
        ]
    }


async def _close_native_vector_stores() -> None:
    from core.memory.rag.singleton import close_all_vector_stores

    logger.info("Vector worker shutdown: closing cached vector stores")
    await _run_native(close_all_vector_stores)


def create_app() -> FastAPI:
    os.environ.pop("ANIMAWORKS_VECTOR_URL", None)
    from core.memory.rag.direct_access import enable_direct_chroma_for_process

    enable_direct_chroma_for_process()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await _close_native_vector_stores()

    app = FastAPI(title="AnimaWorks Vector Worker", lifespan=lifespan)
    quiesced_animas: set[str] = set()

    def unavailable_while_quiesced(anima_name: str | None) -> JSONResponse | None:
        if anima_name and anima_name in quiesced_animas:
            return JSONResponse(
                status_code=423,
                content={"detail": "Vector store is quiesced for repair", "anima_name": anima_name},
            )
        return None

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/admin/quiesce")
    async def vector_quiesce(body: VectorAdminRequest):
        from core.memory.rag.singleton import reset_vector_store

        _log_vector_operation(body.anima_name, "quiesce")
        quiesced_animas.add(body.anima_name)
        try:
            await _run_native(reset_vector_store, body.anima_name)
        except Exception:
            quiesced_animas.discard(body.anima_name)
            raise
        return {"status": "quiesce", "anima_name": body.anima_name, "closed": True}

    @app.post("/admin/resume")
    async def vector_resume(body: VectorAdminRequest):
        _log_vector_operation(body.anima_name, "resume")
        quiesced_animas.discard(body.anima_name)
        return {"status": "resume", "anima_name": body.anima_name}

    @app.post("/query")
    async def vector_query(body: VectorQueryRequest):
        _log_vector_operation(body.anima_name, "query", body.collection)
        if response := unavailable_while_quiesced(body.anima_name):
            return response
        from core.memory.rag.singleton import get_vector_store

        store = get_vector_store(body.anima_name)
        if store is None:
            return {"results": []}
        results = await _run_native(
            store.query,
            body.collection,
            body.embedding,
            body.top_k,
            body.filter_metadata,
        )
        return _search_results_payload(results)

    @app.post("/upsert")
    async def vector_upsert(body: VectorUpsertRequest):
        _log_vector_operation(body.anima_name, "upsert", body.collection)
        if response := unavailable_while_quiesced(body.anima_name):
            return response
        from core.memory.rag.singleton import get_vector_store
        from core.memory.rag.store import Document

        store = get_vector_store(body.anima_name)
        if store is None:
            return JSONResponse(status_code=503, content={"detail": "Vector store unavailable"})
        docs = [
            Document(
                id=d["id"],
                content=d.get("content", ""),
                embedding=d.get("embedding"),
                metadata=d.get("metadata", {}),
            )
            for d in body.documents
        ]
        ok = await _run_native(store.upsert, body.collection, docs)
        if not ok:
            return _vector_write_failed(body.anima_name, "upsert", body.collection)
        return {"status": "ok"}

    @app.post("/update-metadata")
    async def vector_update_metadata(body: VectorUpdateMetadataRequest):
        _log_vector_operation(body.anima_name, "update-metadata", body.collection)
        if response := unavailable_while_quiesced(body.anima_name):
            return response
        from core.memory.rag.singleton import get_vector_store

        store = get_vector_store(body.anima_name)
        if store is None:
            return JSONResponse(status_code=503, content={"detail": "Vector store unavailable"})
        ok = await _run_native(
            store.update_metadata,
            body.collection,
            body.ids,
            body.metadatas,
        )
        if not ok:
            return _vector_write_failed(body.anima_name, "update-metadata", body.collection)
        return {"status": "ok"}

    @app.post("/delete-documents")
    async def vector_delete_documents(body: VectorDeleteDocumentsRequest):
        _log_vector_operation(body.anima_name, "delete-documents", body.collection)
        if response := unavailable_while_quiesced(body.anima_name):
            return response
        from core.memory.rag.singleton import get_vector_store

        store = get_vector_store(body.anima_name)
        if store is None:
            return JSONResponse(status_code=503, content={"detail": "Vector store unavailable"})
        ok = await _run_native(store.delete_documents, body.collection, body.ids)
        if not ok:
            return _vector_write_failed(body.anima_name, "delete-documents", body.collection)
        return {"status": "ok"}

    @app.post("/get-by-metadata")
    async def vector_get_by_metadata(body: VectorGetByMetadataRequest):
        _log_vector_operation(body.anima_name, "get-by-metadata", body.collection)
        if response := unavailable_while_quiesced(body.anima_name):
            return response
        from core.memory.rag.singleton import get_vector_store

        store = get_vector_store(body.anima_name)
        if store is None:
            return {"results": []}
        results = await _run_native(
            store.get_by_metadata,
            body.collection,
            body.where,
            body.limit,
        )
        return _search_results_payload(results)

    @app.post("/get-by-ids")
    async def vector_get_by_ids(body: VectorGetByIdsRequest):
        _log_vector_operation(body.anima_name, "get-by-ids", body.collection)
        if response := unavailable_while_quiesced(body.anima_name):
            return response
        from core.memory.rag.singleton import get_vector_store

        store = get_vector_store(body.anima_name)
        if store is None:
            return {"documents": []}
        docs = await _run_native(store.get_by_ids, body.collection, body.ids)
        return {"documents": [{"id": d.id, "content": d.content, "metadata": d.metadata} for d in docs]}

    @app.post("/create-collection")
    async def vector_create_collection(body: VectorCollectionRequest):
        _log_vector_operation(body.anima_name, "create-collection", body.collection)
        if response := unavailable_while_quiesced(body.anima_name):
            return response
        from core.memory.rag.singleton import get_vector_store

        store = get_vector_store(body.anima_name)
        if store is None:
            return JSONResponse(status_code=503, content={"detail": "Vector store unavailable"})
        ok = await _run_native(store.create_collection, body.collection)
        if not ok:
            return _vector_write_failed(body.anima_name, "create-collection", body.collection)
        return {"status": "ok"}

    @app.post("/delete-collection")
    async def vector_delete_collection(body: VectorCollectionRequest):
        _log_vector_operation(body.anima_name, "delete-collection", body.collection)
        if response := unavailable_while_quiesced(body.anima_name):
            return response
        from core.memory.rag.singleton import get_vector_store

        store = get_vector_store(body.anima_name)
        if store is None:
            return JSONResponse(status_code=503, content={"detail": "Vector store unavailable"})
        ok = await _run_native(store.delete_collection, body.collection)
        if not ok:
            return _vector_write_failed(body.anima_name, "delete-collection", body.collection)
        return {"status": "ok"}

    @app.post("/list-collections")
    async def vector_list_collections(body: VectorListCollectionsRequest):
        _log_vector_operation(body.anima_name, "list-collections")
        if response := unavailable_while_quiesced(body.anima_name):
            return response
        from core.memory.rag.singleton import get_vector_store

        store = get_vector_store(body.anima_name)
        if store is None:
            return {"collections": []}
        collections = await _run_native(store.list_collections)
        return {"collections": collections}

    @app.post("/quick-check")
    async def vector_quick_check(body: VectorQuickCheckRequest):
        _log_vector_operation(body.anima_name, "quick-check")
        if response := unavailable_while_quiesced(body.anima_name):
            return response
        from core.memory.rag.sqlite_health import check_anima_vectordb_health

        result = await _run_native(
            check_anima_vectordb_health,
            body.anima_name,
            timeout_seconds=body.timeout_seconds,
            source=body.source,
            record_repair=body.record_repair,
        )
        return {
            "status": result.status,
            "ok": result.ok,
            "corrupt": result.corrupt,
            "db_path": str(result.db_path),
            "details": list(result.details),
            "error": result.error,
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated AnimaWorks vector worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    args = parser.parse_args()

    import uvicorn

    configure_worker_logging(args.log_file)
    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        log_level="info",
        timeout_keep_alive=65,
        log_config=None,
    )


if __name__ == "__main__":
    main()
