"""
Tests that a recording never replaces an earlier one with the same name.

Recordings are named by hand ("2026-10-12-matilda-rui.mov"), so two meetings
with the same person on the same day arrive under one name. The second used to
be moved over the first in Processed and its transcript written over the first
transcript, losing the earlier meeting entirely.
"""

from types import SimpleNamespace
from unittest.mock import patch

import assemblyai
import pytest

from processors.audio.transcriber import AudioTranscriber
from processors.audio.utils import unused_path, unused_stem
from processors.audio.video_to_audio import VideoToAudioProcessor


def test_unused_path_keeps_a_free_name(tmp_path):
    assert unused_path(tmp_path / "a.mov") == tmp_path / "a.mov"


def test_unused_path_counts_past_taken_names(tmp_path):
    (tmp_path / "a.mov").write_bytes(b"1")
    (tmp_path / "a-2.mov").write_bytes(b"2")
    assert unused_path(tmp_path / "a.mov") == tmp_path / "a-3.mov"


def test_unused_stem_checks_every_candidate(tmp_path):
    """A stem is only free if none of the files that share it exist."""
    (tmp_path / "a.json").write_text("{}")
    stem = unused_stem(
        "a",
        lambda s: tmp_path / f"{s}.md",
        lambda s: tmp_path / f"{s}.json",
    )
    assert stem == "a-2"


@pytest.mark.asyncio
async def test_second_video_with_same_name_does_not_replace_first(tmp_path):
    incoming, processed = tmp_path / "incoming", tmp_path / "processed"
    processor = VideoToAudioProcessor(incoming, incoming, processed)
    name = "2026-10-12-matilda-rui.mov"

    async def fake_extract(_input, output):
        output.write_bytes(b"audio")

    with patch.object(processor, "_extract_audio", side_effect=fake_extract):
        (incoming / name).write_bytes(b"first")
        await processor.process_single_file(name)
        # The first audio has been transcribed and moved on by the time the
        # second recording arrives.
        (incoming / "2026-10-12-matilda-rui.m4a").unlink()

        (incoming / name).write_bytes(b"second")
        await processor.process_single_file(name)

    assert (processed / name).read_bytes() == b"first"
    assert (processed / "2026-10-12-matilda-rui-2.mov").read_bytes() == b"second"
    assert (incoming / "2026-10-12-matilda-rui-2.m4a").exists()


@pytest.fixture
def transcriber(tmp_path):
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


async def _transcribe(transcriber, filename, text):
    transcript = SimpleNamespace(
        status=assemblyai.TranscriptStatus.completed,
        utterances=[SimpleNamespace(speaker="A", text=text)],
        text=text,
        json_response={"text": text},
        id="t",
    )

    async def fake_transcribe(_path):
        return transcript

    async def no_delete(_id):
        pass

    (transcriber.input_dir / filename).write_bytes(text.encode())
    transcriber.files_in_process.add(filename)
    with patch.object(transcriber, "transcribe_audio_file", side_effect=fake_transcribe), \
         patch.object(transcriber, "delete_transcript_data", side_effect=no_delete):
        await transcriber.process_single_file(filename)


@pytest.mark.asyncio
async def test_second_transcript_with_same_name_does_not_replace_first(transcriber):
    name = "2026-10-12-matilda-rui.m4a"
    await _transcribe(transcriber, name, "first meeting")
    await _transcribe(transcriber, name, "second meeting")

    out, processed = transcriber.output_dir, transcriber.processed_dir
    assert "first meeting" in (out / "2026-10-12-matilda-rui.md").read_text()
    second = (out / "2026-10-12-matilda-rui-2.md").read_text()
    assert "second meeting" in second
    assert (out / "2026-10-12-matilda-rui-2.json").exists()

    assert (processed / "2026-10-12_2026-10-12-matilda-rui.m4a").read_bytes() == b"first meeting"
    assert (processed / "2026-10-12_2026-10-12-matilda-rui-2.m4a").read_bytes() == b"second meeting"
    # The note must point at the audio it was actually made from.
    assert "2026-10-12_2026-10-12-matilda-rui-2.m4a" in second
