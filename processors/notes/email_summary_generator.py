"""
Email Summary Generator Processor

Generates AI-powered summaries for daily email digests,
then appends summaries to a monthly email index file.

Unlike MeetingSummaryGenerator, this processor auto-approves summaries
without user validation forms.
"""

from pathlib import Path
from typing import Dict, Any, Optional, List
import asyncio
import aiofiles
import re

from ai_core import AI, Message, MessageContent
from .base import NoteProcessor
from ..common.frontmatter import (
    read_frontmatter_from_file,
    set_frontmatter_in_file,
    frontmatter_to_text,
    read_text_from_content,
    parse_frontmatter_from_content,
)
from config.logging_config import setup_logger
from config.paths import PATHS
from config.services_config import BIG_MODEL
from prompts.prompts import get_prompt

logger = setup_logger(__name__)


class EmailSummaryGenerator(NoteProcessor):
    """Generates email summaries and updates monthly email index.
    
    This processor implements a single-step workflow (no user validation):
    
    1. Read email digest content
    2. Generate summary using AI
    3. Extract participants and entities from content
    4. Update monthly email index
    5. Add summary callout to the original file
    """
    stage_name = "email_summary_generated"
    required_stage = "entities_resolved"  # Run after entity resolution
    
    # Minimum lines to consider previous month context
    MIN_INDEX_LINES = 50
    MAX_INDEX_LINES = 500
    
    # Summary markers
    SUMMARY_START = "<!-- summary:email:start -->"
    SUMMARY_END = "<!-- summary:email:end -->"
    
    def __init__(self, input_dir: Path, index_dir: Path = None):
        super().__init__(input_dir)
        self.index_dir = index_dir or input_dir
        self.ai_model = AI(BIG_MODEL)
    
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        """Additional criteria for processing."""
        # Only process email category files
        if frontmatter.get('category') != 'email':
            return False
        
        # Skip files starting with underscore (triage log, index files)
        if filename.startswith('_'):
            return False
        
        # Skip index files
        if 'Index' in filename:
            return False
        
        return True
    
    # ===== Monthly Index Management =====
    
    def _get_monthly_index_path(self, date_str: str) -> Path:
        """Get monthly index path for given YYYY-MM-DD date."""
        month = date_str[:7]  # YYYY-MM
        return self.index_dir / f"{month} Email Index.md"
    
    def _get_previous_month(self, date_str: str) -> str:
        """Get the previous month's YYYY-MM from a YYYY-MM-DD date."""
        from datetime import datetime, timedelta
        date = datetime.strptime(date_str[:7], "%Y-%m")
        # Go back one month
        first_of_month = date.replace(day=1)
        last_of_prev = first_of_month - timedelta(days=1)
        return last_of_prev.strftime("%Y-%m")
    
    def _load_monthly_index(self, email_date: str) -> str:
        """Load monthly index content with fallback to previous month if sparse."""
        index_path = self._get_monthly_index_path(email_date)
        
        content = ""
        if index_path.exists():
            content = index_path.read_text(encoding='utf-8')
        
        lines = content.split('\n')
        
        # If current month is sparse, include previous month for context
        if len(lines) < self.MIN_INDEX_LINES:
            prev_month = self._get_previous_month(email_date)
            prev_path = self.index_dir / f"{prev_month} Email Index.md"
            if prev_path.exists():
                prev_content = prev_path.read_text(encoding='utf-8')
                content = f"## Previous Month ({prev_month})\n\n{prev_content}\n\n---\n\n{content}"
        
        # Truncate if too long
        lines = content.split('\n')
        if len(lines) > self.MAX_INDEX_LINES:
            content = '\n'.join(lines[-self.MAX_INDEX_LINES:])
        
        return content
    
    def _ensure_monthly_index_exists(self, email_date: str) -> Path:
        """Ensure monthly index file exists, create if needed."""
        from datetime import datetime
        
        index_path = self._get_monthly_index_path(email_date)
        month = email_date[:7]  # YYYY-MM
        
        if not index_path.exists():
            self.index_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            frontmatter = {
                'type': 'email_index',
                'month': month,
                'created': now,
                'updated': now,
                'entry_count': 0,
            }
            content = frontmatter_to_text(frontmatter)
            index_path.write_text(content, encoding='utf-8')
            logger.info("Created monthly email index: %s", index_path)
        
        return index_path
    
    def _parse_monthly_index(self, index_path: Path) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        """Parse monthly index into structured format.
        
        Returns:
            Tuple of (entries dict, frontmatter dict)
        """
        if not index_path.exists():
            return {}, {}
        
        content = index_path.read_text(encoding='utf-8')
        
        # Parse frontmatter if present
        existing_frontmatter = parse_frontmatter_from_content(content) or {}
        body_content = read_text_from_content(content)
        
        entries = {}
        
        # Split by H1 headers with date pattern (# YYYY-MM-DD - ...)
        # This preserves other H1s like "# Email Digest - ..." as part of the content
        sections = re.split(r'^# (\d{4}-\d{2}-\d{2})\s*-\s*', body_content, flags=re.MULTILINE)
        
        # sections[0] is content before first match (empty or garbage)
        # sections[1] is date from first match, sections[2] is content after first match
        # sections[3] is date from second match, sections[4] is content, etc.
        
        for i in range(1, len(sections), 2):
            if i + 1 >= len(sections):
                break
            
            date = sections[i]
            section_content = sections[i + 1]
            
            lines = section_content.strip().split('\n')
            if not lines:
                continue
            
            # First line is the rest of the title (e.g., "2 threads, 4 emails")
            title = lines[0].strip()
            
            # Find source link, participants, entities
            source_link = None
            participants = []
            entities = []
            summary_lines = []
            
            # Track when we've passed the metadata section
            in_summary = False
            
            for line in lines[1:]:
                if line == '---':
                    break
                elif line.startswith('*Source:*'):
                    source_link = line.replace('*Source:*', '').strip()
                elif line.startswith('**Participants:**'):
                    participants = self._parse_wikilinks(line)
                elif line.startswith('**Mentioned:**'):
                    entities = self._parse_wikilinks(line)
                elif line.startswith('# ') or line.startswith('## '):
                    # Summary content starts with H1/H2 headers
                    in_summary = True
                    summary_lines.append(line)
                elif in_summary:
                    summary_lines.append(line)
                elif line.strip() == '':
                    # Empty lines before summary starts - skip
                    continue
                elif not source_link:
                    # Still in metadata section, skip
                    continue
                else:
                    # Content after metadata - treat as summary start
                    in_summary = True
                    summary_lines.append(line)
            
            if source_link:
                entries[source_link] = {
                    'date': date,
                    'title': title,
                    'summary': '\n'.join(summary_lines).strip(),
                    'participants': participants,
                    'entities': entities,
                }
        
        return entries, existing_frontmatter
    
    def _parse_wikilinks(self, text: str) -> List[str]:
        """Extract wikilinks from text."""
        return re.findall(r'\[\[[^\]]+\]\]', text)
    
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
            if entry.get('participants'):
                metadata_lines.append(f"**Participants:** {', '.join(entry['participants'])}")
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
            frontmatter['type'] = 'email_index'
        
        full_content = frontmatter_to_text(frontmatter) + '\n'.join(lines)
        index_path.write_text(full_content, encoding='utf-8')
    
    def _update_monthly_index(self, summary: str, email_date: str, title: str,
                               source_link: str, participants: List[str] = None,
                               entities: List[str] = None) -> None:
        """Update monthly index with entry."""
        index_path = self._ensure_monthly_index_exists(email_date)
        
        # Parse existing entries and frontmatter
        entries, existing_frontmatter = self._parse_monthly_index(index_path)
        
        # Check if this source already exists
        if source_link in entries:
            logger.info("Overwriting existing entry for %s in monthly index", source_link)
        
        # Add/update entry
        entries[source_link] = {
            'date': email_date,
            'title': title,
            'summary': summary,
            'participants': participants or [],
            'entities': entities or [],
        }
        
        # Rebuild file with sorted entries
        self._rebuild_monthly_index(index_path, entries, existing_frontmatter)
        
        logger.info("Updated monthly email index: %s", index_path)
    
    # ===== Content Extraction =====
    
    def _extract_participants(self, content: str) -> List[str]:
        """Extract participant wikilinks from email digest content."""
        participants = set()
        
        # Look for *From:* and *To:* lines
        for line in content.split('\n'):
            if '*From:*' in line or '*To:*' in line:
                # Extract any wikilinks in the line
                links = self._parse_wikilinks(line)
                participants.update(links)
        
        return sorted(list(participants))
    
    def _extract_entities(self, content: str) -> List[str]:
        """Extract all entity wikilinks from content (excluding participants)."""
        # Find all wikilinks
        all_links = set(self._parse_wikilinks(content))
        
        # Remove participant links (already captured separately)
        participants = set(self._extract_participants(content))
        entities = all_links - participants
        
        return sorted(list(entities))
    
    def _build_title(self, frontmatter: Dict) -> str:
        """Build index entry title from frontmatter."""
        email_count = frontmatter.get('email_count', 0)
        thread_count = frontmatter.get('thread_count', 0)
        return f"{thread_count} threads, {email_count} emails"
    
    # ===== Summary Generation =====
    
    async def _generate_summary(self, content: str, monthly_context: str) -> str:
        """Generate email summary using AI."""
        prompt_template = get_prompt("email_summary")
        prompt = prompt_template.replace("{email_content}", content)
        prompt = prompt.replace("{monthly_context}", monthly_context or "No previous context.")
        
        message = Message(
            role="user",
            content=[MessageContent(type="text", text=prompt)]
        )
        
        try:
            response = await asyncio.to_thread(self.ai_model.message, message)
            
            if response.error:
                logger.error("AI error in email summary generation: %s", response.error)
                return "Error generating summary."
            
            return response.content.strip()
            
        except Exception as e:
            logger.error("Error generating email summary: %s", e)
            return "Error generating summary."
    
    def _generate_summary_callout(self, summary: str, email_date: str) -> str:
        """Generate summary callout for the digest file."""
        index_link = f"[[{email_date[:7]} Email Index]]"
        
        return f"""{self.SUMMARY_START}
> [!summary] Email Summary
> *Added to* {index_link}

{summary}
{self.SUMMARY_END}
"""
    
    def _remove_summary_section(self, content: str) -> str:
        """Remove existing summary section from content."""
        pattern = re.escape(self.SUMMARY_START) + r'.*?' + re.escape(self.SUMMARY_END)
        return re.sub(pattern, '', content, flags=re.DOTALL).strip()
    
    # ===== Main Processing =====
    
    async def process_file(self, filename: str) -> None:
        """Main entry point for processing a file."""
        logger.info("Generating email summary for: %s", filename)
        
        file_path = self.input_dir / filename
        
        # Read file
        async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
            content = await f.read()
        
        frontmatter = parse_frontmatter_from_content(content) or {}
        text_content = read_text_from_content(content)
        email_date = str(frontmatter.get('date', ''))[:10]
        
        if not email_date:
            logger.warning("No date in frontmatter for %s, skipping", filename)
            return
        
        # Load monthly context
        monthly_context = self._load_monthly_index(email_date)
        
        # Generate summary
        summary = await self._generate_summary(text_content, monthly_context)
        
        # Extract participants and entities
        participants = self._extract_participants(text_content)
        entities = self._extract_entities(text_content)
        
        # Build source link
        source_link = f"[[{filename.replace('.md', '')}]]"
        title = self._build_title(frontmatter)
        
        # Update monthly index
        self._update_monthly_index(
            summary=summary,
            email_date=email_date,
            title=title,
            source_link=source_link,
            participants=participants,
            entities=entities,
        )
        
        # Add summary callout to original file
        clean_content = self._remove_summary_section(text_content)
        summary_callout = self._generate_summary_callout(summary, email_date)
        
        # Insert summary after the first heading
        lines = clean_content.split('\n')
        insert_idx = 0
        for i, line in enumerate(lines):
            if line.startswith('# '):
                insert_idx = i + 1
                break
        
        lines.insert(insert_idx, '\n' + summary_callout + '\n')
        new_content = '\n'.join(lines)
        
        # Write back with updated content
        full_content = frontmatter_to_text(frontmatter) + new_content
        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
            await f.write(full_content)
        
        logger.info("Email summary generated for: %s", filename)
