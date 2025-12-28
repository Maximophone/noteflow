"""
Tests for MeetingSummaryGenerator functionality.
"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock, Mock

from processors.notes.meeting_summary_generator import MeetingSummaryGenerator


@pytest.fixture
def mock_generator(mock_ai, tmp_path):
    """Create a MeetingSummaryGenerator with mocked dependencies."""
    mock_discord = MagicMock()
    mock_discord.send_dm = AsyncMock(return_value=True)
    input_dir = tmp_path / "transcriptions"
    input_dir.mkdir(parents=True)
    
    with patch("processors.notes.meeting_summary_generator.PATHS") as mock_paths:
        mock_paths.meetings = tmp_path / "meetings"
        mock_paths.meetings.mkdir(parents=True)
        mock_paths.people_path = tmp_path / "people"
        mock_paths.people_path.mkdir(parents=True)
        generator = MeetingSummaryGenerator(input_dir, mock_discord)
        yield generator


class TestMonthlyIndexHelpers:
    """Tests for monthly index path and loading."""
    
    def test_get_monthly_index_path(self, mock_generator, tmp_path):
        """Should generate correct path from date."""
        with patch("processors.notes.meeting_summary_generator.PATHS") as mock_paths:
            mock_paths.meetings = tmp_path / "meetings"
            
            path = mock_generator._get_monthly_index_path("2025-12-27")
            assert path.name == "2025-12 Meetings.md"
    
    def test_get_previous_month(self, mock_generator):
        """Should correctly compute previous month."""
        assert mock_generator._get_previous_month("2025-12-27") == "2025-11"
        assert mock_generator._get_previous_month("2025-01-15") == "2024-12"
        assert mock_generator._get_previous_month("2025-03-01") == "2025-02"
    
    def test_load_monthly_index_with_fallback(self, mock_generator, tmp_path):
        """Should fall back to previous month when current is sparse."""
        with patch("processors.notes.meeting_summary_generator.PATHS") as mock_paths:
            mock_paths.meetings = tmp_path / "meetings"
            mock_paths.meetings.mkdir(parents=True, exist_ok=True)
            
            # Create sparse current month (less than MIN_INDEX_LINES)
            current_file = mock_paths.meetings / "2025-12 Meetings.md"
            current_file.write_text("# 2025-12 Meetings\n\nSmall content here.")
            
            # Create previous month with more content
            prev_file = mock_paths.meetings / "2025-11 Meetings.md"
            prev_content = "# 2025-11 Meetings\n\n" + "\n".join([f"Line {i}" for i in range(200)])
            prev_file.write_text(prev_content)
            
            result = mock_generator._load_monthly_index("2025-12-27")
            
            # Should include content from both months
            assert "Current Month" in result
            assert "2025-12 Meetings" in result or "Small content" in result


class TestAttendeeContext:
    """Tests for loading attendee context."""
    
    def test_load_attendee_context_with_truncation(self, mock_generator, tmp_path):
        """Should truncate long People notes."""
        with patch("processors.notes.meeting_summary_generator.PATHS") as mock_paths:
            mock_paths.people_path = tmp_path / "people"
            mock_paths.people_path.mkdir(parents=True, exist_ok=True)
            mock_generator.people_dir = mock_paths.people_path
            
            # Create a long People note
            person_file = mock_paths.people_path / "John Smith.md"
            long_content = "\n".join([f"Line {i}" for i in range(200)])
            person_file.write_text(long_content)
            
            speaker_mapping = {
                "SPEAKER_00": {"person_id": "[[John Smith]]"}
            }
            
            result = mock_generator._load_attendee_context(speaker_mapping)
            
            assert "John Smith" in result
            assert "[truncated]" in result


class TestFormGenerationAndParsing:
    """Tests for form generation and parsing."""
    
    def test_generate_form_creates_correct_markup(self, mock_generator):
        """Should generate form with correct markers and content."""
        summary = "## Summary\n\nThis is a test meeting summary."
        
        form = mock_generator._generate_form(summary)
        
        assert mock_generator.FORM_START in form
        assert mock_generator.FORM_END in form
        assert "[!info] Meeting Summary" in form
        assert "This is a test meeting summary" in form
        assert "- [ ] Finished" in form
    
    def test_parse_form_extracts_data(self, mock_generator):
        """Should correctly parse form content."""
        content = """<!-- form:meeting_summary:start -->

