"""Integration: codex_shell_writes=private_tmp_only against the real Codex app-server and MCP server.

No model inference: only initialize, thread/start, command/exec, and MCP
list_tools/call_tool.  Every shell attack gets a fresh fixture.  thread/resume
needs a rollout (only created by a model turn), so a real resume readback is not
covered; instead only threads stamped under the same profile are resumed (unit tests).

Opt-in: AW_CODEX_SANDBOX_IT=1 (macOS Seatbelt + bundled codex binary).  When
opted in, missing preconditions fail instead of skipping.
"""
# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AW_CODEX_SANDBOX_IT") != "1",
        reason="set AW_CODEX_SANDBOX_IT=1 to run real Codex sandbox checks",
    ),
]

EXEC_TIMEOUT_MS = 20000
MCP_TIMEOUT_S = 60
DENIED = "operation not permitted"


def _require_env() -> None:
    if sys.platform != "darwin":
        pytest.fail("AW_CODEX_SANDBOX_IT=1 requires macOS Seatbelt")
    from core.execution.codex_sdk import get_codex_executable

    if not get_codex_executable():
        pytest.fail("AW_CODEX_SANDBOX_IT=1 but the bundled codex executable is unavailable")


def _sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None


class Fixture:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.memory.task_queue import TaskQueueManager
        from core.schemas import ModelConfig

        self.data = root / "data"
        self.anima = self.data / "animas" / "fxsena"
        for sub in ("state", "knowledge", "episodes"):
            (self.anima / sub).mkdir(parents=True)
        (self.data / "shared").mkdir(parents=True)
        monkeypatch.setenv("ANIMAWORKS_DATA_DIR", str(self.data))
        (self.anima / "permissions.json").write_text(
            json.dumps({"version": 1, "file_roots": [str(self.anima)], "codex_shell_writes": "private_tmp_only"}),
            encoding="utf-8",
        )
        self.task_id = TaskQueueManager(self.anima).add_task(
            source="human", original_instruction="guard it", assignee="fxsena", summary="guard it"
        ).task_id
        self.queue = self.anima / "state" / "task_queue.jsonl"
        self.model_config = ModelConfig(model="codex/o4-mini", max_tokens=4096, max_turns=5, credential="openai")

    def executor(self):
        from core.execution.codex_sdk import CodexSDKExecutor

        exc = CodexSDKExecutor(model_config=self.model_config, anima_dir=self.anima)
        # The app-server may spawn the aw MCP server from config.toml; keep it on fixture data,
        # never the live ~/.animaworks default.
        base_env = exc._build_mcp_env
        exc._build_mcp_env = lambda: {**base_env(), "ANIMAWORKS_DATA_DIR": str(self.data), "HOME": str(self.data)}
        return exc


async def _start_resume_exec(fx: Fixture, scripts: list[str]) -> dict[str, Any]:
    """Real SDK path: executor config + AsyncCodex.thread_start with executor kwargs.

    thread/resume needs a rollout, which only exists after a model turn, so the
    resume request is covered by the unit test on _start_or_resume_thread instead.
    """
    from pydantic import BaseModel, ConfigDict

    class Raw(BaseModel):
        model_config = ConfigDict(extra="allow")

    exc = fx.executor()
    exc._write_codex_config("guard integration prompt")
    kwargs = exc._codex_thread_kwargs("guard integration prompt")
    codex = exc._create_codex_client()
    captured: dict[str, Any] = {}
    inner = codex._client
    orig_start = inner.thread_start

    async def start(params):
        captured["request"] = params
        resp = await orig_start(params)
        captured["start"] = resp
        return resp

    inner.thread_start = start
    steps: list[dict[str, Any]] = []
    try:
        await codex.thread_start(**kwargs)
        for script in scripts:
            r = (
                await inner.request(
                    "command/exec",
                    {"command": ["/bin/zsh", "-c", script], "cwd": str(fx.anima), "timeoutMs": EXEC_TIMEOUT_MS},
                    response_model=Raw,
                )
            ).model_dump()
            steps.append({"script": script, "exit": r.get("exitCode"), "stdout": r.get("stdout") or "", "stderr": r.get("stderr") or ""})
    finally:
        await codex.close()
    return {"captured": captured, "steps": steps}


def _roots(resp) -> list[str]:
    sandbox = resp.sandbox.model_dump(by_alias=True)
    root = sandbox.get("root", sandbox)
    return [str(r.get("root", r)) if isinstance(r, dict) else str(r) for r in root.get("writableRoots", [])]


