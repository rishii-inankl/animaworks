"""Protected anima files cannot be rewritten via memory tools, including path aliases."""
# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def anima_dir(tmp_path: Path) -> Path:
    d = tmp_path / "animas" / "guard-mcp"
    for sub in ("state", "knowledge", "episodes", "procedures", "skills", ".codex_home"):
        (d / sub).mkdir(parents=True)
    (d / ".codex_home" / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    (d / "permissions.json").write_text(json.dumps({"version": 1, "file_roots": [str(d)]}), encoding="utf-8")
    return d


@pytest.fixture
def handler(anima_dir):
    from core.memory import MemoryManager
    from core.tooling.handler import ToolHandler

    return ToolHandler(anima_dir, MemoryManager(anima_dir))


def _add_task(anima_dir: Path) -> str:
    from core.memory.task_queue import TaskQueueManager

    entry = TaskQueueManager(anima_dir).add_task(
        source="human", original_instruction="guard test", assignee="guard-mcp", summary="guard test"
    )
    return entry.task_id


def _write(handler, path: str) -> str:
    return handler.handle("write_memory_file", {"path": path, "content": "forged\n", "mode": "append"})


@pytest.mark.parametrize(
    "path",
    [
        "state/task_queue.jsonl",
        "STATE/TASK_QUEUE.JSONL",
        "state/Task_Queue_Archive.jsonl",
        "Permissions.JSON",
        ".codex_home/config.toml",
        ".CODEX_HOME/instructions.md",
    ],
)
def test_protected_paths_denied(handler, anima_dir, path):
    _add_task(anima_dir)
    queue = anima_dir / "state" / "task_queue.jsonl"
    before = queue.read_bytes()
    assert "PermissionDenied" in _write(handler, path)
    assert queue.read_bytes() == before
    assert not (anima_dir / "state" / "task_queue_archive.jsonl").exists()


def test_symlink_to_queue_denied(handler, anima_dir):
    _add_task(anima_dir)
    (anima_dir / "knowledge" / "link.md").symlink_to(anima_dir / "state" / "task_queue.jsonl")
    assert "PermissionDenied" in _write(handler, "knowledge/link.md")


def test_existing_hardlink_to_queue_denied(handler, anima_dir):
    _add_task(anima_dir)
    queue = anima_dir / "state" / "task_queue.jsonl"
    before = queue.read_bytes()
    os.link(queue, anima_dir / "knowledge" / "alias.md")
    assert "PermissionDenied" in _write(handler, "knowledge/alias.md")
    assert queue.read_bytes() == before


def test_new_run_dir_under_state_is_writable(handler, anima_dir):
    result = handler.handle("write_memory_file", {"path": "state/seo-phase-b-run/draft.md", "content": "# draft\n"})
    assert "PermissionDenied" not in result
    assert (anima_dir / "state" / "seo-phase-b-run" / "draft.md").read_text(encoding="utf-8") == "# draft\n"


def test_official_update_task_still_appends(handler, anima_dir):
    task_id = _add_task(anima_dir)
    queue = anima_dir / "state" / "task_queue.jsonl"
    before = queue.read_bytes()
    result = json.loads(handler.handle("update_task", {"task_id": task_id, "status": "done", "summary": "ok"}))
    assert result["status"] == "done"
    after = queue.read_bytes()
    assert after.startswith(before) and len(after) > len(before)
