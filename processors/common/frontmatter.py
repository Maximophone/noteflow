import yaml
from typing import Dict, Any, Optional
from config.logging_config import setup_logger
from pathlib import Path

logger = setup_logger(__name__)

def read_frontmatter_from_file(file_path):
    front_matter = {}
    with open(file_path, 'r', encoding='utf-8') as f:
        # Check for the start of front matter
        line = f.readline()
        if line.rstrip() != '---':
            return front_matter  # No front matter present
        # Read lines until the end of front matter
        yaml_lines = []
        for line in f:
            if line.rstrip() == '---':
                break  # End of front matter
            yaml_lines.append(line)
        # Parse the YAML content
        yaml_content = ''.join(yaml_lines)
        try:
            front_matter = yaml.safe_load(yaml_content)
        except yaml.YAMLError as e:
            logger.error("Error parsing YAML front matter in %s: %s", file_path, e)
            raise e
            front_matter = {}
    return front_matter

def read_text_from_file(file_path) -> str:
    """
    Read the text content of a file, excluding the front matter.
    
    Args:
        file_path: Path to the file to read
        
    Returns:
        Text content of the file, excluding the front matter
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    if not lines:
        return ""

    # A valid frontmatter starts with '---' on the first line.
    # The line must be exactly '---' with only a newline.
    if lines[0].rstrip('\r\n') != '---':
        return "".join(lines)

    # Find the end of the frontmatter block.
    end_of_fm_idx = -1
    for i, line in enumerate(lines[1:], start=1):
        # The closing '---' must also be on a line by itself.
        if line.rstrip('\r\n') == '---':
            end_of_fm_idx = i
            break

    if end_of_fm_idx == -1:
        # No closing '---' found. Treat as text.
        return "".join(lines)

    # Check if the content between '---' is valid YAML.
    frontmatter_content = "".join(lines[1:end_of_fm_idx])
    
    # An empty frontmatter is valid.
    if not frontmatter_content.strip():
        return "".join(lines[end_of_fm_idx + 1:])

    try:
        yaml.safe_load(frontmatter_content)
    except yaml.YAMLError:
        # Invalid YAML, so it's not frontmatter.
        return "".join(lines)

    # Valid frontmatter, so return text after it.
    return "".join(lines[end_of_fm_idx + 1:])


def has_frontmatter_from_content(content: str) -> bool:
    """
    Check if a string contains a valid YAML frontmatter block.
    
    Args:
        content: The string content to process.
        
    Returns:
        True if a valid frontmatter block is found, False otherwise.
    """
    return parse_frontmatter_from_content(content) is not None


def has_frontmatter_from_file(file_path: str) -> bool:
    """
    Check if a file contains a valid YAML frontmatter block.
    
    Args:
        file_path: Path to the file to check.
        
    Returns:
        True if a valid frontmatter block is found, False otherwise.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return has_frontmatter_from_content(content)
    except FileNotFoundError:
        return False


def read_text_from_content(content: str) -> str:
    """
    Extracts the text content from a string, excluding the front matter.

    This function is thorough in its validation of frontmatter, checking for
    delimiters and valid YAML syntax. If the frontmatter is not perfectly
    formed, the entire content is treated as text.

    Args:
        content: The string content to process.

    Returns:
        The text content of the string, excluding any valid front matter.
    """
    lines = content.splitlines(True)

    if not lines:
        return ""

    if lines[0].rstrip('\r\n') != '---':
        return content

    end_of_fm_idx = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip('\r\n') == '---':
            end_of_fm_idx = i
            break

    if end_of_fm_idx == -1:
        return content

    frontmatter_content = "".join(lines[1:end_of_fm_idx])
    
    if not frontmatter_content.strip():
        return "".join(lines[end_of_fm_idx + 1:])

    try:
        yaml.safe_load(frontmatter_content)
    except yaml.YAMLError:
        return content

    return "".join(lines[end_of_fm_idx + 1:])


