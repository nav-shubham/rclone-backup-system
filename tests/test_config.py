"""Unit tests for configuration loading and validation."""

import os
from pathlib import Path
import tempfile
import pytest

from gdrive_sync_master import (
    load_config,
    resolve_rclone_binary,
    AppConfig,
    TargetConfig,
    SettingsConfig,
)


def test_load_config_defaults(tmp_path: Path):
    empty_config = tmp_path / "config.toml"
    empty_config.write_text("", encoding="utf-8")

    cfg = load_config(empty_config)
    assert cfg.settings.max_workers == 3
    assert cfg.settings.retry_attempts == 3
    assert cfg.settings.retry_backoff_seconds == [5, 20]
    assert cfg.settings.log_max_megabytes == 5
    assert len(cfg.targets) == 0


def test_load_config_with_values(tmp_path: Path):
    toml_content = """
    [settings]
    rclone_path = "custom_rclone"
    log_dir = "C:\\\\tmp\\\\logs"
    max_workers = 5
    retry_attempts = 4
    retry_backoff_seconds = [2, 10, 30]
    show_file_queue = false

    [rclone]
    flags = ["--fast-list", "--drive-chunk-size", "128M"]

    [notifications]
    enabled = true
    bot_token = "test_bot_token"
    chat_id = "test_chat_id"

    [[targets]]
    source = "D:\\\\FolderA"
    destination = "gdrive:FolderA"
    log_name = "foldera.log"

    [[targets]]
    source = "D:\\\\FolderB"
    destination = "gdrive:FolderB"
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content, encoding="utf-8")

    cfg = load_config(config_file)
    assert cfg.settings.rclone_path == "custom_rclone"
    assert cfg.settings.log_dir == "C:\\tmp\\logs"
    assert cfg.settings.max_workers == 5
    assert cfg.settings.retry_attempts == 4
    assert cfg.settings.retry_backoff_seconds == [2, 10, 30]
    assert cfg.settings.show_file_queue is False
    assert cfg.rclone.flags == ["--fast-list", "--drive-chunk-size", "128M"]
    assert cfg.notifications.enabled is True
    assert cfg.notifications.bot_token == "test_bot_token"
    assert cfg.notifications.chat_id == "test_chat_id"
    assert len(cfg.targets) == 2
    assert cfg.targets[0].source == "D:\\FolderA"
    assert cfg.targets[0].log_name == "foldera.log"
    assert cfg.targets[1].source == "D:\\FolderB"
    assert cfg.targets[1].log_name == "FolderB.log"


def test_load_config_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RCLONE_PATH", "env_rclone_exe")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env_token_123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999888777")

    empty_config = tmp_path / "config.toml"
    empty_config.write_text("", encoding="utf-8")

    cfg = load_config(empty_config)
    assert cfg.settings.rclone_path == "env_rclone_exe"
    assert cfg.notifications.bot_token == "env_token_123"
    assert cfg.notifications.chat_id == "999888777"
