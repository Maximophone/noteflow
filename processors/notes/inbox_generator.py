"""
NoteFlow Inbox Generator

Generates a markdown file showing all notes awaiting user input,
grouped by file and sorted by date.
"""

from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
import re

from config.paths import PATHS
from config.logging_config import setup_logger
from ..common.frontmatter import read_frontmatter_from_file

logger = setup_logger(__name__)


# Form types and their corresponding frontmatter flags
FORM_TYPES = {
    "speaker_validation_pending": "Speaker ID",
    "entity_resolution_pending": "Entity Resolution",
}

# Form markers for error detection
FORM_MARKERS = {
    "speaker_validation_pending": "<!-- form:speaker_identification:start -->",
    "entity_resolution_pending": "<!-- form:entity_resolution:start -->",
}


class InboxGenerator:
    """Generates a markdown inbox showing files awaiting user input."""
    
    def __init__(self, scan_dir: Path, inbox_path: Path, vault_path: Path):
        """
        Initialize the inbox generator.
        
        Args:
            scan_dir: Directory to scan for pending forms
            inbox_path: Path to write the inbox markdown file
            vault_path: Obsidian vault root for computing relative paths
        """
        self.scan_dir = scan_dir
        self.inbox_path = inbox_path
        self.vault_path = vault_path
    
    def _has_error_callout(self, content: str, form_marker: str) -> bool:
        """Check if a form section contains an error callout."""
        start_idx = content.find(form_marker)
        if start_idx == -1:
            return False
        
        # Look for error callout after the form marker
        section = content[start_idx:]
        return "> [!error]" in section
    
    def _scan_file(self, file_path: Path) -> Optional[Dict]:
        """
        Scan a single file for pending forms.
        
        Returns:
            Dict with file info if pending forms found, None otherwise
        """
        try:
            frontmatter = read_frontmatter_from_file(file_path)
        except Exception as e:
            logger.debug(f"Could not read frontmatter from {file_path}: {e}")
            return None
        
        # Check for pending forms
        pending_forms = []
        for flag, form_name in FORM_TYPES.items():
            if frontmatter.get(flag):
                pending_forms.append(form_name)
        
        if not pending_forms:
            return None
        
        # Check for errors by reading file content
        has_error = False
        try:
            content = file_path.read_text(encoding='utf-8')
            for flag in FORM_TYPES.keys():
                if frontmatter.get(flag):
                    marker = FORM_MARKERS.get(flag)
                    if marker and self._has_error_callout(content, marker):
                        has_error = True
                        break
        except Exception as e:
            logger.debug(f"Could not read content from {file_path}: {e}")
        
        # Get date for sorting - normalize to datetime
        file_date = frontmatter.get('date')
        if file_date:
            # Handle both string and date objects
            if isinstance(file_date, str):
                try:
                    file_date = datetime.strptime(file_date, "%Y-%m-%d")
                except ValueError:
                    file_date = None
            elif hasattr(file_date, 'isoformat'):
                # Convert date to datetime if needed
                if not isinstance(file_date, datetime):
                    file_date = datetime.combine(file_date, datetime.min.time())
            else:
                file_date = None
        
        # Get vault-relative path without extension for wikilink (avoids duplicate name issues)
        try:
            relative_path = file_path.relative_to(self.vault_path)
            note_name = str(relative_path.with_suffix(''))
        except ValueError:
            # Fallback to just filename if not under vault
            note_name = file_path.stem
        
        return {
            "name": note_name,
            "path": file_path,
            "forms": pending_forms,
            "has_error": has_error,
            "date": file_date,
        }
    
    def _scan_all(self) -> List[Dict]:
        """Scan all markdown files in the directory for pending forms."""
        results = []
        
        if not self.scan_dir.exists():
            logger.warning(f"Scan directory does not exist: {self.scan_dir}")
            return results
        
        for file_path in self.scan_dir.iterdir():
            if not file_path.suffix == '.md':
                continue
            
            file_info = self._scan_file(file_path)
            if file_info:
                results.append(file_info)
        
        # Sort by date (newest first), with None dates at the end
        def sort_key(x):
            if x['date'] is None:
                return (1, datetime.min)  # None dates go to end
            return (0, x['date'])
        
        results.sort(key=sort_key, reverse=True)
        
        return results
    
    def _generate_markdown(self, items: List[Dict]) -> str:
        """Generate the inbox markdown content."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        lines = [
            "# NoteFlow Inbox",
            "",
            f"> Last updated: {now}",
            "",
        ]
        
        if not items:
            lines.extend([
                "✅ **All clear!** No notes are waiting for input.",
                "",
            ])
        else:
            lines.extend([
                f"## Awaiting Input ({len(items)} {'file' if len(items) == 1 else 'files'})",
                "",
                "| Note | Pending Forms | Status |",
                "|------|---------------|--------|",
            ])
            
            for item in items:
                note_link = f"[[{item['name']}]]"
                forms = ", ".join(item['forms'])
                status = "⚠️ Errors" if item['has_error'] else "Ready"
                lines.append(f"| {note_link} | {forms} | {status} |")
            
            lines.append("")
        
        lines.extend([
            "---",
            "*This file is auto-generated by NoteFlow*",
            "",
        ])
        
        return "\n".join(lines)
    
    def generate(self) -> None:
        """Generate the inbox file."""
        logger.info("Generating NoteFlow inbox...")
        
        items = self._scan_all()
        content = self._generate_markdown(items)
        
        # Write the inbox file
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        self.inbox_path.write_text(content, encoding='utf-8')
        
        logger.info(f"Generated inbox with {len(items)} pending items: {self.inbox_path}")
    
    async def process_all(self) -> None:
        """Async wrapper for scheduler compatibility."""
        self.generate()
