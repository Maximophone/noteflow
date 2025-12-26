"""
Tests for the Obsidian Form validation system.
"""

import pytest
from processors.common.obsidian_form import (
    is_valid_wikilink,
    validate_wikilink_field,
    ValidationError,
    generate_error_callout,
    insert_error_in_section,
    remove_error_callout,
)


class TestWikilinkValidation:
    """Tests for wikilink format validation."""
    
    def test_valid_simple_wikilink(self):
        """Simple wikilink should be valid."""
        assert is_valid_wikilink("[[John Smith]]") is True
    
    def test_valid_wikilink_with_alias(self):
        """Wikilink with alias should be valid."""
        assert is_valid_wikilink("[[John Smith|Johnny]]") is True
    
    def test_valid_empty(self):
        """Empty string should be valid (optional field)."""
        assert is_valid_wikilink("") is True
        assert is_valid_wikilink("  ") is True
    
    def test_invalid_plain_text(self):
        """Plain text without brackets should be invalid."""
        assert is_valid_wikilink("John Smith") is False
        assert is_valid_wikilink("Maxime") is False
    
    def test_invalid_partial_brackets(self):
        """Partial bracket syntax should be invalid."""
        assert is_valid_wikilink("[John Smith]") is False
        assert is_valid_wikilink("[[John Smith]") is False
        assert is_valid_wikilink("[John Smith]]") is False
    
    def test_valid_wikilink_with_whitespace(self):
        """Wikilink with surrounding whitespace should be valid."""
        assert is_valid_wikilink("  [[John Smith]]  ") is True


class TestValidateWikilinkField:
    """Tests for the validate_wikilink_field function."""
    
    def test_valid_wikilink_returns_none(self):
        """Valid wikilink should return no error."""
        assert validate_wikilink_field("[[John Smith]]", "Speaker A") is None
    
    def test_empty_allowed_returns_none(self):
        """Empty value with allow_empty=True should return no error."""
        assert validate_wikilink_field("", "Speaker A", allow_empty=True) is None
    
    def test_empty_not_allowed_returns_error(self):
        """Empty value with allow_empty=False should return error."""
        error = validate_wikilink_field("", "Speaker A", allow_empty=False)
        assert error is not None
        assert error.field_name == "Speaker A"
        assert "required" in error.message.lower()
    
    def test_plain_text_returns_error(self):
        """Plain text should return validation error."""
        error = validate_wikilink_field("Maxime", "Speaker A")
        assert error is not None
        assert error.field_name == "Speaker A"
        assert "wikilink" in error.message.lower()
        assert "Maxime" in error.message


class TestGenerateErrorCallout:
    """Tests for error callout generation."""
    
    def test_empty_errors_returns_empty(self):
        """No errors should return empty string."""
        assert generate_error_callout([]) == ""
    
    def test_single_error(self):
        """Single error should generate callout."""
        errors = [ValidationError("Speaker A", "Must be a wikilink")]
        result = generate_error_callout(errors)
        
        assert "[!error]" in result
        assert "Must be a wikilink" in result
    
    def test_multiple_errors(self):
        """Multiple errors should all be included."""
        errors = [
            ValidationError("Speaker A", "Error 1"),
            ValidationError("Speaker B", "Error 2"),
        ]
        result = generate_error_callout(errors)
        
        assert "Error 1" in result
        assert "Error 2" in result


class TestInsertErrorInSection:
    """Tests for inserting errors into validation sections."""
    
    def test_unchecks_finished_checkbox(self):
        """Should uncheck the Finished checkbox."""
        content = """<!-- validation:start -->

> [!info] Fill in the fields

- [x] Finished <!-- input:finished -->

<!-- validation:end -->"""
        
        errors = [ValidationError("Speaker A", "Test error")]
        result = insert_error_in_section(content, errors, "<!-- validation:start -->")
        
        # Checkbox should be unchecked
        assert "[ ] Finished <!-- input:finished -->" in result
        assert "[x] Finished" not in result
    
    def test_inserts_error_callout(self):
        """Should insert error callout after section marker."""
        content = """<!-- validation:start -->

> [!info] Fill in the fields

<!-- validation:end -->"""
        
        errors = [ValidationError("Speaker A", "Must be wikilink")]
        result = insert_error_in_section(content, errors, "<!-- validation:start -->")
        
        assert "[!error]" in result
        assert "Must be wikilink" in result
    
    def test_no_errors_returns_unchanged(self):
        """No errors should return content unchanged."""
        content = "Some content"
        result = insert_error_in_section(content, [], "<!-- validation:start -->")
        assert result == content


class TestRemoveErrorCallout:
    """Tests for removing error callouts."""
    
    def test_removes_error_callout(self):
        """Should remove error callout block."""
        content = """> [!error] Validation errors
> - Error 1
> - Error 2

Some other content"""
        
        result = remove_error_callout(content)
        
        assert "[!error]" not in result
        assert "Error 1" not in result
        assert "Some other content" in result
    
    def test_no_callout_returns_unchanged(self):
        """No error callout should return content unchanged."""
        content = "Just normal content"
        result = remove_error_callout(content)
        assert result == content
