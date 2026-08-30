"""Unit tests for rclone JSON stats output parsing."""

import json
from gdrive_sync_master import parse_rclone_json_line, format_bytes


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.00 KiB"
    assert format_bytes(45705203) == "43.59 MiB"
    assert format_bytes(1073741824) == "1.00 GiB"


def test_parse_rclone_json_active_file_transfer():
    sample_json = {
        "time": "2026-08-30T11:34:35.5026747+05:30",
        "level": "notice",
        "msg": "Transferred stats",
        "stats": {
            "bytes": 22000000,
            "totalBytes": 45705203,
            "speed": 2621440.0,
            "eta": 9,
            "transferring": [
                {
                    "name": "07 Books/Pyramid_Principle.pdf",
                    "bytes": 11000000,
                    "size": 22000000,
                    "percentage": 50.0,
                    "speed": 1310720.0,
                    "eta": 8,
                }
            ],
        },
    }
    line = json.dumps(sample_json)
    parsed = parse_rclone_json_line(line)

    assert parsed is not None
    assert parsed["transferred"] == "20.98 MiB / 43.59 MiB"
    assert parsed["percentage"] == "48%"
    assert parsed["speed"] == "2.50 MiB/s"
    assert parsed["eta"] == "9s"
    assert parsed["current_file"] == "07 Books/Pyramid_Principle.pdf"
    assert parsed["file_percent"] == 50.0


def test_parse_rclone_json_without_active_transfer():
    sample_json = {
        "time": "2026-08-30T11:34:35.5026747+05:30",
        "level": "notice",
        "msg": "Transferred stats",
        "stats": {
            "bytes": 0,
            "totalBytes": 0,
            "speed": 0,
            "eta": None,
            "transferring": [],
        },
    }
    line = json.dumps(sample_json)
    parsed = parse_rclone_json_line(line)

    assert parsed is not None
    assert parsed["transferred"] == "0 B / 0 B"
    assert parsed["percentage"] == "-"
    assert parsed["speed"] == "0 B/s"
    assert parsed["eta"] == "-"
    assert parsed["current_file"] == ""
    assert parsed["file_percent"] == 0.0


def test_parse_rclone_json_non_stats_event():
    sample_json = {
        "time": "2026-08-30T11:34:35.0466582+05:30",
        "level": "notice",
        "msg": "Skipped copy as --dry-run is set",
        "object": "file.pdf",
    }
    line = json.dumps(sample_json)
    parsed = parse_rclone_json_line(line)
    assert parsed is None


def test_parse_rclone_non_json_line():
    assert parse_rclone_json_line("Not a JSON line") is None
    assert parse_rclone_json_line("") is None


def test_target_state_queue_and_uploaded_lifecycle():
    from gdrive_sync_master import TargetConfig, TargetState

    target = TargetConfig(source="D:/Source", destination="gdrive:Dest", log_name="test.log")
    state = TargetState(target=target)

    # Initial queue
    state.set_initial_queue(["file1.pdf", "file2.pdf", "file3.pdf"])
    snap = state.snapshot()
    assert snap["queue"] == ["file1.pdf", "file2.pdf", "file3.pdf"]
    assert snap["uploaded"] == []

    # File 1 starts transferring and completes
    state.update(current_file="file1.pdf", file_percent=50.0)
    state.on_file_completed("file1.pdf")
    snap = state.snapshot()
    assert snap["queue"] == ["file2.pdf", "file3.pdf"]
    assert snap["uploaded"] == ["file1.pdf"]
    assert snap["current_file"] == ""
    assert snap["file_percent"] == 0.0

    # File 2 completes
    state.on_file_completed("file2.pdf")
    snap = state.snapshot()
    assert snap["queue"] == ["file3.pdf"]
    assert snap["uploaded"] == ["file1.pdf", "file2.pdf"]


def test_fetch_target_queue_parsing():
    from unittest.mock import patch, MagicMock
    from gdrive_sync_master import TargetConfig, AppConfig, SettingsConfig, fetch_target_queue

    sample_output = "\n".join([
        json.dumps({"level": "notice", "msg": "Skipped copy as --dry-run is set", "skipped": "copy", "object": "doc1.pdf"}),
        json.dumps({"level": "notice", "msg": "Skipped copy as --dry-run is set", "skipped": "copy", "object": "doc2.pdf"}),
        json.dumps({"level": "notice", "msg": "Skipped set directory modification time", "skipped": "set directory modification time", "object": "dir"}),
    ])

    mock_res = MagicMock()
    mock_res.stdout = sample_output

    with patch("subprocess.run", return_value=mock_res):
        target = TargetConfig(source="D:/Source", destination="gdrive:Dest", log_name="test.log")
        cfg = AppConfig(settings=SettingsConfig())
        queue = fetch_target_queue(target, cfg)
        assert queue == ["doc1.pdf", "doc2.pdf"]

