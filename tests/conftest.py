"""
Shared test fixtures and configuration for NoteFlow E2E tests.
"""

import pytest
import asyncio
import shutil
import json
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from unittest.mock import MagicMock, AsyncMock, patch

# Test directories
TESTS_DIR = Path(__file__).parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
EXPECTED_DIR = TESTS_DIR / "expected"


# ===== Fixtures for temp directories =====

@pytest.fixture
def test_vault(tmp_path):
    """Create a temporary vault structure for testing."""
    vault = tmp_path / "vault"
    vault.mkdir()
    
    # Create standard directories
    (vault / "KnowledgeBot" / "Transcriptions").mkdir(parents=True)
    (vault / "Meetings").mkdir()
    (vault / "People").mkdir()
    
    return vault


@pytest.fixture
def transcriptions_dir(test_vault):
    """Return the transcriptions directory within the test vault."""
    return test_vault / "KnowledgeBot" / "Transcriptions"


# ===== Fixture file helpers =====

def load_fixture(name: str) -> str:
    """Load a fixture file by relative name."""
    path = FIXTURES_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding='utf-8')


def load_expected(name: str) -> str:
    """Load an expected output file by relative name."""
    path = EXPECTED_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Expected file not found: {path}")
    return path.read_text(encoding='utf-8')


def copy_fixture(name: str, dest_dir: Path) -> Path:
    """Copy a fixture file to a destination directory."""
    source = FIXTURES_DIR / name
    dest = dest_dir / source.name
    shutil.copy(source, dest)
    return dest


# ===== AI Mocking =====

@dataclass
class MockAIResponse:
    """Mock AI response object."""
    content: Optional[str] = None
    error: Optional[str] = None
    reasoning: Optional[str] = None


@dataclass
class MockAIController:
    """Controller for managing mock AI responses."""
    responses: Dict[str, MockAIResponse] = field(default_factory=dict)
    call_log: list = field(default_factory=list)
    
    def add_response(self, prompt_contains: str, response: str):
        """Add a canned response for prompts containing the given text."""
        self.responses[prompt_contains] = MockAIResponse(content=response)
    
    def load_responses(self, fixture_name: str):
        """Load canned responses from a JSON fixture file."""
        path = FIXTURES_DIR / "ai_responses" / fixture_name
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            for key, value in data.items():
                self.responses[key] = MockAIResponse(content=value)
    
    def get_response(self, prompt: str) -> MockAIResponse:
        """Get a response for the given prompt."""
        self.call_log.append(prompt)
        
        # Look for matching response
        for key, response in self.responses.items():
            if key in prompt:
                return response
        
        # Default response
        return MockAIResponse(content="Default mock AI response")


@pytest.fixture
def mock_ai(monkeypatch):
    """
    Mock AI to return canned responses.
    
    Usage:
        def test_something(mock_ai):
            mock_ai.add_response("classify", "meeting")
            # ... run processor
            assert len(mock_ai.call_log) == 1
    """
    controller = MockAIController()
    
    class MockAI:
        """Mock AI class that returns canned responses."""
        def __init__(self, model_name=None):
            self.model_name = model_name
        
        def message(self, message):
            """Mock AI.message() method."""
            if hasattr(message, 'content') and message.content:
                prompt_text = message.content[0].text if hasattr(message.content[0], 'text') else str(message.content[0])
            else:
                prompt_text = str(message)
            return controller.get_response(prompt_text)
    
    # Patch AI class in all locations where it's imported
    monkeypatch.setattr("processors.notes.base.AI", MockAI)
    monkeypatch.setattr("processors.notes.entity_resolver.AI", MockAI)
    monkeypatch.setattr("ai_core.AI", MockAI)
    
    return controller


# ===== Discord Mocking =====

@pytest.fixture
def mock_discord():
    """Create a mock Discord IO object."""
    mock = MagicMock()
    mock.send_dm = AsyncMock(return_value=True)
    return mock


# ===== File Comparison =====

def parse_frontmatter(content: str) -> tuple[Dict[str, Any], str]:
    """Parse frontmatter and content from a markdown file."""
    import yaml
    
    if not content.startswith('---'):
        return {}, content
    
    parts = content.split('---', 2)
    if len(parts) < 3:
        return {}, content
    
    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        frontmatter = {}
    
    return frontmatter, parts[2]


def assert_files_match(actual: str, expected: str, ignore_fields: list = None):
    """
    Assert that two markdown files match, ignoring specified frontmatter fields.
    
    Args:
        actual: Actual file content
        expected: Expected file content
        ignore_fields: List of frontmatter fields to ignore (e.g., ['date', 'timestamp'])
    """
    ignore_fields = ignore_fields or []
    
    actual_fm, actual_content = parse_frontmatter(actual)
    expected_fm, expected_content = parse_frontmatter(expected)
    
    # Remove ignored fields from both
    for field in ignore_fields:
        actual_fm.pop(field, None)
        expected_fm.pop(field, None)
    
    # Compare frontmatter
    assert actual_fm == expected_fm, f"Frontmatter mismatch:\nActual: {actual_fm}\nExpected: {expected_fm}"
    
    # Compare content (normalize whitespace)
    actual_lines = [l.rstrip() for l in actual_content.strip().splitlines()]
    expected_lines = [l.rstrip() for l in expected_content.strip().splitlines()]
    
    assert actual_lines == expected_lines, f"Content mismatch:\nActual:\n{actual_content}\n\nExpected:\n{expected_content}"


# ===== Async test support =====

@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
