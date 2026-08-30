"""Rclone Auto Backup System (v2).

Multi-target Google Drive backup manager using rclone with:
- Config-driven targets and flags (config.toml)
- Live TUI progress monitoring with rich
- Telegram failure alerts and end-of-run summaries
- Exponential backoff retries and non-swallowed exceptions
- Startup environment & remote validation
- Rotating file logging
- PyTool CLI integration
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

# Use standard library tomllib (Python 3.11+)
import tomllib
import requests
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ==============================================================================
# Data Models & Enums
# ==============================================================================

class TargetStatus(str, Enum):
    QUEUED = "QUEUED"
    SYNCING = "SYNCING"
    RETRYING = "RETRYING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@dataclass
class TargetConfig:
    source: str
    destination: str
    log_name: str
    name: str = ""

    def __post_init__(self):
        if not self.name:
            self.name = Path(self.source).name or self.source


@dataclass
class SettingsConfig:
    rclone_path: str = "rclone"
    log_dir: str = str(Path(__file__).parent / "logs")
    max_workers: int = 3
    retry_attempts: int = 3
    retry_backoff_seconds: list[int] = field(default_factory=lambda: [5, 20])
    log_max_megabytes: int = 5
    log_backup_count: int = 5
    show_file_queue: bool = True


@dataclass
class RcloneConfig:
    flags: list[str] = field(
        default_factory=lambda: [
            "--exclude", "*.zip",
            "--max-size", "15M",
            "--transfers", "8",
            "--checkers", "16",
            "--drive-chunk-size", "64M",
            "--fast-list",
            "--log-level", "INFO",
        ]
    )


@dataclass
class NotificationConfig:
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class AppConfig:
    settings: SettingsConfig = field(default_factory=SettingsConfig)
    rclone: RcloneConfig = field(default_factory=RcloneConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    targets: list[TargetConfig] = field(default_factory=list)


def format_bytes(num_bytes: float | int) -> str:
    """Format byte count into human-readable representation."""
    if num_bytes <= 0:
        return "0 B"
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.2f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PiB"


@dataclass
class TargetState:
    target: TargetConfig
    status: TargetStatus = TargetStatus.QUEUED
    attempt: int = 0
    max_attempts: int = 3
    transferred: str = "-"
    percentage: str = "-"
    speed: str = "-"
    eta: str = "-"
    current_file: str = ""
    file_percent: float = 0.0
    queue: list[str] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    error_message: str = ""
    start_time: float | None = None
    end_time: float | None = None
    retry_wait_until: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_initial_queue(self, initial_queue: list[str]) -> None:
        """Seed the target queue with dry-run discovered files."""
        with self._lock:
            self.queue = list(initial_queue)
            self.uploaded = []

    def on_file_completed(self, file_name: str) -> None:
        """Handle per-file completion event: move from queue to uploaded."""
        with self._lock:
            if file_name in self.queue:
                self.queue.remove(file_name)
            if file_name not in self.uploaded:
                self.uploaded.append(file_name)
            if self.current_file == file_name:
                self.current_file = ""
                self.file_percent = 0.0

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "target": self.target,
                "status": self.status,
                "attempt": self.attempt,
                "max_attempts": self.max_attempts,
                "transferred": self.transferred,
                "percentage": self.percentage,
                "speed": self.speed,
                "eta": self.eta,
                "current_file": self.current_file,
                "file_percent": self.file_percent,
                "queue": list(self.queue),
                "uploaded": list(self.uploaded),
                "error_message": self.error_message,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "retry_wait_until": self.retry_wait_until,
            }


@dataclass
class TargetResult:
    target: TargetConfig
    success: bool
    attempts: int
    error: str = ""
    duration_seconds: float = 0.0


# ==============================================================================
# Configuration Loader
# ==============================================================================

def load_config(config_path: Path | str | None = None) -> AppConfig:
    """Load configuration from TOML file with environment variable fallbacks."""
    if config_path is None:
        config_path = Path(__file__).parent / "config.toml"
    else:
        config_path = Path(config_path)

    raw_data: dict[str, Any] = {}
    if config_path.is_file():
        try:
            with open(config_path, "rb") as f:
                raw_data = tomllib.load(f)
        except Exception as e:
            logging.error(f"Failed to parse config file at {config_path}: {e}")

    # Settings
    raw_settings = raw_data.get("settings", {})
    rclone_path = os.getenv("RCLONE_PATH", raw_settings.get("rclone_path", "rclone"))
    log_dir = raw_settings.get("log_dir", str(Path(__file__).parent / "logs"))
    max_workers = int(raw_settings.get("max_workers", 3))
    retry_attempts = int(raw_settings.get("retry_attempts", 3))
    retry_backoff_seconds = raw_settings.get("retry_backoff_seconds", [5, 20])
    if not isinstance(retry_backoff_seconds, list):
        retry_backoff_seconds = [5, 20]
    log_max_mb = int(raw_settings.get("log_max_megabytes", 5))
    log_backup_count = int(raw_settings.get("log_backup_count", 5))
    show_file_queue = bool(raw_settings.get("show_file_queue", True))

    settings = SettingsConfig(
        rclone_path=rclone_path,
        log_dir=log_dir,
        max_workers=max_workers,
        retry_attempts=retry_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        log_max_megabytes=log_max_mb,
        log_backup_count=log_backup_count,
        show_file_queue=show_file_queue,
    )

    # Rclone flags
    raw_rclone = raw_data.get("rclone", {})
    flags = raw_rclone.get(
        "flags",
        [
            "--exclude", "*.zip",
            "--max-size", "15M",
            "--transfers", "8",
            "--checkers", "16",
            "--drive-chunk-size", "64M",
            "--fast-list",
            "--log-level", "INFO",
        ],
    )
    rclone_cfg = RcloneConfig(flags=flags)

    # Notifications
    raw_notif = raw_data.get("notifications", {})
    notif_enabled = raw_notif.get("enabled", True)
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", raw_notif.get("bot_token", "")).strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", str(raw_notif.get("chat_id", ""))).strip()
    notifications = NotificationConfig(
        enabled=notif_enabled,
        bot_token=bot_token,
        chat_id=chat_id,
    )

    # Targets
    raw_targets = raw_data.get("targets", [])
    targets: list[TargetConfig] = []
    for t in raw_targets:
        source = t.get("source", "").strip()
        dest = t.get("destination", "").strip()
        log_name = t.get("log_name", "").strip()
        if source and dest:
            if not log_name:
                clean_name = re.sub(r"[^\w\-]", "_", Path(source).name or "target")
                log_name = f"{clean_name}.log"
            targets.append(
                TargetConfig(
                    source=source,
                    destination=dest,
                    log_name=log_name,
                )
            )

    return AppConfig(
        settings=settings,
        rclone=rclone_cfg,
        notifications=notifications,
        targets=targets,
    )


# ==============================================================================
# Notifications (Telegram Outbound)
# ==============================================================================

def notify(message: str, config: NotificationConfig | None = None) -> bool:
    """Send an outbound notification (Telegram Bot API).

    Swappable notification channel without touching sync logic.
    """
    if config is None or not config.enabled or not config.bot_token or not config.chat_id:
        return False

    url = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
    payload = {
        "chat_id": config.chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            logging.warning(
                f"Telegram notification failed (status {response.status_code}): {response.text}"
            )
            return False
        return True
    except Exception as e:
        logging.warning(f"Failed to send Telegram notification: {e}")
        return False


# ==============================================================================
# Logging Setup
# ==============================================================================

def setup_logging(config: AppConfig, interactive_tui: bool = False) -> Path:
    """Configure rotating file logging and optional stdout stream logging."""
    log_dir = Path(config.settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    master_log_path = log_dir / "master_sync.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Remove existing handlers to avoid duplicates on re-init
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating File Handler (prevents unbounded log growth)
    max_bytes = config.settings.log_max_megabytes * 1024 * 1024
    file_handler = RotatingFileHandler(
        master_log_path,
        maxBytes=max_bytes,
        backupCount=config.settings.log_backup_count,
        encoding="utf-8",
        errors="replace",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # If non-interactive (or explicitly disabled TUI), also stream to stdout
    if not interactive_tui:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    return master_log_path


# ==============================================================================
# Startup Validation
# ==============================================================================

def resolve_rclone_binary(rclone_path: str) -> str:
    """Resolve the rclone executable path, checking explicit paths, PATH, and project local directory."""
    path_obj = Path(rclone_path)
    # 1. If existing file path
    if path_obj.is_file():
        return str(path_obj.resolve())
    
    # 2. If user specified an explicit path with directories (e.g. C:\bin\rclone.exe) that does not exist
    if len(path_obj.parts) > 1 or "/" in rclone_path or "\\" in rclone_path:
        return rclone_path

    # 3. System PATH check for bare name (e.g. "rclone")
    which_path = shutil.which(rclone_path)
    if which_path:
        return which_path

    # 4. Project local folder fallback (e.g. D:\PythonTools\rclone\rclone.exe)
    local_candidate = Path(__file__).parent / "rclone.exe"
    if local_candidate.is_file():
        return str(local_candidate.resolve())

    return rclone_path


def validate_environment(config: AppConfig) -> tuple[bool, str]:
    """Fail-fast startup validation for rclone binary and destination remotes."""
    resolved_rclone = resolve_rclone_binary(config.settings.rclone_path)

    # 1. Check rclone binary
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        res = subprocess.run(
            [resolved_rclone, "version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=flags,
        )
        if res.returncode != 0:
            return False, f"Rclone binary '{resolved_rclone}' failed to execute: {res.stderr.strip()}"
    except FileNotFoundError:
        return False, f"Rclone binary not found at '{resolved_rclone}'"
    except Exception as e:
        return False, f"Error validating rclone executable '{resolved_rclone}': {e}"

    # Update config with resolved path
    config.settings.rclone_path = resolved_rclone

    # 2. Extract and validate unique remotes from targets
    remotes: set[str] = set()
    for target in config.targets:
        if ":" in target.destination:
            remote_part = target.destination.split(":", 1)[0] + ":"
            remotes.add(remote_part)

    for remote in remotes:
        logging.info(f"Validating remote connectivity: {remote}")
        # Try `rclone about` first (fast quota/auth check), fallback to `rclone lsd`
        validated = False
        last_error = ""

        for check_cmd in ([resolved_rclone, "about", remote], [resolved_rclone, "lsd", remote, "--max-depth", "1"]):
            try:
                res = subprocess.run(
                    check_cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    creationflags=flags,
                )
                if res.returncode == 0:
                    validated = True
                    break
                else:
                    last_error = res.stderr.strip() or f"Return code {res.returncode}"
            except subprocess.TimeoutExpired:
                last_error = f"Remote check timed out after 30s for '{remote}'"
            except Exception as e:
                last_error = f"Remote check failed for '{remote}': {e}"

        if not validated:
            return False, f"Remote '{remote}' is unreachable or misconfigured: {last_error}"

    return True, "Environment valid"


# ==============================================================================
# JSON Stats Parser & Worker Engine
# ==============================================================================

def parse_rclone_json_line(line: str) -> dict[str, Any] | None:
    """Parse a single JSON log line from rclone into target metrics and file transfer state."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except Exception:
        return None

    stats = data.get("stats")
    if not isinstance(stats, dict):
        return None

    total_bytes = stats.get("totalBytes", 0) or 0
    bytes_done = stats.get("bytes", 0) or 0
    speed = stats.get("speed", 0) or 0
    eta = stats.get("eta")

    transferred_str = f"{format_bytes(bytes_done)} / {format_bytes(total_bytes)}"
    if total_bytes > 0:
        percent_val = (bytes_done / total_bytes) * 100
        percent_str = f"{percent_val:.0f}%"
    else:
        percent_str = "-"

    speed_str = f"{format_bytes(speed)}/s" if speed > 0 else "0 B/s"
    eta_str = f"{int(eta)}s" if eta is not None else "-"

    # Extract active transferring file
    transferring = stats.get("transferring")
    current_file = ""
    file_percent = 0.0

    if isinstance(transferring, list) and len(transferring) > 0:
        first_trans = transferring[0]
        if isinstance(first_trans, dict):
            current_file = first_trans.get("name", "")
            if "percentage" in first_trans:
                try:
                    file_percent = float(first_trans.get("percentage", 0.0) or 0.0)
                except (ValueError, TypeError):
                    file_percent = 0.0
            elif first_trans.get("size", 0) > 0:
                file_percent = (first_trans.get("bytes", 0) / first_trans["size"]) * 100.0

    return {
        "transferred": transferred_str,
        "percentage": percent_str,
        "speed": speed_str,
        "eta": eta_str,
        "current_file": current_file,
        "file_percent": file_percent,
        "bytes_done": bytes_done,
        "total_bytes": total_bytes,
    }


