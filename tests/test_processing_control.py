"""
Tests for the per-note stop switches: `processing_stopped` and `skip_stages`.
"""

import pytest

from processors.common import error_registry
from processors.common.processing_control import (
    is_processing_stopped,
    is_stage_skipped,
)
from processors.notes.base import NoteProcessor
from processors.notes.inbox_generator import InboxGenerator


@pytest.fixture(autouse=True)
def clean_error_registry():
    """Keep the shared error registry isolated between tests."""
    error_registry.clear_all()
    yield
    error_registry.clear_all()


class RecordingProcessor(NoteProcessor):
    """Processor that records which files it was allowed to touch."""
    stage_name = "recording_stage"

    def __init__(self, input_dir):
        super().__init__(input_dir=input_dir)
        self.processed = []

    def should_process(self, filename, frontmatter):
        return True

    async def process_file(self, filename):
        self.processed.append(filename)


class InteractionLoggerLike(RecordingProcessor):
    """Stands in for the interaction logger, to test skipping one named stage."""
    stage_name = "interactions_logged"


class TestFlagParsing:
    """The helpers, away from the filesystem."""

    def test_missing_frontmatter_is_not_stopped(self):
        assert is_processing_stopped(None) is False
        assert is_processing_stopped({}) is False

    def test_stop_flag(self):
        assert is_processing_stopped({'processing_stopped': True}) is True
        assert is_processing_stopped({'processing_stopped': False}) is False

    def test_legacy_abandoned_flag_still_honoured(self):
        assert is_processing_stopped({'abandoned': True}) is True

    def test_skip_stages_accepts_a_list(self):
        fm = {'skip_stages': ['interactions_logged', 'todoist_synced']}
        assert is_stage_skipped(fm, 'interactions_logged') is True
        assert is_stage_skipped(fm, 'meeting_summarized') is False

    def test_skip_stages_accepts_a_bare_string(self):
        # `skip_stages: interactions_logged` is what a hand-typed line parses to
        fm = {'skip_stages': 'interactions_logged'}
        assert is_stage_skipped(fm, 'interactions_logged') is True

    def test_skip_stages_ignores_case_and_padding(self):
        fm = {'skip_stages': ['  Interactions_Logged ']}
        assert is_stage_skipped(fm, 'interactions_logged') is True

    def test_empty_skip_stages_skips_nothing(self):
        assert is_stage_skipped({'skip_stages': []}, 'interactions_logged') is False
        assert is_stage_skipped({}, 'interactions_logged') is False


class TestProcessorsHonourTheFlags:
    """A stopped note must not reach any stage."""

    def _write(self, transcriptions_dir, name, frontmatter_lines):
        path = transcriptions_dir / name
        path.write_text(
            "---\ncategory: meeting\n" + "".join(frontmatter_lines) + "---\nContent\n",
            encoding='utf-8',
        )
        return path

    async def test_normal_note_is_processed(self, mock_ai, transcriptions_dir):
        self._write(transcriptions_dir, "live.md", [])

        processor = RecordingProcessor(input_dir=transcriptions_dir)
        await processor.process_all()

        assert processor.processed == ["live.md"]

    async def test_stopped_note_is_skipped(self, mock_ai, transcriptions_dir):
        self._write(transcriptions_dir, "stopped.md", ["processing_stopped: true\n"])

        processor = RecordingProcessor(input_dir=transcriptions_dir)
        await processor.process_all()

        assert processor.processed == []

    async def test_legacy_abandoned_note_is_skipped(self, mock_ai, transcriptions_dir):
        self._write(transcriptions_dir, "old.md", ["abandoned: true\n"])

        processor = RecordingProcessor(input_dir=transcriptions_dir)
        await processor.process_all()

        assert processor.processed == []

    async def test_stopped_note_keeps_its_frontmatter_untouched(self, mock_ai, transcriptions_dir):
        path = self._write(
            transcriptions_dir,
            "stopped.md",
            ["processing_stopped: true\n", "meeting_summary_pending: true\n"],
        )
        before = path.read_text(encoding='utf-8')

        processor = RecordingProcessor(input_dir=transcriptions_dir)
        await processor.process_all()

        # Nothing is rewritten, so clearing the flag resumes where it left off
        assert path.read_text(encoding='utf-8') == before

    async def test_resumes_once_the_flag_is_removed(self, mock_ai, transcriptions_dir):
        path = self._write(transcriptions_dir, "paused.md", ["processing_stopped: true\n"])

        processor = RecordingProcessor(input_dir=transcriptions_dir)
        await processor.process_all()
        assert processor.processed == []

        path.write_text("---\ncategory: meeting\n---\nContent\n", encoding='utf-8')
        await processor.process_all()
        assert processor.processed == ["paused.md"]

    async def test_skip_stages_blocks_only_the_named_stage(self, mock_ai, transcriptions_dir):
        self._write(
            transcriptions_dir,
            "no_logging.md",
            ["skip_stages:\n", "  - interactions_logged\n"],
        )

        logger_stage = InteractionLoggerLike(input_dir=transcriptions_dir)
        other_stage = RecordingProcessor(input_dir=transcriptions_dir)
        await logger_stage.process_all()
        await other_stage.process_all()

        assert logger_stage.processed == []
        assert other_stage.processed == ["no_logging.md"]


