"""
E2E tests for InboxGenerator processor.
"""

import pytest
from pathlib import Path

from processors.notes.inbox_generator import InboxGenerator


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
