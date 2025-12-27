"""
Tests for EntityResolver functionality.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from processors.notes.entity_resolver import EntityResolver


@pytest.fixture
def mock_resolver(mock_ai):
    """Create an EntityResolver with mocked dependencies."""
    mock_discord = MagicMock()
    mock_input_dir = Path("/tmp")
    
    # Mock PATHS to avoid real filesystem dependencies
    with patch("processors.notes.entity_resolver.PATHS") as mock_paths:
        mock_paths.vault_path = Path("/tmp/vault")
        resolver = EntityResolver(mock_input_dir, mock_discord)
        yield resolver

class TestEntityReferenceParsing:
    """Tests for parsing Entity Reference file."""
    
    def test_parses_existing_references(self, mock_resolver):
        """Should correctly parse reference file content."""
        content = """# Entity Resolution Reference

## People Aliases
| Detected Name | Resolved Link |
|---------------|---------------|
| maxime | [[Maxime Fournes]] |
| max | [[Maxime Fournes]] |

## Organisation Aliases
| Detected Name | Resolved Link |
|---------------|---------------|
| pause ai | [[Pause IA]] |

## Other Aliases
| Detected Name | Resolved Link |
|---------------|---------------|
| agi | [[AGI]] |
"""
        # Mock file read
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=content):
            
            result = mock_resolver._parse_entity_reference()
            
            assert result["people"]["maxime"] == "[[Maxime Fournes]]"
            assert result["people"]["max"] == "[[Maxime Fournes]]"
            assert result["org"]["pause ai"] == "[[Pause IA]]"
            assert result["other"]["agi"] == "[[AGI]]"

class TestReferenceMethods:
    """Tests for helper methods related to references."""
    
    def test_update_reference_adds_new(self, mock_resolver):
        """Should add new entities to references."""
        # Mock parsing to return empty
        with patch.object(mock_resolver, "_parse_entity_reference", return_value={"people": {}, "org": {}, "other": {}}), \
             patch.object(Path, "write_text") as mock_write:
            
            new_entities = [
                {"detected_name": "Maxime", "resolved_link": "[[Maxime Fournes]]", "entity_type": "people"},
                {"detected_name": "Pause AI", "resolved_link": "[[Pause IA]]", "entity_type": "org"}
            ]
            
            mock_resolver._update_entity_reference(new_entities)
            
            # Verify write
            mock_write.assert_called_once()
            content = mock_write.call_args[0][0]
            assert "| Maxime | [[Maxime Fournes]] |" in content
            assert "| Pause Ai | [[Pause IA]] |" in content

class TestFormGenerationAndParsing:
    """Tests for form generation and parsing."""
    
    def test_generate_form_creates_correct_markup(self, mock_resolver):
        """Should generate form with correct markers and content."""
        entities = [
            {"detected_name": "Maxime", "suggested_link": "[[Maxime Fournes]]", "entity_type": "people"},
            {"detected_name": "Pause AI", "suggested_link": "[[Pause IA]]", "entity_type": "org"}
        ]
        
        form = mock_resolver._generate_form(entities)
        
        assert mock_resolver.FORM_START in form
        assert mock_resolver.FORM_END in form
        assert "## Maxime" in form
        assert "<!-- input:entity_0_link -->[[Maxime Fournes]]" in form
        assert "<!-- input:entity_0_type -->people" in form
        assert "## Pause AI" in form
        
    def test_parse_form_extracts_data(self, mock_resolver):
        """Should correctly parse form content."""
        content = """<!-- form:entity_resolution:start -->

> [!info] Entity Resolution

## Maxime
**Link:** <!-- input:entity_0_link -->[[Maxime Fournes]]
**Type:** <!-- input:entity_0_type -->people

## Pause AI
**Link:** <!-- input:entity_1_link -->[[Pause IA]]
**Type:** <!-- input:entity_1_type -->org

- [x] Finished <!-- input:finished -->

<!-- form:entity_resolution:end -->"""

        result = mock_resolver._parse_form(content)
        
        assert result["finished"] is True
        assert len(result["entities"]) == 2
        assert result["entities"][0]["link"] == "[[Maxime Fournes]]"
        assert result["entities"][0]["type"] == "people"
        assert result["entities"][1]["link"] == "[[Pause IA]]"
        assert result["entities"][1]["type"] == "org"

    def test_parse_form_handles_unchecked_finished(self, mock_resolver):
        """Should check if finished is false."""
        content = """<!-- form:entity_resolution:start -->
        - [ ] Finished <!-- input:finished -->
        <!-- form:entity_resolution:end -->"""
        
        result = mock_resolver._parse_form(content)
        assert result["finished"] is False

class TestFormRemoval:
    """Tests for removing form sections."""
    
    def test_removes_form_section(self, mock_resolver):
        """Should remove the form section."""
        content = """Before
<!-- form:entity_resolution:start -->
Form content
<!-- form:entity_resolution:end -->
After"""
        
        result = mock_resolver._remove_form_section(content)
        assert result.strip() == "Before\nAfter"
