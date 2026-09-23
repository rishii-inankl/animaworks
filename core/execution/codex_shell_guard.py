"""Codex shell write guard (``codex_shell_writes = "private_tmp_only"``).

When an Anima opts in via ``permissions.json``, the Codex model shell becomes
read-only except for the root-owned sticky ``/private/tmp``.  Persistent
writes go through the MCP tools (``write_memory_file``, ``update_task``),
which run outside the sandbox and apply their own protected-path checks.

Why not enumerate writable sub-directories of the anima dir: Codex
re-canonicalises writable roots on every exec, so a writable root whose
directory entry the shell can remove or move can be replaced by a symlink to
a protected file.  Seatbelt rules also match the path spelling as given, so a
writable ancestor lets case aliases (``STATE/TASK_QUEUE.JSONL``) bypass
per-file read rules.  A single root the shell cannot replace, with no
writable ancestor of any anima file, avoids both.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from core.config.schemas import PermissionsConfig, load_permissions

PRIVATE_TMP = Path("/private/tmp")
PROFILE_NAME = "aw_private_tmp_only"
# Threads started under the profile, "<thread_id>\t<profile_fingerprint>" per line.
# Lives in .codex_home/, which is read-only to the guarded shell and protected from memory tools.
THREAD_LEDGER = "private_tmp_only_threads.tsv"


class CodexShellGuardError(RuntimeError):
    """Raised when the Codex shell guard cannot be applied safely (fail closed)."""


def load_codex_permissions(anima_dir: Path) -> PermissionsConfig:
    """Load permissions for the Codex backend without falling back to open defaults.

    An existing ``permissions.json`` that cannot be read, parsed or validated
    raises instead of silently granting full access, because a broken file
    may be the one that carried the guard opt-in.  A missing file keeps the
    legacy behaviour (``permissions.md`` migration or open defaults).
    """
    json_path = anima_dir / "permissions.json"
    # Only a genuinely absent entry is "missing".  Path.exists() would also report
    # False for a dangling symlink or an unstat-able path, which must fail closed.
    try:
        os.lstat(json_path)
    except FileNotFoundError:
        return load_permissions(anima_dir)
    except OSError as exc:
        raise CodexShellGuardError(f"Cannot stat {json_path} for Codex sandbox config: {exc}") from exc
    try:
        raw = json_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CodexShellGuardError(f"Cannot read {json_path} for Codex sandbox config: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexShellGuardError(f"Invalid JSON in {json_path}; refusing to start Codex with open defaults: {exc}") from exc
    try:
        return PermissionsConfig.model_validate(data)
    except ValueError as exc:
        raise CodexShellGuardError(f"Invalid permissions in {json_path}; refusing to start Codex: {exc}") from exc


def is_private_tmp_only(permissions: PermissionsConfig) -> bool:
    return permissions.codex_shell_writes == "private_tmp_only"


def _check_private_tmp() -> None:
    """Verify /private/tmp is a root-owned sticky dir under a root-owned, non-writable /private."""
    try:
        parent = os.lstat(PRIVATE_TMP.parent)
        tmp = os.lstat(PRIVATE_TMP)
    except OSError as exc:
        raise CodexShellGuardError(f"Cannot stat {PRIVATE_TMP}: {exc}") from exc
    if not stat.S_ISDIR(tmp.st_mode) or tmp.st_uid != 0 or not tmp.st_mode & stat.S_ISVTX:
        raise CodexShellGuardError(f"{PRIVATE_TMP} must be a root-owned sticky directory")
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise CodexShellGuardError(f"{PRIVATE_TMP.parent} must be root-owned and not group/other writable")
    if os.getuid() == 0:
        raise CodexShellGuardError("private_tmp_only guard requires a non-root runtime user")


def validate_private_tmp_only(permissions: PermissionsConfig, anima_dir: Path, task_cwd: Path | None) -> None:
    """Fail closed on any setting that would widen shell writes beyond /private/tmp."""
    anima = anima_dir.resolve()
    if "/" in permissions.file_roots:
        raise CodexShellGuardError("codex_shell_writes=private_tmp_only cannot be combined with file_roots ['/']")
    extra = [r for r in permissions.file_roots if Path(r).expanduser().resolve() != anima]
    if extra:
        raise CodexShellGuardError(
            f"codex_shell_writes=private_tmp_only does not allow extra writable roots: {extra}"
        )
    # The anima dir itself as cwd is the normal case; any other cwd means a workspace edit run.
    if task_cwd is not None and Path(task_cwd).resolve() != anima:
        raise CodexShellGuardError(
            f"codex_shell_writes=private_tmp_only does not allow workspace runs (task cwd {task_cwd})"
        )
    if anima.is_relative_to(PRIVATE_TMP.resolve()):
        raise CodexShellGuardError(f"Anima dir {anima} lies inside the writable {PRIVATE_TMP}")
    _check_private_tmp()


def render_private_tmp_only_toml(escape) -> tuple[str, str]:
    """Return (top-level key, tables) replacing sandbox_mode / [sandbox_workspace_write].

    ``sandbox_mode`` must not be emitted: Codex rejects it together with
    ``default_permissions``, and a thread-level sandbox override replaces the
    profile entirely (see ``CodexSDKExecutor._sdk_sandbox``).
    """
    tmp = escape(str(PRIVATE_TMP))
    head = f'default_permissions = "{PROFILE_NAME}"\n'
    tables = (
        f"\n[permissions.{PROFILE_NAME}]\n"
        f'extends = ":read-only"\n'
        f"\n[permissions.{PROFILE_NAME}.filesystem]\n"
        f'"{tmp}" = "write"\n'
        f"\n[permissions.{PROFILE_NAME}.network]\n"
        f"enabled = true\n"
    )
    return head, tables


# ── Thread resume control ────────────────────────────────────
#
# Resuming a thread created before opt-in may keep that thread's earlier
# workspace-write permissions; this could not be verified without a model
# turn (thread/resume needs a rollout).  So a stored thread id is resumed only
# if it was started under the same profile, recorded here by the runtime.


def profile_fingerprint(escape) -> str:
    head, tables = render_private_tmp_only_toml(escape)
    return hashlib.sha256((head + tables).encode("utf-8")).hexdigest()


def is_guard_thread(codex_home: Path, thread_id: str, fingerprint: str) -> bool:
    ledger = codex_home / THREAD_LEDGER
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CodexShellGuardError(f"Cannot read Codex thread ledger {ledger}: {exc}") from exc
    return f"{thread_id}\t{fingerprint}" in lines


def revoke_guard_threads(codex_home: Path) -> None:
    """Drop all stamps; called whenever a thread starts or resumes without the guard."""
    ledger = codex_home / THREAD_LEDGER
    try:
        ledger.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CodexShellGuardError(f"Cannot revoke Codex thread ledger {ledger}: {exc}") from exc


def record_guard_thread(codex_home: Path, thread_id: str, fingerprint: str) -> None:
    if not thread_id or "\t" in thread_id or "\n" in thread_id:
        raise CodexShellGuardError(f"Refusing to record malformed Codex thread id {thread_id!r}")
    codex_home.mkdir(parents=True, exist_ok=True)
    ledger = codex_home / THREAD_LEDGER
    try:
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(f"{thread_id}\t{fingerprint}\n")
    except OSError as exc:
        raise CodexShellGuardError(f"Cannot record Codex thread {thread_id} in {ledger}: {exc}") from exc
