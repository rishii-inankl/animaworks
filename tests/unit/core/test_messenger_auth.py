"""Unit tests for inbox from_person validation and inbox directory permissions."""
# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.messenger import InboxItem, Messenger
from core.schemas import Message


def _mock_config_with_animas(*names: str) -> MagicMock:
    cfg = MagicMock()
    cfg.animas = {n: MagicMock() for n in names}
    return cfg


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir()
    return d


# ── receive() from_person validation ─────────────────────


class TestReceiveFromPersonValidation:
    @patch("core.config.models.load_config")
    def test_known_anima_accepted(self, mock_load: MagicMock, shared_dir: Path):
        mock_load.return_value = _mock_config_with_animas("alice", "bob")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"
        msg = Message(from_person="bob", to_person="alice", content="Hello")
        (inbox / "msg.json").write_text(msg.model_dump_json(), encoding="utf-8")

        messages = alice.receive()
        assert len(messages) == 1
        assert messages[0].from_person == "bob"

    @patch("core.config.models.load_config")
    def test_unknown_sender_ignored(self, mock_load: MagicMock, shared_dir: Path):
        mock_load.return_value = _mock_config_with_animas("alice", "bob")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"
        msg = Message(from_person="hacker", to_person="alice", content="Spoofed")
        (inbox / "msg.json").write_text(msg.model_dump_json(), encoding="utf-8")

        messages = alice.receive()
        assert len(messages) == 0

    @patch("core.config.models.load_config")
    def test_unknown_sender_warning_logged(
        self, mock_load: MagicMock, shared_dir: Path, caplog: pytest.LogCaptureFixture,
    ):
        import logging

        mock_load.return_value = _mock_config_with_animas("alice")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"
        msg = Message(from_person="impostor", to_person="alice", content="Fake")
        (inbox / "msg.json").write_text(msg.model_dump_json(), encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="animaworks.messenger"):
            alice.receive()

        assert any("unknown from_person" in r.message for r in caplog.records)
        assert any("impostor" in r.message for r in caplog.records)

    @patch("core.config.models.load_config")
    def test_unknown_sender_file_not_deleted(self, mock_load: MagicMock, shared_dir: Path):
        mock_load.return_value = _mock_config_with_animas("alice")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"
        msg = Message(from_person="hacker", to_person="alice", content="Spoofed")
        msg_path = inbox / "msg.json"
        msg_path.write_text(msg.model_dump_json(), encoding="utf-8")

        alice.receive()
        assert msg_path.exists(), "Unknown sender message file must not be deleted"

    @patch("core.config.models.load_config")
    def test_mixed_known_and_unknown_senders(self, mock_load: MagicMock, shared_dir: Path):
        mock_load.return_value = _mock_config_with_animas("alice", "bob")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"

        good = Message(from_person="bob", to_person="alice", content="Valid")
        (inbox / "01_good.json").write_text(good.model_dump_json(), encoding="utf-8")
        bad = Message(from_person="stranger", to_person="alice", content="Bad")
        (inbox / "02_bad.json").write_text(bad.model_dump_json(), encoding="utf-8")

        messages = alice.receive()
        assert len(messages) == 1
        assert messages[0].content == "Valid"


# ── receive_with_paths() from_person validation ──────────


class TestReceiveWithPathsFromPersonValidation:
    @patch("core.config.models.load_config")
    def test_known_anima_accepted(self, mock_load: MagicMock, shared_dir: Path):
        mock_load.return_value = _mock_config_with_animas("alice", "bob")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"
        msg = Message(from_person="bob", to_person="alice", content="Hello")
        (inbox / "msg.json").write_text(msg.model_dump_json(), encoding="utf-8")

        items = alice.receive_with_paths()
        assert len(items) == 1
        assert isinstance(items[0], InboxItem)
        assert items[0].msg.from_person == "bob"

    @patch("core.config.models.load_config")
    def test_unknown_sender_ignored(self, mock_load: MagicMock, shared_dir: Path):
        mock_load.return_value = _mock_config_with_animas("alice")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"
        msg = Message(from_person="hacker", to_person="alice", content="Spoofed")
        (inbox / "msg.json").write_text(msg.model_dump_json(), encoding="utf-8")

        items = alice.receive_with_paths()
        assert len(items) == 0

    @patch("core.config.models.load_config")
    def test_cli_human_sender_accepted(self, mock_load: MagicMock, shared_dir: Path):
        mock_load.return_value = _mock_config_with_animas("alice", "bob")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"
        msg = Message(
            from_person="reoishii",
            to_person="alice",
            content="Please start",
            source="cli",
            origin_chain=["human"],
        )
        (inbox / "msg.json").write_text(msg.model_dump_json(), encoding="utf-8")

        items = alice.receive_with_paths()

        assert len(items) == 1
        assert items[0].msg.source == "cli"
        assert items[0].msg.origin_chain == ["human"]

    @patch("core.config.models.load_config")
    def test_unknown_source_is_ignored_and_operationally_logged(
        self, mock_load: MagicMock, shared_dir: Path, caplog: pytest.LogCaptureFixture,
    ):
        import logging

        mock_load.return_value = _mock_config_with_animas("alice")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"
        msg = Message(from_person="someone", to_person="alice", content="Bad", source="carrier-pigeon")
        (inbox / "msg.json").write_text(msg.model_dump_json(), encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            items = alice.receive_with_paths()

        assert items == []
        assert any(r.name == "animaworks.messenger" and "unknown source" in r.message for r in caplog.records)

    @patch("core.config.models.load_config")
    def test_unknown_sender_warning_logged(
        self, mock_load: MagicMock, shared_dir: Path, caplog: pytest.LogCaptureFixture,
    ):
        import logging

        mock_load.return_value = _mock_config_with_animas("alice")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"
        msg = Message(from_person="impostor", to_person="alice", content="Fake")
        (inbox / "msg.json").write_text(msg.model_dump_json(), encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="animaworks.messenger"):
            alice.receive_with_paths()

        assert any("unknown from_person" in r.message for r in caplog.records)

    @patch("core.config.models.load_config")
    def test_unknown_sender_file_not_deleted(self, mock_load: MagicMock, shared_dir: Path):
        mock_load.return_value = _mock_config_with_animas("alice")
        alice = Messenger(shared_dir, "alice")
        inbox = shared_dir / "inbox" / "alice"
        msg = Message(from_person="hacker", to_person="alice", content="Spoofed")
        msg_path = inbox / "msg.json"
        msg_path.write_text(msg.model_dump_json(), encoding="utf-8")

        alice.receive_with_paths()
        assert msg_path.exists(), "Unknown sender message file must not be deleted"


# ── Inbox directory permissions ──────────────────────────


class TestInboxPermissions:
    def test_messenger_init_sets_inbox_chmod_700(self, shared_dir: Path):
        Messenger(shared_dir, "bob")
        inbox = shared_dir / "inbox" / "bob"
        mode = stat.S_IMODE(inbox.stat().st_mode)
        assert mode == 0o700

    def test_ensure_runtime_only_dirs_sets_inbox_chmod_700(self, tmp_path: Path):
        from core.init import _ensure_runtime_only_dirs

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _ensure_runtime_only_dirs(data_dir)

        inbox = data_dir / "shared" / "inbox"
        mode = stat.S_IMODE(inbox.stat().st_mode)
        assert mode == 0o700
