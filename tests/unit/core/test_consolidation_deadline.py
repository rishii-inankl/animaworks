"""Consolidation deadlines release work without reporting a false success."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core._anima_lifecycle import LifecycleMixin
from core.config.models import ConsolidationConfig
from core.time_utils import now_local
from core.tooling.handler import active_session_type


class _Anima(LifecycleMixin):
    def __init__(self, tmp_path):
        self.name = "test"
        self.anima_dir = tmp_path
        (tmp_path / "state").mkdir()
        self._background_lock = asyncio.Lock()
        self._status_slots = {"background": "idle"}
        self._task_slots = {"background": ""}
        self._last_progress_at = now_local() - timedelta(minutes=10)
        self._activity = MagicMock()
        self._write_busy_status_sidecar = MagicMock()
        self._notify_lock_released = MagicMock()
        handler = SimpleNamespace(set_active_session_type=active_session_type.set)
        self.agent = SimpleNamespace(_tool_handler=handler)

    def _mark_busy_start(self):
        self._last_progress_at = now_local()


@pytest.fixture
def anima(tmp_path, monkeypatch):
    monkeypatch.setattr("core.memory.consolidation.ConsolidationEngine", MagicMock())
    cfg = SimpleNamespace(consolidation=SimpleNamespace(hard_timeout_seconds=0.04))
    monkeypatch.setattr("core.config.load_config", lambda: cfg)
    return _Anima(tmp_path)


def test_default_and_invalid_deadline():
    assert ConsolidationConfig().hard_timeout_seconds == 1800
    with pytest.raises(ValueError):
        ConsolidationConfig(hard_timeout_seconds=0)


@pytest.mark.parametrize("kind", ["daily", "weekly"])
async def test_timeout_cancels_work_releases_lock_and_preserves_memory(anima, kind, caplog):
    cancelled = asyncio.Event()
    memory = anima.anima_dir / "state" / "saved-memory.txt"
    memory.write_text("keep this memory")

    async def hung(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    setattr(anima, "_run_" + kind + "_consolidation", hung)
    with pytest.raises(TimeoutError):
        # The outer limit fails this test promptly on the unfixed implementation.
        async with asyncio.timeout(0.4):
            await anima.run_consolidation(consolidation_type=kind)
    assert cancelled.is_set()
    assert not anima._background_lock.locked()
    assert anima._status_slots["background"] == "idle"
    assert not (anima.anima_dir / "state" / ".consolidation_mode").exists()
    assert memory.read_text() == "keep this memory"
    assert "consolidation_deadline" in caplog.text
    assert all(call.args[0] != "consolidation_end" for call in anima._activity.log.call_args_list)


@pytest.mark.parametrize("kind", ["daily", "weekly"])
async def test_success_returns_result_and_cleans_up(anima, kind):
    result = SimpleNamespace(summary="done", duration_ms=1)
    setattr(anima, "_run_" + kind + "_consolidation", AsyncMock(return_value=result))
    assert await anima.run_consolidation(consolidation_type=kind) is result
    assert not anima._background_lock.locked()
    assert not (anima.anima_dir / "state" / ".consolidation_mode").exists()
    assert any(call.args[0] == "consolidation_end" for call in anima._activity.log.call_args_list)


async def test_deadline_includes_lock_wait_without_stopping_other_work(anima, caplog):
    await anima._background_lock.acquire()
    anima._status_slots["background"] = "checking"
    phase = AsyncMock()
    anima._run_daily_consolidation = phase
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.4):
                await anima.run_consolidation()
        assert "consolidation_deadline" in caplog.text
        assert anima._background_lock.locked()
        assert anima._status_slots["background"] == "checking"
        phase.assert_not_awaited()
    finally:
        anima._background_lock.release()


async def test_consolidation_keepalive_does_not_fabricate_progress(anima):
    before = anima._last_progress_at
    task = asyncio.create_task(anima._keepalive_while_busy(interval=0.005, update_progress=False))
    await asyncio.sleep(0.02)
    task.cancel()
    await task
    assert anima._last_progress_at == before
    assert anima._write_busy_status_sidecar.call_count > 0


async def test_caller_cancellation_is_not_reported_as_deadline(anima, caplog):
    started = asyncio.Event()

    async def hung(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    anima._run_daily_consolidation = hung
    task = asyncio.create_task(anima.run_consolidation())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "consolidation_deadline" not in caplog.text
    assert not anima._background_lock.locked()
    assert not (anima.anima_dir / "state" / ".consolidation_mode").exists()


async def test_suppressed_cancellation_cannot_report_success(anima, caplog):
    async def suppress(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return SimpleNamespace(summary="partial", duration_ms=40)

    anima._run_daily_consolidation = suppress
    with pytest.raises(TimeoutError):
        await anima.run_consolidation()
    assert "consolidation_deadline" in caplog.text
    assert all(call.args[0] != "consolidation_end" for call in anima._activity.log.call_args_list)
    assert not anima._background_lock.locked()
