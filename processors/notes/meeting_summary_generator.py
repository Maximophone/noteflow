"""
Meeting Summary Generator Processor

Generates AI-powered meeting summaries with user validation,
then appends validated summaries to a monthly meetings index file.
"""

from pathlib import Path
from typing import Dict, Any, Optional, List
import aiofiles
import os
import re
import asyncio
from datetime import datetime

from .base import NoteProcessor
from .entity_resolver import EntityResolver
from ..common.frontmatter import parse_frontmatter_from_content, frontmatter_to_text, read_text_from_content
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from config.paths import PATHS
from config.user_config import TARGET_DISCORD_USER_ID
from integrations.discord import DiscordIOCore
from prompts.prompts import get_prompt

logger = setup_logger(__name__)


class ResultsNotReadyError(Exception):
    """Raised when user input is not yet available."""
    pass


class MeetingSummaryGenerator(NoteProcessor):
    """Generates meeting summaries with user validation and monthly index updates.
    
    This processor implements a multi-substage workflow:
    
    **Substage 1: AI Summary Generation**
        - Loads transcript with frontmatter
        - Loads monthly index context (with fallback to previous month)
        - Loads People notes for each attendee
        - Generates structured summary via AI
    
    **Substage 2: Form Creation**
        - Prepends generated summary to transcript
        - Adds "Finished" checkbox for user validation
        - Sends Discord notification
    
    **Substage 3: Processing**
        - Waits for user to check "Finished"
        - Replaces form with summary callout
        - Appends summary to monthly meetings index
    
    **Frontmatter Fields**:
        - meeting_summary_pending: True while waiting for user input
    """
    stage_name = "meeting_summarized"
    required_stage = EntityResolver.stage_name
    
    # Form markers
    FORM_START = "<!-- form:meeting_summary:start -->"
    FORM_END = "<!-- form:meeting_summary:end -->"
    SUMMARY_START = "<!-- summary:meeting_summary:start -->"
    SUMMARY_END = "<!-- summary:meeting_summary:end -->"
    
    # Start date for automatic processing (YYYY-MM-DD)
    # Files before this date will be skipped unless they have 'force_meeting_summary' tag
    START_DATE = "2025-12-24"
    
    # Minimum lines to consider an index "sparse" (triggers previous month fallback)
    MIN_INDEX_LINES = 100
    # Maximum lines to load from monthly index
    MAX_INDEX_LINES = 500
    # Maximum lines to load per attendee note
    MAX_ATTENDEE_LINES = 100
    
    def __init__(self, input_dir: Path, discord_io: DiscordIOCore):
        super().__init__(input_dir)
        self.discord_io = discord_io
        self.people_dir = PATHS.people_path

    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        """Additional criteria for processing."""
        source_tags = frontmatter.get("source_tags", [])
        
        # Always process if force tag is present
        if "force_meeting_summary" in source_tags:
            return True
        
        # Always process if already pending user input
        if frontmatter.get('meeting_summary_pending'):
            return True
        
        # Only process meeting category files
        if frontmatter.get('category') != 'meeting':
            return False
        
        # Check date for automatic processing
        file_date = frontmatter.get('date')
        if file_date:
            date_str = str(file_date)
            if date_str < self.START_DATE:
                return False
            
        return True
    
    # ===== Monthly Index Helpers =====
    
    def _get_monthly_index_path(self, date_str: str) -> Path:
        """Get monthly index path for given YYYY-MM-DD date."""
        year_month = date_str[:7]  # "2025-12"
        return PATHS.meetings / f"{year_month} Meetings.md"
    
    def _get_previous_month(self, date_str: str) -> str:
        """Get the previous month's YYYY-MM from a YYYY-MM-DD date."""
        year = int(date_str[:4])
        month = int(date_str[5:7])
        if month == 1:
            return f"{year - 1}-12"
        else:
            return f"{year}-{month - 1:02d}"
    
    def _load_monthly_index(self, meeting_date: str) -> str:
        """Load monthly index content with fallback to previous month if sparse.
        
        Returns up to MAX_INDEX_LINES lines from current month's index.
        If current month has fewer than MIN_INDEX_LINES lines, also includes
        content from the previous month.
        """
        current_index_path = self._get_monthly_index_path(meeting_date)
        content_lines = []
        
        # Try current month
        if current_index_path.exists():
            current_content = current_index_path.read_text(encoding='utf-8')
            current_lines = current_content.strip().split('\n')
            content_lines = current_lines[-self.MAX_INDEX_LINES:]
        
        # If sparse, supplement with previous month
        if len(content_lines) < self.MIN_INDEX_LINES:
            prev_month = self._get_previous_month(meeting_date)
            prev_index_path = PATHS.meetings / f"{prev_month} Meetings.md"
            
            if prev_index_path.exists():
                prev_content = prev_index_path.read_text(encoding='utf-8')
                prev_lines = prev_content.strip().split('\n')
                # Take remaining capacity from previous month
                remaining = self.MAX_INDEX_LINES - len(content_lines)
                prev_portion = prev_lines[-remaining:]
                content_lines = prev_portion + ["\n--- Current Month ---\n"] + content_lines
        
        if not content_lines:
            return "No previous meetings in index."
        
        return '\n'.join(content_lines)
    
    def _ensure_monthly_index_exists(self, meeting_date: str) -> Path:
        """Ensure monthly index file exists, create if needed."""
        from datetime import datetime
        
        index_path = self._get_monthly_index_path(meeting_date)
        month = meeting_date[:7]  # YYYY-MM
        
        if not index_path.exists():
            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            frontmatter = {
                'type': 'meeting_index',
                'month': month,
                'created': now,
                'updated': now,
                'entry_count': 0,
            }
            content = frontmatter_to_text(frontmatter)
            index_path.write_text(content, encoding='utf-8')
            logger.info("Created monthly index: %s", index_path)
        
        return index_path
    
    # ===== Attendee Context Loading =====
    
    def _load_attendee_context(self, speaker_mapping: Dict) -> str:
        """Load truncated context from People notes for each attendee."""
        context_parts = []
        
        # Extract unique person IDs from speaker mapping
        person_ids = set()
        for speaker_data in speaker_mapping.values():
            person_id = speaker_data.get('person_id')
            if person_id:
                person_ids.add(person_id)
        
        for person_id in person_ids:
            person_name = person_id.replace('[[', '').replace(']]', '')
            person_file = self.people_dir / f"{person_name}.md"
            
            if person_file.exists():
                try:
                    content = person_file.read_text(encoding='utf-8')
                    lines = content.split('\n')
                    truncated = '\n'.join(lines[:self.MAX_ATTENDEE_LINES])
                    if len(lines) > self.MAX_ATTENDEE_LINES:
                        truncated += "\n...[truncated]"
                    context_parts.append(f"### {person_name}\n{truncated}")
                except Exception as e:
                    logger.warning("Failed to read People note %s: %s", person_name, e)
            else:
                context_parts.append(f"### {person_name}\n(No notes available)")
        
        if not context_parts:
            return "No attendee context available."
        
        return '\n\n'.join(context_parts)
    
    # ===== Form Generation and Parsing =====
    
    def _generate_form(self, summary: str) -> str:
        """Generate the meeting summary form with checkbox."""
        lines = [
            self.FORM_START,
            "",
            "> [!info] Meeting Summary — Review and edit as needed",
            "",
            summary,
            "",
            "---",
            "",
            "- [ ] Finished <!-- input:finished -->",
            "",
            self.FORM_END,
            "",
        ]
        return '\n'.join(lines)
    
    def _parse_form(self, content: str) -> Optional[Dict[str, Any]]:
        """Parse the meeting summary form from content.
        
        Returns:
            Dict with:
                - 'summary': The summary content (may have been edited by user)
                - 'finished': Boolean
            Returns None if form not found.
        """
        start_idx = content.find(self.FORM_START)
        end_idx = content.find(self.FORM_END)
        
        if start_idx == -1 or end_idx == -1:
            return None
        
        section = content[start_idx:end_idx + len(self.FORM_END)]
        
        # Extract summary (between callout and horizontal rule)
        summary_match = re.search(
            r'>\s*\[!info\].*?\n\n(.*?)\n\n---',
            section,
            re.DOTALL
        )
        summary = summary_match.group(1).strip() if summary_match else ""
        
        # Check finished checkbox
        finished_pattern = r'\[(x|X)\]\s+Finished\s+<!-- input:finished -->'
        finished = bool(re.search(finished_pattern, section))
        
        return {
            'summary': summary,
            'finished': finished,
        }
    
    def _remove_form_section(self, content: str) -> str:
        """Remove form or summary section from content."""
        for start_marker, end_marker in [
            (self.FORM_START, self.FORM_END),
            (self.SUMMARY_START, self.SUMMARY_END),
        ]:
            start_idx = content.find(start_marker)
            end_idx = content.find(end_marker)
            
            if start_idx != -1 and end_idx != -1:
                end_line_idx = content.find('\n', end_idx)
                if end_line_idx == -1:
                    end_line_idx = len(content)
                else:
                    end_line_idx += 1
                
                return content[:start_idx] + content[end_line_idx:]
        
        return content
    
    def _generate_summary_callout(self, summary: str, meeting_date: str) -> str:
        """Generate completion summary callout with link to monthly index."""
        year_month = meeting_date[:7]  # "2025-12"
        index_link = f"[[{year_month} Meetings]]"
        
        lines = [
            self.SUMMARY_START,
            "",
            f"> [!success] Meeting Summary Complete — See {index_link}",
            "",
            summary,
            "",
            self.SUMMARY_END,
            "",
        ]
        return '\n'.join(lines)
    
    # ===== Monthly Index Operations =====
    
    def _parse_monthly_index(self, index_path: Path) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        """Parse monthly index into structured format.
        
        Returns:
            Tuple of (entries dict, frontmatter dict)
            entries: {"[[2025-12-27 Meeting]]": {"date": "2025-12-27", "title": "...", ...}}
        """
        if not index_path.exists():
            return {}, {}
        
        content = index_path.read_text(encoding='utf-8')
        
        # Parse frontmatter if present
        existing_frontmatter = parse_frontmatter_from_content(content) or {}
        body_content = read_text_from_content(content)
        
        entries = {}
        
        # Split content by # or ## headers (each entry starts with # YYYY-MM-DD or ## YYYY-MM-DD)
        # We allow both to support migration from old format
        sections = re.split(r'\n(?=#{1,2} \d{4}-\d{2}-\d{2})', body_content)
        
        for section in sections:
            stripped = section.strip()
            if not stripped or not stripped.startswith('#'):
                continue
            
            # Parse header: # YYYY-MM-DD - Title (or ##)
            header_match = re.match(r'#{1,2} (\d{4}-\d{2}-\d{2}) - (.+)', section)
            if not header_match:
                continue
            
            date = header_match.group(1)
            title = header_match.group(2).strip()
            
            # Parse source link
            source_match = re.search(r'\*Source:\*\s*(\[\[.+?\]\])', section)
            if not source_match:
                continue
            source_link = source_match.group(1)
            
            # Parse optional attendees
            attendees = []
            attendees_match = re.search(r'\*\*Attendees:\*\*\s*(.+?)(?=\n|$)', section)
            if attendees_match:
                attendees = re.findall(r'\[\[[^\]]+\]\]', attendees_match.group(1))
            
            # Parse optional entities
            entities = []
            entities_match = re.search(r'\*\*Mentioned:\*\*\s*(.+?)(?=\n|$)', section)
            if entities_match:
                entities = re.findall(r'\[\[[^\]]+\]\]', entities_match.group(1))
            
            # Parse summary - everything after metadata until --- or end
            lines = section.split('\n')
            summary_lines = []
            in_summary = False
            for line in lines:
                # Skip header, source, attendees, mentioned lines
                if (line.startswith('#') or 
                    line.startswith('*Source:*') or 
                    line.startswith('**Attendees:**') or
                    line.startswith('**Mentioned:**')):
                    continue
                # Stop at --- separator
                if line.strip() == '---':
                    break
                # Start collecting after we pass empty line following metadata
                if in_summary or (line.strip() and not line.startswith('*') and not line.startswith('**')):
                    in_summary = True
                    summary_lines.append(line)
            
            summary = '\n'.join(summary_lines).strip()
            
            entries[source_link] = {
                'date': date,
                'title': title,
                'summary': summary,
                'attendees': attendees,
                'entities': entities,
            }
        
        return entries, existing_frontmatter
    
    def _rebuild_monthly_index(self, index_path: Path, entries: Dict[str, Dict[str, Any]],
                                existing_frontmatter: Dict[str, Any] = None) -> None:
        """Rebuild monthly index file from entries, sorted by date (newest first)."""
        from datetime import datetime
        
        lines = []
        
        # Sort entries by date (newest first)
        sorted_entries = sorted(
            entries.items(),
            key=lambda x: x[1]['date'],
            reverse=True
        )
        
        for source_link, entry in sorted_entries:
            # Build metadata lines
            metadata_lines = []
            if entry.get('attendees'):
                metadata_lines.append(f"**Attendees:** {', '.join(entry['attendees'])}")
            if entry.get('entities'):
                metadata_lines.append(f"**Mentioned:** {', '.join(entry['entities'])}")
            
            lines.extend([
                f"# {entry['date']} - {entry['title']}",
                "",
                f"*Source:* {source_link}",
                "",
            ])
            if metadata_lines:
                lines.extend(metadata_lines)
                lines.append("")
            lines.extend([
                entry['summary'],
                "",
                "---",
                "",
            ])
        
        # Build frontmatter
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        frontmatter = existing_frontmatter.copy() if existing_frontmatter else {}
        frontmatter['updated'] = now
        frontmatter['entry_count'] = len(entries)
        if 'type' not in frontmatter:
            frontmatter['type'] = 'meeting_index'
        
        full_content = frontmatter_to_text(frontmatter) + '\n'.join(lines)
        index_path.write_text(full_content, encoding='utf-8')
    
    def _update_monthly_index(self, summary: str, meeting_date: str, meeting_title: str, 
                               source_link: str, attendees: List[str] = None, 
                               entities: List[str] = None) -> None:
        """Update monthly index with entry, inserting in chronological order.
        
        If an entry with the same source_link exists, it will be overwritten.
        
        Args:
            attendees: List of attendee wikilinks (e.g., ["[[Maxime Fournes]]", ...])
            entities: List of mentioned entity wikilinks
        """
        index_path = self._ensure_monthly_index_exists(meeting_date)
        
        # Parse existing entries and frontmatter
        entries, existing_frontmatter = self._parse_monthly_index(index_path)
        
        # Check if this source already exists (for overwriting)
        if source_link in entries:
            logger.info("Overwriting existing entry for %s in monthly index", source_link)
        
        # Add/update entry
        entries[source_link] = {
            'date': meeting_date,
            'title': meeting_title,
            'summary': summary,
            'attendees': attendees or [],
            'entities': entities or [],
        }
        
        # Rebuild file with sorted entries
        self._rebuild_monthly_index(index_path, entries, existing_frontmatter)
        
        logger.info("Updated monthly index: %s", index_path)
    
    # ===== Main Processing =====
    
    async def process_file(self, filename: str) -> None:
        """Main entry point for processing a file."""
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)
        
        if not frontmatter:
            raise ValueError(f"No frontmatter found in: {filename}")
        
        if frontmatter.get('meeting_summary_pending'):
            await self._substage3_process_results(filename, frontmatter, content)
        else:
            summary = await self._substage1_generate_summary(filename, frontmatter, content)
            await self._substage2_create_form(filename, frontmatter, content, summary)
            raise ResultsNotReadyError(f"Form created, waiting for user input: {filename}")
    
    async def _substage1_generate_summary(
        self, filename: str, frontmatter: Dict, content: str
    ) -> str:
        """Substage 1: Generate meeting summary using AI."""
        logger.info("Generating meeting summary for: %s", filename)
        
        meeting_date = frontmatter.get('date', '')
        speaker_mapping = frontmatter.get('final_speaker_mapping', {})
        
        # Load context
        monthly_index = self._load_monthly_index(str(meeting_date))
        attendee_notes = self._load_attendee_context(speaker_mapping)
        
        # Prepare prompt - use full content (includes frontmatter)
        prompt_template = get_prompt("meeting_summary_ai")
        prompt = prompt_template.format(
            transcript=content,
            monthly_index=monthly_index,
            attendee_notes=attendee_notes,
        )
        
        message = Message(
            role="user",
            content=[MessageContent(type="text", text=prompt)]
        )
        
        # Use tiny model as per requirements
        response = await asyncio.to_thread(self.tiny_ai_model.message, message)
        
        if response.error:
            logger.error("AI error in summary generation: %s", response.error)
            return "Error generating summary. Please write manually."
        
        return response.content.strip() if response.content else ""
    
    async def _substage2_create_form(
        self, filename: str, frontmatter: Dict, content: str, summary: str
    ) -> None:
        """Substage 2: Create form for user validation."""
        logger.info("Creating meeting summary form for: %s", filename)
        
        # Generate form
        form_content = self._generate_form(summary)
        
        # Update frontmatter
        frontmatter['meeting_summary_pending'] = True
        
        # Get transcript (content without frontmatter)
        transcript = read_text_from_content(content)
        
        # Save file: frontmatter + form + transcript
        full_content = frontmatter_to_text(frontmatter) + form_content + transcript
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(self.input_dir / filename, None)
        
        # Send Discord notification
        try:
            meeting_title = frontmatter.get('title', filename)
            dm_text = (
                f"📝 **Meeting Summary Ready for Review**\n"
                f"File: `{filename}`\n"
                f"Meeting: {meeting_title}\n"
                f"Please review, edit if needed, and check 'Finished' in Obsidian."
            )
            await self.discord_io.send_dm(TARGET_DISCORD_USER_ID, dm_text)
            logger.info("Sent Discord notification for: %s", filename)
        except Exception as e:
            logger.warning("Failed to send Discord notification: %s", e)
    
    async def _substage3_process_results(
        self, filename: str, frontmatter: Dict, content: str
    ) -> None:
        """Substage 3: Process user validation."""
        logger.info("Processing meeting summary validation for: %s", filename)
        
        form_data = self._parse_form(content)
        
        if form_data is None:
            raise ValueError(f"Could not parse form in: {filename}")
        
        if not form_data['finished']:
            raise ResultsNotReadyError(f"User has not checked 'Finished' in: {filename}")
        
        summary = form_data['summary']
        meeting_date = str(frontmatter.get('date', ''))
        meeting_title = frontmatter.get('title', filename.replace('.md', ''))
        source_link = f"[[{filename.replace('.md', '')}]]"
        
        # Extract attendees from speaker mapping
        speaker_mapping = frontmatter.get('final_speaker_mapping', {})
        attendees = list(set(
            speaker_data.get('person_id') 
            for speaker_data in speaker_mapping.values() 
            if speaker_data.get('person_id')
        ))
        
        # Extract mentioned entities from resolved_entities (deduplicated)
        resolved_entities = frontmatter.get('resolved_entities', [])
        entities = list(set(
            entity.get('resolved_link') 
            for entity in resolved_entities 
            if entity.get('resolved_link')
        ))
        # Exclude attendees from entities (they're already in attendees list)
        entities = [e for e in entities if e not in attendees]
        
        # Sort for consistent output
        attendees = sorted(attendees)
        entities = sorted(entities)
        
        # Append to monthly index with attendees and entities
        self._update_monthly_index(
            summary, meeting_date, meeting_title, source_link,
            attendees=attendees, entities=entities
        )
        
        # Remove form and add summary callout
        content_without_form = self._remove_form_section(content)
        transcript = read_text_from_content(content_without_form)
        summary_callout = self._generate_summary_callout(summary, meeting_date)
        
        # Update frontmatter
        del frontmatter['meeting_summary_pending']
        
        # Save file
        full_content = frontmatter_to_text(frontmatter) + summary_callout + transcript
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(self.input_dir / filename, None)
        
        logger.info("Completed meeting summary for: %s", filename)
    
    async def reset(self, filename: str) -> None:
        """Reset meeting summary for a file."""
        logger.info(f"Resetting meeting summary for: {filename}")
        
        file_path = self.input_dir / filename
        if not file_path.exists():
            logger.error(f"File not found: {filename}")
            return
        
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)
        
        if not frontmatter:
            return
        
        # Remove form/summary section
        content_without_form = self._remove_form_section(content)
        transcript = read_text_from_content(content_without_form)
        
        # TODO: Remove entry from monthly index if it was appended
        
        # Clean frontmatter
        keys_to_remove = ['meeting_summary_pending']
        cleaned_frontmatter = {k: v for k, v in frontmatter.items() if k not in keys_to_remove}
        
        # Remove stage from processing_stages
        if self.stage_name in cleaned_frontmatter.get('processing_stages', []):
            cleaned_frontmatter['processing_stages'].remove(self.stage_name)
        
        # Save
        full_content = frontmatter_to_text(cleaned_frontmatter) + transcript
        async with aiofiles.open(file_path, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(file_path, None)
        
        logger.info(f"Reset complete for: {filename}")
