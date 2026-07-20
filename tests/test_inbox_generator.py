"""
E2E tests for InboxGenerator processor.
"""

import pytest
from pathlib import Path

from processors.notes.inbox_generator import InboxGenerator
from processors.notes.base import NoteProcessor
from processors.common import error_registry


@pytest.fixture(autouse=True)
def clean_error_registry():
    """Keep the shared error registry isolated between tests."""
    error_registry.clear_all()
    yield
    error_registry.clear_all()


class TestInboxGenerator:
    """E2E tests for InboxGenerator processor."""
    
    def test_generates_empty_inbox(self, test_vault, transcriptions_dir):
        """Should generate empty inbox when no pending forms."""
        inbox_path = test_vault / "NoteFlow Inbox.md"
        
        generator = InboxGenerator(
            scan_dir=transcriptions_dir,
            inbox_path=inbox_path,
            vault_path=test_vault
        )
        generator.generate()
        
        content = inbox_path.read_text(encoding='utf-8')
        assert "# NoteFlow Inbox" in content
        assert "All clear!" in content
    
    def test_detects_pending_speaker_form(self, test_vault, transcriptions_dir):
        """Should detect files with speaker_validation_pending."""
        inbox_path = test_vault / "NoteFlow Inbox.md"
        
        # Create a file with pending speaker validation
        test_file = transcriptions_dir / "test_meeting.md"
        test_file.write_text("""---
speaker_validation_pending: true
date: '2025-12-27'
---
Some content
""", encoding='utf-8')
        
        generator = InboxGenerator(
            scan_dir=transcriptions_dir,
            inbox_path=inbox_path,
            vault_path=test_vault
        )
        generator.generate()
        
        content = inbox_path.read_text(encoding='utf-8')
        assert "[[KnowledgeBot/Transcriptions/test_meeting]]" in content
        assert "Speaker ID" in content
        assert "1 file" in content
    
    def test_detects_error_status(self, test_vault, transcriptions_dir):
        """Should show error status when form has validation errors."""
        inbox_path = test_vault / "NoteFlow Inbox.md"
        
        # Create a file with pending form AND error callout
        test_file = transcriptions_dir / "test_with_error.md"
        test_file.write_text("""---
speaker_validation_pending: true
date: '2025-12-27'
---
<!-- form:speaker_identification:start -->

> [!error] Validation errors
> - Speaker A must be a wikilink

<!-- form:speaker_identification:end -->
""", encoding='utf-8')
        
        generator = InboxGenerator(
            scan_dir=transcriptions_dir,
            inbox_path=inbox_path,
            vault_path=test_vault
        )
        generator.generate()
        
        content = inbox_path.read_text(encoding='utf-8')
        assert "⚠️ Errors" in content


class TestInboxProcessingErrors:
    """Tests for the Processing Errors section of the inbox."""

    def _generate(self, test_vault, transcriptions_dir):
        inbox_path = test_vault / "NoteFlow Inbox.md"
        generator = InboxGenerator(
            scan_dir=transcriptions_dir,
            inbox_path=inbox_path,
            vault_path=test_vault
        )
        generator.generate()
        return inbox_path.read_text(encoding='utf-8')

    def test_detects_broken_frontmatter(self, test_vault, transcriptions_dir):
        """A pipeline file whose frontmatter no longer parses should be flagged."""
        test_file = transcriptions_dir / "broken_meeting.md"
        # Leading tab before '---' makes the frontmatter unparseable
        test_file.write_text("""\t---
category: meeting
processing_stages:
- transcribed
entity_resolution_pending: true
---
Some content
""", encoding='utf-8')

        content = self._generate(test_vault, transcriptions_dir)
        assert "Processing Errors" in content
        assert "[[KnowledgeBot/Transcriptions/broken_meeting]]" in content
        assert "Frontmatter could not be parsed" in content

    def test_ignores_plain_files_without_frontmatter(self, test_vault, transcriptions_dir):
        """Files that were never in the pipeline should not be flagged."""
        test_file = transcriptions_dir / "random_note.md"
        test_file.write_text("Just a note without frontmatter\n", encoding='utf-8')

        content = self._generate(test_vault, transcriptions_dir)
        assert "Processing Errors" not in content
        assert "All clear!" in content

    def test_shows_recorded_processor_errors(self, test_vault, transcriptions_dir):
        """Errors recorded by processors should appear in the inbox."""
        test_file = transcriptions_dir / "failing_meeting.md"
        test_file.write_text("""---
category: meeting
date: '2026-04-01'
---
Content
""", encoding='utf-8')
        error_registry.record_error(test_file, "entities_resolved", "ValueError: boom | with pipe\nand newline")

        content = self._generate(test_vault, transcriptions_dir)
        assert "Processing Errors" in content
        assert "[[KnowledgeBot/Transcriptions/failing_meeting]]" in content
        assert "entities_resolved" in content
        # Message is sanitized for the markdown table
        assert "ValueError: boom \\| with pipe and newline" in content

    def test_hides_errors_for_deleted_files(self, test_vault, transcriptions_dir):
        """Errors for files that no longer exist should not appear."""
        error_registry.record_error(
            transcriptions_dir / "gone.md", "entities_resolved", "boom"
        )

        content = self._generate(test_vault, transcriptions_dir)
        assert "Processing Errors" not in content


class FailingProcessor(NoteProcessor):
    """Minimal processor that always fails, for error-registry tests."""
    stage_name = "failing_stage"

    def should_process(self, filename, frontmatter):
        return True

    async def process_file(self, filename):
        raise ValueError("simulated failure")


class WaitingProcessor(NoteProcessor):
    """Processor that raises the waiting-for-user error."""
    stage_name = "waiting_stage"

    def should_process(self, filename, frontmatter):
        return True

    async def process_file(self, filename):
        from processors.notes.entity_resolver import ResultsNotReadyError
        raise ResultsNotReadyError("waiting for user")


class TestBaseErrorRecording:
    """Tests that NoteProcessor records and clears errors in the registry."""

    async def test_processing_error_is_recorded(self, mock_ai, transcriptions_dir):
        test_file = transcriptions_dir / "meeting.md"
        test_file.write_text("""---
category: meeting
---
Content
""", encoding='utf-8')

        processor = FailingProcessor(input_dir=transcriptions_dir)
        await processor.process_all()

        errors = error_registry.get_errors()
        assert len(errors) == 1
        assert errors[0]['stage'] == "failing_stage"
        assert "simulated failure" in errors[0]['message']

    async def test_results_not_ready_is_not_recorded(self, mock_ai, transcriptions_dir):
        test_file = transcriptions_dir / "meeting.md"
        test_file.write_text("""---
category: meeting
---
Content
""", encoding='utf-8')

        processor = WaitingProcessor(input_dir=transcriptions_dir)
        await processor.process_all()

        assert error_registry.get_errors() == []

    async def test_error_cleared_when_file_completes_stage(self, mock_ai, transcriptions_dir):
        test_file = transcriptions_dir / "meeting.md"
        test_file.write_text("""---
category: meeting
---
Content
""", encoding='utf-8')

        processor = FailingProcessor(input_dir=transcriptions_dir)
        await processor.process_all()
        assert len(error_registry.get_errors()) == 1

        # File completes the stage — next scan should clear the stale error
        test_file.write_text("""---
category: meeting
processing_stages:
- failing_stage
---
Content
""", encoding='utf-8')
        await processor.process_all()
        assert error_registry.get_errors() == []
