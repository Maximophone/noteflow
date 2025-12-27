"""
Obsidian Form - Lightweight validation for text-based forms in Obsidian.

This module provides utilities for creating, parsing, and validating
inline form sections in Obsidian markdown files.
"""

import re
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


@dataclass
class ValidationError:
    """Represents a validation error for a form field."""
    field_name: str
    message: str


def is_valid_wikilink(value: str) -> bool:
    """Check if value is a valid wikilink format: [[...]] or [[...|...]]"""
    if not value or not value.strip():
        return True  # Empty is valid (field is optional)
    value = value.strip()
    return value.startswith("[[") and value.endswith("]]")


def validate_wikilink_field(value: str, field_name: str, allow_empty: bool = True) -> Optional[ValidationError]:
    """
    Validate that a field contains a valid wikilink.
    
    Args:
        value: The field value to validate
        field_name: Human-readable field name for error messages
        allow_empty: If True, empty values are valid
    
    Returns:
        ValidationError if invalid, None if valid
    """
    if not value or not value.strip():
        if allow_empty:
            return None
        else:
            return ValidationError(field_name, f"{field_name} is required")
    
    value = value.strip()
    if not is_valid_wikilink(value):
        return ValidationError(
            field_name, 
            f"{field_name} must be a wikilink (e.g., [[Person Name]]), got: {value}"
        )
    return None


def validate_choice_field(
    value: str, 
    choices: set, 
    field_name: str, 
    allow_empty: bool = False
) -> Optional[ValidationError]:
    """
    Validate that a field value is one of the allowed choices.
    
    Args:
        value: The field value to validate
        choices: Set of allowed values
        field_name: Human-readable field name for error messages
        allow_empty: If True, empty values are valid
    
    Returns:
        ValidationError if invalid, None if valid
    """
    if not value or not value.strip():
        if allow_empty:
            return None
        else:
            return ValidationError(field_name, f"{field_name} is required")
    
    value = value.strip().lower()
    normalized_choices = {c.lower() for c in choices}
    
    if value not in normalized_choices:
        choices_str = ", ".join(sorted(choices))
        return ValidationError(
            field_name,
            f"{field_name} must be one of: {choices_str}. Got: {value}"
        )
    return None


def generate_error_callout(errors: List[ValidationError]) -> str:
    """
    Generate an Obsidian error callout for validation errors.
    
    Args:
        errors: List of validation errors to display
    
    Returns:
        Markdown string for the error callout
    """
    if not errors:
        return ""
    
    lines = [
        "> [!error] Validation errors - please fix and check Finished again",
    ]
    for error in errors:
        lines.append(f"> - {error.message}")
    lines.append("")
    
    return "\n".join(lines)


def insert_error_in_section(
    section_content: str, 
    errors: List[ValidationError],
    section_start_marker: str
) -> str:
    """
    Insert an error callout at the top of a validation section.
    Also unchecks the Finished checkbox.
    
    Args:
        section_content: The full file content
        errors: List of validation errors
        section_start_marker: The marker that starts the validation section
    
    Returns:
        Updated content with error callout inserted and Finished unchecked
    """
    if not errors:
        return section_content
    
    error_callout = generate_error_callout(errors)
    
    # Find the section start and insert error after it
    start_idx = section_content.find(section_start_marker)
    if start_idx == -1:
        return section_content
    
    # Find end of the start marker line
    marker_end = section_content.find("\n", start_idx)
    if marker_end == -1:
        marker_end = len(section_content)
    
    # Remove any existing error callout (between marker and first heading)
    after_marker = section_content[marker_end:]
    # Find where the actual content starts (first # or > [!info])
    content_match = re.search(r'\n(#|> \[!(?!error))', after_marker)
    if content_match:
        # Remove everything between marker and content (old errors)
        after_marker = after_marker[content_match.start():]
    
    # Uncheck the Finished checkbox
    after_marker = re.sub(
        r'\[x\]\s+Finished\s+(<!-- input:finished -->)',
        r'[ ] Finished \1',
        after_marker,
        flags=re.IGNORECASE
    )
    
    # Reconstruct with error callout
    result = (
        section_content[:marker_end + 1] +  # Include newline after marker
        "\n" + error_callout +
        after_marker
    )
    
    return result


def remove_error_callout(content: str) -> str:
    """Remove any existing error callout from content."""
    # Pattern to match error callout block
    pattern = r'\n?> \[!error\].*?(?=\n(?!>)|\Z)'
    return re.sub(pattern, '', content, flags=re.DOTALL)
