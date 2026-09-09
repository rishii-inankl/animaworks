"""A lost consolidation response must not leave its worker busy forever."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.supervisor._mgr_scheduler import SchedulerMixin
from core.supervisor.ipc import IPCResponse
from core.supervisor.process_handle import ProcessState


def _supervisor(handle):
    sup = object.__new__(SchedulerMixin)
    sup.processes = {"test": handle}
    sup._restarting = set()
    sup._consolidating = set()

    async def restart(name):
        replacement = _handle(False)
        replacement.get_pid.return_value = 456
        sup.processes[name] = replacement

    sup.restart_anima = AsyncMock(side_effect=restart)
    return sup


def _handle(*statuses):
    handle = MagicMock()
    handle.state = ProcessState.RUNNING
    handle.get_pid.return_value = 123
    queue = list(statuses)

    async def request(method, params, timeout):
        if method == "get_status":
            value = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(value, Exception):
                raise value
            return IPCResponse(id="test", result={"status": "processing", "consolidation_running": value})
        return IPCResponse(id="test", result={"status": "interrupted"})

    handle.send_request = AsyncMock(side_effect=request)
    return handle


async def test_finished_consolidation_does_not_interrupt_next_work():
    handle = _handle(False)
    sup = _supervisor(handle)
    assert await sup._recover_consolidation_timeout("test", handle, expected_pid=123)
    assert [c.args[0] for c in handle.send_request.await_args_list] == ["get_status"]
    sup.restart_anima.assert_not_awaited()


@pytest.mark.parametrize("kind", ["daily", "weekly"])
async def test_request_uses_configured_limit_and_retains_timeout_status(kind):
    handle = _handle(False)
    handle.send_request = AsyncMock(return_value=IPCResponse(id="test", result={"status": "timeout"}))
    sup = _supervisor(handle)
    outcome, stopped = await sup._request_consolidation("test", handle, kind=kind, max_turns=12, hard_timeout=600)
    assert outcome["status"] == "timeout"
    assert stopped
    assert handle.send_request.await_args.kwargs["timeout"] == 630
    assert not sup._consolidating
    sup.restart_anima.assert_not_awaited()


async def test_worker_reports_timeout_instead_of_completed():
    from core.supervisor.runner import AnimaRunner

    runner = object.__new__(AnimaRunner)
    runner.anima_name = "test"
    runner.anima = MagicMock()
    runner.anima.run_consolidation = AsyncMock(side_effect=TimeoutError("deadline"))
    result = await runner._handle_run_consolidation({"consolidation_type": "weekly"})
    assert result["status"] == "timeout"


async def test_status_exposes_consolidation_even_when_chat_is_foreground():
    from core.supervisor.runner import AnimaRunner

    runner = object.__new__(AnimaRunner)
    runner.anima = MagicMock()
    runner.anima.primary_status = "chatting"
    runner.anima._status_slots = {"background": "consolidating"}
    runner.anima.bootstrap_state = {}
    runner._scheduler_mgr = None
    status = await runner._handle_get_status({})
    assert status["status"] == "chatting"
    assert status["consolidation_running"] is True


@pytest.mark.parametrize("lock_acquired", [True, False])
async def test_restart_clears_crash_marker_only_after_exclusive_lock(tmp_path, monkeypatch, lock_acquired):
    from core.supervisor.runner import AnimaRunner

    marker = tmp_path / "state" / ".consolidation_mode"
    marker.parent.mkdir()
    marker.write_text("1")
    runner = object.__new__(AnimaRunner)
    runner.anima_name = "test"
    runner._anima_dir = tmp_path
    runner.shared_dir = tmp_path
    runner.socket_path = tmp_path / "test.sock"
    runner._cleanup = AsyncMock()
    runner._acquire_process_lock = MagicMock(side_effect=None if lock_acquired else asyncio.CancelledError())
    monkeypatch.setattr("core.supervisor.runner.IPCServer", MagicMock(return_value=AsyncMock()))

    def initialize(**kwargs):
        assert not marker.exists()
        # End the test before starting autonomous work or a real model.
        raise asyncio.CancelledError()

    monkeypatch.setattr("core.supervisor.runner.DigitalAnima", initialize)
    with pytest.raises(asyncio.CancelledError):
        await runner.run()
    assert marker.exists() is not lock_acquired


async def test_interrupt_is_only_for_background_and_exit_is_confirmed():
    handle = _handle(True, False)
    sup = _supervisor(handle)
    assert await sup._recover_consolidation_timeout("test", handle, expected_pid=123)
    calls = handle.send_request.await_args_list
    assert [c.args[0] for c in calls] == ["get_status", "interrupt", "get_status"]
    assert calls[1].args[1] == {"thread_id": "_background"}
    sup.restart_anima.assert_not_awaited()


@pytest.mark.parametrize("status", [True, TimeoutError("IPC unresponsive")])
async def test_worker_still_running_after_grace_is_restarted(monkeypatch, status, caplog):
    monkeypatch.setattr("core.supervisor._mgr_scheduler._CONSOLIDATION_RECOVERY_TIMEOUT", 0.02)
    handle = _handle(status)
    sup = _supervisor(handle)
    async with asyncio.timeout(0.5):
        assert await sup._recover_consolidation_timeout("test", handle, expected_pid=123)
    sup.restart_anima.assert_awaited_once_with("test")
    assert "consolidation_recovery" in caplog.text


async def test_replaced_worker_is_not_interrupted_or_restarted():
    handle = _handle(True)
    sup = _supervisor(handle)
    sup.processes["test"] = _handle(False)
    assert not await sup._recover_consolidation_timeout("test", handle, expected_pid=123)
    handle.send_request.assert_not_awaited()
    sup.restart_anima.assert_not_awaited()


async def test_pid_change_in_same_handle_is_not_restarted():
    handle = _handle(True)
    sup = _supervisor(handle)
    handle.get_pid.return_value = 456
    assert not await sup._recover_consolidation_timeout("test", handle, expected_pid=123)
    handle.send_request.assert_not_awaited()
    sup.restart_anima.assert_not_awaited()


async def test_restart_failure_is_not_reported_as_recovery(monkeypatch, caplog):
    monkeypatch.setattr("core.supervisor._mgr_scheduler._CONSOLIDATION_RECOVERY_TIMEOUT", 0.01)
    handle = _handle(True)
    sup = _supervisor(handle)
    sup.restart_anima.side_effect = RuntimeError("start failed")
    assert not await sup._recover_consolidation_timeout("test", handle, expected_pid=123)
    assert "consolidation_recovery_failed" in caplog.text


async def test_restart_without_new_running_process_is_not_recovery(monkeypatch, caplog):
    monkeypatch.setattr("core.supervisor._mgr_scheduler._CONSOLIDATION_RECOVERY_TIMEOUT", 0.01)
    handle = _handle(True)
    sup = _supervisor(handle)
    sup.restart_anima.side_effect = None
    assert not await sup._recover_consolidation_timeout("test", handle, expected_pid=123)
    assert "replacement not running" in caplog.text


async def test_worker_replaced_during_status_read_is_not_interrupted():
    handle = _handle(True)
    sup = _supervisor(handle)

    async def replace(method, params, timeout):
        sup.processes["test"] = _handle(False)
        return IPCResponse(id="test", result={"consolidation_running": True})

    handle.send_request.side_effect = replace
    assert not await sup._recover_consolidation_timeout("test", handle, expected_pid=123)
    assert [c.args[0] for c in handle.send_request.await_args_list] == ["get_status"]
    sup.restart_anima.assert_not_awaited()
