"""
E2E tests for MeditationProcessor.
"""

import pytest
from pathlib import Path

from processors.notes.meditation import MeditationProcessor


@pytest.fixture
def mock_meditation_processor(mock_ai, tmp_path):
    """Create a MeditationProcessor with mocked dependencies."""
    input_dir = tmp_path / "transcriptions"
    output_dir = tmp_path / "meditations"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    
    processor = MeditationProcessor(input_dir=input_dir, output_dir=output_dir)
    return processor


class TestShouldProcess:
    """Tests for should_process logic."""
    
    def test_processes_meditation_category(self, mock_meditation_processor):
        """Should process files with category=meditation."""
        frontmatter = {"category": "meditation"}
        result = mock_meditation_processor.should_process("test.md", frontmatter)
        assert result is True
    
    def test_skips_non_meditation_category(self, mock_meditation_processor):
        """Should skip files without meditation category."""
        frontmatter = {"category": "meeting"}
        result = mock_meditation_processor.should_process("test.md", frontmatter)
        assert result is False
    
    def test_skips_if_output_exists(self, mock_meditation_processor):
        """Should skip if meditation note already exists."""
        output_file = mock_meditation_processor.output_dir / "test.md"
        output_file.write_text("existing content")
        
        frontmatter = {"category": "meditation"}
        result = mock_meditation_processor.should_process("test.md", frontmatter)
        assert result is False


class TestProcessFile:
    """Tests for meditation processing."""
    
    @pytest.mark.asyncio
    async def test_creates_meditation_note(self, mock_meditation_processor, mock_ai):
        """Should create a structured meditation note."""
        # Create input file
        input_file = mock_meditation_processor.input_dir / "2025-12-27-meditation.md"
        input_file.write_text("""---
date: '2025-12-27'
category: meditation
original_file: meditation.m4a
---
Close your eyes and take a deep breath. Focus on the present moment.
""")
        
        # Mock AI response
        mock_ai.add_response("deep breath", "# Meditation Summary\n\n- Focus on breathing\n- Stay present")
        
        await mock_meditation_processor._process_file("2025-12-27-meditation.md")
        
        # Check output file was created
        output_file = mock_meditation_processor.output_dir / "2025-12-27-meditation.md"
        assert output_file.exists()
        
        content = output_file.read_text()
        assert "meditation" in content.lower()
