"""
E2E tests for IdeaProcessor.
"""

import pytest
from pathlib import Path

from processors.notes.ideas import IdeaProcessor


@pytest.fixture
def mock_idea_processor(mock_ai, tmp_path):
    """Create an IdeaProcessor with mocked dependencies."""
    input_dir = tmp_path / "transcriptions"
    directory_file = tmp_path / "Ideas Directory.md"
    input_dir.mkdir(parents=True)
    
    processor = IdeaProcessor(input_dir=input_dir, directory_file=directory_file)
    return processor


class TestShouldProcess:
    """Tests for should_process logic."""
    
    def test_processes_idea_category(self, mock_idea_processor):
        """Should process files with category=idea."""
        frontmatter = {"category": "idea"}
        result = mock_idea_processor.should_process("test.md", frontmatter)
        assert result is True
    
    def test_skips_non_idea_category(self, mock_idea_processor):
        """Should skip files without idea category."""
        frontmatter = {"category": "meeting"}
        result = mock_idea_processor.should_process("test.md", frontmatter)
        assert result is False
    
    def test_skips_if_already_referenced(self, mock_idea_processor):
        """Should skip if file is already in directory."""
        # Add reference to directory
        mock_idea_processor.directory_file.write_text(
            "# Ideas\n\n- [[test.md]]\n"
        )
        
        frontmatter = {"category": "idea"}
        result = mock_idea_processor.should_process("test.md", frontmatter)
        assert result is False


class TestProcessFile:
    """Tests for idea processing."""
    
    @pytest.mark.asyncio
    async def test_appends_to_directory(self, mock_idea_processor, mock_ai):
        """Should append ideas to directory file."""
        # Create input file
        input_file = mock_idea_processor.input_dir / "2025-12-27-idea.md"
        input_file.write_text("""---
date: '2025-12-27'
category: idea
---
I have an idea for a new app that tracks habits.
""")
        
        # Mock AI to return extracted ideas
        mock_ai.add_response("tracks habits", "- Habit tracking app\n- Daily reminders")
        
        directory_before = mock_idea_processor.directory_file.read_text()
        
        await mock_idea_processor._process_file("2025-12-27-idea.md")
        
        directory_after = mock_idea_processor.directory_file.read_text()
        
        # Check content was appended
        assert len(directory_after) > len(directory_before)
        assert "2025-12-27-idea.md" in directory_after