def fetch_target_queue(target: TargetConfig, config: AppConfig) -> list[str]:
    """Pre-scan target with dry-run to discover the ordered list of files to transfer."""
    rclone_exe = config.settings.rclone_path
    cmd = [
        rclone_exe,
        "sync",
        target.source,
        target.destination,
    ] + config.rclone.flags + ["--dry-run", "--use-json-log"]

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=flags,
        )
        queue: list[str] = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
                if (data.get("skipped") == "copy" or data.get("msg", "").startswith("Skipped copy")) and data.get("object"):
                    queue.append(data["object"])
            except Exception:
                continue
        return queue
    except Exception as e:
        logging.warning(f"[{target.name}] Failed to pre-calculate queue via dry-run: {e}")
        return []


def execute_sync(
    target: TargetConfig,
    state: TargetState,
    config: AppConfig,
    interactive_tui: bool = False,
) -> TargetResult:
    """Execute rclone sync with retry, exponential backoff, and live JSON stats streaming."""
    start_wall_time = time.time()
    state.update(start_time=start_wall_time, status=TargetStatus.SYNCING)

    log_dir = Path(config.settings.log_dir)
    target_log_path = log_dir / target.log_name
    target_log_path.parent.mkdir(parents=True, exist_ok=True)
    rclone_exe = config.settings.rclone_path
    max_attempts = max(1, config.settings.retry_attempts)
    backoff_list = config.settings.retry_backoff_seconds or [5, 20]
    flags = config.rclone.flags

    # Validate source path exists
    source_path = Path(target.source)
    if not source_path.exists():
        err = f"Source directory does not exist: {target.source}"
        logging.error(err)
        state.update(
            status=TargetStatus.FAILED,
            error_message=err,
            end_time=time.time(),
        )
        # Notify single target failure
        notify(
            f"🚨 <b>Target Missing</b>\n"
            f"<b>Source:</b> <code>{target.source}</code>\n"
            f"Directory does not exist on disk.",
            config.notifications,
        )
        return TargetResult(
            target=target,
            success=False,
            attempts=0,
            error=err,
            duration_seconds=time.time() - start_wall_time,
        )

    # Priority 6: Pre-calculate file queue via dry-run pass if enabled
    if config.settings.show_file_queue:
        initial_queue = fetch_target_queue(target, config)
        state.set_initial_queue(initial_queue)
        if not interactive_tui and initial_queue:
            logging.info(f"[{target.name}] Pre-scan found {len(initial_queue)} file(s) in queue")

    last_error = ""

    for attempt in range(1, max_attempts + 1):
        state.update(attempt=attempt, status=TargetStatus.SYNCING, retry_wait_until=None)
        logging.info(
            f"[{target.name}] Starting Sync (Attempt {attempt}/{max_attempts}): {target.source} -> {target.destination}"
        )

        cmd = [
            rclone_exe,
            "sync",
            target.source,
            target.destination,
        ] + flags + ["--use-json-log", "--stats=1s"]

        try:
            with open(target_log_path, "a", encoding="utf-8", errors="replace") as log_file:
                timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                log_file.write(f"\n{'='*70}\n")
                log_file.write(
                    f"[{timestamp_str}] Sync Attempt {attempt}/{max_attempts}: {target.source} -> {target.destination}\n"
                )
                log_file.write(f"Command: {' '.join(cmd)}\n")
                log_file.write(f"{'='*70}\n")
                log_file.flush()

                creation_flags = (
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                )

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creation_flags,
                )

                if proc.stdout:
                    for line in iter(proc.stdout.readline, ""):
                        if not line:
                            break
                        log_file.write(line)
                        log_file.flush()

                        # Check for per-file completion events
                        line_str = line.strip()
                        if line_str.startswith("{"):
                            try:
                                json_data = json.loads(line_str)
                                if json_data.get("object"):
                                    msg = json_data.get("msg", "")
                                    if msg.startswith("Copied") or "Copied" in msg:
                                        file_obj = json_data["object"]
                                        state.on_file_completed(file_obj)
                                        if not interactive_tui:
                                            logging.info(f"[{target.name}] Uploaded: {file_obj}")
                            except Exception:
                                pass

                        # Parse JSON stats metrics
                        parsed_stats = parse_rclone_json_line(line)
                        if parsed_stats:
                            state.update(
                                transferred=parsed_stats["transferred"],
                                percentage=parsed_stats["percentage"],
                                speed=parsed_stats["speed"],
                                eta=parsed_stats["eta"],
                                current_file=parsed_stats["current_file"],
                                file_percent=parsed_stats["file_percent"],
                            )
                            # Non-interactive (no-tty) log fallback
                            if not interactive_tui:
                                snap = state.snapshot()
                                file_info = (
                                    f" | File: {parsed_stats['current_file']} ({parsed_stats['file_percent']:.0f}%)"
                                    if parsed_stats["current_file"]
                                    else ""
                                )
                                q_info = f" | Uploaded: {len(snap['uploaded'])}, Queued: {len(snap['queue'])}"
                                logging.info(
                                    f"[{target.name}] {parsed_stats['transferred']} ({parsed_stats['percentage']}) "
                                    f"| {parsed_stats['speed']} | ETA: {parsed_stats['eta']}{file_info}{q_info}"
                                )

                return_code = proc.wait()

            if return_code == 0:
                end_time = time.time()
                state.update(
                    status=TargetStatus.SUCCESS,
                    end_time=end_time,
                    percentage="100%",
                    current_file="",
                    file_percent=100.0,
                )
                logging.info(f"SUCCESS: {target.source} -> {target.destination}")
                return TargetResult(
                    target=target,
                    success=True,
                    attempts=attempt,
                    duration_seconds=end_time - start_wall_time,
                )
            else:
                last_error = f"Process exited with non-zero code {return_code}"
                logging.warning(
                    f"Attempt {attempt}/{max_attempts} failed for {target.source} ({last_error})"
                )

        except Exception as exc:
            last_error = f"Exception during sync: {exc}"
            logging.exception(f"Unexpected exception during sync for {target.source}: {exc}")

        # Check if we should retry
        if attempt < max_attempts:
            backoff_index = min(attempt - 1, len(backoff_list) - 1)
            backoff_delay = backoff_list[backoff_index]
            wait_until = time.time() + backoff_delay
            state.update(
                status=TargetStatus.RETRYING,
                retry_wait_until=wait_until,
                error_message=last_error,
            )
            logging.warning(
                f"Retrying target {target.source} in {backoff_delay}s (Attempt {attempt+1}/{max_attempts})..."
            )
            time.sleep(backoff_delay)

    # All retry attempts exhausted
    end_time = time.time()
    state.update(
        status=TargetStatus.FAILED,
        end_time=end_time,
        error_message=last_error,
    )
    logging.critical(
        f"FINAL FAILURE: {target.source} after {max_attempts} attempts. Error: {last_error}"
    )

    # Priority 3: Telegram notification for final target failure
    notify(
        f"🚨 <b>FINAL FAILURE: Backup Target Failed</b>\n"
        f"<b>Source:</b> <code>{target.source}</code>\n"
        f"<b>Destination:</b> <code>{target.destination}</code>\n"
        f"<b>Attempts:</b> {max_attempts}\n"
        f"<b>Error:</b> <code>{last_error}</code>",
        config.notifications,
    )

    return TargetResult(
        target=target,
        success=False,
        attempts=max_attempts,
        error=last_error,
        duration_seconds=end_time - start_wall_time,
    )


