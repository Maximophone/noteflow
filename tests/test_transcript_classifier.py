"""
E2E tests for TranscriptClassifier processor.
"""

import pytest
from pathlib import Path

from processors.notes.transcript_classifier import TranscriptClassifier
from tests.conftest import (
    copy_fixture, 
    load_expected, 
    assert_files_match,
)


class TestTranscriptClassifier:
    """E2E tests for TranscriptClassifier processor."""
    
    @pytest.mark.asyncio
    async def test_classifies_meeting_transcript(self, transcriptions_dir, mock_ai):
        """Given a transcribed meeting, classifier should add meeting category."""
        # Setup: copy fixture to test directory
        input_file = copy_fixture("transcripts/meeting_transcribed.md", transcriptions_dir)
        
        # Configure mock AI to return "meeting"
        mock_ai.add_response("project timeline", "meeting")
        
        # Create processor and run (use _process_file to include stage tracking)
        processor = TranscriptClassifier(input_dir=transcriptions_dir)
        await processor._process_file(input_file.name)
        
        # Assert output matches expected
        actual = input_file.read_text(encoding='utf-8')
        expected = load_expected("transcripts/meeting_classified.md")
        
        assert_files_match(actual, expected, ignore_fields=['date'])
    
    @pytest.mark.asyncio
    async def test_classifies_idea_transcript(self, transcriptions_dir, mock_ai):
        """Given a transcribed idea, classifier should add idea category."""
        # Setup
        input_file = copy_fixture("transcripts/idea_transcribed.md", transcriptions_dir)
        
        # Configure mock AI to return "idea" 
        mock_ai.add_response("custom templates", "idea")
        
        # Create processor and run (use _process_file to include stage tracking)
        processor = TranscriptClassifier(input_dir=transcriptions_dir)
        await processor._process_file(input_file.name)
        
        # Assert output matches expected
        actual = input_file.read_text(encoding='utf-8')
        expected = load_expected("transcripts/idea_classified.md")
        
        assert_files_match(actual, expected, ignore_fields=['date'])
    
    @pytest.mark.asyncio
    async def test_forced_category_via_source_tags(self, transcriptions_dir, mock_ai):
        """Source tags should override AI classification."""
        # Setup: create a file with forced category in source_tags
        input_file = copy_fixture("transcripts/meeting_transcribed.md", transcriptions_dir)
        
        # Modify the fixture to add forced category
        content = input_file.read_text(encoding='utf-8')
        content = content.replace("source_tags: []", "source_tags:\n- diary")
        input_file.write_text(content, encoding='utf-8')
        
        # Create processor and run (AI should NOT be called)
        processor = TranscriptClassifier(input_dir=transcriptions_dir)
        await processor.process_file(input_file.name)
        
        # Assert category is "diary" (forced), not "meeting" (AI would have said)
        result = input_file.read_text(encoding='utf-8')
        assert "category: diary" in result
        assert "- diary" in result  # tag should be added
        
        # AI should not have been called
        assert len(mock_ai.call_log) == 0
