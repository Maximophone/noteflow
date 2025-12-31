"""
Email Digest Processor

Fetches important emails daily and creates self-contained digest files
with embedded thread context for downstream processing.
"""

import json
import os
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

import aiofiles

from .base import NoteProcessor
from ..common.frontmatter import frontmatter_to_text
from ai_core import AI
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from config.paths import PATHS
from config.services_config import SMALL_MODEL
from integrations.gmail_utils import GmailUtils
from prompts.prompts import get_prompt

logger = setup_logger(__name__)


class EmailDigestProcessor:
    """
    Fetches important emails and creates daily digest files.
    
    This processor does NOT extend NoteProcessor because:
    1. It doesn't process existing files - it creates new ones
    2. It uses state-based scheduling (skip if already run today)
    3. It fetches from external API (Gmail) rather than local files
    
    Output files are created in the Email Digests folder and can be
    processed by EntityResolver and InteractionLogger.
    """
    
    # Importance threshold (1-10 scale, include if >= this value)
    IMPORTANCE_THRESHOLD = 5
    
    # Maximum thread context messages to embed
    MAX_THREAD_CONTEXT = 10
    
    # Maximum body length per email (characters) - high to avoid mid-sentence cuts
    MAX_BODY_LENGTH = 10000
    
    # Earliest date to process (floor) - won't fetch emails before this
    EARLIEST_DATE = datetime(2025, 12, 19, 0, 0, 0)
    
    def __init__(self, 
                 output_dir: Optional[Path] = None,
                 state_file: Optional[Path] = None,
                 overwrite_existing: bool = False):
        self.output_dir = output_dir or PATHS.email_digests
        self.state_file = state_file or PATHS.email_state
        self.overwrite_existing = overwrite_existing
        self.gmail = GmailUtils()
        self.ai_model = AI(SMALL_MODEL)
        self._regeneration_mode = False  # Set True in process_all if state was deleted
        
        # Ensure directories exist
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
    
    def _load_state(self) -> Dict[str, Any]:
        """Load processor state from file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Error loading state file: {e}")
        return {}
    
    def _save_state(self, state: Dict[str, Any]) -> None:
        """Save processor state to file."""
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving state file: {e}")
    
    def _should_run_today(self) -> bool:
        """Check if processor has already run today."""
        state = self._load_state()
        last_run = state.get('last_run_date')
        
        if not last_run:
            return True
        
        today = datetime.now().strftime('%Y-%m-%d')
        return last_run != today
    
    def _get_last_run_timestamp(self) -> datetime:
        """Get timestamp of last run, respecting EARLIEST_DATE floor."""
        state = self._load_state()
        last_timestamp = state.get('last_run_timestamp')
        
        if last_timestamp:
            try:
                saved_time = datetime.fromisoformat(last_timestamp)
                # Ensure we never go before EARLIEST_DATE
                return max(saved_time, self.EARLIEST_DATE)
            except Exception:
                pass
        
        # Default to EARLIEST_DATE (first run)
        return self.EARLIEST_DATE
    
    async def process_all(self) -> None:
        """Main entry point - fetch and process emails."""
        if not self._should_run_today():
            logger.debug("Email digest already processed today, skipping")
            return
        
        logger.info("Starting email digest processing")
        
        # Check regeneration mode at runtime (detect if state file was deleted)
        regeneration_mode = not self.state_file.exists()
        if regeneration_mode:
            logger.info("State file missing - entering regeneration mode (will overwrite existing files)")
        self._regeneration_mode = regeneration_mode
        
        # Triage log entries: list of (email_summary, decision, reason)
        triage_log: List[Dict[str, str]] = []
        
        try:
            # Get emails since last run
            since = self._get_last_run_timestamp()
            logger.info(f"Fetching emails since {since}")
            
            emails = await asyncio.to_thread(
                self.gmail.get_emails_since, since
            )
            
            if not emails:
                logger.info("No new emails found")
                self._update_state_after_run()
                return
            
            logger.info(f"Found {len(emails)} emails")
            
            # Pre-filter: remove promotional categories and automated emails
            filtered_emails, prefilter_triage = self._pre_filter_emails(emails)
            triage_log.extend(prefilter_triage)
            logger.info(f"After pre-filter: {len(filtered_emails)} emails")
            
            if not filtered_emails:
                logger.info("No emails remaining after pre-filter")
                await self._write_triage_log(triage_log, since)
                self._update_state_after_run()
                return
            
            # AI importance scoring
            important_emails, scoring_triage = await self._score_importance(filtered_emails)
            triage_log.extend(scoring_triage)
            logger.info(f"After importance scoring: {len(important_emails)} important emails")
            
            # Write triage log
            await self._write_triage_log(triage_log, since)
            
            if not important_emails:
                logger.info("No important emails found")
                self._update_state_after_run()
                return
            
            # Group by date and create digest files
            await self._create_digest_files(important_emails)
            
            # Update state
            self._update_state_after_run()
            
            logger.info("Email digest processing complete")
            
        except Exception as e:
            logger.error(f"Error in email digest processing: {e}", exc_info=True)
    
    def _update_state_after_run(self) -> None:
        """Update state file after successful run."""
        state = self._load_state()
        now = datetime.now()
        state['last_run_date'] = now.strftime('%Y-%m-%d')
        state['last_run_timestamp'] = now.isoformat()
        self._save_state(state)
    
    def _pre_filter_emails(self, emails: List[Dict]) -> tuple[List[Dict], List[Dict]]:
        """Pre-filter emails by category and automated detection.
        
        Returns:
            Tuple of (filtered_emails, triage_entries)
        """
        filtered = []
        triage = []
        
        for email in emails:
            email_summary = self._email_summary(email)
            
            # Skip promotional/social/updates categories
            if self.gmail.is_filtered_category(email):
                labels = email.get('labels', [])
                category = next((l for l in labels if l.startswith('CATEGORY_')), 'unknown')
                triage.append({
                    **email_summary,
                    'decision': '❌ FILTERED',
                    'stage': 'pre-filter',
                    'reason': f"Gmail category: {category}"
                })
                continue
            
            # Skip automated emails
            is_automated, auto_reason = self.gmail.is_automated_email(email)
            if is_automated:
                triage.append({
                    **email_summary,
                    'decision': '❌ FILTERED',
                    'stage': 'pre-filter',
                    'reason': f"Automated: {auto_reason}"
                })
                continue
            
            # Passed pre-filter
            triage.append({
                **email_summary,
                'decision': '✅ PASSED',
                'stage': 'pre-filter',
                'reason': "Passed pre-filter checks"
            })
            filtered.append(email)
        
        return filtered, triage
    
    def _email_summary(self, email: Dict) -> Dict[str, str]:
        """Create a summary dict for triage logging."""
        email_dt = self.gmail.format_datetime(email.get('date', ''))
        date_str = email_dt.strftime('%Y-%m-%d %H:%M') if email_dt else 'unknown'
        return {
            'date': date_str,
            'from': email.get('from', '')[:50],
            'to': email.get('to', '')[:50],
            'subject': email.get('subject', '')[:60],
        }
    
    async def _write_triage_log(self, triage: List[Dict], since: datetime) -> None:
        """Write triage log to markdown file for inspection."""
        if not triage:
            return
        
        # Group by date
        by_date: Dict[str, List[Dict]] = {}
        for entry in triage:
            date_str = entry.get('date', 'unknown')[:10]  # Just the date part
            if date_str not in by_date:
                by_date[date_str] = []
            by_date[date_str].append(entry)
        
        # Build log content
        lines = [
            f"# Email Triage Log",
            f"",
            f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
            f"*Processing emails since: {since.strftime('%Y-%m-%d %H:%M')}*",
            f"",
            f"## Summary",
            f"",
        ]
        
        # Count decisions
        included = sum(1 for t in triage if '✅' in t.get('decision', ''))
        filtered = sum(1 for t in triage if '❌' in t.get('decision', ''))
        warnings = sum(1 for t in triage if '⚠️' in t.get('decision', ''))
        
        lines.extend([
            f"- **Total emails:** {len(triage)}",
            f"- **Included:** {included}",
            f"- **Filtered:** {filtered}",
            f"- **Warnings:** {warnings}",
            f"",
            f"---",
            f"",
        ])
        
        # Details by date
        for date_str in sorted(by_date.keys(), reverse=True):
            entries = by_date[date_str]
            lines.append(f"## {date_str}")
            lines.append("")
            lines.append("| Decision | Stage | From | Subject | Reason |")
            lines.append("|----------|-------|------|---------|--------|")
            
            for entry in entries:
                decision = entry.get('decision', '?')
                stage = entry.get('stage', '?')
                from_addr = entry.get('from', '')[:30].replace('|', '/')
                subject = entry.get('subject', '')[:40].replace('|', '/')
                reason = entry.get('reason', '')[:50].replace('|', '/')
                lines.append(f"| {decision} | {stage} | {from_addr} | {subject} | {reason} |")
            
            lines.append("")
        
        # Write to file
        log_path = self.output_dir / "_Email Triage Log.md"
        async with aiofiles.open(log_path, 'w', encoding='utf-8') as f:
            await f.write('\n'.join(lines))
        
        logger.info(f"Wrote triage log: {log_path}")
    
    async def _score_importance(self, emails: List[Dict]) -> tuple[List[Dict], List[Dict]]:
        """Use AI to score email importance and filter by threshold.
        
        Returns:
            Tuple of (important_emails, triage_entries)
        """
        if not emails:
            return [], []
        
        triage = []
        
        # Prepare email summaries for AI
        email_summaries = []
        for email in emails:
            email_summaries.append({
                'email_id': email['id'],
                'from': email.get('from', ''),
                'to': email.get('to', ''),
                'subject': email.get('subject', ''),
                'snippet': email.get('snippet', '')[:200],
            })
        
        # Get importance prompt
        prompt_template = get_prompt("email_importance")
        prompt = prompt_template.format(emails=json.dumps(email_summaries, indent=2))
        
        message = Message(
            role="user",
            content=[MessageContent(type="text", text=prompt)]
        )
        
        try:
            response = await asyncio.to_thread(self.ai_model.message, message)
            
            if response.error:
                logger.error(f"AI error in importance scoring: {response.error}")
                # Fallback: include all emails with error note
                for email in emails:
                    triage.append({
                        **self._email_summary(email),
                        'decision': '⚠️ INCLUDED',
                        'stage': 'ai-scoring',
                        'reason': f"AI error, included by default: {response.error}"
                    })
                return emails, triage
            
            # Parse JSON response
            content = response.content.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            scores = json.loads(content)
            
            # Build ID to score mapping
            score_map = {item['email_id']: item for item in scores}
            
            # Filter by threshold
            important = []
            for email in emails:
                score_data = score_map.get(email['id'], {})
                score = score_data.get('score', 5)  # Default to threshold
                ai_reason = score_data.get('reason', 'No reason provided')
                
                email_summary = self._email_summary(email)
                
                if score >= self.IMPORTANCE_THRESHOLD:
                    email['importance_score'] = score
                    email['importance_reason'] = ai_reason
                    important.append(email)
                    triage.append({
                        **email_summary,
                        'decision': f'✅ INCLUDED (score: {score})',
                        'stage': 'ai-scoring',
                        'reason': ai_reason
                    })
                else:
                    triage.append({
                        **email_summary,
                        'decision': f'❌ FILTERED (score: {score})',
                        'stage': 'ai-scoring',
                        'reason': ai_reason
                    })
            
            return important, triage
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse importance scores: {e}")
            # Fallback: include all emails
            for email in emails:
                triage.append({
                    **self._email_summary(email),
                    'decision': '⚠️ INCLUDED',
                    'stage': 'ai-scoring',
                    'reason': f"JSON parse error, included by default"
                })
            return emails, triage
        except Exception as e:
            logger.error(f"Error in importance scoring: {e}")
            for email in emails:
                triage.append({
                    **self._email_summary(email),
                    'decision': '⚠️ INCLUDED',
                    'stage': 'ai-scoring',
                    'reason': f"Error, included by default: {e}"
                })
            return emails, triage
    
    async def _create_digest_files(self, emails: List[Dict]) -> None:
        """Create daily digest files with thread context.
        
        Only processes emails from COMPLETED days (not today).
        Today's emails will be processed tomorrow to ensure we capture
        all emails for a given day.
        """
        today = datetime.now().strftime('%Y-%m-%d')
        
        # Group emails by date
        by_date: Dict[str, List[Dict]] = {}
        
        for email in emails:
            email_dt = self.gmail.format_datetime(email.get('date', ''))
            if email_dt:
                date_key = email_dt.strftime('%Y-%m-%d')
            else:
                date_key = datetime.now().strftime('%Y-%m-%d')
            
            if date_key not in by_date:
                by_date[date_key] = []
            by_date[date_key].append(email)
        
        # Filter out today's emails - they'll be processed tomorrow
        if today in by_date:
            today_count = len(by_date[today])
            logger.info(f"Deferring {today_count} emails from today ({today}) to next run")
            del by_date[today]
        
        if not by_date:
            logger.info("No emails from completed days to process")
            return
        
        # Create a digest file for each date
        for date_str, date_emails in sorted(by_date.items()):
            await self._create_single_digest(date_str, date_emails)
    
    async def _create_single_digest(self, date_str: str, emails: List[Dict]) -> None:
        """Create a single digest file for a specific date."""
        filename = f"{date_str} Emails.md"
        filepath = self.output_dir / filename
        
        # Skip if file exists and we're not overwriting
        # Regeneration mode (state file was deleted) also enables overwriting
        should_overwrite = self.overwrite_existing or self._regeneration_mode
        if filepath.exists() and not should_overwrite:
            logger.info(f"Digest already exists for {date_str}, skipping")
            return
        
        # Group emails by thread
        threads: Dict[str, List[Dict]] = {}
        for email in emails:
            thread_id = email.get('thread_id', email['id'])
            if thread_id not in threads:
                threads[thread_id] = []
            threads[thread_id].append(email)
        
        # Build frontmatter
        frontmatter = {
            'date': date_str,
            'category': 'email',
            'email_count': len(emails),
            'thread_count': len(threads),
            'processing_stages': ['email_digest_created'],
        }
        
        # Build content
        content_parts = [
            frontmatter_to_text(frontmatter),
            f"# Email Digest - {date_str}\n\n",
        ]
        
        # Process each thread
        for thread_id, thread_emails in threads.items():
            content_parts.append(await self._format_thread(thread_id, thread_emails, date_str))
        
        # Write file
        content = ''.join(content_parts)
        
        async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
            await f.write(content)
        
        logger.info(f"Created digest: {filename} ({len(emails)} emails in {len(threads)} threads)")
    
    async def _format_thread(self, thread_id: str, new_emails: List[Dict], digest_date: str) -> str:
        """Format a thread with all messages, marking new ones."""
        lines = []
        
        # Get full thread context
        try:
            all_messages = await asyncio.to_thread(
                self.gmail.get_thread_messages,
                thread_id,
                self.MAX_THREAD_CONTEXT
            )
        except Exception as e:
            logger.warning(f"Error fetching thread {thread_id}: {e}")
            all_messages = new_emails
        
        if not all_messages:
            all_messages = new_emails
        
        # Filter to only messages on or before the digest date
        # This prevents future messages from appearing in earlier digests
        filtered_messages = []
        for msg in all_messages:
            msg_dt = self.gmail.format_datetime(msg.get('date', ''))
            if msg_dt:
                msg_date_str = msg_dt.strftime('%Y-%m-%d')
                if msg_date_str <= digest_date:
                    filtered_messages.append(msg)
            else:
                # If we can't parse the date, include it (safer default)
                filtered_messages.append(msg)
        
        all_messages = filtered_messages if filtered_messages else new_emails
        
        # Get IDs of new emails (the ones from today that passed filtering)
        new_email_ids = {e['id'] for e in new_emails}
        
        # Get thread subject from first message
        first_msg = all_messages[0] if all_messages else new_emails[0]
        subject = first_msg.get('subject', '(No Subject)')
        
        # Collect all participants with their emails
        participants_info = []  # List of (name, email) tuples
        seen_emails = set()
        for msg in all_messages:
            from_parsed = self.gmail.parse_email_address(msg.get('from', ''))
            to_parsed = self.gmail.parse_email_address(msg.get('to', ''))
            for parsed in [from_parsed, to_parsed]:
                email = parsed.get('email', '')
                if email and email not in seen_emails:
                    seen_emails.add(email)
                    name = parsed.get('name', '')
                    participants_info.append((name, email))
        
        # Format participants
        participant_strs = []
        for name, email in sorted(participants_info, key=lambda x: x[0] or x[1]):
            if name:
                participant_strs.append(f"{name} ({email})")
            else:
                participant_strs.append(email)
        
        # Thread header with clear subject
        lines.append(f"## Subject: {subject}\n")
        if participant_strs:
            lines.append(f"*Participants: {', '.join(participant_strs)}*\n\n")
        else:
            lines.append("\n")
        
        # Messages in reverse chronological order (newest first)
        for msg in reversed(all_messages):
            is_new = msg['id'] in new_email_ids
            lines.append(self._format_single_message(msg, is_new))
        
        lines.append("---\n\n")
        
        return ''.join(lines)
    
    def _strip_quoted_content(self, body: str) -> str:
        """Strip quoted email content from body.
        
        Removes content after patterns like:
        - "On Mon, Dec 23, 2025 at 1:56 PM John wrote:"
        - Lines starting with > (quoted text)
        """
        import re
        
        # Pattern 1: "On [date], [name] <email> wrote:" - cuts here
        # Match "On" followed by date-like content up to "wrote:" 
        on_wrote_pattern = r'\n+On [^<\n]*<[^>]+> wrote:\s*$'
        match = re.search(on_wrote_pattern, body, re.MULTILINE | re.IGNORECASE)
        if match:
            body = body[:match.start()].rstrip()
        
        # Pattern 2: "On [weekday], [date], [name] <email> wrote:" with newline
        on_wrote_pattern2 = r'\n+On [A-Za-z]{3}, [^\n]*wrote:\s*$'
        match = re.search(on_wrote_pattern2, body, re.MULTILINE | re.IGNORECASE)
        if match:
            body = body[:match.start()].rstrip()
        
        # Pattern 3: Remove lines starting with > (quoted text)
        # Only if they appear consecutively after a blank line
        lines = body.split('\n')
        result_lines = []
        in_quote_block = False
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            if stripped.startswith('>'):
                in_quote_block = True
                continue  # Skip quoted lines
            
            if in_quote_block and stripped == '':
                continue  # Skip blank lines within quote block
            
            if in_quote_block and not stripped.startswith('>'):
                # Check if this looks like a quote header
                if stripped.startswith('On ') and 'wrote:' in stripped.lower():
                    continue
                in_quote_block = False
            
            result_lines.append(line)
        
        return '\n'.join(result_lines).rstrip()
    
    def _format_single_message(self, msg: Dict, is_new: bool) -> str:
        """Format a single message within a thread."""
        lines = []
        
        # Parse sender and recipient
        from_parsed = self.gmail.parse_email_address(msg.get('from', ''))
        to_parsed = self.gmail.parse_email_address(msg.get('to', ''))
        
        sender_name = from_parsed.get('name') or from_parsed.get('email', 'Unknown')
        sender_email = from_parsed.get('email', '')
        recipient_email = to_parsed.get('email', '')
        recipient_name = to_parsed.get('name', '')
        
        # Format datetime
        msg_dt = self.gmail.format_datetime(msg.get('date', ''))
        if msg_dt:
            date_str = msg_dt.strftime('%b %d, %H:%M')
        else:
            date_str = ''
        
        # Visual indicator for new messages
        new_marker = "🆕 " if is_new else ""
        
        # Message header with sender name and date
        lines.append(f"### {new_marker}{sender_name} — {date_str}\n")
        
        # From/To line with email addresses
        if recipient_name:
            lines.append(f"*From:* {sender_email} → *To:* {recipient_name} ({recipient_email})\n")
        else:
            lines.append(f"*From:* {sender_email} → *To:* {recipient_email}\n")
        
        # Attachments
        attachments = msg.get('attachments', [])
        if attachments:
            attachment_strs = []
            for att in attachments:
                size_bytes = att.get('size', 0)
                # Human-readable size
                if size_bytes >= 1_000_000:
                    size_str = f"{size_bytes / 1_000_000:.1f} MB"
                elif size_bytes >= 1_000:
                    size_str = f"{size_bytes / 1_000:.1f} KB"
                else:
                    size_str = f"{size_bytes} B"
                attachment_strs.append(f"{att.get('filename', 'unknown')} ({size_str})")
            lines.append(f"*Attachments:* {', '.join(attachment_strs)}\n")
        
        lines.append("\n")
        
        # Message body - strip quoted content first
        body = msg.get('body', msg.get('snippet', ''))
        if body:
            body = self._strip_quoted_content(body)
            
            # Truncate if too long
            if len(body) > self.MAX_BODY_LENGTH:
                body = body[:self.MAX_BODY_LENGTH] + "\n\n*[Message truncated]*"
            
            lines.append(f"{body}\n\n")
        
        return ''.join(lines)

