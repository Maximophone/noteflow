"""
E2E tests for DiaryProcessor.
"""

import pytest
from pathlib import Path
from unittest.mock import patch

from processors.notes.diary import DiaryProcessor
from tests.conftest import copy_fixture, assert_files_match


@pytest.fixture
def mock_diary_processor(mock_ai, tmp_path):
    """Create a DiaryProcessor with mocked dependencies."""
    input_dir = tmp_path / "transcriptions"
    output_dir = tmp_path / "diary"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    
    processor = DiaryProcessor(input_dir=input_dir, output_dir=output_dir)
    return processor


class TestShouldProcess:
    """Tests for should_process logic."""
    
    def test_processes_diary_category(self, mock_diary_processor, tmp_path):
        """Should process files with category=diary."""
        frontmatter = {"category": "diary"}
        result = mock_diary_processor.should_process("test.md", frontmatter)
        assert result is True
    
    def test_skips_non_diary_category(self, mock_diary_processor):
        """Should skip files without diary category."""
        frontmatter = {"category": "meeting"}
        result = mock_diary_processor.should_process("test.md", frontmatter)
        assert result is False
    
    def test_skips_if_output_exists(self, mock_diary_processor):
        """Should skip if diary entry already exists."""
        # Create existing output file
        output_file = mock_diary_processor.output_dir / "test.md"
        output_file.write_text("existing content")
        
        frontmatter = {"category": "diary"}
        result = mock_diary_processor.should_process("test.md", frontmatter)
        assert result is False


class TestProcessFile:
    """Tests for diary processing."""
    
    @pytest.mark.asyncio
    async def test_creates_formatted_diary_entry(self, mock_diary_processor, mock_ai):
        """Should create a formatted diary entry in output dir."""
        # Create input file
        input_file = mock_diary_processor.input_dir / "2025-12-27-diary.md"
        input_file.write_text("""---
date: '2025-12-27'
title: My Diary
category: diary
---
Today was a good day. I went for a walk in the park.
""")
        
        # Mock AI to return formatted content
        mock_ai.add_response("walk in the park", "# Formatted Diary Entry\n\nToday I had a wonderful day...")
        
        await mock_diary_processor._process_file("2025-12-27-diary.md")
        
        # Check output file was created
        output_file = mock_diary_processor.output_dir / "2025-12-27-diary.md"
        assert output_file.exists()
        
        content = output_file.read_text()
        assert "diary" in content.lower()