def _tmp_name(prefix: str) -> Path:
    return Path("/private/tmp") / f"aw-guard-it-{prefix}-{uuid.uuid4().hex[:8]}"


# (name, scripts builder, expectation) — expectation: index of step that must be denied, or "ok".
def _attack_cases(fx: Fixture, tmp_entry: Path) -> dict[str, tuple[list[str], Any]]:
    q, a = fx.queue, fx.anima
    return {
        "direct_append": ([f"echo x >> {q}"], 0),
        "case_alias_append": ([f"echo x >> {a}/STATE/TASK_QUEUE.JSONL"], 0),
        "dotdot_via_tmp": ([f"echo x >> /private/tmp/../..{q}"], 0),
        "symlink_in_tmp_then_write": ([f"ln -s {q} {tmp_entry}", f"echo x >> {tmp_entry}"], 1),
        "hardlink_into_tmp": ([f"ln {q} {tmp_entry}"], 0),
        "rename_queue": ([f"mv {q} {a}/state/q.txt"], 0),
        "move_state_into_tmp": ([f"mv {a}/state {tmp_entry}"], 0),
        "move_anima_into_tmp": ([f"mv {a} {tmp_entry}"], 0),
        "write_knowledge_side_effect": ([f"echo x > {a}/knowledge/shell.md"], 0),
        "control_tmp_write_and_mktemp": (
            # macOS `mktemp` / `mktemp -t` use the per-user confstr dir (denied) even with TMPDIR set;
            # an explicit "$TMPDIR/..." template and TMPDIR-aware tools (Python tempfile) work.
            [
                f"echo ok > {tmp_entry}",
                'test "$TMPDIR" = /private/tmp',
                'f=$(mktemp "$TMPDIR/aw-guard.XXXXXX") && echo "$f" && rm "$f"',
                "python3 -c 'import os, tempfile; fd, p = tempfile.mkstemp(); os.close(fd); os.remove(p); print(p)'",
            ],
            "ok",
        ),
        "control_read_queue": ([f"wc -l < {q}"], "ok"),
    }


