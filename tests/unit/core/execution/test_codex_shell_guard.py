"""Unit tests for the Codex shell write guard (codex_shell_writes=private_tmp_only)."""
# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from core.execution import codex_shell_guard as guard
from core.execution.codex_sdk import CodexSDKExecutor
from core.execution.codex_shell_guard import CodexShellGuardError, load_codex_permissions


@pytest.fixture
def anima_dir(tmp_path: Path) -> Path:
    d = tmp_path / "animas" / "guard-codex"
    for sub in ("state", "knowledge", "episodes", "procedures", "skills"):
        (d / sub).mkdir(parents=True)
    return d


@pytest.fixture
def model_config():
    from core.schemas import ModelConfig

    return ModelConfig(model="codex/o4-mini", max_tokens=4096, max_turns=30, credential="openai", api_key="test-key-123")


def _write_perms(anima_dir: Path, **overrides) -> None:
    data = {"version": 1, "file_roots": [str(anima_dir)], **overrides}
    (anima_dir / "permissions.json").write_text(json.dumps(data), encoding="utf-8")


def _config_toml(model_config, anima_dir: Path, task_cwd: Path | None = None) -> dict:
    exc = CodexSDKExecutor(model_config=model_config, anima_dir=anima_dir)
    if task_cwd is not None:
        exc.set_task_cwd(task_cwd)
    exc._write_codex_config("prompt")
    return tomllib.loads((anima_dir / ".codex_home" / "config.toml").read_text(encoding="utf-8"))


# ── Strict permissions loading (Codex backend only) ─────────────


class TestStrictLoading:
    def test_missing_file_keeps_legacy_open_default(self, anima_dir):
        assert load_codex_permissions(anima_dir).file_roots == ["/"]

    def test_invalid_json_raises_instead_of_open_default(self, anima_dir):
        (anima_dir / "permissions.json").write_text('{"codex_shell_writes": "private_tmp_only",', encoding="utf-8")
        with pytest.raises(CodexShellGuardError, match="Invalid JSON"):
            load_codex_permissions(anima_dir)

    def test_unknown_mode_value_raises(self, anima_dir):
        _write_perms(anima_dir, codex_shell_writes="tmp_only")
        with pytest.raises(CodexShellGuardError, match="Invalid permissions"):
            load_codex_permissions(anima_dir)

    def test_unreadable_file_raises(self, anima_dir):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        path = anima_dir / "permissions.json"
        path.chmod(0)
        try:
            with pytest.raises(CodexShellGuardError, match="Cannot read"):
                load_codex_permissions(anima_dir)
        finally:
            path.chmod(0o644)

    def test_dangling_symlink_is_not_treated_as_missing(self, anima_dir):
        (anima_dir / "permissions.json").symlink_to(anima_dir / "does-not-exist.json")
        with pytest.raises(CodexShellGuardError, match="Cannot read"):
            load_codex_permissions(anima_dir)

    def test_unstatable_parent_is_not_treated_as_missing(self, anima_dir):
        anima_dir.chmod(0o600)  # no search permission: lstat fails with PermissionError
        try:
            with pytest.raises(CodexShellGuardError, match="Cannot stat"):
                load_codex_permissions(anima_dir)
        finally:
            anima_dir.chmod(0o755)

    def test_broken_file_blocks_config_write_and_sdk_sandbox(self, model_config, anima_dir):
        (anima_dir / "permissions.json").write_text("{not json", encoding="utf-8")
        exc = CodexSDKExecutor(model_config=model_config, anima_dir=anima_dir)
        with pytest.raises(CodexShellGuardError):
            exc._write_codex_config("prompt")
        with pytest.raises(CodexShellGuardError):
            exc._sdk_sandbox()


# ── Config generation ─────────────────────────────────────────


class TestConfigGeneration:
    def test_non_opt_in_keeps_workspace_write(self, model_config, anima_dir):
        _write_perms(anima_dir)
        parsed = _config_toml(model_config, anima_dir)
        assert parsed["sandbox_mode"] == "workspace-write"
        assert parsed["sandbox_workspace_write"]["writable_roots"] == [str(anima_dir)]
        assert "default_permissions" not in parsed
        exc = CodexSDKExecutor(model_config=model_config, anima_dir=anima_dir)
        assert exc._sdk_sandbox() is not None

    def test_private_tmp_only_profile(self, model_config, anima_dir):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        parsed = _config_toml(model_config, anima_dir)
        assert "sandbox_mode" not in parsed
        assert "sandbox_workspace_write" not in parsed
        name = parsed["default_permissions"]
        profile = parsed["permissions"][name]
        assert profile["extends"] == ":read-only"
        assert profile["filesystem"] == {"/private/tmp": "write"}
        assert profile["network"]["enabled"] is True
        assert parsed["mcp_servers"]["aw"]["default_tools_approval_mode"] == "approve"
        exc = CodexSDKExecutor(model_config=model_config, anima_dir=anima_dir)
        assert exc._build_env()["TMPDIR"] == "/private/tmp"

    def test_non_opt_in_env_has_no_tmpdir_override(self, model_config, anima_dir):
        _write_perms(anima_dir)
        exc = CodexSDKExecutor(model_config=model_config, anima_dir=anima_dir)
        assert "TMPDIR" not in exc._build_env()

    def test_private_tmp_only_sends_no_thread_sandbox(self, model_config, anima_dir):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        exc = CodexSDKExecutor(model_config=model_config, anima_dir=anima_dir)
        assert exc._codex_thread_kwargs("prompt")["sandbox"] is None

    def test_anima_dir_as_task_cwd_is_normal(self, model_config, anima_dir):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        parsed = _config_toml(model_config, anima_dir, task_cwd=anima_dir)
        assert "default_permissions" in parsed


