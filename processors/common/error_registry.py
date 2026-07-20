"""
In-process registry of file processing errors.

Processors record errors here as they occur; the InboxGenerator reads the
registry to surface failing files in the NoteFlow Inbox. The registry is
in-memory only: processors retry failing files on every scheduler cycle, so
entries are re-recorded while a problem persists and cleared as soon as the
file is processed successfully (or is no longer eligible for the stage).
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

_errors: Dict[Tuple[str, str], Dict] = {}


def record_error(file_path: Path, stage: str, message: str) -> None:
    """Record (or refresh) an error for a file at a given stage."""
    key = (str(file_path), stage)
    existing = _errors.get(key)
    _errors[key] = {
        'path': Path(file_path),
        'stage': stage,
        'message': message,
        # Keep the first-seen timestamp so the inbox shows how long it's been failing
        'first_seen': existing['first_seen'] if existing else datetime.now(),
    }


def clear_error(file_path: Path, stage: str) -> None:
    """Remove the error entry for a file at a given stage, if present."""
    _errors.pop((str(file_path), stage), None)


def get_errors() -> List[Dict]:
    """Return all recorded errors, oldest first."""
    return sorted(_errors.values(), key=lambda e: e['first_seen'])


def clear_all() -> None:
    """Remove all entries (used by tests)."""
    _errors.clear()