> [!info] Meeting Summary — Review and edit as needed

## Summary

This is the summary content.

## Decisions Made

- Decision 1
- Decision 2

---

- [x] Finished <!-- input:finished -->

<!-- form:meeting_summary:end -->

The transcript content here."""

        result = mock_generator._parse_form(content)
        
        assert result["finished"] is True
        assert "Summary" in result["summary"]
        assert "Decision 1" in result["summary"]
    
    def test_parse_form_handles_unchecked_finished(self, mock_generator):
        """Should detect unchecked finished checkbox."""
        content = """<!-- form:meeting_summary:start -->

> [!info] Meeting Summary

Summary here.

---

- [ ] Finished <!-- input:finished -->

<!-- form:meeting_summary:end -->"""

        result = mock_generator._parse_form(content)
        assert result["finished"] is False


class TestFormRemoval:
    """Tests for removing form sections."""
    
    def test_removes_form_section(self, mock_generator):
        """Should remove the form section."""
        content = """Before content
<!-- form:meeting_summary:start -->
Form content
<!-- form:meeting_summary:end -->
After content"""
        
        result = mock_generator._remove_form_section(content)
        assert "Before content" in result
        assert "After content" in result
        assert "Form content" not in result
    
    def test_removes_summary_section(self, mock_generator):
        """Should remove the summary section."""
        content = """Before
<!-- summary:meeting_summary:start -->
Summary content
<!-- summary:meeting_summary:end -->
After"""
        
        result = mock_generator._remove_form_section(content)
        assert result.strip() == "Before\nAfter"


class TestReset:
    """Tests for reset/revert functionality."""
    
    @pytest.mark.asyncio
    async def test_reset_removes_form_and_cleans_frontmatter(self, mock_generator):
        """Reset should remove form section and clean up frontmatter."""
        input_file = mock_generator.input_dir / "test_meeting.md"
        input_file.write_text("""---
date: '2025-12-27'
category: meeting
processing_stages:
  - transcribed
  - classified
  - speakers_identified
  - entities_resolved
  - meeting_summarized
meeting_summary_pending: true
---
<!-- form:meeting_summary:start -->
Form content here
<!-- form:meeting_summary:end -->

