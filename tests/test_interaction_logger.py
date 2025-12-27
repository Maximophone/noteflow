"""
E2E tests for InteractionLogger processor.
"""

import pytest
from pathlib import Path
from unittest.mock import patch

from processors.notes.interaction_logger import InteractionLogger


@pytest.fixture
def mock_interaction_logger(mock_ai, tmp_path):
    """Create an InteractionLogger with mocked dependencies."""
    input_dir = tmp_path / "transcriptions"
    people_dir = tmp_path / "people"
    input_dir.mkdir(parents=True)
    people_dir.mkdir(parents=True)
    
    with patch("processors.notes.interaction_logger.PATHS") as mock_paths:
        mock_paths.people_path = people_dir
        processor = InteractionLogger(input_dir=input_dir)
        processor.people_dir = people_dir  # Ensure it's using our mock dir
        yield processor


class TestShouldProcess:
    """Tests for should_process logic."""
    
    def test_processes_meeting_with_speakers(self, mock_interaction_logger):
        """Should process meetings that have final_speaker_mapping."""
        frontmatter = {
            "category": "meeting",
            "final_speaker_mapping": {
                "Speaker A": {"person_id": "[[John Smith]]"}
            }
        }
        result = mock_interaction_logger.should_process("test.md", frontmatter)
        assert result is True
    
    def test_skips_non_meeting_category(self, mock_interaction_logger):
        """Should skip non-meeting files."""
        frontmatter = {
            "category": "idea",
            "final_speaker_mapping": {"Speaker A": {}}
        }
        result = mock_interaction_logger.should_process("test.md", frontmatter)
        assert result is False
    
    def test_skips_without_speaker_mapping(self, mock_interaction_logger):
        """Should skip files without final_speaker_mapping."""
        frontmatter = {"category": "meeting"}
        result = mock_interaction_logger.should_process("test.md", frontmatter)
        assert result is False


class TestFindAILogsSection:
    """Tests for finding AI Logs section in notes."""
    
    @pytest.mark.asyncio
    async def test_finds_existing_section(self, mock_interaction_logger):
        """Should locate existing AI Logs section."""
        content = """---
name: John Smith
---
Some content about John.

# AI Logs

## 2025-12-27
Some log entry
"""
        exists, position, before = await mock_interaction_logger._find_ai_logs_section(content)
        
        assert exists is True
        assert "# AI Logs" in content[position:]
        assert "# AI Logs" not in before
    
    @pytest.mark.asyncio
    async def test_handles_missing_section(self, mock_interaction_logger):
        """Should handle notes without AI Logs section."""
        content = """---
name: John Smith
---
Just some content, no logs.
"""
        exists, position, before = await mock_interaction_logger._find_ai_logs_section(content)
        
        assert exists is False
        assert position == len(content)


class TestParseExistingLogs:
    """Tests for parsing existing log entries."""
    
    @pytest.mark.asyncio
    async def test_parses_logs_by_date(self, mock_interaction_logger):
        """Should parse logs into date-grouped structure."""
        content = """---
name: John Smith
---
# AI Logs

## 2025-12-27
*category*: meeting
*source:* [[2025-12-27-meeting]]
*notes*: 
- Discussed project timeline
- Assigned tasks

## 2025-12-26
*category*: meeting
*source:* [[2025-12-26-standup]]
*notes*: 
- Quick standup
"""
        logs = await mock_interaction_logger._parse_existing_logs(content)
        
        assert "2025-12-27" in logs
        assert "2025-12-26" in logs
        assert len(logs["2025-12-27"]) == 1
        assert logs["2025-12-27"][0]["category"] == "meeting"
        assert logs["2025-12-27"][0]["source"] == "[[2025-12-27-meeting]]"
    
    @pytest.mark.asyncio
    async def test_handles_empty_logs(self, mock_interaction_logger):
        """Should return empty dict when no logs exist."""
        content = """---
name: John Smith
---
No AI Logs section here.
"""
        logs = await mock_interaction_logger._parse_existing_logs(content)
        assert logs == {}


class TestUpdatePersonNote:
    """Tests for updating person notes with new logs."""
    
    @pytest.mark.asyncio
    async def test_adds_log_to_person_note(self, mock_interaction_logger):
        """Should add a new log entry to person's note."""
        # Create person file
        person_file = mock_interaction_logger.people_dir / "John Smith.md"
        person_file.write_text("""---
name: John Smith
---
Some info about John.
""")
        
        success = await mock_interaction_logger._update_person_note(
            person_id="[[John Smith]]",
            meeting_date="2025-12-27",
            source_link="[[2025-12-27-meeting]]",
            log_content="- Discussed project timeline",
            category="meeting"
        )
        
        assert success is True
        
        updated_content = person_file.read_text()
        assert "# AI Logs" in updated_content
        assert "2025-12-27" in updated_content
        assert "[[2025-12-27-meeting]]" in updated_content
        assert "Discussed project timeline" in updated_content
    
    @pytest.mark.asyncio
    async def test_handles_missing_person_file(self, mock_interaction_logger):
        """Should return False when person file doesn't exist."""
        success = await mock_interaction_logger._update_person_note(
            person_id="[[Unknown Person]]",
            meeting_date="2025-12-27",
            source_link="[[2025-12-27-meeting]]",
            log_content="Some log",
            category="meeting"
        )
        
        assert success is False


class TestReset:
    """Tests for reset/revert functionality."""
    
    @pytest.mark.asyncio
    async def test_reset_removes_logs_and_cleans_frontmatter(self, mock_interaction_logger):
        """Reset should remove log entries from person notes and clean transcript frontmatter."""
        # Create person file with AI logs
        person_file = mock_interaction_logger.people_dir / "John Smith.md"
        person_file.write_text("""---
name: John Smith
---
Some info about John.

# AI Logs
>[!warning] Do not Modify

## 2025-12-27
*category*: meeting
*source:* [[2025-12-27-meeting]]
*notes*: 
- Discussed project timeline

## 2025-12-26
*category*: meeting
*source:* [[2025-12-26-standup]]
*notes*: 
- Standup notes
""")
        
        # Create transcript file with logged_interactions
        input_file = mock_interaction_logger.input_dir / "2025-12-27-meeting.md"
        input_file.write_text("""---
date: '2025-12-27'
category: meeting
processing_stages:
  - transcribed
  - classified
  - speakers_identified
  - entities_resolved
  - interactions_logged
final_speaker_mapping:
  Speaker A:
    person_id: "[[John Smith]]"
logged_interactions:
  - "[[John Smith]]"
---
The transcript content.
""")
        
        await mock_interaction_logger.reset("2025-12-27-meeting.md")
        
        # Check transcript frontmatter was cleaned
        result = input_file.read_text()
        assert "logged_interactions" not in result
        assert "interactions_logged" not in result
        assert "The transcript content" in result
        
        # Check log was removed from person note
        person_content = person_file.read_text()
        assert "[[2025-12-27-meeting]]" not in person_content
        # But 2025-12-26 log should remain
        assert "[[2025-12-26-standup]]" in person_content

