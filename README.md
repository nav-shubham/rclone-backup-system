# Rclone Auto Backup System (v2)

Robust, multi-target Google Drive backup automation system powered by `rclone sync`, with live TUI monitoring, exponential backoff retries, Telegram notifications, and `pytool` integration.

---

## Key Features

- **Concurrent Sync Execution**: Syncs multiple directories to Google Drive in parallel with `ThreadPoolExecutor`.
- **Full Exception Catching & Non-Zero Exit Codes**: No silent failures. All target failures and unhandled exceptions are caught, logged, surfaced in the summary, and result in a non-zero exit code (`sys.exit(1)`) for Task Scheduler.
- **Config-Driven Architecture**: All settings, rclone flags, targets, and notification credentials live in `config.toml`. Adding or modifying backup targets requires no code edits.
- **Live Terminal Dashboard (TUI)**: Interactive runs render a real-time progress dashboard using `rich` displaying transfer speed, bytes copied, percentages, and ETAs. Non-interactive runs (Task Scheduler / headless) automatically fall back to plain rotating log lines (`sys.stdout.isatty()` detection).
- **Outbound Telegram Notifications**: Sends failure alerts when a target exhausts all retry attempts and a one-line summary at the end of every run.
- **Startup Fail-Fast Validation**: Verifies the `rclone` binary and remote connectivity (`rclone lsd gdrive:`) prior to spinning up worker threads.
- **Exponential Backoff Retries**: Configurable delays between retry attempts (e.g. 5s, 20s) instead of instant retrying.
- **Rotating Log Files**: `master_sync.log` uses `RotatingFileHandler` (5MB limit with 5 backups) to prevent unbounded growth, alongside dedicated per-target log files.
- **PyTool Integration**: Registered as a global command (`gbackup`) to execute from anywhere in Windows CMD, PowerShell, or Win+R.

---

## Configuration (`config.toml`)

```toml
[settings]
rclone_path = "rclone"              # Overridable via RCLONE_PATH env var
log_dir = "D:\\PythonTools\\rclone\\logs"
max_workers = 3
retry_attempts = 3
retry_backoff_seconds = [5, 20]     # Delays before retrying
log_max_megabytes = 5
log_backup_count = 5

[rclone]
flags = [
    "--exclude", "*.zip",
    "--max-size", "15M",
    "--transfers", "8",
    "--checkers", "16",
    "--drive-chunk-size", "64M",
    "--fast-list",
    "--log-level", "INFO",
]

[notifications]
enabled = true
bot_token = ""                      # Or set TELEGRAM_BOT_TOKEN env var
chat_id = ""                        # Or set TELEGRAM_CHAT_ID env var

[[targets]]
source = "D:\\03 Permanent Personal Data"
destination = "gdrive:03 Permanent Personal Data"
log_name = "personal_data.log"

[[targets]]
source = "D:\\02 ChatGPT"
destination = "gdrive:02 ChatGPT"
log_name = "chatgpt.log"
```

---

## CLI Usage

### Global execution via `pytool`:
```cmd
gbackup
```

### Direct execution:
```powershell
.\.venv\Scripts\python.exe gdrive_sync_master.py
```

### Run without TUI (plain logs):
```powershell
.\.venv\Scripts\python.exe gdrive_sync_master.py --no-tui
```

### Custom configuration file:
```powershell
.\.venv\Scripts\python.exe gdrive_sync_master.py --config path\to\custom_config.toml
```

---

## Running Tests

```powershell
.\.venv\Scripts\pytest -v
```
