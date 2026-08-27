"""Tests for the durable record of untranscribable recordings.

The point of putting this on disk is that it survives a restart, which the
in-memory error registry does not. These tests pin that down, plus the property
that keeps it honest: the report follows the files, so it cannot go stale.
"""

import asyncio

import pytest

from processors.common import error_registry
from processors.common.failed_audio import (
    list_failures, reason_path, write_failure_reason,
)
from processors.notes.inbox_generator import InboxGenerator


REASON = "language_detection cannot be performed on files with no spoken audio."


@pytest.fixture
def failed_dir(tmp_path):
    d = tmp_path / "Failed"
    d.mkdir()
    return d


def park(failed_dir, name, reason=REASON):
    audio = failed_dir / name
    audio.write_bytes(b"fake audio")
    if reason is not None:
        write_failure_reason(audio, reason)
    return audio


class TestDurableRecord:
    def test_reason_is_readable_back(self, failed_dir):
        park(failed_dir, "2026-08-26-Todo 11-04-03 #todo.m4a")
        failures = list_failures(failed_dir)
        assert len(failures) == 1
        assert failures[0]["name"] == "2026-08-26-Todo 11-04-03 #todo.m4a"
        assert failures[0]["reason"] == REASON

    def test_survives_a_restart(self, failed_dir):
        """The whole point: in-memory state is gone, the record is not."""
        park(failed_dir, "memo.m4a")
        error_registry.clear_all()          # as a restart would
        assert list_failures(failed_dir)[0]["reason"] == REASON

    def test_deleting_the_recording_clears_the_report(self, failed_dir):
        audio = park(failed_dir, "memo.m4a")
        assert list_failures(failed_dir)
        audio.unlink()
        assert list_failures(failed_dir) == []

    def test_a_missing_reason_is_reported_honestly(self, failed_dir):
        park(failed_dir, "memo.m4a", reason=None)
        assert list_failures(failed_dir)[0]["reason"] == "no reason recorded"

    def test_sidecars_are_not_listed_as_recordings(self, failed_dir):
        park(failed_dir, "memo.m4a")
        assert reason_path(failed_dir / "memo.m4a").exists()
        assert [f["name"] for f in list_failures(failed_dir)] == ["memo.m4a"]

    def test_non_audio_and_hidden_files_are_ignored(self, failed_dir):
        park(failed_dir, "memo.m4a")
        (failed_dir / ".DS_Store").write_bytes(b"junk")
        (failed_dir / "notes.txt").write_text("hi")
        assert [f["name"] for f in list_failures(failed_dir)] == ["memo.m4a"]

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert list_failures(tmp_path / "nope") == []
        assert list_failures(None) == []


class TestInboxRendering:
    def _inbox(self, tmp_path, failed_dir):
        scan = tmp_path / "Transcriptions"
        scan.mkdir()
        inbox_path = tmp_path / "Inbox.md"
        gen = InboxGenerator(
            scan_dirs=[scan], inbox_path=inbox_path, vault_path=tmp_path,
            failed_audio_dir=failed_dir,
        )
        asyncio.run(gen.process_all())
        return inbox_path.read_text()

    def test_parked_recording_appears_with_its_reason(self, tmp_path, failed_dir):
        park(failed_dir, "2026-08-26-Todo 11-04-03 #todo.m4a")
        text = self._inbox(tmp_path, failed_dir)
        assert "Could Not Be Transcribed" in text
        assert "2026-08-26-Todo 11-04-03 #todo.m4a" in text
        assert "no spoken audio" in text

    def test_says_what_to_do_about_it(self, tmp_path, failed_dir):
        park(failed_dir, "memo.m4a")
        text = self._inbox(tmp_path, failed_dir)
        assert "Incoming" in text and "delete" in text

    def test_not_rendered_as_a_wikilink(self, tmp_path, failed_dir):
        """The audio lives outside the vault, so a wikilink would be broken."""
        park(failed_dir, "memo.m4a")
        text = self._inbox(tmp_path, failed_dir)
        assert "[[memo" not in text
        assert "`memo.m4a`" in text

    def test_no_section_when_nothing_is_parked(self, tmp_path, failed_dir):
        text = self._inbox(tmp_path, failed_dir)
        assert "Could Not Be Transcribed" not in text


class TestAllClearHonesty:
    """"All clear" must not appear above unresolved problems."""

    def _inbox(self, tmp_path, failed_dir):
        scan = tmp_path / "Transcriptions"
        scan.mkdir(exist_ok=True)
        inbox_path = tmp_path / "Inbox.md"
        gen = InboxGenerator(
            scan_dirs=[scan], inbox_path=inbox_path, vault_path=tmp_path,
            failed_audio_dir=failed_dir,
        )
        asyncio.run(gen.process_all())
        return inbox_path.read_text()

    def test_not_all_clear_while_a_recording_is_parked(self, tmp_path, failed_dir):
        park(failed_dir, "memo.m4a")
        text = self._inbox(tmp_path, failed_dir)
        assert "All clear" not in text
        assert "see above" in text

    def test_not_all_clear_while_a_note_has_an_error(self, tmp_path, failed_dir):
        note = tmp_path / "Transcriptions" / "broken.md"
        note.parent.mkdir(exist_ok=True)
        note.write_text("---\ncategory: todo\n---\nbody\n")
        error_registry.record_error(note, "classified", "boom")
        try:
            text = self._inbox(tmp_path, failed_dir)
            assert "All clear" not in text
        finally:
            error_registry.clear_all()

    def test_all_clear_when_genuinely_clear(self, tmp_path, failed_dir):
        error_registry.clear_all()
        text = self._inbox(tmp_path, failed_dir)
        assert "All clear" in text
