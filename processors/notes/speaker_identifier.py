from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import aiofiles
import os
import re
import asyncio

from .base import NoteProcessor
from ..common.frontmatter import parse_frontmatter_from_content, frontmatter_to_text, read_text_from_content
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from config.user_config import TARGET_DISCORD_USER_ID, USER_NAME, USER_ORGANIZATION
from integrations.discord import DiscordIOCore
from .transcript_classifier import TranscriptClassifier
from prompts.prompts import get_prompt

logger = setup_logger(__name__)

SPEAKER_IDENTIFICATION_MAX_RETRIES = 3

class SpeakerIdentificationError(Exception):
    """Exception raised when speaker identification processing encounters an error."""
    pass

class ResultsNotReadyError(Exception):
    """Exception raised when the speaker matching results are not yet available.
    This is expected behavior and will cause the processor to retry later."""
    pass

class SpeakerIdentifier(NoteProcessor):
    """Identifies speakers in transcripts using AI, initiates matching UI, and processes results."""
    stage_name = "speakers_identified"
    required_stage = TranscriptClassifier.stage_name

    def __init__(self, input_dir: Path, discord_io: DiscordIOCore):
        super().__init__(input_dir)
        self.discord_io = discord_io
        
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        """
        Determine if the file should be processed.
        The base class already checks if the stage_name exists in processing_stages.
        We provide additional criteria here.
        """
        if "nospeaker" in frontmatter.get("source_tags", []):
            return False
        return True

    # ===== Validation Section Markers =====
    VALIDATION_SECTION_START = "<!-- validation:start -->"
    VALIDATION_SECTION_END = "<!-- validation:end -->"
    
    def _generate_validation_section(self, speaker_mapping: Dict[str, Dict]) -> str:
        """
        Generate the inline data validation section for Obsidian.
        
        Args:
            speaker_mapping: Dict mapping speaker labels to their AI-identified data
                             e.g., {"Speaker A": {"name": "John", "reason": "..."}}
        
        Returns:
            Markdown string for the validation section
        """
        lines = [
            self.VALIDATION_SECTION_START,
            "",
            "> [!info] Data validation section — Fill in the fields below and check \"Finished\" when done",
            "",
            "# Speaker Identification",
            "",
        ]
        
        # Sort speakers by label for consistent ordering
        sorted_speakers = sorted(speaker_mapping.keys(), key=lambda x: x.replace("Speaker ", ""))
        
        for speaker_id in sorted_speakers:
            data = speaker_mapping[speaker_id]
            detected_name = data.get("name", "Unknown")
            reason = data.get("reason", "No analysis available.")
            label = speaker_id.replace("Speaker ", "").lower()
            
            lines.extend([
                f"## {speaker_id}",
                f"**Detected:** {detected_name}",
                f"<details><summary>🔍 Reasoning</summary>",
                "",
                reason,
                "",
                "</details>",
                "",
                f"**Real answer:** <!-- input:speaker_{label} -->",
                "",
                "---",
                "",
            ])
        
        lines.extend([
            "## Additional Notes",
            "<!-- input:notes -->",
            "",
            "",
            "---",
            "",
            "## Validation",
            "- [ ] Transcript has quality issues (bad transcription, wrong diarization, etc.) <!-- input:quality_issues -->",
            "- [ ] Finished <!-- input:finished -->",
            "",
            self.VALIDATION_SECTION_END,
            "",
        ])
        
        return "\n".join(lines)
    
    def _parse_validation_section(self, content: str) -> Optional[Dict[str, Any]]:
        """
        Parse the validation section from file content.
        
        Returns:
            Dict with keys:
                - 'speakers': Dict mapping speaker labels to user-entered wikilinks
                - 'notes': User's additional notes (may be multi-line)
                - 'finished': Boolean indicating if the checkbox is checked
            Returns None if validation section not found or malformed.
        """
        # Find validation section
        start_marker = self.VALIDATION_SECTION_START
        end_marker = self.VALIDATION_SECTION_END
        
        start_idx = content.find(start_marker)
        end_idx = content.find(end_marker)
        
        if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
            return None
        
        section = content[start_idx + len(start_marker):end_idx]
        
        result = {
            'speakers': {},
            'notes': '',
            'quality_issues': False,
            'finished': False
        }
        
        # Parse speaker inputs: <!-- input:speaker_X --> followed by any text on same line
        # Captures both wikilinks [[Name]] and plain text (which will be validated later)
        speaker_pattern = r'<!-- input:speaker_([a-z]+) -->([^\n]*)'
        for match in re.finditer(speaker_pattern, section):
            speaker_key = f"Speaker {match.group(1).upper()}"
            value = match.group(2).strip() if match.group(2) else ""
            result['speakers'][speaker_key] = value
        
        # Parse additional notes: everything after <!-- input:notes --> until the next ---
        notes_pattern = r'<!-- input:notes -->\s*(.*?)\s*---'
        notes_match = re.search(notes_pattern, section, re.DOTALL)
        if notes_match:
            result['notes'] = notes_match.group(1).strip()
        
        # Parse quality issues checkbox
        quality_pattern = r'\[(x|X)\]\s+Transcript has quality issues.*<!-- input:quality_issues -->'
        result['quality_issues'] = bool(re.search(quality_pattern, section))
        
        # Parse finished checkbox: [x] or [X] before <!-- input:finished -->
        finished_pattern = r'\[(x|X)\]\s+Finished\s+<!-- input:finished -->'
        result['finished'] = bool(re.search(finished_pattern, section))
        
        return result
    
    def _extract_person_from_wikilink(self, wikilink: str) -> Tuple[str, str]:
        """
        Extract person name and ID from a wikilink.
        
        Args:
            wikilink: e.g. "[[John Smith]]" or "[[John Smith|Johnny]]"
        
        Returns:
            Tuple of (person_id, display_name)
        """
        if not wikilink:
            return ("Unknown", "Unknown")
        
        # Remove [[ and ]]
        inner = wikilink.strip()[2:-2] if wikilink.startswith("[[") and wikilink.endswith("]]") else wikilink
        
        if "|" in inner:
            parts = inner.split("|", 1)
            return (parts[0].strip(), parts[1].strip())
        else:
            return (inner.strip(), inner.strip())
    
    def _generate_speaker_summary(
        self, 
        final_mapping: Dict[str, Dict], 
        notes: str,
        unidentified_speakers: List[str],
        has_quality_issues: bool = False
    ) -> str:
        """
        Generate the compact summary to replace the validation section after completion.
        
        Args:
            final_mapping: Dict mapping speaker labels to final speaker data
            notes: User's additional notes
            unidentified_speakers: List of speaker labels that were not filled in
            has_quality_issues: Whether user flagged quality issues
        
        Returns:
            Markdown string for the summary section
        """
        # Collect unique person links
        person_links = []
        for speaker_data in final_mapping.values():
            person_id = speaker_data.get('person_id', '')
            if person_id:
                person_links.append(person_id)
        
        # Determine callout type and text based on issues
        if has_quality_issues:
            callout_type = "warning"
            callout_text = "Speaker identification complete (quality issues flagged)"
        elif unidentified_speakers:
            callout_type = "warning"
            callout_text = "Speaker identification complete (some speakers not identified)"
        else:
            callout_type = "success"
            callout_text = "Speaker identification complete"
        
        lines = [
            self.VALIDATION_SECTION_START,
            "",
            f"> [!{callout_type}] {callout_text}",
            "",
        ]
        
        if person_links:
            lines.extend([
                f"**Speakers:** {', '.join(person_links)}",
                "",
            ])
        
        if unidentified_speakers:
            lines.extend([
                f"**Not identified:** {', '.join(unidentified_speakers)}",
                "",
            ])
        
        if has_quality_issues:
            lines.extend([
                "⚠️ **Quality issues flagged**",
                "",
            ])
        
        if notes:
            lines.extend([
                f"**Notes:** {notes}",
                "",
            ])
        
        lines.extend([
            self.VALIDATION_SECTION_END,
            "",
        ])
        
        return "\n".join(lines)
    
    def _remove_validation_section(self, content: str) -> str:
        """Remove the validation section from content, preserving surrounding content."""
        start_marker = self.VALIDATION_SECTION_START
        end_marker = self.VALIDATION_SECTION_END
        
        start_idx = content.find(start_marker)
        end_idx = content.find(end_marker)
        
        if start_idx == -1 or end_idx == -1:
            return content
        
        # Find the end of the line containing the end marker
        end_line_idx = content.find('\n', end_idx)
        if end_line_idx == -1:
            end_line_idx = len(content)
        else:
            end_line_idx += 1  # Include the newline
        
        return content[:start_idx] + content[end_line_idx:]

    def _extract_unique_speakers(self, transcript: str) -> set:
        """Extract all unique speaker labels from the transcript."""
        speaker_lines = [line for line in transcript.split('\n') if line.startswith('Speaker ')]
        return set(line.split(':')[0].strip() for line in speaker_lines)
                 
    async def identify_speaker(self, transcript: str, speaker_label: str) -> str:
        """Use AI to identify a specific speaker from the transcript."""
        prompt = get_prompt("identify_speaker").format(speaker_label=speaker_label) + f"\n\nTranscript:\n{transcript}"
        
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=prompt
            )]
        )
        
        max_retries = SPEAKER_IDENTIFICATION_MAX_RETRIES
        for retry_count in range(max_retries):
            response = await asyncio.to_thread(self.tiny_ai_model.message, message)
            if response.content is not None:
                return response.content.strip()
            logger.warning(f"Empty response for speaker {speaker_label}. Retry {retry_count + 1}/{max_retries}...")
        
        logger.error("Response from AI is empty after retries. Response error: %s", response.error)
        return f"PROBLEM WITH SPEAKER IDENTIFICATION FOR SPEAKER {speaker_label}."

    async def consolidate_answer(self, text: str) -> str:
        """Extract just the name from the verbose AI response."""
        prompt = get_prompt("consolidate_speaker_name").format(text=text)
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=prompt
            )]
        )
        response = await asyncio.to_thread(self.tiny_ai_model.message, message)
        if response.content is None:
            logger.error("Response from AI is empty. Response error: %s", response.error)
            return "unknown"
        else:
            return response.content.strip()

    async def process_file(self, filename: str) -> None:
        """Process a transcript file through all substages: identify speakers, create validation section, and process results."""
        logger.info("Processing file for speaker identification: %s", filename)
        
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)
        transcript = read_text_from_content(content)
        
        # --- Special case: Check for single speaker transcripts ---
        unique_speakers = self._extract_unique_speakers(transcript)
        if len(unique_speakers) == 1:
            await self._handle_single_speaker(filename, frontmatter, transcript, list(unique_speakers)[0])
            return
        
        # --- Check if validation section exists (substage 2 already done) ---
        if frontmatter.get('speaker_validation_pending'):
            # Validation section exists, try to process results
            await self._substage3_process_results(filename, frontmatter, content)
        else:
            # Need to run substage 1 (AI identification) and substage 2 (create validation section)
            speaker_mapping = await self._substage1_identify_speakers(filename, frontmatter, transcript)
            await self._substage2_create_validation_section(filename, frontmatter, transcript, speaker_mapping)
            # Raise to prevent base class from marking stage complete - we're waiting for user input
            raise ResultsNotReadyError(f"Validation section created, waiting for user input in: {filename}")

    async def _handle_single_speaker(self, filename: str, frontmatter: Dict, transcript: str, speaker_label: str) -> None:
        """Handle transcripts with a single speaker by automatically assigning user's info."""
        logger.info("Detected single speaker transcript in %s. Automatically assigning to user: %s", filename, USER_NAME)
        
        # Create a final speaker mapping for a single speaker
        final_mapping = {
            speaker_label: {
                "name": USER_NAME,
                "organisation": f"[[{USER_ORGANIZATION}]]",
                "person_id": f"[[{USER_NAME}]]"
            }
        }
        
        # Add to frontmatter
        frontmatter['final_speaker_mapping'] = final_mapping
        
        # Replace speaker labels in the transcript
        new_transcript = transcript
        pattern = re.escape(f"{speaker_label}:") 
        new_transcript = re.sub(pattern, f"{USER_NAME} ([[{USER_ORGANIZATION}]]):", new_transcript)
        
        # Save the updated file
        full_content = frontmatter_to_text(frontmatter) + new_transcript
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(self.input_dir / filename, None)
        
        logger.info("Completed automatic speaker identification for single-speaker file: %s", filename)
            
    async def _substage1_identify_speakers(self, filename: str, frontmatter: Dict, transcript: str) -> Dict[str, Dict]:
        """Substage 1: Identify speakers using AI and return the mapping.
        
        Returns:
            Dict mapping speaker labels to AI-identified data, e.g.:
            {"Speaker A": {"name": "John", "reason": "Based on..."}}
        """
        logger.info("Identifying speakers in: %s", filename)
        unique_speakers = self._extract_unique_speakers(transcript)
        
        speaker_mapping = {}
        for speaker in unique_speakers:
            logger.info("Identifying %s...", speaker)
            label = speaker.replace('Speaker ', '')
            identified_name_verbose = await self.identify_speaker(transcript, label)
            identified_name = await self.consolidate_answer(identified_name_verbose)

            logger.info("Result: %s", identified_name_verbose)
            # Store both name and reason
            speaker_mapping[speaker] = {
                "name": identified_name,
                "reason": identified_name_verbose.strip()
            }
        
        logger.info("Identified speakers for: %s", filename)
        return speaker_mapping
    
    async def _substage2_create_validation_section(
        self, 
        filename: str, 
        frontmatter: Dict, 
        transcript: str, 
        speaker_mapping: Dict[str, Dict]
    ) -> None:
        """Substage 2: Create inline validation section and send Discord notification.
        
        This inserts a data validation section at the top of the transcript content
        (after frontmatter) where the user can review AI guesses and enter the real
        speaker names as wikilinks.
        """
        logger.info("Creating validation section for: %s", filename)
        
        # Generate the validation section markdown
        validation_section = self._generate_validation_section(speaker_mapping)
        
        # Mark as pending in frontmatter
        frontmatter['speaker_validation_pending'] = True
        
        # Combine: frontmatter + validation section + transcript
        full_content = frontmatter_to_text(frontmatter) + validation_section + transcript
        
        # Save the file
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(self.input_dir / filename, None)
        
        # Send Discord notification
        try:
            logger.info("Sending Discord notification for: %s", filename)
            file_path = self.input_dir / filename
            dm_text = (
                f"📝 **Speaker identification needed**\n"
                f"Please review and fill in the speaker names for: `{filename}`\n"
                f"Open the file in Obsidian and complete the validation section."
            )
            success = await self.discord_io.send_dm(TARGET_DISCORD_USER_ID, dm_text)
            
            if not success:
                logger.warning("Failed to send Discord DM for: %s", filename)
            else:
                logger.info("Successfully sent Discord notification for: %s", filename)
        except Exception as e:
            logger.warning("Error sending Discord notification for %s: %s", filename, str(e))
            # Don't fail the whole process just because Discord notification failed
        
        logger.info("Created validation section for: %s", filename)

    async def _substage3_process_results(self, filename: str, frontmatter: Dict, content: str) -> None:
        """Substage 3: Parse validation section and process user input.
        
        Args:
            filename: Name of the file to process
            frontmatter: Parsed frontmatter dict
            content: Full file content (including validation section)
        """
        from ..common.obsidian_form import validate_wikilink_field, insert_error_in_section
        
        logger.info("Checking validation section for: %s", filename)
        
        # Parse the validation section
        validation_data = self._parse_validation_section(content)
        
        if validation_data is None:
            error_msg = f"Could not find or parse validation section in: {filename}"
            logger.error(error_msg)
            raise SpeakerIdentificationError(error_msg)
        
        # Check if user has marked as finished
        if not validation_data['finished']:
            logger.info("Validation not complete yet for: %s. Will retry later.", filename)
            raise ResultsNotReadyError(f"User has not checked 'Finished' in: {filename}")
        
        # Validate the input fields
        errors = []
        for speaker_id, value in validation_data['speakers'].items():
            if value and value.strip():  # Only validate non-empty fields
                error = validate_wikilink_field(value, speaker_id)
                if error:
                    errors.append(error)
        
        # If validation errors, update file with errors and notify user
        if errors:
            logger.warning("Validation errors in %s: %s", filename, [e.message for e in errors])
            
            # Insert error callout and uncheck Finished
            updated_content = insert_error_in_section(
                content, 
                errors, 
                self.VALIDATION_SECTION_START
            )
            
            # Save the updated file
            async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
                await f.write(updated_content)
            os.utime(self.input_dir / filename, None)
            
            # Send Discord notification about the errors
            try:
                error_summary = "; ".join(e.message for e in errors)
                dm_text = (
                    f"⚠️ **Validation errors in speaker identification**\n"
                    f"File: `{filename}`\n"
                    f"Errors: {error_summary}\n"
                    f"Please fix and check Finished again."
                )
                await self.discord_io.send_dm(TARGET_DISCORD_USER_ID, dm_text)
            except Exception as e:
                logger.warning("Failed to send Discord notification: %s", e)
            
            raise ResultsNotReadyError(f"Validation errors in: {filename}")
        
        logger.info("Validation complete for: %s. Processing results.", filename)
        
        # Extract the transcript (content after validation section)
        transcript = self._remove_validation_section(content)
        transcript = read_text_from_content(transcript)  # Remove frontmatter from what remains
        
        # Build the final speaker mapping from user input
        final_mapping = {}
        unidentified_speakers = []
        
        for speaker_id, wikilink in validation_data['speakers'].items():
            if not wikilink or not wikilink.strip():
                # Empty entry - keep original speaker label
                unidentified_speakers.append(speaker_id)
                final_mapping[speaker_id] = {
                    "name": speaker_id,  # Keep as "Speaker A", etc.
                    "person_id": ""
                }
            else:
                person_id, display_name = self._extract_person_from_wikilink(wikilink)
                final_mapping[speaker_id] = {
                    "name": display_name,
                    "person_id": f"[[{person_id}]]" if person_id != "Unknown" else ""
                }
        
        # Update frontmatter
        frontmatter['final_speaker_mapping'] = final_mapping
        del frontmatter['speaker_validation_pending']
        
        # Store user notes if any
        if validation_data['notes']:
            frontmatter['speaker_identification_notes'] = validation_data['notes']
        
        # Store quality issues flag
        if validation_data['quality_issues']:
            frontmatter['transcript_quality_issues'] = True
        
        # Replace speaker labels in the transcript (only for identified speakers)
        new_transcript = transcript
        for speaker_id, speaker_data in final_mapping.items():
            # Skip replacement for unidentified speakers (keep original label)
            if speaker_id in unidentified_speakers:
                continue
            
            name = speaker_data.get("name", "Unknown")
            person_id = speaker_data.get("person_id", "")
            
            if person_id:
                replacement = f"{name} ({person_id}):"
            else:
                replacement = f"{name}:"
            
            pattern = re.escape(f"{speaker_id}:")
            new_transcript = re.sub(pattern, replacement, new_transcript)
        
        # Generate the summary section to replace validation section
        summary_section = self._generate_speaker_summary(
            final_mapping, 
            validation_data['notes'],
            unidentified_speakers,
            has_quality_issues=validation_data['quality_issues']
        )
        
        # Save the updated file: frontmatter + summary + modified transcript
        full_content = frontmatter_to_text(frontmatter) + summary_section + new_transcript
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(self.input_dir / filename, None)
        
        logger.info("Completed speaker identification workflow for: %s", filename)

    async def reset(self, filename: str) -> None:
        """Resets the speaker identification stage for a file."""
        logger.info(f"Attempting to reset stage '{self.stage_name}' for: {filename}")
        file_path = self.input_dir / filename
        if not file_path.exists():
            logger.error(f"File not found during reset: {filename}")
            return

        try:
            content = await self.read_file(filename)
            frontmatter = parse_frontmatter_from_content(content)

            if not frontmatter:
                logger.warning(f"No frontmatter found in {filename}. Cannot reset stage.")
                return

            processing_stages = frontmatter.get('processing_stages', [])
            if self.stage_name not in processing_stages:
                logger.info(f"Stage '{self.stage_name}' not found in processing stages for {filename}. No reset needed.")
                return

            # Remove any validation section (pending or completed summary)
            content_without_validation = self._remove_validation_section(content)
            current_transcript = read_text_from_content(content_without_validation)
            transcript_to_save = current_transcript
            reverted = False

            # Check for completed speaker mapping and revert names
            final_mapping = frontmatter.get('final_speaker_mapping')
            if final_mapping:
                logger.info(f"Detected speaker mapping for {filename}. Reverting names.")
                modified_transcript = current_transcript
                logger.debug(f"Reverting transcript text based on mapping: {final_mapping}")
                for speaker_id, speaker_data in final_mapping.items():
                    name = speaker_data.get("name", "Unknown")
                    person_id = speaker_data.get("person_id", "").replace('[[', '').replace(']]', '')
                    original_label = f"{speaker_id}:"
                    
                    if person_id:
                        # New format: Name ([[Person ID]]):
                        replaced_string = f"{name} ([[{person_id}]]):"
                        modified_transcript = modified_transcript.replace(replaced_string, original_label)
                    
                    # Also try without person_id (fallback)
                    replaced_string = f"{name}:"
                    modified_transcript = modified_transcript.replace(replaced_string, original_label)
                    
                    # Legacy format: Name (Organisation):
                    organization = speaker_data.get("organisation", "").replace('[[', '').replace(']]', '')
                    if organization:
                        replaced_string = f"{name} ({organization}):"
                        modified_transcript = modified_transcript.replace(replaced_string, original_label)
                        replaced_string = f"{name} ([[{organization}]]):"
                        modified_transcript = modified_transcript.replace(replaced_string, original_label)
                
                transcript_to_save = modified_transcript
                reverted = True
            else:
                # Handle legacy old format (list of speaker names)
                old_identified_speakers = frontmatter.get('identified_speakers')
                if isinstance(old_identified_speakers, list):
                    logger.info(f"Detected old speaker format (list) for {filename}. Reverting names.")
                    identified_names = old_identified_speakers
                    modified_transcript = current_transcript
                    for i, name in enumerate(identified_names):
                        if i >= 26:
                            logger.warning(f"More than 26 speakers detected in old format list for {filename}, stopping revert.")
                            break
                        original_label = f"Speaker {chr(ord('A') + i)}:"
                        replaced_string = f"{name}:"
                        modified_transcript = modified_transcript.replace(replaced_string, original_label)
                    transcript_to_save = modified_transcript
                    reverted = True
                else:
                    logger.warning(f"Cannot revert transcript text for {filename}: Missing 'final_speaker_mapping'.")

            # Remove all speaker-identification related keys
            keys_to_remove = [
                'identified_speakers',
                'speaker_matcher_ui_url',
                'speaker_matcher_results_url',
                'speaker_matcher_task_id',
                'speaker_validation_pending',
                'speaker_identification_notes',
                'final_speaker_mapping'
            ]
            cleaned_frontmatter = {k: v for k, v in frontmatter.items() if k not in keys_to_remove}
            
            if 'processing_stages' in cleaned_frontmatter:
                 if self.stage_name in cleaned_frontmatter['processing_stages']:
                     cleaned_frontmatter['processing_stages'].remove(self.stage_name)

            new_content = frontmatter_to_text(cleaned_frontmatter) + transcript_to_save

            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                await f.write(new_content)

            os.utime(file_path, None)
            if reverted:
                logger.info(f"Successfully reset stage '{self.stage_name}' and reverted transcript names for: {filename}")
            else:
                 logger.info(f"Successfully reset stage '{self.stage_name}' (frontmatter only) for: {filename}")

        except Exception as e:
            logger.error(f"Error resetting stage '{self.stage_name}' for {filename}: {e}", exc_info=True)





