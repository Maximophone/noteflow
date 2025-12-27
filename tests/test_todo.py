"""
E2E tests for TodoProcessor.
"""

import pytest
from pathlib import Path

from processors.notes.todo import TodoProcessor


@pytest.fixture
def mock_todo_processor(mock_ai, tmp_path):
    """Create a TodoProcessor with mocked dependencies."""
    input_dir = tmp_path / "transcriptions"
    directory_file = tmp_path / "Todo Directory.md"
    input_dir.mkdir(parents=True)
    
    processor = TodoProcessor(input_dir=input_dir, directory_file=directory_file)
    return processor


class TestShouldProcess:
    """Tests for should_process logic."""
    
    def test_processes_todo_category(self, mock_todo_processor):
        """Should process files with category=todo."""
        frontmatter = {"category": "todo"}
        result = mock_todo_processor.should_process("test.md", frontmatter)
        assert result is True
    
    def test_skips_non_todo_category(self, mock_todo_processor):
        """Should skip files without todo category."""
        frontmatter = {"category": "meeting"}
        result = mock_todo_processor.should_process("test.md", frontmatter)
        assert result is False
    
    def test_skips_if_already_referenced(self, mock_todo_processor):
        """Should skip if file is already in directory."""
        mock_todo_processor.directory_file.write_text(
            "# Todos\n\n- [[test.md]]\n"
        )
        
        frontmatter = {"category": "todo"}
        result = mock_todo_processor.should_process("test.md", frontmatter)
        assert result is False


class TestProcessFile:
    """Tests for todo processing."""
    
    @pytest.mark.asyncio
    async def test_appends_to_directory(self, mock_todo_processor, mock_ai):
        """Should append todos to directory file."""
        # Create input file
        input_file = mock_todo_processor.input_dir / "2025-12-27-todos.md"
        input_file.write_text("""---
date: '2025-12-27'
category: todo
---
I need to buy groceries and call mom.
""")
        
        mock_ai.add_response("groceries", "- [ ] Buy groceries\n- [ ] Call mom")
        
        directory_before = mock_todo_processor.directory_file.read_text()
        
        await mock_todo_processor._process_file("2025-12-27-todos.md")
        
        directory_after = mock_todo_processor.directory_file.read_text()
        
        assert len(directory_after) > len(directory_before)
        assert "2025-12-27-todos.md" in directory_after
