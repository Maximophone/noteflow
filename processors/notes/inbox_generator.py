"""
NoteFlow Inbox Generator

Generates a markdown file showing all notes awaiting user input,
grouped by file and sorted by date. Also sends consolidated Discord
notifications when new pending items are detected.
"""

from pathlib import Path
from typing import Dict, List, Optional, Set
from datetime import datetime
import re

from config.paths import PATHS
from config.logging_config import setup_logger
from config.user_config import TARGET_DISCORD_USER_ID
from integrations.discord import DiscordIOCore
from ..common.frontmatter import read_frontmatter_from_file
from ..common import error_registry
from ..common.failed_audio import list_failures

logger = setup_logger(__name__)


# Form types and their corresponding frontmatter flags
FORM_TYPES = {
    "speaker_validation_pending": "Speaker ID",
    "entity_resolution_pending": "Entity Resolution",
    "meeting_summary_pending": "Meeting Summary",
}

# Form markers for error detection
FORM_MARKERS = {
    "speaker_validation_pending": "<!-- form:speaker_identification:start -->",
    "entity_resolution_pending": "<!-- form:entity_resolution:start -->",
    "meeting_summary_pending": "<!-- form:meeting_summary:start -->",
}


class InboxGenerator:
    """Generates a markdown inbox showing files awaiting user input.
    
    Also sends consolidated Discord notifications when new pending items are detected,
    reducing notification spam when multiple processors trigger simultaneously.
    """
    
    def __init__(self, scan_dir: Path = None, inbox_path: Path = None, vault_path: Path = None, 
                 scan_dirs: List[Path] = None, discord_io: DiscordIOCore = None,
                 failed_audio_dir: Path = None):
        """
        Initialize the inbox generator.
        
        Args:
            scan_dir: Single directory to scan (deprecated, use scan_dirs)
            inbox_path: Path to write the inbox markdown file
            vault_path: Obsidian vault root for computing relative paths
            scan_dirs: List of directories to scan for pending forms
            discord_io: Discord I/O core for sending notifications
            failed_audio_dir: Audio/Failed, listed so recordings that can never
                be transcribed stay visible instead of being silently dropped
        """
        # Support both single dir and list of dirs
        if scan_dirs:
            self.scan_dirs = scan_dirs
        elif scan_dir:
            self.scan_dirs = [scan_dir]
        else:
            self.scan_dirs = []
        
        self.inbox_path = inbox_path
        self.vault_path = vault_path
        self.discord_io = discord_io
        self.failed_audio_dir = failed_audio_dir
        
        # Track known pending items to detect new ones
        # Key is file path string, value is set of pending form types
        self._known_pending_items: Dict[str, Set[str]] = {}
    
    def _has_error_callout(self, content: str, form_marker: str) -> bool:
        """Check if a form section contains an error callout."""
        start_idx = content.find(form_marker)
        if start_idx == -1:
            return False
        
        # Look for error callout after the form marker
        section = content[start_idx:]
        return "> [!error]" in section
    
    def _note_name(self, file_path: Path) -> str:
        """Vault-relative path without extension for wikilinks."""
        try:
            return str(file_path.relative_to(self.vault_path).with_suffix(''))
        except ValueError:
            # Fallback to just filename if not under vault
            return file_path.stem

    def _check_broken_frontmatter(self, file_path: Path) -> Optional[Dict]:
        """Detect pipeline files whose frontmatter can no longer be parsed.

        A file that is mid-pipeline always carries frontmatter (processing_stages,
        pending flags, form markers). If the content shows those traces but the
        frontmatter parses to nothing, the file is stuck and every processor
        silently skips it — so surface it as an error.

        Returns an error dict if the file looks broken, None otherwise.
        """
        try:
            frontmatter = read_frontmatter_from_file(file_path)
            message = "Frontmatter could not be parsed — check the '---' delimiters at the top of the file"
        except Exception as e:
            frontmatter = None
            message = f"Frontmatter YAML error: {e}"

        if frontmatter:
            return None

        # No parseable frontmatter — only a problem if the content shows the
        # file was already in the pipeline. Full read happens only here, so
        # healthy files never pay for it.
        try:
            content = file_path.read_text(encoding='utf-8')
        except Exception:
            return None

        has_pipeline_traces = (
            'processing_stages:' in content
            or any(marker in content for marker in FORM_MARKERS.values())
        )
        if not has_pipeline_traces:
            return None

        return {
            'name': self._note_name(file_path),
            'stage': 'frontmatter',
            'message': message,
        }

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
        
        return {
            # Vault-relative path without extension for wikilink (avoids duplicate name issues)
            "name": self._note_name(file_path),
            "path": file_path,
            "forms": pending_forms,
            "has_error": has_error,
            "date": file_date,
        }
    
    def _scan_all(self) -> tuple[List[Dict], List[Dict]]:
        """Scan all markdown files in all directories for pending forms and errors.

        Returns:
            Tuple of (pending items, error items). Error items combine broken
            files found during the scan with errors recorded by processors.
        """
        results = []
        errors = []

        for scan_dir in self.scan_dirs:
            if not scan_dir.exists():
                logger.warning(f"Scan directory does not exist: {scan_dir}")
                continue

            for file_path in scan_dir.iterdir():
                if not file_path.suffix == '.md':
                    continue

                broken = self._check_broken_frontmatter(file_path)
                if broken:
                    errors.append(broken)
                    continue

                file_info = self._scan_file(file_path)
                if file_info:
                    results.append(file_info)

        # Add errors recorded by processors, skipping files that no longer exist
        for entry in error_registry.get_errors():
            if entry['path'].exists():
                errors.append({
                    'name': self._note_name(entry['path']),
                    'stage': entry['stage'],
                    'message': entry['message'],
                })

        # Sort by date (newest first), with None dates at the end
        def sort_key(x):
            if x['date'] is None:
                return (1, datetime.min)  # None dates go to end
            return (0, x['date'])

        results.sort(key=sort_key, reverse=True)

        return results, errors
    
    def _find_new_items(self, items: List[Dict]) -> List[Dict]:
        """Compare current items with known items to find new pending forms.
        
        Returns:
            List of items that are new (not previously seen)
        """
        new_items = []
        current_pending: Dict[str, Set[str]] = {}
        
        for item in items:
            path_key = str(item['path'])
            current_forms = set(item['forms'])
            current_pending[path_key] = current_forms
            
            # Check if this file+forms combination is new
            known_forms = self._known_pending_items.get(path_key, set())
            new_forms = current_forms - known_forms
            
            if new_forms:
                # Create a modified item with only the new forms for notification
                new_item = item.copy()
                new_item['forms'] = list(new_forms)
                new_items.append(new_item)
        
        # Update known items for next run
        self._known_pending_items = current_pending
        
        return new_items
    
    async def _send_notification(self, new_items: List[Dict]) -> None:
        """Send a consolidated Discord notification for new pending items."""
        if not self.discord_io or not new_items:
            return
        
        try:
            # Build notification message
            if len(new_items) == 1:
                item = new_items[0]
                forms = ", ".join(item['forms'])
                filename = item['path'].name
                dm_text = (
                    f"📝 **NoteFlow: Action Required**\n"
                    f"File: `{filename}`\n"
                    f"Pending: {forms}\n"
                    f"Open the file in Obsidian to complete the form."
                )
            else:
                dm_text = f"📝 **NoteFlow: {len(new_items)} files need your attention**\n\n"
                for item in new_items[:5]:  # Limit to first 5 files
                    filename = item['path'].name
                    forms = ", ".join(item['forms'])
                    status = " ⚠️" if item['has_error'] else ""
                    dm_text += f"• `{filename}` — {forms}{status}\n"
                
                if len(new_items) > 5:
                    dm_text += f"\n...and {len(new_items) - 5} more. Check NoteFlow Inbox in Obsidian."
            
            success = await self.discord_io.send_dm(TARGET_DISCORD_USER_ID, dm_text)
            
            if success:
                logger.info("Sent Discord notification for %d new pending item(s)", len(new_items))
            else:
                logger.warning("Failed to send Discord notification")
                
        except Exception as e:
            logger.warning("Error sending Discord notification: %s", e)
    
    @staticmethod
    def _table_cell(text: str) -> str:
        """Make an error message safe for a single markdown table cell."""
        text = ' '.join(text.split())  # collapse newlines/whitespace
        if len(text) > 300:
            text = text[:300] + "…"
        return text.replace('|', '\\|')

    def _generate_markdown(self, items: List[Dict], error_items: List[Dict] = None) -> str:
        """Generate the inbox markdown content."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        error_items = error_items or []

        lines = [
            "# NoteFlow Inbox",
            "",
            f"> Last updated: {now}",
            "",
        ]

        if error_items:
            lines.extend([
                f"## Processing Errors ({len(error_items)} {'file' if len(error_items) == 1 else 'files'})",
                "",
                "These files hit an error and will not progress until fixed.",
                "",
                "| Note | Stage | Error |",
                "|------|-------|-------|",
            ])
            for item in error_items:
                lines.append(
                    f"| [[{item['name']}]] | {item['stage']} | {self._table_cell(item['message'])} |"
                )
            lines.append("")

        failed_audio = list_failures(self.failed_audio_dir)
        if failed_audio:
            lines.extend([
                f"## Recordings That Could Not Be Transcribed "
                f"({len(failed_audio)} {'file' if len(failed_audio) == 1 else 'files'})",
                "",
                "Moved out of `Audio/Incoming` and no longer retried. Drop one back "
                "into Incoming to try again, or delete it to clear it from here.",
                "",
                "| Recording | Reason |",
                "|-----------|--------|",
            ])
            for failure in failed_audio:
                # Plain names, not wikilinks: the audio lives outside the vault.
                lines.append(
                    f"| `{failure['name']}` | {self._table_cell(failure['reason'])} |"
                )
            lines.append("")

        if not items:
            # "All clear" is only true if nothing above needs attention either.
            # Claiming it under a list of failures is how an inbox stops being
            # worth reading.
            if error_items or failed_audio:
                lines.extend([
                    "No notes are waiting for your input, but see above.",
                    "",
                ])
            else:
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
        """Generate the inbox file (sync version, no notifications)."""
        logger.info("Generating NoteFlow inbox...")
        
        items, error_items = self._scan_all()
        content = self._generate_markdown(items, error_items)

        # Write the inbox file
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        self.inbox_path.write_text(content, encoding='utf-8')

        logger.info(f"Generated inbox with {len(items)} pending items and {len(error_items)} errors: {self.inbox_path}")
    
    async def process_all(self) -> None:
        """Async entry point for scheduler - generates inbox and sends notifications."""
        logger.info("Generating NoteFlow inbox...")
        
        items, error_items = self._scan_all()

        # Find new items for notification
        new_items = self._find_new_items(items)

        # Generate and write inbox markdown
        content = self._generate_markdown(items, error_items)
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        self.inbox_path.write_text(content, encoding='utf-8')

        logger.info(f"Generated inbox with {len(items)} pending items and {len(error_items)} errors: {self.inbox_path}")
        
        # Send notification for new items only
        if new_items:
            await self._send_notification(new_items)
