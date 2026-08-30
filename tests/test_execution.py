"""Unit and integration tests for execution engine, retries, and exit codes."""

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, patch
import pytest

from gdrive_sync_master import (
    AppConfig,
    NotificationConfig,
    RcloneConfig,
    SettingsConfig,
    TargetConfig,
    TargetResult,
    TargetState,
    TargetStatus,
    execute_sync,
    run_backup,
    validate_environment,
    resolve_rclone_binary,
)


def test_missing_source_path_marks_failed(tmp_path: Path):
    non_existent = tmp_path / "does_not_exist_xyz"
    target = TargetConfig(
        source=str(non_existent),
        destination="gdrive:NonExistent",
        log_name="test.log",
    )
    state = TargetState(target=target)
    cfg = AppConfig(
        settings=SettingsConfig(log_dir=str(tmp_path / "logs")),
        notifications=NotificationConfig(enabled=False),
    )

    result = execute_sync(target, state, cfg)
    assert result.success is False
    assert state.status == TargetStatus.FAILED
    assert "does not exist" in result.error


@patch("subprocess.Popen")
def test_sync_retries_and_exponential_backoff(mock_popen: MagicMock, tmp_path: Path):
    source_dir = tmp_path / "source_folder"
    source_dir.mkdir()

    # Mock Popen to fail on attempt 1, fail on attempt 2, succeed on attempt 3
    proc_fail = MagicMock()
    proc_fail.stdout = None
    proc_fail.wait.return_value = 1

    proc_success = MagicMock()
    proc_success.stdout = None
    proc_success.wait.return_value = 0

    mock_popen.side_effect = [proc_fail, proc_fail, proc_success]

    target = TargetConfig(
        source=str(source_dir),
        destination="gdrive:source_folder",
        log_name="source.log",
    )
    state = TargetState(target=target, max_attempts=3)
    cfg = AppConfig(
        settings=SettingsConfig(
            log_dir=str(tmp_path / "logs"),
            retry_attempts=3,
            retry_backoff_seconds=[0.01, 0.02],
            show_file_queue=False,
        ),
        notifications=NotificationConfig(enabled=False),
    )

    result = execute_sync(target, state, cfg)
    assert result.success is True
    assert result.attempts == 3
    assert state.status == TargetStatus.SUCCESS
    assert mock_popen.call_count == 3


@patch("subprocess.Popen")
def test_sync_final_failure_after_exhausting_retries(mock_popen: MagicMock, tmp_path: Path):
    source_dir = tmp_path / "source_folder_fail"
    source_dir.mkdir()

    proc_fail = MagicMock()
    proc_fail.stdout = None
    proc_fail.wait.return_value = 2

    mock_popen.return_value = proc_fail

    target = TargetConfig(
        source=str(source_dir),
        destination="gdrive:source_folder_fail",
        log_name="source_fail.log",
    )
    state = TargetState(target=target, max_attempts=2)
    cfg = AppConfig(
        settings=SettingsConfig(
            log_dir=str(tmp_path / "logs"),
            retry_attempts=2,
            retry_backoff_seconds=[0.01],
            show_file_queue=False,
        ),
        notifications=NotificationConfig(enabled=False),
    )

    result = execute_sync(target, state, cfg)
    assert result.success is False
    assert result.attempts == 2
    assert state.status == TargetStatus.FAILED
    assert "code 2" in result.error


@patch("gdrive_sync_master.validate_environment", return_value=(True, "OK"))
def test_run_backup_returns_exit_code_1_on_failure(mock_val: MagicMock, tmp_path: Path):
    # Setup one valid source and one missing source
    valid_source = tmp_path / "valid"
    valid_source.mkdir()
    missing_source = tmp_path / "missing"

    cfg = AppConfig(
        settings=SettingsConfig(
            log_dir=str(tmp_path / "logs"),
            max_workers=2,
            retry_attempts=1,
            retry_backoff_seconds=[0.01],
        ),
        notifications=NotificationConfig(enabled=False),
        targets=[
            TargetConfig(source=str(valid_source), destination="gdrive:valid", log_name="v.log"),
            TargetConfig(source=str(missing_source), destination="gdrive:missing", log_name="m.log"),
        ],
    )

    with patch("gdrive_sync_master.execute_sync") as mock_exec:
        # One succeeds, one fails
        mock_exec.side_effect = [
            TargetResult(target=cfg.targets[0], success=True, attempts=1),
            TargetResult(target=cfg.targets[1], success=False, attempts=1, error="Missing dir"),
        ]

        exit_code = run_backup(cfg, force_no_tui=True)
        assert exit_code == 1


@patch("gdrive_sync_master.validate_environment", return_value=(True, "OK"))
def test_run_backup_returns_exit_code_0_on_success(mock_val: MagicMock, tmp_path: Path):
    valid_source = tmp_path / "valid"
    valid_source.mkdir()

    cfg = AppConfig(
        settings=SettingsConfig(
            log_dir=str(tmp_path / "logs"),
            max_workers=1,
            retry_attempts=1,
        ),
        notifications=NotificationConfig(enabled=False),
        targets=[
            TargetConfig(source=str(valid_source), destination="gdrive:valid", log_name="v.log"),
        ],
    )

    with patch("gdrive_sync_master.execute_sync") as mock_exec:
        mock_exec.return_value = TargetResult(target=cfg.targets[0], success=True, attempts=1)
        exit_code = run_backup(cfg, force_no_tui=True)
        assert exit_code == 0


def test_startup_validation_failure(tmp_path: Path):
    cfg = AppConfig(
        settings=SettingsConfig(
            rclone_path=str(tmp_path / "non_existent_rclone.exe"),
            log_dir=str(tmp_path / "logs"),
        ),
        notifications=NotificationConfig(enabled=False),
    )

    is_valid, msg = validate_environment(cfg)
    assert is_valid is False
    assert "not found" in msg.lower()
