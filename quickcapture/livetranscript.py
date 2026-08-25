"""Live dictation via AssemblyAI's Universal-Streaming API.

Unlike the recording actions, nothing here touches the pipeline: audio is
streamed straight to AssemblyAI and the text goes to the clipboard. It is a
dictation tool, not a capture one.

Audio comes from ffmpeg as raw 16 kHz mono PCM on stdout, which avoids adding a
second audio dependency alongside the one already used for recording.

Note that streaming is billed per minute of connected session, so the session
is opened when dictation starts and closed as soon as it stops.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from typing import Callable, Dict, Optional, Tuple

from assemblyai.streaming.v3 import (
    StreamingClient, StreamingClientOptions, StreamingError, StreamingEvents,
    StreamingParameters, TurnEvent,
)

from config.logging_config import setup_logger
from config.secrets import ASSEMBLY_AI_KEY

logger = setup_logger(__name__)

# The SDK warns about event types it does not model (SpeechStarted), once per
# utterance. Nothing actionable, and it would bury the real log lines.
logging.getLogger("assemblyai.streaming.v3.client").setLevel(logging.ERROR)

SAMPLE_RATE = 16000
CHUNK_MS = 50
CHUNK_BYTES = int(SAMPLE_RATE * 2 * CHUNK_MS / 1000)
DEFAULT_MIC = ":default"


class LiveTranscriptError(RuntimeError):
    """Raised when dictation cannot start or the stream fails."""


class TranscriptBuffer:
    """Assembles turn events into one running transcript.

    Events arrive as successive revisions of a turn, keyed by `turn_order`, so
    the buffer keeps the latest text per turn rather than appending. A formatted
    revision (punctuated, capitalised) is never replaced by an unformatted one,
    which can otherwise arrive late and undo the formatting.
    """

    def __init__(self) -> None:
        self._turns: Dict[int, Tuple[str, bool]] = {}
        self._lock = threading.Lock()

    def update(self, turn_order: int, transcript: str, formatted: bool) -> None:
        with self._lock:
            existing = self._turns.get(turn_order)
            if existing is not None and existing[1] and not formatted:
                return
            self._turns[turn_order] = (transcript, formatted)

    @property
    def text(self) -> str:
        with self._lock:
            parts = [self._turns[key][0].strip() for key in sorted(self._turns)]
        return " ".join(part for part in parts if part)


class LiveTranscriber:
    """Streams the microphone to AssemblyAI, reporting text as it arrives."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        mic: Optional[str] = None,
        ffmpeg: Optional[str] = None,
        on_update: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self.api_key = api_key or ASSEMBLY_AI_KEY
        self.mic = mic or os.environ.get("NOTEFLOW_CAPTURE_MIC") or DEFAULT_MIC
        self.ffmpeg = (
            ffmpeg
            or os.environ.get("NOTEFLOW_FFMPEG")
            or shutil.which("ffmpeg")
            or "/opt/homebrew/bin/ffmpeg"
        )
        self._on_update = on_update
        self._on_error = on_error

        self.buffer = TranscriptBuffer()
        self._client: Optional[StreamingClient] = None
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def text(self) -> str:
        return self.buffer.text

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        if self.is_running:
            raise LiveTranscriptError("already transcribing")
        if not self.api_key:
            raise LiveTranscriptError("ASSEMBLY_AI_KEY is not set")

        mic_spec = self.mic if self.mic.startswith(":") else f":{self.mic}"
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "avfoundation", "-i", mic_spec,
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "s16le", "-",
        ]
        logger.info("starting live transcription")
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise LiveTranscriptError(f"could not start ffmpeg: {exc}") from exc

        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="live-transcribe"
        )
        self._thread.start()

    def stop(self) -> str:
        """End the session and return the transcript collected so far."""
        self._stopping.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()          # ends the chunk generator, unblocking stream()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("live transcription thread did not exit cleanly")
        self._thread = None
        self._proc = None
        logger.info("live transcription stopped (%d chars)", len(self.text))
        return self.text

    # ------------------------------------------------------------------ internals

    def _run(self) -> None:
        try:
            client = StreamingClient(StreamingClientOptions(api_key=self.api_key))
            self._client = client
            client.on(StreamingEvents.Turn, self._on_turn)
            client.on(StreamingEvents.Error, self._on_stream_error)
            client.connect(StreamingParameters(
                sample_rate=SAMPLE_RATE, format_turns=True,
            ))
            try:
                client.stream(self._chunks())
            finally:
                client.disconnect(terminate=True)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI instead
            if not self._stopping.is_set():
                logger.exception("live transcription failed")
                self._report_error(str(exc))
        finally:
            self._client = None

    def _chunks(self):
        proc = self._proc
        while proc is not None and not self._stopping.is_set():
            chunk = proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                return
            yield chunk

    def _on_turn(self, client, event: TurnEvent) -> None:
        self.buffer.update(
            event.turn_order, event.transcript or "",
            bool(getattr(event, "turn_is_formatted", False)),
        )
        if self._on_update is not None:
            self._on_update(self.buffer.text)

    def _on_stream_error(self, client, error: StreamingError) -> None:
        if self._stopping.is_set():
            return
        logger.error("streaming error: %s", error)
        self._report_error(str(error))

    def _report_error(self, message: str) -> None:
        if self._on_error is not None:
            self._on_error(message)

    def ffmpeg_error(self) -> str:
        """Whatever ffmpeg complained about, for diagnosing a dead capture."""
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            return (self._proc.stderr.read() or b"").decode(errors="replace").strip()
        except (OSError, ValueError):
            return ""