# ==============================================================================
# Rich TUI Live Dashboard
# ==============================================================================

def generate_dashboard(
    states: dict[str, TargetState],
    start_time: float,
    max_workers: int,
) -> Panel:
    """Render the live multi-target status table for terminal."""
    elapsed = int(time.time() - start_time)
    elapsed_str = f"{elapsed // 60:02d}:{elapsed % 60:02d}"

    # Counts
    snapshots = [s.snapshot() for s in states.values()]
    total = len(snapshots)
    success_count = sum(1 for s in snapshots if s["status"] == TargetStatus.SUCCESS)
    failed_count = sum(1 for s in snapshots if s["status"] == TargetStatus.FAILED)
    active_count = sum(
        1 for s in snapshots if s["status"] in (TargetStatus.SYNCING, TargetStatus.RETRYING)
    )

    table = Table(expand=True, box=None, show_header=True, header_style="bold cyan")
    table.add_column("Target", style="bold white", no_wrap=True, ratio=2)
    table.add_column("Destination", style="dim", no_wrap=True, ratio=3)
    table.add_column("Status", justify="center", no_wrap=True, ratio=2)
    table.add_column("Active File", style="cyan", no_wrap=False, ratio=3)
    table.add_column("Queue / Uploaded", style="white", no_wrap=False, ratio=3)
    table.add_column("Transferred", justify="right", ratio=2)
    table.add_column("Speed / ETA", justify="right", ratio=2)
    table.add_column("Time", justify="right", ratio=1)

    for snap in snapshots:
        t: TargetConfig = snap["target"]
        st: TargetStatus = snap["status"]
        attempt_str = f"({snap['attempt']}/{snap['max_attempts']})" if snap["attempt"] > 0 else ""

        # Status and Active File formatting
        if st == TargetStatus.QUEUED:
            status_render = Text("QUEUED", style="bold blue")
            file_render = Text("-", style="dim")
        elif st == TargetStatus.SYNCING:
            status_render = Text(f"SYNCING {attempt_str}", style="bold yellow")
            if snap["current_file"]:
                file_render = Text(f"📄 {snap['current_file']} ({snap['file_percent']:.0f}%)", style="cyan")
            else:
                file_render = Text("Checking / Scanning...", style="dim italic")
        elif st == TargetStatus.RETRYING:
            remain = max(0, int(snap["retry_wait_until"] - time.time())) if snap["retry_wait_until"] else 0
            status_render = Text(f"RETRY in {remain}s {attempt_str}", style="bold magenta")
            file_render = Text("Waiting for retry backoff...", style="dim yellow")
        elif st == TargetStatus.SUCCESS:
            status_render = Text("SUCCESS", style="bold green")
            file_render = Text("✓ Complete", style="dim green")
        elif st == TargetStatus.FAILED:
            status_render = Text("FAILED", style="bold red")
            file_render = Text(snap["error_message"] or "Failed", style="dim red")
        else:
            status_render = Text(str(st), style="white")
            file_render = Text("-", style="dim")

        # Queue / Uploaded summary
        uploaded_count = len(snap["uploaded"])
        queued_count = len(snap["queue"])

        last_uploaded_preview = f" ([dim]{Path(snap['uploaded'][-1]).name}[/])" if snap["uploaded"] else ""
        next_queued_preview = f" ([dim]{Path(snap['queue'][0]).name}[/])" if snap["queue"] else ""

        if st == TargetStatus.SUCCESS:
            queue_render = Text.from_markup(f"[green]✓ Uploaded: {uploaded_count}[/]{last_uploaded_preview}")
        elif queued_count > 0 or uploaded_count > 0:
            queue_render = Text.from_markup(
                f"[green]Done: {uploaded_count}[/]{last_uploaded_preview}  "
                f"[yellow]Queue: {queued_count}[/]{next_queued_preview}"
            )
        else:
            queue_render = Text("-", style="dim")

        # Transferred string
        trans = snap["transferred"]
        if snap["percentage"] and snap["percentage"] != "-":
            trans = f"{trans} ({snap['percentage']})"

        # Speed & ETA
        speed_eta = "-"
        if snap["speed"] != "-" or snap["eta"] != "-":
            speed = snap["speed"] if snap["speed"] != "-" else "0 B/s"
            eta = f"ETA {snap['eta']}" if snap["eta"] != "-" else ""
            speed_eta = f"{speed} {eta}".strip()

        # Elapsed
        if snap["start_time"]:
            t_end = snap["end_time"] or time.time()
            t_dur = int(t_end - snap["start_time"])
            time_str = f"{t_dur // 60:02d}:{t_dur % 60:02d}"
        else:
            time_str = "--:--"

        table.add_row(
            t.name,
            t.destination,
            status_render,
            file_render,
            queue_render,
            trans,
            speed_eta,
            time_str,
        )

    header_text = (
        f"[bold]Elapsed:[/] {elapsed_str}  |  "
        f"[bold]Workers:[/] {active_count}/{max_workers} active  |  "
        f"[bold green]Success:[/] {success_count}/{total}  |  "
        f"[bold red]Failed:[/] {failed_count}/{total}"
    )

    return Panel(
        table,
        title="[bold cyan]⚡ Rclone Auto Backup System (v2)[/bold cyan]",
        subtitle=header_text,
        border_style="cyan",
    )