class TestInboxHidesStoppedNotes:
    """The point of stopping a note is that it leaves the inbox."""

    def _generate(self, test_vault, transcriptions_dir):
        inbox_path = test_vault / "NoteFlow Inbox.md"
        generator = InboxGenerator(
            scan_dir=transcriptions_dir,
            inbox_path=inbox_path,
            vault_path=test_vault,
        )
        generator.generate()
        return inbox_path.read_text(encoding='utf-8')

    def test_stopped_note_with_a_pending_form_is_hidden(self, test_vault, transcriptions_dir):
        (transcriptions_dir / "stuck_meeting.md").write_text("""---
category: meeting
date: '2026-08-28'
meeting_summary_pending: true
processing_stopped: true
---
<!-- form:meeting_summary:start -->
- [ ] Finished <!-- input:finished -->
<!-- form:meeting_summary:end -->
""", encoding='utf-8')

        content = self._generate(test_vault, transcriptions_dir)
        assert "stuck_meeting" not in content
        assert "All clear!" in content

    def test_pending_note_without_the_flag_still_shows(self, test_vault, transcriptions_dir):
        (transcriptions_dir / "live_meeting.md").write_text("""---
category: meeting
date: '2026-08-28'
meeting_summary_pending: true
---
Content
""", encoding='utf-8')

        content = self._generate(test_vault, transcriptions_dir)
        assert "[[KnowledgeBot/Transcriptions/live_meeting]]" in content
        assert "Meeting Summary" in content

    def test_recorded_errors_for_stopped_notes_are_hidden(self, test_vault, transcriptions_dir):
        stopped = transcriptions_dir / "stopped_meeting.md"
        stopped.write_text("""---
category: meeting
processing_stopped: true
---
Content
""", encoding='utf-8')
        error_registry.record_error(stopped, "entities_resolved", "boom")

        content = self._generate(test_vault, transcriptions_dir)
        assert "Processing Errors" not in content
        assert "stopped_meeting" not in content

    def test_stopped_note_never_becomes_a_new_notification(self, test_vault, transcriptions_dir):
        (transcriptions_dir / "stuck_meeting.md").write_text("""---
category: meeting
meeting_summary_pending: true
processing_stopped: true
---
Content
""", encoding='utf-8')

        generator = InboxGenerator(
            scan_dir=transcriptions_dir,
            inbox_path=test_vault / "NoteFlow Inbox.md",
            vault_path=test_vault,
        )
        items, _ = generator._scan_all()

        assert generator._find_new_items(items) == []

    def test_inbox_explains_how_to_stop_a_note(self, test_vault, transcriptions_dir):
        (transcriptions_dir / "live_meeting.md").write_text("""---
category: meeting
meeting_summary_pending: true
---
Content
""", encoding='utf-8')

        content = self._generate(test_vault, transcriptions_dir)
        assert "processing_stopped: true" in content
