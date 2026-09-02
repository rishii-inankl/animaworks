from __future__ import annotations

import sqlite3
from pathlib import Path

from core.memory.rag.repair_rebuild import (
    assert_archive_unchanged,
    quarantine_vectordb,
    snapshot_archive_mtimes,
)
from core.memory.rag.sqlite_health import quick_check_chroma_sqlite


def test_worker_handle_is_closed_before_quarantine_and_archive_stays_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    anima_dir = tmp_path / "animas" / "sora"
    old_dir = anima_dir / "vectordb"
    old_dir.mkdir(parents=True)
    old_db = old_dir / "chroma.sqlite3"
    with sqlite3.connect(old_db) as conn:
        conn.execute("CREATE TABLE collections(id TEXT PRIMARY KEY)")

    class WorkerHandle:
        closed = False

        def close(self) -> None:
            self.closed = True

        def ghost_write(self, path: Path) -> None:
            if not self.closed:
                path.write_bytes(path.read_bytes() + b"ghost")

    handle = WorkerHandle()
    transitions: list[bool] = []

    def set_quiesced(_url: str, _name: str, *, quiesced: bool) -> bool:
        transitions.append(quiesced)
        if quiesced:
            handle.close()
        return quiesced

    monkeypatch.setenv("ANIMAWORKS_VECTOR_URL", "http://worker")
    monkeypatch.setattr("core.paths.get_anima_vectordb_dir", lambda _name: old_dir)
    monkeypatch.setattr("core.memory.rag.repair_rebuild._set_worker_quiesced", set_quiesced)

    archive = quarantine_vectordb("sora")
    before = snapshot_archive_mtimes(archive)
    assert archive is not None
    handle.ghost_write(archive / "chroma.sqlite3")
    assert_archive_unchanged(archive, before)

    old_dir.mkdir()
    with sqlite3.connect(old_dir / "chroma.sqlite3") as conn:
        conn.execute("CREATE TABLE collections(id TEXT PRIMARY KEY)")
    health = quick_check_chroma_sqlite(old_dir)

    assert transitions == [True, False]
    assert health.ok is True
    assert (old_dir / "chroma.sqlite3").stat().st_size > 0