# ==============================================================================
# Main Orchestrator
# ==============================================================================

def run_backup(config: AppConfig, force_no_tui: bool = False) -> int:
    """Run the complete backup protocol across all configured targets."""
    # Determine interactive TUI mode
    is_tty = sys.stdout.isatty() and not force_no_tui
    master_log_file = setup_logging(config, interactive_tui=is_tty)
    console = Console()

    logging.info("=== Starting Master Sync Protocol ===")
    logging.info(f"Loaded {len(config.targets)} backup targets from configuration")

    if not config.targets:
        msg = "No backup targets configured in config.toml"
        logging.warning(msg)
        print(f"[!] {msg}")
        return 0

    # Priority 4: Startup validation (fail-fast)
    is_valid, val_msg = validate_environment(config)
    if not is_valid:
        logging.critical(f"Startup validation failed: {val_msg}")
        notify(
            f"❌ <b>Backup Aborted</b>\nStartup validation failed:\n<code>{val_msg}</code>",
            config.notifications,
        )
        if not is_tty:
            print(f"[CRITICAL] Startup validation failed: {val_msg}", file=sys.stderr)
        else:
            console.print(f"[bold red]❌ Startup Validation Failed:[/] {val_msg}")
        return 1

    # Initialize shared states
    states: dict[str, TargetState] = {}
    for target in config.targets:
        states[target.source] = TargetState(
            target=target,
            max_attempts=config.settings.retry_attempts,
        )

    start_wall_time = time.time()
    results: dict[str, TargetResult] = {}

    # Execution with thread pool
    max_workers = max(1, config.settings.max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                execute_sync,
                target,
                states[target.source],
                config,
                is_tty,
            ): target
            for target in config.targets
        }

        # Priority 2: Live TUI Dashboard for interactive terminals
        if is_tty:
            with Live(
                generate_dashboard(states, start_wall_time, max_workers),
                console=console,
                refresh_per_second=4,
                transient=False,
            ) as live:
                # Loop until all futures are complete
                while any(not f.done() for f in futures):
                    live.update(generate_dashboard(states, start_wall_time, max_workers))
                    time.sleep(0.25)
                # Final update
                live.update(generate_dashboard(states, start_wall_time, max_workers))

        # Priority 0: Collect futures and call .result() inside try/except
        for future in as_completed(futures):
            target = futures[future]
            try:
                result = future.result()
                results[target.source] = result
            except Exception as exc:
                logging.exception(
                    f"Unexpected fatal exception waiting for target {target.source}: {exc}"
                )
                results[target.source] = TargetResult(
                    target=target,
                    success=False,
                    attempts=1,
                    error=str(exc),
                )

    # Tally results
    total_count = len(config.targets)
    succeeded_targets = [r for r in results.values() if r.success]
    failed_targets = [r for r in results.values() if not r.success]
    succeeded_count = len(succeeded_targets)
    failed_count = len(failed_targets)
    failed_names = [r.target.source for r in failed_targets]

    total_duration = int(time.time() - start_wall_time)
    duration_str = f"{total_duration // 60}m {total_duration % 60}s"

    # Priority 0: Log one-line summary
    summary_log = (
        f"=== Master Sync Protocol Complete: {succeeded_count}/{total_count} succeeded, "
        f"{failed_count}/{total_count} failed in {duration_str} | "
        f"Failed: {', '.join(failed_names) if failed_names else 'None'} ==="
    )
    if failed_count == 0:
        logging.info(summary_log)
    else:
        logging.error(summary_log)

    # Priority 3: Send Telegram summary notification at the end of every run
    if failed_count == 0:
        notif_msg = (
            f"✅ <b>Backup Complete</b>\n"
            f"All <b>{succeeded_count}</b> targets synced successfully.\n"
            f"⏱ <b>Duration:</b> {duration_str}"
        )
    else:
        notif_msg = (
            f"❌ <b>Backup Alert: Failures Detected</b>\n"
            f"<b>{succeeded_count}/{total_count}</b> succeeded, <b>{failed_count}</b> failed.\n"
            f"⏱ <b>Duration:</b> {duration_str}\n\n"
            f"<b>Failed Targets:</b>\n• " + "\n• ".join(failed_names)
        )
    notify(notif_msg, config.notifications)

    # Console summary output
    if not is_tty:
        print(f"\n{summary_log}")
    else:
        if failed_count == 0:
            console.print(
                f"\n[bold green]✅ Master Sync Complete:[/] All {succeeded_count} targets synced successfully ({duration_str})."
            )
        else:
            console.print(
                f"\n[bold red]❌ Master Sync Incomplete:[/] {failed_count} target(s) failed ({', '.join(failed_names)})."
            )
        console.print(f"[dim]Master log: {master_log_file}[/dim]\n")

    # Priority 0: Exit non-zero if any target failed, 0 if all succeeded
    return 1 if failed_count > 0 else 0


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Rclone Auto Backup System (v2) - Multi-target Google Drive sync manager"
    )
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default=None,
        help="Path to custom config.toml file",
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Disable interactive TUI dashboard (force plain logging)",
    )
    args = parser.parse_args()

    app_config = load_config(args.config)
    exit_code = run_backup(app_config, force_no_tui=args.no_tui)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