def set_frontmatter_in_file(file_path, new_front_matter):
    # Check for invalid lines in the new front matter (lines that are just '---' possibly with spaces)
    front_matter_str_check = yaml.dump(new_front_matter)
    for i, line in enumerate(front_matter_str_check.splitlines(), 1):
        if line.rstrip() == '---':
            reason = f"Invalid line in front matter: line {i} is just '---'. Not allowed as this will break the parsing."
            logger.error(reason)
            raise ValueError(reason)
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    # Check if the file has front matter
    if not lines or lines[0].rstrip('\r\n') != '---':
        # No front matter, so add it
        front_matter_str = '---\n' + yaml.dump(new_front_matter) + '---\n'
        new_content = front_matter_str + ''.join(lines)
    else:
        # Replace existing front matter
        end_index = None
        for i, line in enumerate(lines[1:], start=1):
            if line.rstrip('\r\n') == '---':
                end_index = i
                break
        if end_index is None:
            logger.error("Error: Closing '---' not found in %s", file_path)
            # Prepend frontmatter since the existing one is malformed
            front_matter_str = '---\n' + yaml.dump(new_front_matter) + '---\n'
            new_content = front_matter_str + ''.join(lines)
        else:
            front_matter_str = '---\n' + yaml.dump(new_front_matter) + '---\n'
            new_content = front_matter_str + ''.join(lines[end_index+1:])

    # Write the updated content back to the file
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)

def parse_frontmatter_from_content(content: str) -> Optional[Dict[str, Any]]:
    """
    Extract and parse YAML frontmatter from markdown content.
    
    Args:
        content: String containing markdown content with potential frontmatter
        
    Returns:
        Dictionary of frontmatter data or None if no frontmatter found
    """
    lines = content.splitlines(True)
    if not lines or lines[0].rstrip('\r\n') != '---':
        return None
        
    end_of_fm_idx = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip('\r\n') == '---':
            end_of_fm_idx = i
            break
            
    if end_of_fm_idx == -1:
        return None
            
    fm_content = "".join(lines[1:end_of_fm_idx])
    try:
        parsed = yaml.safe_load(fm_content)
        # Treat frontmatter that is empty or just whitespace as an empty dict
        return parsed if parsed is not None else {}
    except (yaml.YAMLError, ValueError):
        return None

def frontmatter_to_text(frontmatter: Dict[str, Any]) -> str:
    """
    Convert a frontmatter dictionary to YAML text format.
    
    Args:
        frontmatter: Dictionary of frontmatter data
        
    Returns:
        Formatted string with YAML frontmatter delimiters
    """
    yaml_text = yaml.dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False
    )
    return f"---\n{yaml_text}---\n"

def update_frontmatter_in_content(content: str, updates: Dict[str, Any]) -> str:
    """
    Update existing frontmatter in markdown content.
    
    Args:
        content: Original markdown content with frontmatter
        updates: Dictionary of frontmatter fields to update
        
    Returns:
        Updated content string
    """
    lines = content.splitlines(True)
    
    if not lines or lines[0].rstrip('\r\n') != '---':
        # No frontmatter. Prepend a new one.
        new_fm = {}
        new_fm.update(updates)
        return frontmatter_to_text(new_fm) + content

    end_of_fm_idx = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip('\r\n') == '---':
            end_of_fm_idx = i
            break
            
    if end_of_fm_idx == -1:
        # No closing tag, treat as if there's no frontmatter.
        new_fm = {}
        new_fm.update(updates)
        return frontmatter_to_text(new_fm) + content

    # Valid frontmatter found
    fm_content = "".join(lines[1:end_of_fm_idx])
    body_content = "".join(lines[end_of_fm_idx+1:])
    
    try:
        existing = yaml.safe_load(fm_content)
        if existing is None:
            existing = {}
    except yaml.YAMLError:
        # The existing frontmatter is invalid, so we start fresh.
        existing = {}
        
    existing.update(updates)
    
    return frontmatter_to_text(existing) + body_content