# ── Fail closed ──────────────────────────────────────────────


class TestFailClosed:
    def test_extra_writable_root(self, model_config, anima_dir, tmp_path):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only", file_roots=[str(anima_dir), str(tmp_path)])
        with pytest.raises(CodexShellGuardError, match="extra writable roots"):
            _config_toml(model_config, anima_dir)

    def test_full_access_root(self, model_config, anima_dir):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only", file_roots=["/"])
        with pytest.raises(CodexShellGuardError, match="file_roots"):
            _config_toml(model_config, anima_dir)

    def test_workspace_task_cwd(self, model_config, anima_dir, tmp_path):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        workspace = tmp_path / "repo"
        workspace.mkdir()
        with pytest.raises(CodexShellGuardError, match="workspace runs"):
            _config_toml(model_config, anima_dir, task_cwd=workspace)

    def test_anima_inside_writable_tmp(self, model_config, anima_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "PRIVATE_TMP", anima_dir.parent)
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        with pytest.raises(CodexShellGuardError, match="inside the writable"):
            _config_toml(model_config, anima_dir)

    def test_tmp_not_root_owned(self, model_config, anima_dir, tmp_path, monkeypatch):
        fake = tmp_path / "fake_parent" / "tmp"
        fake.mkdir(parents=True)
        monkeypatch.setattr(guard, "PRIVATE_TMP", fake)
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        with pytest.raises(CodexShellGuardError, match="root-owned"):
            _config_toml(model_config, anima_dir)



# ── Thread resume control ────────────────────────────────────


def _codex_mock(new_id: str):
    from unittest.mock import AsyncMock, MagicMock

    codex = MagicMock()
    codex.thread_start = AsyncMock(return_value=MagicMock(id=new_id))
    codex.thread_resume = AsyncMock(side_effect=lambda tid, **_: MagicMock(id=tid))
    return codex


def _run_thread(model_config, anima_dir: Path, thread_id: str | None, new_id: str):
    import asyncio

    from core.execution.codex_sdk import _load_thread_id, _save_thread_id

    exc = CodexSDKExecutor(model_config=model_config, anima_dir=anima_dir)
    if thread_id:
        _save_thread_id(anima_dir, thread_id, "chat")
    codex = _codex_mock(new_id)
    thread = asyncio.run(exc._start_or_resume_thread(codex, thread_id, "chat", "prompt"))
    return codex, thread, _load_thread_id(anima_dir, "chat")


class TestThreadResumeControl:
    def test_pre_opt_in_thread_is_not_resumed(self, model_config, anima_dir):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        codex, thread, stored = _run_thread(model_config, anima_dir, "t-legacy", "t-new")
        codex.thread_resume.assert_not_called()
        assert codex.thread_start.call_args.kwargs["sandbox"] is None
        assert thread.id == "t-new"
        assert stored is None  # stale legacy id cleared

    def test_guard_started_thread_is_resumed_without_thread_sandbox(self, model_config, anima_dir):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        _run_thread(model_config, anima_dir, None, "t-guard")
        codex, thread, _ = _run_thread(model_config, anima_dir, "t-guard", "unused")
        codex.thread_start.assert_not_called()
        assert codex.thread_resume.call_args.kwargs["sandbox"] is None
        assert thread.id == "t-guard"

    def test_guard_off_resume_revokes_stamp(self, model_config, anima_dir):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        _run_thread(model_config, anima_dir, None, "t-guard")  # stamped under the guard
        _write_perms(anima_dir)  # guard off: same thread resumed with workspace-write
        codex, _, _ = _run_thread(model_config, anima_dir, "t-guard", "unused")
        codex.thread_resume.assert_called_once()
        assert codex.thread_resume.call_args.kwargs["sandbox"] is not None
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")  # guard back on
        codex, thread, _ = _run_thread(model_config, anima_dir, "t-guard", "t-fresh")
        codex.thread_resume.assert_not_called()
        assert thread.id == "t-fresh"

    def test_profile_change_invalidates_stamp(self, model_config, anima_dir, monkeypatch):
        _write_perms(anima_dir, codex_shell_writes="private_tmp_only")
        _run_thread(model_config, anima_dir, None, "t-guard")
        monkeypatch.setattr(guard, "PROFILE_NAME", "aw_private_tmp_only_v2")
        codex, thread, _ = _run_thread(model_config, anima_dir, "t-guard", "t-fresh")
        codex.thread_resume.assert_not_called()
        assert thread.id == "t-fresh"
