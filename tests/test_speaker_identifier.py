"""
E2E tests for SpeakerIdentifier processor.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from processors.notes.speaker_identifier import SpeakerIdentifier


@pytest.fixture
def mock_speaker_identifier(mock_ai, mock_discord, tmp_path):
    """Create a SpeakerIdentifier with mocked dependencies."""
    input_dir = tmp_path / "transcriptions"
    input_dir.mkdir(parents=True)
    identifier = SpeakerIdentifier(input_dir, mock_discord)
    yield identifier


class TestFormGeneration:
    """Tests for validation form generation."""
    
    def test_generates_form_with_speakers(self, mock_speaker_identifier):
        """Should generate form with all speakers."""
        speaker_mapping = {
            "Speaker A": {"name": "John Smith", "reason": "Introduced himself as John"},
            "Speaker B": {"name": "Jane Doe", "reason": "Was addressed as Jane"},
        }
        
        form = mock_speaker_identifier._generate_validation_section(speaker_mapping)
        
        assert mock_speaker_identifier.FORM_START in form
        assert mock_speaker_identifier.FORM_END in form
        assert "## Speaker A" in form
        assert "## Speaker B" in form
        assert "John Smith" in form
        assert "Jane Doe" in form
        assert "<!-- input:speaker_a -->" in form
        assert "<!-- input:speaker_b -->" in form
        assert "- [ ] Finished <!-- input:finished -->" in form


class TestFormParsing:
    """Tests for validation form parsing."""
    
    def test_parses_completed_form(self, mock_speaker_identifier):
        """Should correctly parse a completed validation form."""
        content = """---
date: '2025-12-27'
---
<!-- form:speaker_identification:start -->

> [!info] Data validation section

# Speaker Identification

## Speaker A
**Detected:** John Smith
**Real answer:** <!-- input:speaker_a -->[[John Smith]]

## Speaker B
**Detected:** Jane Doe
**Real answer:** <!-- input:speaker_b -->[[Jane Doe]]

## Additional Notes
<!-- input:notes -->
Some notes here

## Validation
- [ ] Transcript has quality issues <!-- input:quality_issues -->
- [x] Finished <!-- input:finished -->

<!-- form:speaker_identification:end -->

Transcript content here...
"""
        result = mock_speaker_identifier._parse_validation_section(content)
        
        assert result is not None
        assert result["finished"] is True
        assert result["speakers"]["Speaker A"] == "[[John Smith]]"
        assert result["speakers"]["Speaker B"] == "[[Jane Doe]]"
    
    def test_parses_unfinished_form(self, mock_speaker_identifier):
        """Should recognize unfinished forms."""
        content = """<!-- form:speaker_identification:start -->
- [ ] Finished <!-- input:finished -->
<!-- form:speaker_identification:end -->"""
        
        result = mock_speaker_identifier._parse_validation_section(content)
        
        assert result is not None
        assert result["finished"] is False
    
    def test_returns_none_for_missing_form(self, mock_speaker_identifier):
        """Should return None when no form section exists."""
        content = """---
date: '2025-12-27'
---
Just regular transcript content with no form.
"""
        result = mock_speaker_identifier._parse_validation_section(content)
        assert result is None


class TestFormRemoval:
    """Tests for removing validation sections."""
    
    def test_removes_form_section(self, mock_speaker_identifier):
        """Should remove the form section while preserving surrounding content."""
        content = """---
date: '2025-12-27'
---
<!-- form:speaker_identification:start -->
Form content here
<!-- form:speaker_identification:end -->

Transcript content here
"""
        result = mock_speaker_identifier._remove_validation_section(content)
        
        assert "<!-- form:speaker_identification:start -->" not in result
        assert "Form content here" not in result
        assert "Transcript content here" in result


class TestSpeakerExtraction:
    """Tests for extracting speakers from transcript."""
    
    def test_extracts_unique_speakers(self, mock_speaker_identifier):
        """Should extract all unique speaker labels."""
        transcript = """Speaker A: Hello everyone.
Speaker B: Hi there!
Speaker A: How are you?
Speaker C: I'm good, thanks.
Speaker A: Great to hear."""
        
        speakers = mock_speaker_identifier._extract_unique_speakers(transcript)
        
        assert len(speakers) == 3
        assert "Speaker A" in speakers
        assert "Speaker B" in speakers
        assert "Speaker C" in speakers


class TestWikilinkExtraction:
    """Tests for extracting person info from wikilinks."""
    
    def test_extracts_simple_wikilink(self, mock_speaker_identifier):
        """Should extract name from simple wikilink."""
        person_id, display = mock_speaker_identifier._extract_person_from_wikilink("[[John Smith]]")
        
        assert person_id == "John Smith"  # Inner text without brackets
        assert display == "John Smith"
    
    def test_extracts_aliased_wikilink(self, mock_speaker_identifier):
        """Should extract both parts from aliased wikilink."""
        person_id, display = mock_speaker_identifier._extract_person_from_wikilink("[[John Smith|Johnny]]")
        
        assert person_id == "John Smith"  # Link target
        assert display == "Johnny"  # Display text


class TestReset:
    """Tests for reset/revert functionality."""
    
    @pytest.mark.asyncio
    async def test_reset_removes_form_section(self, mock_speaker_identifier):
        """Reset should remove the validation form section."""
        # Create a file with a form section
        input_file = mock_speaker_identifier.input_dir / "test_meeting.md"
        input_file.write_text("""---
date: '2025-12-27'
processing_stages:
  - transcribed
  - classified
  - speakers_identified
speaker_validation_pending: true
---
<!-- form:speaker_identification:start -->
Form content here
<!-- form:speaker_identification:end -->

Speaker A: Hello everyone.
Speaker B: Hi there!
""")
        
        await mock_speaker_identifier.reset("test_meeting.md")
        
        result = input_file.read_text()
        
        # Form should be removed
        assert "<!-- form:speaker_identification:start -->" not in result
        assert "Form content here" not in result
        
        # Transcript should be preserved
        assert "Speaker A: Hello" in result
        
        # Frontmatter should be cleaned
        assert "speaker_validation_pending" not in result
        assert "speakers_identified" not in result
    
    @pytest.mark.asyncio
    async def test_reset_reverts_speaker_names(self, mock_speaker_identifier):
        """Reset should revert processed speaker names back to original labels."""
        # Create a file that has been fully processed (names replaced)
        input_file = mock_speaker_identifier.input_dir / "test_meeting.md"
        input_file.write_text("""---
date: '2025-12-27'
processing_stages:
  - transcribed
  - classified
  - speakers_identified
final_speaker_mapping:
  Speaker A:
    name: John Smith
    person_id: "[[John Smith]]"
  Speaker B:
    name: Jane Doe
    person_id: "[[Jane Doe]]"
---
<!-- summary:speaker_identification:start -->
Summary content
<!-- summary:speaker_identification:end -->

John Smith ([[John Smith]]): Hello everyone.
Jane Doe ([[Jane Doe]]): Hi there!
John Smith ([[John Smith]]): How are you?
""")
        
        await mock_speaker_identifier.reset("test_meeting.md")
        
        result = input_file.read_text()
        
        # Names should be reverted to Speaker A, Speaker B
        assert "Speaker A:" in result
        assert "Speaker B:" in result
        
        # Summary section should be removed
        assert "<!-- summary:speaker_identification" not in result
        
        # final_speaker_mapping should be removed from frontmatter
        assert "final_speaker_mapping" not in result
