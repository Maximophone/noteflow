from pathlib import Path
from typing import Dict, Any, List, Tuple
import aiofiles
import os
import re
import asyncio
import datetime
import logging
from collections import defaultdict

from .base import NoteProcessor
from ..common.frontmatter import read_text_from_content, parse_frontmatter_from_content, frontmatter_to_text
from ai_core import AI
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from config.paths import PATHS
from .speaker_identifier import SpeakerIdentifier
from .entity_resolver import EntityResolver
from .meeting_summary_generator import MeetingSummaryGenerator
from prompts.prompts import get_prompt

import traceback

logger = setup_logger(__name__)

class InteractionLogger(NoteProcessor):
    """Processes meetings/emails and adds AI-generated logs to each person's note.
    
    Supports two modes:
    - Meetings: Uses final_speaker_mapping for participants, processes speakers + mentions
    - Emails: Extracts participants from From/To lines, processes correspondents + mentions
    
    For emails, instance must have custom required_stage set.
    """
    stage_name = "interactions_logged"
    required_stage = MeetingSummaryGenerator.stage_name  # Override via instance for emails

    def __init__(self, input_dir: Path):
        super().__init__(input_dir)
        self.people_dir = PATHS.people_path
        
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        category = frontmatter.get('category', '').lower()
        
        if category == 'meeting':
            # Meetings require speaker mapping
            if 'final_speaker_mapping' not in frontmatter:
                return False
        elif category == 'email':
            # Emails don't need speaker mapping, participants extracted from content
            pass
        else:
            return False
        
        return True
    
    def _extract_email_participants(self, content: str) -> List[str]:
        """Extract participant wikilinks from From/To lines in email digest."""
        participants = set()
        for line in content.split('\n'):
            if '*From:*' in line or '*To:*' in line:
                links = re.findall(r'\[\[[^\]]+\]\]', line)
                participants.update(links)
        return sorted(list(participants))

    async def _find_ai_logs_section(self, content: str) -> Tuple[bool, int, str]:
        """Find the AI Logs section in a note."""
        match = re.search(r'^# AI Logs\s*$', content, re.MULTILINE)
        
        if not match:
            return False, len(content), content
        
        return True, match.start(), content[:match.start()]
    
    async def _parse_existing_logs(self, content: str) -> Dict[str, List[Dict[str, Any]]]:
        """Parse existing AI logs into a structured format."""
        section_exists, section_pos, _ = await self._find_ai_logs_section(content)
        
        if not section_exists:
            return {}
            
        section_content = content[section_pos:]
        logs_by_date = defaultdict(list)
        date_headers_iter = re.finditer(r'^## (\d{4}-\d{2}-\d{2})\s*$', section_content, re.MULTILINE)
        date_headers = list(date_headers_iter)
        
        for i, date_match in enumerate(date_headers):
            date_str = date_match.group(1)
            start_pos = date_match.end()
            end_pos = date_headers[i+1].start() if i < len(date_headers) - 1 else len(section_content)
            
            date_section = section_content[start_pos:end_pos].strip()
            
            entry_matches = re.finditer(r'\*category\*: (.*?)\n\*source:\* (.*?)\n\*notes\*:\s(.*?)(?=\n\*category\*:|$)', 
                                       date_section, re.DOTALL)
            
            for entry_match in entry_matches:
                category = entry_match.group(1).strip()
                source = entry_match.group(2).strip()
                notes = entry_match.group(3).strip()
                
                logs_by_date[date_str].append({
                    'category': category,
                    'source': source,
                    'notes': notes
                })
        
        return logs_by_date
    
    async def _filter_future_logs(self, person_content: str, meeting_date_str: str) -> str:
        """Filters the AI Logs section, removing entries dated after meeting_date_str."""
        logger.debug(f"Filtering future logs for meeting date: {meeting_date_str}")
        
        section_exists, section_pos, content_before_section = await self._find_ai_logs_section(person_content)
        
        if not section_exists:
            logger.debug("No AI Logs section found. Returning original content.")
            return person_content
            
        all_logs_by_date = await self._parse_existing_logs(person_content)
        
        filtered_logs_by_date = defaultdict(list)
        for log_date, logs in all_logs_by_date.items():
            if log_date <= meeting_date_str:
                filtered_logs_by_date[log_date] = logs
            else:
                logger.debug(f"Filtering out log date {log_date} (future relative to {meeting_date_str})")

        ai_logs_section_content = person_content[section_pos:]
        header_match = re.match(r'^# AI Logs\s*(\n>\[!warning\] Do not Modify\s*\n)?\n*', ai_logs_section_content, re.IGNORECASE)
        filtered_section = header_match.group(0) if header_match else "# AI Logs\n\n"

        for date in sorted(filtered_logs_by_date.keys(), reverse=True):
            filtered_section += f"## {date}\n"
            for log in filtered_logs_by_date[date]:
                filtered_section += f"*category*: {log['category']}\n"
                filtered_section += f"*source:* {log['source']}\n"
                filtered_section += f"*notes*: \n{log['notes']}\n\n"
        
        filtered_content = content_before_section.rstrip() + "\n\n" + filtered_section.strip()
        logger.debug("Finished filtering future logs using _parse_existing_logs.")
        return filtered_content

    async def _generate_log(self, transcript_content: str, person_content: str, 
                           person_name: str, meeting_date: str, meeting_title: str) -> str:
        """Generate a log entry for a person using AI."""
        
        filtered_person_content = await self._filter_future_logs(person_content, meeting_date)

        if len(filtered_person_content) > 10000:
            filtered_person_content = filtered_person_content[:10000] + "...[truncated]"

        prompt = get_prompt("interaction_log").format(
            transcript_content=transcript_content,
            person_name=person_name,
            person_content=filtered_person_content,
            meeting_date=meeting_date,
            meeting_title=meeting_title
        )
        
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=prompt
            )]
        )
        
        response = await asyncio.to_thread(self.ai_model.message, message)
        return response.content.strip()

    async def _generate_mention_logs_batch(self, transcript_content: str,
                                            mentioned_names: List[str], meeting_title: str) -> Dict[str, Dict]:
        """Generate log entries for all mentioned people in a single AI call.
        
        Returns:
            Dict mapping person name to their log data:
            {"Person Name": {"why_mentioned": "...", "information_learned": "..."}}
        """
        if not mentioned_names:
            return {}
        
        mentioned_list = "\n".join(f"- {name}" for name in mentioned_names)
        
        prompt = get_prompt("mention_log").format(
            transcript_content=transcript_content,
            mentioned_people_list=mentioned_list,
            meeting_title=meeting_title
        )
        
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=prompt
            )]
        )
        
        response = await asyncio.to_thread(self.tiny_ai_model.message, message)
        response_text = response.content.strip()
        
        # Parse JSON response
        try:
            # Extract JSON from response (may have markdown code block)
            import json
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(response)
            
            # Convert to dict keyed by name
            result = {}
            for item in data:
                name = item.get('name', '')
                notes = item.get('notes', '')
                if name and notes:
                    result[name] = notes
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse mention logs JSON: {e}. Response: {response_text[:200]}")
            # Fallback: return empty dict (skip mentions if parsing fails)
            return {}
    
    async def _generate_email_participant_logs_batch(self, email_content: str,
                                                      correspondent_names: List[str],
                                                      digest_date: str) -> Dict[str, str]:
        """Generate log entries for all email correspondents in a single AI call.
        
        Returns:
            Dict mapping person name to their notes string
        """
        if not correspondent_names:
            return {}
        
        correspondents_list = "\n".join(f"- {name}" for name in correspondent_names)
        
        prompt = get_prompt("email_participant_log_batch").format(
            email_content=email_content,
            correspondents_list=correspondents_list,
            digest_date=digest_date
        )
        
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=prompt
            )]
        )
        
        response = await asyncio.to_thread(self.tiny_ai_model.message, message)
        response_text = response.content.strip()
        
        # Parse JSON response
        try:
            import json
            json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(response_text)
            
            # Convert to dict keyed by name
            result = {}
            for item in data:
                name = item.get('name', '')
                notes = item.get('notes', '')
                if name and notes:
                    result[name] = notes
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse email participant logs JSON: {e}. Response: {response_text[:200]}")
            return {}
    
    async def _generate_email_mention_logs_batch(self, email_content: str,
                                                  mentioned_names: List[str],
                                                  digest_date: str) -> Dict[str, str]:
        """Generate log entries for all mentioned people in emails in a single AI call.
        
        Returns:
            Dict mapping person name to their notes string
        """
        if not mentioned_names:
            return {}
        
        mentioned_list = "\n".join(f"- {name}" for name in mentioned_names)
        
        prompt = get_prompt("email_mention_log").format(
            email_content=email_content,
            mentioned_people_list=mentioned_list,
            digest_date=digest_date
        )
        
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=prompt
            )]
        )
        
        response = await asyncio.to_thread(self.tiny_ai_model.message, message)
        response_text = response.content.strip()
        
        # Parse JSON response
        try:
            import json
            json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(response_text)
            
            # Convert to dict keyed by name
            result = {}
            for item in data:
                name = item.get('name', '')
                notes = item.get('notes', '')
                if name and notes:
                    result[name] = notes
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse email mention logs JSON: {e}. Response: {response_text[:200]}")
            return {}

    async def process_file(self, filename: str) -> None:
        """Process meetings or emails and add logs to person notes.
        
        Dispatches to category-specific processing:
        - Meetings: Uses speaker_mapping for participants
        - Emails: Extracts participants from From/To lines
        """
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)
        category = frontmatter.get('category', '').lower()
        
        if category == 'email':
            await self._process_email_file(filename, content, frontmatter)
        else:  # meeting
            await self._process_meeting_file(filename, content, frontmatter)
    
    async def _process_meeting_file(self, filename: str, content: str, frontmatter: Dict) -> None:
        """Process meeting transcript and add logs to speaker/mention notes."""
        logger.info(f"Processing interactions from meeting: {filename}")
        
        transcript = read_text_from_content(content)
        
        meeting_date = frontmatter.get('date')
        meeting_title = frontmatter.get('title', filename)
        source_link = f"[[{filename.replace('.md', '')}]]"
        
        if not meeting_date:
            logger.error(f"Missing date in frontmatter for {filename}")
            raise ValueError(f"Meeting date is required in frontmatter for {filename}")
        
        speaker_mapping = frontmatter.get('final_speaker_mapping', {})
        
        if not speaker_mapping:
            logger.warning(f"Empty speaker mapping in {filename}")
            return
        
        logged_interactions = frontmatter.get('logged_interactions', [])
        
        all_speakers = set(speaker_data.get('person_id') for speaker_data in speaker_mapping.values() 
                         if speaker_data.get('person_id'))
        
        pending_speakers = [speaker for speaker in all_speakers if speaker not in logged_interactions]
        
        if not pending_speakers:
            logger.info(f"All speakers in {filename} have already been processed")
            return
            
        logger.info(f"Processing {len(pending_speakers)} remaining speakers in {filename}")
        
        for person_id in pending_speakers:
            person_name = person_id.replace('[[', '').replace(']]', '')
            person_file_path = self.people_dir / f"{person_name}.md"
            
            if not person_file_path.exists():
                logger.warning(f"Person note not found: {person_file_path}")
                continue
            
            try:
                async with aiofiles.open(person_file_path, 'r', encoding='utf-8') as f:
                    person_content = await f.read()
                
                log_content = await self._generate_log(
                    transcript_content=transcript,
                    person_content=person_content,
                    person_name=person_name,
                    meeting_date=meeting_date,
                    meeting_title=meeting_title
                )
                
                success = await self._update_person_note(
                    person_id=person_id,
                    meeting_date=meeting_date,
                    source_link=source_link,
                    log_content=log_content,
                    category='meeting'
                )
                
                if success:
                    if 'logged_interactions' not in frontmatter:
                        frontmatter['logged_interactions'] = []
                    
                    frontmatter['logged_interactions'].append(person_id)
                    
                    file_path = self.input_dir / filename
                    updated_content = frontmatter_to_text(frontmatter) + transcript
                    
                    async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                        await f.write(updated_content)
                    
                    os.utime(file_path, None)
                    
                    logger.info(f"Updated transcript {filename} - logged interaction for {person_name}")
                else:
                    logger.error(f"Failed to update note for {person_id}")
                
            except Exception as e:
                logger.error(f"Error generating log for {person_name}: {str(e)}")
                logger.error(traceback.format_exc())
                continue
        
        logged_interactions = frontmatter.get('logged_interactions', [])
        all_speakers_processed = all(speaker in logged_interactions for speaker in all_speakers)
        
        if not all_speakers_processed:
            remaining = len(all_speakers) - len(logged_interactions)
            logger.info(f"{remaining} speakers still pending in {filename}. Stage not marked complete yet.")
            raise Exception(f"Not all speakers processed in {filename}. Will retry later.")
        
        # ===== Process Mentions =====
        # Get mentioned people from resolved_entities (people only, not participants)
        resolved_entities = frontmatter.get('resolved_entities', [])
        speaker_person_ids = set(speaker_data.get('person_id', '') for speaker_data in speaker_mapping.values())
        
        mentioned_people = [
            entity['resolved_link'] 
            for entity in resolved_entities 
            if entity.get('entity_type') == 'people' 
            and entity.get('resolved_link')
            and entity['resolved_link'] not in speaker_person_ids
        ]
        
        logged_mentions = frontmatter.get('logged_mentions', [])
        pending_mentions = [mention for mention in mentioned_people if mention not in logged_mentions]
        
        if pending_mentions:
            logger.info(f"Processing {len(pending_mentions)} mentions in {filename}")
            
            # Get names for batch processing
            pending_names = [m.replace('[[', '').replace(']]', '') for m in pending_mentions]
            
            # Single AI call for all mentions
            mention_logs = await self._generate_mention_logs_batch(
                transcript_content=transcript,
                mentioned_names=pending_names,
                meeting_title=meeting_title
            )
            
            # Process each mention with the batch results
            for person_id in pending_mentions:
                person_name = person_id.replace('[[', '').replace(']]', '')
                person_file_path = self.people_dir / f"{person_name}.md"
                
                if not person_file_path.exists():
                    logger.warning(f"Person note not found for mention: {person_file_path}")
                    # Still mark as logged to avoid retrying
                    if 'logged_mentions' not in frontmatter:
                        frontmatter['logged_mentions'] = []
                    frontmatter['logged_mentions'].append(person_id)
                    continue
                
                try:
                    # Get notes from batch result (notes is already a string with bullet points)
                    log_content = mention_logs.get(person_name, '')
                    
                    if not log_content:
                        # AI decided this person had nothing meaningful to log
                        if 'logged_mentions' not in frontmatter:
                            frontmatter['logged_mentions'] = []
                        frontmatter['logged_mentions'].append(person_id)
                        logger.info(f"Skipping {person_name} - no meaningful mention notes")
                        continue
                    
                    success = await self._update_person_note(
                        person_id=person_id,
                        meeting_date=meeting_date,
                        source_link=source_link,
                        log_content=log_content,
                        category='mention'
                    )
                    
                    if success:
                        if 'logged_mentions' not in frontmatter:
                            frontmatter['logged_mentions'] = []
                        
                        frontmatter['logged_mentions'].append(person_id)
                        
                        file_path = self.input_dir / filename
                        updated_content = frontmatter_to_text(frontmatter) + transcript
                        
                        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                            await f.write(updated_content)
                        
                        os.utime(file_path, None)
                        
                        logger.info(f"Updated transcript {filename} - logged mention for {person_name}")
                    else:
                        logger.error(f"Failed to update note for mention {person_id}")
                    
                except Exception as e:
                    logger.error(f"Error generating mention log for {person_name}: {str(e)}")
                    logger.error(traceback.format_exc())
                    continue
        
        # Final completion check
        logged_mentions = frontmatter.get('logged_mentions', [])
        all_mentions_processed = all(mention in logged_mentions for mention in mentioned_people)
        
        if all_speakers_processed and all_mentions_processed:
            logger.info(f"All speakers and mentions in {filename} have been processed. Marking stage as complete.")
        else:
            remaining_mentions = len(mentioned_people) - len(logged_mentions)
            logger.info(f"{remaining_mentions} mentions still pending in {filename}. Stage not marked complete yet.")
            raise Exception(f"Not all mentions processed in {filename}. Will retry later.")
    
    async def _process_email_file(self, filename: str, content: str, frontmatter: Dict) -> None:
        """Process email digest and add logs to correspondent/mention notes."""
        logger.info(f"Processing interactions from email digest: {filename}")
        
        email_content = read_text_from_content(content)
        
        digest_date = frontmatter.get('date')
        source_link = f"[[{filename.replace('.md', '')}]]"
        
        if not digest_date:
            logger.error(f"Missing date in frontmatter for {filename}")
            raise ValueError(f"Date is required in frontmatter for {filename}")
        
        # Convert date to string format
        digest_date_str = str(digest_date)[:10]
        
        # Extract participants from From/To lines
        all_participants = set(self._extract_email_participants(email_content))
        
        if not all_participants:
            logger.warning(f"No participants found in email digest {filename}")
            # Still continue to process mentions
        
        logged_interactions = frontmatter.get('logged_interactions', [])
        pending_participants = [p for p in all_participants if p not in logged_interactions]
        
        # ===== Process Email Participants (Batch) =====
        if pending_participants:
            logger.info(f"Processing {len(pending_participants)} email correspondents in {filename}")
            
            # Get names for batch processing
            pending_names = [p.replace('[[', '').replace(']]', '') for p in pending_participants]
            
            # Single AI call for all participants
            participant_logs = await self._generate_email_participant_logs_batch(
                email_content=email_content,
                correspondent_names=pending_names,
                digest_date=digest_date_str
            )
            
            # Process each participant with batch results
            for person_id in pending_participants:
                person_name = person_id.replace('[[', '').replace(']]', '')
                person_file_path = self.people_dir / f"{person_name}.md"
                
                if not person_file_path.exists():
                    logger.warning(f"Person note not found: {person_file_path}")
                    # Mark as logged to prevent retrying
                    if 'logged_interactions' not in frontmatter:
                        frontmatter['logged_interactions'] = []
                    frontmatter['logged_interactions'].append(person_id)
                    continue
                
                try:
                    log_content = participant_logs.get(person_name, '')
                    
                    if not log_content:
                        # AI decided nothing meaningful to log for this person
                        if 'logged_interactions' not in frontmatter:
                            frontmatter['logged_interactions'] = []
                        frontmatter['logged_interactions'].append(person_id)
                        logger.info(f"Skipping {person_name} - no meaningful email content")
                        continue
                    
                    success = await self._update_person_note(
                        person_id=person_id,
                        meeting_date=digest_date_str,
                        source_link=source_link,
                        log_content=log_content,
                        category='email_participant'
                    )
                    
                    if success:
                        if 'logged_interactions' not in frontmatter:
                            frontmatter['logged_interactions'] = []
                        frontmatter['logged_interactions'].append(person_id)
                        
                        file_path = self.input_dir / filename
                        updated_content = frontmatter_to_text(frontmatter) + email_content
                        
                        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                            await f.write(updated_content)
                        
                        os.utime(file_path, None)
                        
                        logger.info(f"Updated {filename} - logged email interaction for {person_name}")
                    else:
                        logger.error(f"Failed to update note for {person_id}")
                    
                except Exception as e:
                    logger.error(f"Error updating log for {person_name}: {str(e)}")
                    logger.error(traceback.format_exc())
                    continue
        
        # Check if all participants processed
        logged_interactions = frontmatter.get('logged_interactions', [])
        all_participants_processed = all(p in logged_interactions for p in all_participants)
        
        if not all_participants_processed:
            remaining = len(all_participants) - len(logged_interactions)
            logger.info(f"{remaining} correspondents still pending in {filename}.")
            raise Exception(f"Not all correspondents processed in {filename}. Will retry.")
        
        # ===== Process Email Mentions =====
        resolved_entities = frontmatter.get('resolved_entities', [])
        
        mentioned_people = [
            entity['resolved_link']
            for entity in resolved_entities
            if entity.get('entity_type') == 'people'
            and entity.get('resolved_link')
            and entity['resolved_link'] not in all_participants  # Not in From/To
        ]
        
        # Deduplicate
        mentioned_people = list(set(mentioned_people))
        
        logged_mentions = frontmatter.get('logged_mentions', [])
        pending_mentions = [m for m in mentioned_people if m not in logged_mentions]
        
        if pending_mentions:
            logger.info(f"Processing {len(pending_mentions)} email mentions in {filename}")
            
            pending_names = [m.replace('[[', '').replace(']]', '') for m in pending_mentions]
            
            # Single AI call for all mentions
            mention_logs = await self._generate_email_mention_logs_batch(
                email_content=email_content,
                mentioned_names=pending_names,
                digest_date=digest_date_str
            )
            
            for person_id in pending_mentions:
                person_name = person_id.replace('[[', '').replace(']]', '')
                person_file_path = self.people_dir / f"{person_name}.md"
                
                if not person_file_path.exists():
                    logger.warning(f"Person note not found for mention: {person_file_path}")
                    if 'logged_mentions' not in frontmatter:
                        frontmatter['logged_mentions'] = []
                    frontmatter['logged_mentions'].append(person_id)
                    continue
                
                try:
                    log_content = mention_logs.get(person_name, '')
                    
                    if not log_content:
                        if 'logged_mentions' not in frontmatter:
                            frontmatter['logged_mentions'] = []
                        frontmatter['logged_mentions'].append(person_id)
                        logger.info(f"Skipping {person_name} - no meaningful email mention")
                        continue
                    
                    success = await self._update_person_note(
                        person_id=person_id,
                        meeting_date=digest_date_str,
                        source_link=source_link,
                        log_content=log_content,
                        category='email_mention'
                    )
                    
                    if success:
                        if 'logged_mentions' not in frontmatter:
                            frontmatter['logged_mentions'] = []
                        frontmatter['logged_mentions'].append(person_id)
                        
                        file_path = self.input_dir / filename
                        updated_content = frontmatter_to_text(frontmatter) + email_content
                        
                        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                            await f.write(updated_content)
                        
                        os.utime(file_path, None)
                        
                        logger.info(f"Updated {filename} - logged email mention for {person_name}")
                    else:
                        logger.error(f"Failed to update note for email mention {person_id}")
                    
                except Exception as e:
                    logger.error(f"Error generating email mention log for {person_name}: {str(e)}")
                    logger.error(traceback.format_exc())
                    continue
        
        # Final completion check
        logged_mentions = frontmatter.get('logged_mentions', [])
        all_mentions_processed = all(m in logged_mentions for m in mentioned_people)
        
        if all_participants_processed and all_mentions_processed:
            logger.info(f"All correspondents and mentions in {filename} processed.")
        else:
            remaining = len(mentioned_people) - len(logged_mentions)
            logger.info(f"{remaining} email mentions still pending in {filename}.")
            raise Exception(f"Not all email mentions processed in {filename}. Will retry.")
    
    async def _update_person_note(self, person_id: str, 
                                 meeting_date: str, 
                                 source_link: str, 
                                 log_content: str,
                                 category: str = 'meeting') -> bool:
        """Update a person's note with the new log entry."""
        person_name = person_id.replace('[[', '').replace(']]', '')
        person_file_path = self.people_dir / f"{person_name}.md"
        
        if not person_file_path.exists():
            logger.warning(f"Person note not found: {person_file_path}")
            return False
        
        try:
            async with aiofiles.open(person_file_path, 'r', encoding='utf-8') as f:
                person_content = await f.read()
            
            logs_by_date = await self._parse_existing_logs(person_content)
            section_exists, section_pos, content_before_section = await self._find_ai_logs_section(person_content)
            
            new_log = {
                'category': category,
                'source': source_link,
                'notes': log_content
            }
            
            found_and_updated = False
            if meeting_date in logs_by_date:
                for existing_log in logs_by_date[meeting_date]:
                    if existing_log['source'] == source_link:
                        logger.info(f"Overwriting existing log for {source_link} on {meeting_date} in {person_name}'s note")
                        existing_log['notes'] = log_content
                        found_and_updated = True
                        break
                
                if not found_and_updated:
                    logs_by_date[meeting_date].append(new_log)
            else:
                logs_by_date[meeting_date] = [new_log]
            
            new_section = "# AI Logs\n>[!warning] Do not Modify\n\n"
            
            for date in sorted(logs_by_date.keys(), reverse=True):
                new_section += f"## {date}\n"
                for log in logs_by_date[date]:
                    new_section += f"*category*: {log['category']}\n"
                    new_section += f"*source:* {log['source']}\n"
                    new_section += f"*notes*: \n{log['notes']}\n\n"
            
            if section_exists:
                new_content = content_before_section + new_section
            else:
                new_content = person_content + "\n\n" + new_section
            
            async with aiofiles.open(person_file_path, 'w', encoding='utf-8') as f:
                await f.write(new_content)
            
            os.utime(person_file_path, None)
            
            logger.info(f"Updated {person_name}'s note with log for {meeting_date}")
            return True
            
        except Exception as e:
            logger.error(f"Error updating person note {person_name}: {str(e)}")
            return False

    async def reset(self, filename: str) -> None:
        """Resets the interaction logging stage for a transcript file."""
        logger.info(f"Attempting to reset stage '{self.stage_name}' for: {filename}")
        file_path = self.input_dir / filename
        if not file_path.exists():
            logger.error(f"File not found during reset: {filename}")
            return

        try:
            content = await self.read_file(filename)
            frontmatter = parse_frontmatter_from_content(content)
            transcript = read_text_from_content(content)

            if not frontmatter:
                logger.warning(f"No frontmatter found in {filename}. Cannot reset stage.")
                return

            processing_stages = frontmatter.get('processing_stages', [])
            if self.stage_name not in processing_stages:
                logger.info(f"Stage '{self.stage_name}' not found in processing stages for {filename}. No reset needed.")
                return

            logged_interactions = frontmatter.get('logged_interactions', [])
            meeting_date = frontmatter.get('date')
            source_link = f"[[{filename.replace('.md', '')}]]"
            
            if not meeting_date:
                logger.warning(f"Missing date in frontmatter for {filename}. Cannot identify logs to remove.")

            for person_id in logged_interactions:
                await self._remove_log_entry(person_id, meeting_date, source_link)

            if 'logged_interactions' in frontmatter:
                del frontmatter['logged_interactions']
            
            if self.stage_name in processing_stages:
                processing_stages.remove(self.stage_name)
                frontmatter['processing_stages'] = processing_stages

            updated_content = frontmatter_to_text(frontmatter) + transcript
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                await f.write(updated_content)
            
            os.utime(file_path, None)
            logger.info(f"Successfully reset stage '{self.stage_name}' for: {filename}")

        except Exception as e:
            logger.error(f"Error resetting stage '{self.stage_name}' for {filename}: {e}")
            logger.error(traceback.format_exc())

    async def _remove_log_entry(self, person_id: str, meeting_date: str, source_link: str) -> None:
        """Removes a specific log entry from a person's note."""
        person_name = person_id.replace('[[', '').replace(']]', '')
        person_file_path = self.people_dir / f"{person_name}.md"
        
        if not person_file_path.exists():
            logger.warning(f"Person note not found during reset: {person_file_path}")
            return
        
        try:
            async with aiofiles.open(person_file_path, 'r', encoding='utf-8') as f:
                person_content = await f.read()
            
            section_exists, section_pos, content_before_section = await self._find_ai_logs_section(person_content)
            
            if not section_exists:
                logger.warning(f"No AI Logs section found in {person_name}'s note. Nothing to reset.")
                return
                
            logs_by_date = await self._parse_existing_logs(person_content)
            
            entry_removed = False
            if meeting_date in logs_by_date:
                logs_by_date[meeting_date] = [
                    log for log in logs_by_date[meeting_date] 
                    if log['source'] != source_link
                ]
                
                if not logs_by_date[meeting_date]:
                    del logs_by_date[meeting_date]
                    
                entry_removed = True
            
            if not entry_removed:
                logger.warning(f"No log entry found for {source_link} on {meeting_date} in {person_name}'s note.")
                return
                
            new_section = "# AI Logs\n>[!warning] Do not Modify\n\n"
            
            for date in sorted(logs_by_date.keys(), reverse=True):
                new_section += f"## {date}\n"
                for log in logs_by_date[date]:
                    new_section += f"*category*: {log['category']}\n"
                    new_section += f"*source:* {log['source']}\n"
                    new_section += f"*notes*: \n{log['notes']}\n\n"
            
            new_content = content_before_section + new_section
            
            async with aiofiles.open(person_file_path, 'w', encoding='utf-8') as f:
                await f.write(new_content)
            
            os.utime(person_file_path, None)
            
            logger.info(f"Removed log entry for {meeting_date} from {person_name}'s note")
                
        except Exception as e:
            logger.error(f"Error removing log entry from {person_name}'s note: {e}")
            logger.error(traceback.format_exc())