The transcript content here.
""")
        
        await mock_generator.reset("test_meeting.md")
        
        result = input_file.read_text()
        
        # Form should be removed
        assert "<!-- form:meeting_summary:start -->" not in result
        assert "Form content here" not in result
        
        # Transcript should be preserved
        assert "The transcript content here" in result
        
        # Fields should be removed from frontmatter
        assert "meeting_summary_pending" not in result
        assert "meeting_summarized" not in result


class TestMonthlyIndexUpdate:
    """Tests for updating monthly index."""
    
    def test_creates_file_if_needed(self, mock_generator, tmp_path):
        """Should create monthly index if it doesn't exist."""
        with patch("processors.notes.meeting_summary_generator.PATHS") as mock_paths:
            mock_paths.meetings = tmp_path / "meetings"
            mock_paths.meetings.mkdir(parents=True, exist_ok=True)
            
            mock_generator._update_monthly_index(
                summary="Test summary content",
                meeting_date="2025-12-27",
                meeting_title="Test Meeting",
                source_link="[[2025-12-27 Test Meeting]]"
            )
            
            index_file = mock_paths.meetings / "2025-12 Meetings.md"
            assert index_file.exists()
            
            content = index_file.read_text()
            assert "# 2025-12-27 - Test Meeting" in content
            assert "# 2025-12 Meetings" not in content  # Should not have file title
            assert "Test summary content" in content
            assert "[[2025-12-27 Test Meeting]]" in content
    
    def test_maintains_chronological_order(self, mock_generator, tmp_path):
        """Should maintain entries in reverse chronological order (newest first)."""
        with patch("processors.notes.meeting_summary_generator.PATHS") as mock_paths:
            mock_paths.meetings = tmp_path / "meetings"
            mock_paths.meetings.mkdir(parents=True, exist_ok=True)
            
            # Add entries out of order
            mock_generator._update_monthly_index("Summary 1", "2025-12-25", "Meeting 1", "[[Meeting 1]]")
            mock_generator._update_monthly_index("Summary 2", "2025-12-27", "Meeting 2", "[[Meeting 2]]")
            mock_generator._update_monthly_index("Summary 3", "2025-12-26", "Meeting 3", "[[Meeting 3]]")
            
            content = (mock_paths.meetings / "2025-12 Meetings.md").read_text()
            
            # Find positions - should be 27, 26, 25 (newest first)
            pos_27 = content.find("2025-12-27")
            pos_26 = content.find("2025-12-26")
            pos_25 = content.find("2025-12-25")
            
            assert pos_27 < pos_26 < pos_25
    
    def test_overwrites_existing_entry(self, mock_generator, tmp_path):
        """Should overwrite entry with same source_link."""
        with patch("processors.notes.meeting_summary_generator.PATHS") as mock_paths:
            mock_paths.meetings = tmp_path / "meetings"
            mock_paths.meetings.mkdir(parents=True, exist_ok=True)
            
            # Add initial entry
            mock_generator._update_monthly_index("Original summary", "2025-12-27", "Meeting", "[[Meeting]]")
            
            # Update same entry
            mock_generator._update_monthly_index("Updated summary", "2025-12-27", "Meeting", "[[Meeting]]")
            
            content = (mock_paths.meetings / "2025-12 Meetings.md").read_text()
            
            # Should have updated content, not original
            assert "Updated summary" in content
            assert "Original summary" not in content
            # Should only have one entry
            assert content.count("# 2025-12-27") == 1

    async def test_deduplicates_entities(self, mock_generator):
        """Should deduplicate entities and exclude attendees from entities list."""
        # Setup mocks
        mock_generator._parse_form = Mock(return_value={
            'finished': True,
            'summary': "Summary"
        })
        mock_generator._update_monthly_index = Mock()
        mock_generator._remove_form_section = Mock(return_value="Content")
        mock_generator._generate_summary_callout = Mock(return_value="Callout")
        
        # input data with duplicates
        frontmatter = {
            'date': '2025-12-27',
            'title': 'Test Meeting',
            'meeting_summary_pending': True,
            'final_speaker_mapping': {
                '0': {'person_id': '[[Alice]]'},
                '1': {'person_id': '[[Bob]]'},
                '2': {'person_id': '[[Alice]]'}  # Duplicate attendee
            },
            'resolved_entities': [
                {'resolved_link': '[[Charlie]]'},
                {'resolved_link': '[[Alice]]'},   # Overlap with attendee
                {'resolved_link': '[[Charlie]]'}, # Duplicate entity
                {'resolved_link': '[[Dave]]'}
            ]
        }
        
        # Run substage 3
        mock_file_ctx = MagicMock()
        mock_file_handle = AsyncMock()
        mock_file_ctx.__aenter__.return_value = mock_file_handle
        mock_file_ctx.__aexit__.return_value = None
        
        with patch("processors.notes.meeting_summary_generator.read_text_from_content", return_value=""), \
             patch("processors.notes.meeting_summary_generator.frontmatter_to_text", return_value=""), \
             patch("processors.notes.meeting_summary_generator.aiofiles.open", return_value=mock_file_ctx), \
             patch("processors.notes.meeting_summary_generator.os.utime"):
            
            await mock_generator._substage3_process_results("test.md", frontmatter, "content")
            
            # Verify _update_monthly_index call arguments
            args, kwargs = mock_generator._update_monthly_index.call_args
            
            # Check attendees (should be unique and sorted)
            assert kwargs['attendees'] == ['[[Alice]]', '[[Bob]]']
            
            # Check entities (should be unique, sorted, and exclude Alice who is an attendee)
            assert kwargs['entities'] == ['[[Charlie]]', '[[Dave]]']
