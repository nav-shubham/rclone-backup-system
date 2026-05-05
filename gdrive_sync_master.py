import subprocess
import logging
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


import os
# --- Configuration ---
RCLONE_EXE = os.getenv("RCLONE_PATH", "rclone")

BASE_LOG_DIR = Path(r"D:\PythonTools\rclone\logs")

BASE_LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_LOG_DIR / "master_sync.log")
    ]
)

# --- Rclone Flags ---
RCLONE_FLAGS = [
    "--exclude", "*.zip",
    "--max-size", "15M",
    "--transfers", "8",
    "--checkers", "16",
    "--drive-chunk-size", "64M",
    "--fast-list",
    "--log-level", "INFO"
]

# --- Backup Targets ---
BACKUP_TARGETS = [
    (r"D:\03 Permanent Personal Data", "gdrive:03 Permanent Personal Data", "personal_data.log"),
    (r"D:\02 ChatGPT", "gdrive:02 ChatGPT", "chatgpt.log"),
    (r"D:\07 obsidian", "gdrive:04 obsidian", "obsidian.log"),
    (r"D:\01 CA Final Master\03 AUDIT", "gdrive:04 Audit", "audit.log"),
    (r"D:\01 CA Final Master\01 FR", "gdrive:04 FR", "fr.log")
]

def execute_sync(source: str, destination: str, log_filename: str):
    """Execute rclone sync with creationflags to suppress console windows."""
    
    log_path = BASE_LOG_DIR / log_filename

    # Validate source path
    if not Path(source).exists():
        logging.error(f"Source path does not exist: {source}")
        return

    command = [
        RCLONE_EXE,
        "sync",
        source,
        destination
    ] + RCLONE_FLAGS + [f"--log-file={log_path}"]

    logging.info(f"Starting Sync: {source} -> {destination}")

    # Retry mechanism
    for attempt in range(2):
        try:
            # CREATE_NO_WINDOW (0x08000000) prevents the rclone black boxes from popping up
            subprocess.run(
                command, 
                check=True, 
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            logging.info(f"SUCCESS: {source}")
            return

        except subprocess.CalledProcessError:
            logging.warning(f"Attempt {attempt + 1} failed for {source}")

        except FileNotFoundError:
            logging.critical(f"rclone.exe not found at {RCLONE_EXE}")
            sys.exit(1)

    logging.critical(f"FINAL FAILURE: {source}")

def main():
    logging.info("=== Starting Master Sync Protocol ===")

    # Parallel execution
    with ThreadPoolExecutor(max_workers=3) as executor:
        for source, destination, log_name in BACKUP_TARGETS:
            executor.submit(execute_sync, source, destination, log_name)

    logging.info("=== Master Sync Protocol Complete ===")

if __name__ == "__main__":
    main()
