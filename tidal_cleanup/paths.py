"""Where tidal-cleanup keeps its state."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Credentials live outside the project so they never end up in a repo.
CONFIG_DIR = Path(
    os.environ.get("TIDAL_CLEANUP_CONFIG", Path.home() / ".config" / "tidal-cleanup")
)
SESSION_FILE = CONFIG_DIR / "session.json"

# Snapshots, backups and logs live next to the code so they are easy to inspect.
DATA_DIR = Path(os.environ.get("TIDAL_CLEANUP_DATA", PROJECT_ROOT / "data"))
SNAPSHOT_FILE = DATA_DIR / "snapshot.json"
CACHE_FILE = DATA_DIR / "cache.json"
PLAN_FILE = DATA_DIR / "plan.tsv"
BACKUP_DIR = DATA_DIR / "backups"
IGNORE_FILE = DATA_DIR / "ignored.json"
ACTION_LOG = DATA_DIR / "actions.jsonl"
TRIAGE_FILE = DATA_DIR / "triage.json"
ATMOS_CACHE = DATA_DIR / "atmos.json"
NOTES_BACKUP_DIR = DATA_DIR / "notes"


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
