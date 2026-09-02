"""Persistent repair-state helpers for RAG auto-repair."""

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from core.memory.rag.repair_types import RepairResult
from core.memory.rag.repair_utils import iso, parse_dt, utc_now


def now_iso() -> str:
    return iso()


def state_path(anima_name: str, *, animas_dir: Path | None = None) -> Path:
    if animas_dir is None:
        from core.paths import get_animas_dir

        animas_dir = get_animas_dir()

    return animas_dir / anima_name / "state" / "rag_repair.json"


def read_state(anima_name: str, *, animas_dir: Path | None = None) -> dict[str, Any]:
    path = state_path(anima_name, animas_dir=animas_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("status") in {"healthy", "success"}:
        from core.memory.rag.sqlite_health import quick_check_chroma_sqlite

        health = quick_check_chroma_sqlite(path.parent.parent / "vectordb")
        if health.corrupt:
            data.update(
                {
                    "status": "corrupt",
                    "stage": "detect",
                    "updated_at": iso(),
                    "last_error": health.error or "; ".join(health.details),
                }
            )
            try:
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            except OSError:
                pass
    return data


def write_state(anima_name: str, state: dict[str, Any], *, animas_dir: Path | None = None) -> None:
    path = state_path(anima_name, animas_dir=animas_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def state_with_defaults(state: dict[str, Any] | None = None) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "status": "healthy",
        "stage": "complete",
        "pid": None,
        "started_at": None,
        "requested_at": None,
        "updated_at": iso(),
        "heartbeat_at": None,
        "reason": None,
        "collection": None,
        "source": None,
        "include_shared": False,
        "last_error": None,
        "last_quarantine_path": None,
        "last_chunks_indexed": 0,
    }
    if state:
        merged.update(state)
    return merged


def write_repair_request_state(
    anima_name: str,
    *,
    reason: str,
    collection: str | None,
    source: str,
    include_shared: bool,
    animas_dir: Path | None = None,
) -> None:
    now = iso()
    state = state_with_defaults(read_state(anima_name, animas_dir=animas_dir))
    state.update(
        {
            "status": "requested",
            "stage": "detect",
            "pid": None,
            "requested_at": now,
            "updated_at": now,
            "heartbeat_at": now,
            "reason": reason,
            "collection": collection,
            "source": source,
            "include_shared": bool(include_shared),
            "last_error": None,
        }
    )
    write_state(anima_name, state, animas_dir=animas_dir)


def write_blocked_state(anima_name: str, result: RepairResult, *, animas_dir: Path | None = None) -> None:
    now = iso()
    state = state_with_defaults(read_state(anima_name, animas_dir=animas_dir))
    state.update(
        {
            "status": result.status,
            "stage": result.stage or result.status,
            "updated_at": now,
            "heartbeat_at": now,
            "reason": result.reason,
            "last_error": result.error,
        }
    )
    write_state(anima_name, state, animas_dir=animas_dir)


def update_repair_state(
    anima_name: str,
    *,
    animas_dir: Path | None = None,
    **updates: Any,
) -> dict[str, Any]:
    now = iso()
    state = state_with_defaults(read_state(anima_name, animas_dir=animas_dir))
    state.update(updates)
    state["updated_at"] = now
    state["heartbeat_at"] = now
    write_state(anima_name, state, animas_dir=animas_dir)
    return state


def append_state_signal(
    anima_name: str,
    signal: dict[str, Any],
    window: timedelta,
    *,
    animas_dir: Path | None = None,
) -> None:
    state = read_state(anima_name, animas_dir=animas_dir)
    signals = state.get("recent_signals")
    if not isinstance(signals, list):
        signals = []
    cutoff = utc_now() - window
    signals.append(signal)
    state["recent_signals"] = [s for s in signals[-50:] if (parse_dt(s.get("at")) or utc_now()) >= cutoff]
    write_state(anima_name, state, animas_dir=animas_dir)
