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
