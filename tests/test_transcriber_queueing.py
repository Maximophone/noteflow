"""
Tests for AudioTranscriber queueing guards.

process_all runs on a 30 second scheduler tick while a transcription of a
40 minute recording is still in flight, so the guards that stop a file being
queued twice are load-bearing: a duplicate queue means the same audio is sent
to AssemblyAI twice and billed twice.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from processors.audio.transcriber import AudioTranscriber


@pytest.fixture
def transcriber(tmp_path):
    """An AudioTranscriber over temp dirs, with AI title generation stubbed out."""
    input_dir = tmp_path / "incoming"
    input_dir.mkdir(parents=True)
    with patch("processors.audio.transcriber.AI"), \
         patch("processors.audio.transcriber.get_prompt", return_value=""):
        yield AudioTranscriber(
            input_dir=input_dir,
            output_dir=tmp_path / "out",
            processed_dir=tmp_path / "processed",
            api_key="test-key",
        )


@pytest.mark.asyncio
async def test_file_in_flight_is_not_queued_again(transcriber):
    """A second tick must not re-queue a file whose transcription is still running."""
    (transcriber.input_dir / "meeting.m4a").write_bytes(b"fake audio")

    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow_process(filename):
        calls.append(filename)
        transcriber.files_in_process.add(filename)
        started.set()
        # Bounded rather than an unbounded release.wait(): if the guard regresses,
        # the second tick queues a duplicate and gathers it, so an unbounded wait
        # would deadlock the suite instead of failing it.
        try:
            await asyncio.wait_for(release.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        transcriber.files_in_process.discard(filename)

    with patch.object(transcriber, "process_single_file", side_effect=slow_process):
        first_tick = asyncio.create_task(transcriber.process_all())
        await asyncio.wait_for(started.wait(), timeout=2)

        # Second scheduler tick while the first transcription is still in flight.
        # This must return promptly having queued nothing; if it blocks, it is
        # waiting on a duplicate transcription it should never have started.
        try:
            await asyncio.wait_for(transcriber.process_all(), timeout=2)
        except asyncio.TimeoutError:
            release.set()
            await asyncio.wait_for(first_tick, timeout=5)
            pytest.fail(
                f"second tick blocked on a duplicate transcription; "
                f"process_single_file was called {len(calls)}x for one file"
            )

        release.set()
        await asyncio.wait_for(first_tick, timeout=5)

    assert calls == ["meeting.m4a"], f"file was queued {len(calls)} times, expected once"


@pytest.mark.asyncio
async def test_recent_failure_is_not_retried_immediately(transcriber):
    """A file that just failed is skipped rather than retried on the next tick."""
    (transcriber.input_dir / "broken.m4a").write_bytes(b"fake audio")
    transcriber.failed_recently["broken.m4a"] = datetime.now()

    calls = []

    async def record(filename):
        calls.append(filename)
        transcriber.files_in_process.discard(filename)

    with patch.object(transcriber, "process_single_file", side_effect=record):
        await transcriber.process_all()

    assert calls == [], "file that failed seconds ago should not be retried yet"


@pytest.mark.asyncio
async def test_failure_backoff_expires(transcriber):
    """Once the backoff window has passed, the file is retried and the entry cleared."""
    (transcriber.input_dir / "broken.m4a").write_bytes(b"fake audio")
    transcriber.failed_recently["broken.m4a"] = datetime.now() - timedelta(seconds=301)

    calls = []

    async def record(filename):
        calls.append(filename)
        transcriber.files_in_process.discard(filename)

    with patch.object(transcriber, "process_single_file", side_effect=record):
        await transcriber.process_all()

    assert calls == ["broken.m4a"]
    assert "broken.m4a" not in transcriber.failed_recently


@pytest.mark.asyncio
async def test_failure_is_recorded_for_backoff(transcriber, monkeypatch):
    """process_single_file records the failure so the next tick can back off."""
    (transcriber.input_dir / "broken.m4a").write_bytes(b"fake audio")
    transcriber.files_in_process.add("broken.m4a")

    async def boom(_path):
        raise RuntimeError("AssemblyAI exploded")

    monkeypatch.setattr(transcriber, "transcribe_audio_file", boom)

    with pytest.raises(RuntimeError):
        await transcriber.process_single_file("broken.m4a")

    assert "broken.m4a" in transcriber.failed_recently
    assert "broken.m4a" not in transcriber.files_in_process