ATTACKS = [
    "direct_append",
    "case_alias_append",
    "dotdot_via_tmp",
    "symlink_in_tmp_then_write",
    "hardlink_into_tmp",
    "rename_queue",
    "move_state_into_tmp",
    "move_anima_into_tmp",
    "write_knowledge_side_effect",
    "control_tmp_write_and_mktemp",
    "control_read_queue",
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_shell_attack_on_fresh_fixture(attack, tmp_path, monkeypatch, request):
    _require_env()
    fx = Fixture(tmp_path, monkeypatch)
    tmp_entry = _tmp_name(attack)
    scripts, expect = _attack_cases(fx, tmp_entry)[attack]
    before = {"queue": _sha(fx.queue), "anima": fx.anima.is_dir(), "state": (fx.anima / "state").is_dir()}
    try:
        result = asyncio.run(_start_resume_exec(fx, scripts))
    finally:
        if tmp_entry.is_symlink() or tmp_entry.is_file():
            tmp_entry.unlink()
    after = {"queue": _sha(fx.queue), "anima": fx.anima.is_dir(), "state": (fx.anima / "state").is_dir()}
    request.node.user_properties.append(("evidence", json.dumps({"steps": result["steps"], "before": before, "after": after})))

    # The profile, not a thread-level sandbox override, reached the app-server.
    assert result["captured"]["request"].sandbox is None
    assert _roots(result["captured"]["start"]) == ["/private/tmp"], result["captured"]["start"].sandbox

    assert after == before, f"protected state changed: {before} -> {after}"
    steps = result["steps"]
    if expect == "ok":
        failed = [s for s in steps if s["exit"] != 0]
        assert not failed, "control step failed: " + json.dumps(steps, ensure_ascii=False)
        return
    for s in steps[:expect]:
        assert s["exit"] == 0, f"setup step failed: {s}"
    denied = steps[expect]
    assert denied["exit"] != 0, denied
    assert DENIED in denied["stderr"].lower(), denied


# ── MCP: real stdio server, official paths and protected writes ─────────


async def _mcp(fx: Fixture, calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = fx.executor()._build_mcp_env()
    params = StdioServerParameters(command=sys.executable, args=["-m", "core.mcp.server"], env=env)
    out: dict[str, Any] = {"calls": []}
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await asyncio.wait_for(session.initialize(), MCP_TIMEOUT_S)
            tools = await asyncio.wait_for(session.list_tools(), MCP_TIMEOUT_S)
            out["tools"] = {t.name for t in tools.tools}
            for name, args in calls:
                res = await asyncio.wait_for(session.call_tool(name, args), MCP_TIMEOUT_S)
                out["calls"].append("".join(getattr(c, "text", "") for c in res.content))
    return out


def test_mcp_official_update_and_state_run_write(tmp_path, monkeypatch):
    _require_env()
    fx = Fixture(tmp_path, monkeypatch)
    before = fx.queue.read_bytes()
    run_draft = fx.anima / "state" / "seo-monthly-phase-b-it" / "draft.md"
    out = asyncio.run(
        _mcp(
            fx,
            [
                ("update_task", {"task_id": fx.task_id, "status": "in_progress", "summary": "start"}),
                ("update_task", {"task_id": fx.task_id, "status": "done", "summary": "finished"}),
                ("write_memory_file", {"path": str(run_draft), "content": "# draft\n"}),
            ],
        )
    )
    assert {"update_task", "write_memory_file"} <= out["tools"]
    after = fx.queue.read_bytes()
    assert after.startswith(before)
    assert json.loads(after.splitlines()[-1])["status"] == "done"
    assert run_draft.read_text(encoding="utf-8") == "# draft\n"


@pytest.mark.parametrize(
    "target",
    [
        "state/task_queue.jsonl",
        "STATE/TASK_QUEUE.JSONL",
        "state/task_queue_archive.jsonl",
        "Permissions.json",
        ".codex_home/config.toml",
        "knowledge/symlink-to-queue.md",
        "knowledge/hardlink-to-queue.md",
    ],
)
def test_mcp_protected_write_denied(target, tmp_path, monkeypatch):
    _require_env()
    fx = Fixture(tmp_path, monkeypatch)
    (fx.anima / "knowledge" / "symlink-to-queue.md").symlink_to(fx.queue)
    os.link(fx.queue, fx.anima / "knowledge" / "hardlink-to-queue.md")
    fx.executor()._write_codex_config("prompt")
    watched = [fx.queue, fx.anima / "permissions.json", fx.anima / ".codex_home" / "config.toml"]
    before = {str(p): _sha(p) for p in watched}
    out = asyncio.run(_mcp(fx, [("write_memory_file", {"path": target, "content": "forged\n", "mode": "append"})]))
    assert "PermissionDenied" in out["calls"][0], out["calls"][0]
    assert {str(p): _sha(p) for p in watched} == before
    assert not (fx.anima / "state" / "task_queue_archive.jsonl").exists()


def test_stale_pre_opt_in_thread_starts_fresh_under_profile(tmp_path, monkeypatch):
    """A thread id stored before opt-in is not resumed; the fresh thread gets the profile."""
    _require_env()
    fx = Fixture(tmp_path, monkeypatch)
    from core.execution.codex_sdk import _load_thread_id, _save_thread_id
    from core.execution.codex_shell_guard import THREAD_LEDGER

    _save_thread_id(fx.anima, "01a0ffff-0000-7000-8000-000000000000", "chat")
    exc = fx.executor()
    exc._write_codex_config("prompt")

    async def run() -> dict[str, Any]:
        codex = exc._create_codex_client()
        inner = codex._client
        sent: list[str] = []
        orig_start, orig_resume = inner.thread_start, inner.thread_resume

        async def start(params):
            sent.append("start")
            resp = await orig_start(params)
            sent.append(json.dumps(_roots(resp)))
            return resp

        async def resume(thread_id, params):
            sent.append("resume")
            return await orig_resume(thread_id, params)

        inner.thread_start, inner.thread_resume = start, resume
        try:
            stale = _load_thread_id(fx.anima, "chat")
            thread = await exc._start_or_resume_thread(codex, stale, "chat", "prompt")
        finally:
            await codex.close()
        return {"sent": sent, "thread_id": thread.id}

    out = asyncio.run(run())
    assert out["sent"] == ["start", json.dumps(["/private/tmp"])], out
    assert _load_thread_id(fx.anima, "chat") is None
    ledger = (fx.anima / ".codex_home" / THREAD_LEDGER).read_text(encoding="utf-8").splitlines()
    assert [line.split("\t")[0] for line in ledger] == [out["thread_id"]]
