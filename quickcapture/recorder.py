"""Records a voice memo and hands it to the existing pipeline.

The pipeline's entry point is a file appearing in Audio/Incoming, and the file
name is what tells it what to do:

    2026-08-25-Todo 16-19-55 #todo.m4a
    |__date__| |___title____| |_tag_|

Two constraints come from downstream code, both load-bearing:

* The character right after the date must be a hyphen. The transcriber only
  extracts a title and #tags when it is (processors/audio/transcriber.py), and
  a space there silently loses the tag — the memo then gets AI-classified
  instead of being forced to the category we asked for.
* The tag must name a category the classifier accepts
  (processors/notes/transcript_classifier.py: VALID_CATEGORIES).

Recording itself is ffmpeg over avfoundation. The default input is ":default",
which follows whatever the system input device is, so external mics and
headsets work with no configuration.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.logging_config import setup_logger

logger = setup_logger(__name__)

# avfoundation input spec is "[video]:[audio]" — no video, system default audio.
DEFAULT_MIC = ":default"
FINALIZE_TIMEOUT = 10.0  # seconds to let ffmpeg close the container cleanly


class RecorderError(RuntimeError):
    """Raised when a recording cannot be started, finished or delivered."""


def build_filename(tag: str, when: datetime, suffix: str = ".m4a") -> str:
    """Name a memo so the pipeline forces it to `tag`'s category.

    See the module docstring: the hyphen after the date is required.
    """
    return f"{when:%Y-%m-%d}-{tag.capitalize()} {when:%H-%M-%S} #{tag.lower()}{suffix}"


def _mic_spec(mic: str) -> str:
    return mic if mic.startswith(":") else f":{mic}"


class Recorder:
    """Starts and stops a single ffmpeg recording at a time."""

    def __init__(
        self,
        incoming_dir: Path,
        *,
        mic: Optional[str] = None,
        ffmpeg: Optional[str] = None,
        work_dir: Optional[Path] = None,
    ):
        self.incoming_dir = Path(incoming_dir)
        self.mic = mic or os.environ.get("NOTEFLOW_CAPTURE_MIC") or DEFAULT_MIC
        self.ffmpeg = (
            ffmpeg
            or os.environ.get("NOTEFLOW_FFMPEG")
            or shutil.which("ffmpeg")
            or "/opt/homebrew/bin/ffmpeg"
        )
        self.work_dir = Path(work_dir) if work_dir else (
            Path(tempfile.gettempdir()) / f"noteflow-capture-{os.getuid()}"
        )

        self._proc: Optional[subprocess.Popen] = None
        self._tag: Optional[str] = None
        self._temp_path: Optional[Path] = None
        self._log_path: Optional[Path] = None
        self._started_at: Optional[datetime] = None
        self._started_monotonic: float = 0.0

    # ------------------------------------------------------------------ state

    @property
    def is_recording(self) -> bool:
        return self._proc is not None

    @property
    def tag(self) -> Optional[str]:
        return self._tag

    @property
    def elapsed(self) -> float:
        if not self.is_recording:
            return 0.0
        return time.monotonic() - self._started_monotonic

    def failure(self) -> Optional[str]:
        """Return an error message if ffmpeg died on its own, else None.

        Called while recording so the UI can surface a denied microphone or a
        bad device promptly, rather than at save time.
        """
        if self._proc is None or self._proc.poll() is None:
            return None
        detail = self._log_tail()
        if "Failed to create AV capture input device" in detail or "denied" in detail.lower():
            return "Microphone unavailable — check Privacy & Security settings"
        return detail or "recording stopped unexpectedly"

    # ------------------------------------------------------------------ actions

    def start(self, tag: str) -> None:
        if self.is_recording:
            raise RecorderError("already recording")

        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._tag = tag.lower()
        self._started_at = datetime.now()
        self._started_monotonic = time.monotonic()
        self._temp_path = self.work_dir / f"capture-{self._tag}.m4a"
        self._log_path = self.work_dir / "ffmpeg.log"
        self._temp_path.unlink(missing_ok=True)

        cmd = [
            self.ffmpeg, "-hide_banner", "-nostdin",
            "-f", "avfoundation", "-i", _mic_spec(self.mic),
            "-ac", "1", "-c:a", "aac", "-b:a", "96k",
            "-y", str(self._temp_path),
        ]
        logger.info("starting recording (%s): %s", self._tag, " ".join(cmd))
        try:
            log_file = self._log_path.open("w")
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=log_file, stderr=log_file,
            )
        except OSError as exc:
            self._reset()
            raise RecorderError(f"could not start ffmpeg ({self.ffmpeg}): {exc}") from exc

    def stop(self) -> Path:
        """Finish the recording and move it into Incoming. Returns the path.

        Blocks while ffmpeg finalizes the container, so call it off the UI
        thread.
        """
        if self._proc is None or self._temp_path is None or self._started_at is None:
            raise RecorderError("not recording")

        proc, temp_path, tag, started_at = (
            self._proc, self._temp_path, self._tag, self._started_at
        )
        self._proc = None  # stop reporting as recording while we finalize

        proc.send_signal(signal.SIGINT)
        deadline = time.monotonic() + FINALIZE_TIMEOUT
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            logger.warning("ffmpeg did not exit within %ss; killing", FINALIZE_TIMEOUT)
            proc.kill()
            proc.wait(timeout=2)

        try:
            if not temp_path.exists() or temp_path.stat().st_size == 0:
                raise RecorderError(self._log_tail() or "recording produced no audio")
            return self._deliver(temp_path, tag, started_at)
        finally:
            self._reset()

    def cancel(self) -> None:
        """Discard an in-progress recording."""
        proc, temp_path = self._proc, self._temp_path
        self._proc = None
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        logger.info("recording discarded")
        self._reset()

    # ------------------------------------------------------------------ helpers

    def _deliver(self, temp_path: Path, tag: str, when: datetime) -> Path:
        """Copy the finished memo into Incoming under its pipeline name.

        Lands as a dotfile first: the transcriber skips dotfiles, so it can
        never pick the memo up while the bytes are still being copied (Incoming
        lives in Google Drive, so this is a cross-filesystem copy).
        """
        if not self.incoming_dir.is_dir():
            raise RecorderError(f"Incoming folder not found: {self.incoming_dir}")

        dest = self.incoming_dir / build_filename(tag, when)
        staging = self.incoming_dir / f".noteflow-capture-{os.getpid()}.tmp"
        try:
            shutil.copyfile(temp_path, staging)
            staging.replace(dest)
        except OSError as exc:
            staging.unlink(missing_ok=True)
            raise RecorderError(f"could not write to Incoming: {exc}") from exc

        temp_path.unlink(missing_ok=True)
        logger.info("delivered memo: %s", dest.name)
        return dest

    def _log_tail(self, lines: int = 3) -> str:
        if self._log_path is None or not self._log_path.exists():
            return ""
        try:
            tail = self._log_path.read_text(errors="replace").strip().splitlines()[-lines:]
        except OSError:
            return ""
        return " / ".join(line.strip() for line in tail if line.strip())

    def _reset(self) -> None:
        self._proc = None
        self._tag = None
        self._temp_path = None
        self._started_at = None
        self._started_monotonic = 0.0
