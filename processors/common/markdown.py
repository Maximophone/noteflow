import re
from pathlib import Path
from typing import List, Tuple, Optional

def extract_sections(content: str) -> List[Tuple[str, str]]:
    """
    Extract markdown sections based on headers.
    
    Args:
        content: Markdown content string
        
    Returns:
        List of tuples (header, section_content)
    """
    sections = []
    current_header = None
    current_content = []
    
    for line in content.split('\n'):
        if line.startswith('#'):
            if current_header is not None:
                sections.append((
                    current_header,
                    '\n'.join(current_content).strip()
                ))
            current_header = line
            current_content = []
        else:
            current_content.append(line)
            
    if current_header is not None:
        sections.append((
            current_header,
            '\n'.join(current_content).strip()
        ))
        
    return sections

def create_wikilink(text: str, alias: Optional[str] = None) -> str:
    """
    Create an Obsidian-style wikilink.
    
    Args:
        text: Link target
        alias: Optional display text
        
    Returns:
        Formatted wikilink string
    """
    if alias:
        return f"[[{text}|{alias}]]"
    return f"[[{text}]]"

def extract_wikilinks(content: str) -> List[str]:
    """
    Extract all wikilinks from markdown content.
    
    Args:
        content: Markdown content string
        
    Returns:
        List of link targets (without brackets and aliases)
    """
    pattern = r'\[\[(.*?)\]\]'
    matches = re.findall(pattern, content)
    
    # Remove aliases if present
    return [link.split('|')[0] if '|' in link else link 
            for link in matches]

def sanitize_filename(text: str) -> str:
    """
    Convert text to a safe filename.
    
    Args:
        text: Input text
        
    Returns:
        Sanitized string safe for use as filename
    """
    # Remove or replace unsafe characters
    safe = "".join(c for c in text if c.isalnum() or c in (' ', '-', '_')).strip()
    # Replace multiple spaces/dashes with single dash
    safe = re.sub(r'[-\s]+', '-', safe)
    return safe

def get_relative_link(from_path: Path, to_path: Path) -> str:
    """
    Create a relative link between two files in the vault.
    
    Args:
        from_path: Path of the source file
        to_path: Path of the target file
        
    Returns:
        Relative path suitable for markdown links
    """
    try:
        relative = to_path.relative_to(from_path.parent)
        return str(relative).replace('\\', '/')
    except ValueError:
        # Files are in different branches of directory tree
        relative = to_path.relative_to(from_path.parent.parent)
        return '../' + str(relative).replace('\\', '/')





