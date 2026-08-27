"""The durable record of audio that could never be transcribed.

Audio-stage failures cannot live in the in-memory error registry the way note
failures do. A note that fails is still scanned every cycle, so its entry is
re-recorded continuously; a recording that fails is moved out of Incoming and
never looked at again, so its entry disappears on the next restart and the
inbox goes back to claiming everything is fine.

So the folder itself is the record: the recording sits in Audio/Failed with the
reason in a sidecar next to it. Nothing to keep in sync — deleting the recording
clears the report, and dropping it back into Incoming retries it.
"""

from pathlib import Path
from typing import Dict, List

from config.logging_config import setup_logger

logger = setup_logger(__name__)

REASON_SUFFIX = ".why.txt"
AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".aiff"}


def reason_path(audio_path: Path) -> Path:
    """Where the reason for this recording's failure is kept."""
    return audio_path.with_name(audio_path.name + REASON_SUFFIX)


def write_failure_reason(audio_path: Path, reason: str) -> None:
    try:
        reason_path(audio_path).write_text(reason.strip() + "\n", encoding="utf-8")
    except OSError as exc:
        # The recording is already parked; losing the note is not worth failing over.
        logger.error("Could not record why %s failed: %s", audio_path.name, exc)


def list_failures(failed_dir: Path) -> List[Dict]:
    """Every parked recording with its reason, oldest first."""
    if not failed_dir or not failed_dir.is_dir():
        return []

    failures = []
    try:
        entries = list(failed_dir.iterdir())
    except OSError as exc:
        logger.error("Could not read %s: %s", failed_dir, exc)
        return []

    for path in entries:
        if path.name.startswith(".") or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        reason = ""
        note = reason_path(path)
        if note.exists():
            try:
                reason = note.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                reason = ""
        try:
            when = path.stat().st_mtime
        except OSError:
            when = 0.0
        failures.append({
            "name": path.name,
            "reason": reason or "no reason recorded",
            "modified": when,
        })

    failures.sort(key=lambda f: f["modified"])
    return failures
