from pathlib import Path
from typing import Dict, Any
import aiofiles
import aiohttp
import json
import os
import re
import traceback
import asyncio

from .base import NoteProcessor
from ..common.frontmatter import read_frontmatter_from_file, parse_frontmatter_from_content, frontmatter_to_text, read_text_from_content
from ai_core import AI
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from config.user_config import TARGET_DISCORD_USER_ID, USER_NAME, USER_ORGANIZATION
from config.services_config import SPEAKER_MATCHER_UI_URL
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

    def _extract_unique_speakers(self, transcript: str) -> set:
        """Extract all unique speaker labels from the transcript."""
        speaker_lines = [line for line in transcript.split('\n') if line.startswith('Speaker ')]
        return set(line.split(':')[0].strip() for line in speaker_lines)
    
    def _parse_transcript_segments(self, transcript: str) -> list:
        """Parse transcript into segments for the speaker resolution API."""
        segments = []
        lines = transcript.split('\n')
        current_speaker = None
        current_text_lines = []
        
        for line in lines:
            if line.startswith('Speaker ') and ':' in line:
                # Save previous speaker's text if any
                if current_speaker and current_text_lines:
                    segments.append({
                        "speaker_id": current_speaker,
                        "text": ' '.join(current_text_lines).strip()
                    })
                # Start new speaker
                current_speaker = line.split(':')[0].strip()
                # Check if there's text on the same line after colon
                after_colon = line.split(':', 1)[1].strip() if ':' in line else ''
                current_text_lines = [after_colon] if after_colon else []
            elif current_speaker and line.strip():
                # Continuation of current speaker's text
                current_text_lines.append(line.strip())
        
        # Don't forget the last speaker
        if current_speaker and current_text_lines:
            segments.append({
                "speaker_id": current_speaker,
                "text": ' '.join(current_text_lines).strip()
            })
        
        return segments
                 
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
        response = await asyncio.to_thread(self.ai_model.message, message)
        fallback = False
        fallback_message = ""
        response_content = ""
        response_content = ""
        fallback = False
        fallback_message = ""
        if response.content is None:
            # Try fallback model up to max_retries times
            # Often this happens because the reasoning model reaches its max tokens during its reasoning.
            max_retries = SPEAKER_IDENTIFICATION_MAX_RETRIES
            fallback = True
            fallback_message = "Used fallback model for speaker identification. "
            logger.warning("Fallback to tiny model used for speaker identification.")
            retry_count = 0
            while retry_count < max_retries:
                response = await asyncio.to_thread(self.tiny_ai_model.message, message)
                if response.content is not None:
                    response_content = response.content
                    break
                retry_count += 1
                logger.warning(f"Fallback tiny model response was empty for speaker {speaker_label}. Retry {retry_count}/{max_retries}...")
            else:
                logger.error("Response from AI is empty after retries. Response error: %s", response.error)
                response_content = f"PROBLEM WITH SPEAKER IDENTIFICATION FOR SPEAKER {speaker_label}."
        else:
            response_content = response.content

        return fallback_message + response_content.strip()

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
        """Process a transcript file through all substages: identify speakers, initiate matching, and process results."""
        logger.info("Processing file for speaker identification: %s", filename)
        
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)
        transcript = read_text_from_content(content)
        
        # --- Special case: Check for single speaker transcripts ---
        unique_speakers = self._extract_unique_speakers(transcript)
        if len(unique_speakers) == 1:
            await self._handle_single_speaker(filename, frontmatter, transcript, list(unique_speakers)[0])
            return
        
        # --- Substage 1: Speaker Identification (if not already done) ---
        if 'identified_speakers' not in frontmatter:
            await self._substage1_identify_speakers(filename, frontmatter, transcript)
            # Reload frontmatter and transcript after modifications
            content = await self.read_file(filename)
            frontmatter = parse_frontmatter_from_content(content)
            transcript = read_text_from_content(content)
        else:
            logger.info("Speakers already identified for: %s", filename)
        
        # --- Substage 2: Initiate Matching UI & Send Discord Notification (if needed) ---
        if 'speaker_matcher_task_id' not in frontmatter:
            await self._substage2_initiate_matching(filename, frontmatter, transcript)
            # Reload frontmatter and transcript after modifications
            content = await self.read_file(filename)
            frontmatter = parse_frontmatter_from_content(content)
            transcript = read_text_from_content(content)
        else:
            logger.info("Speaker matching UI already initiated for: %s", filename)
        
        # --- Substage 3: Poll for Results & Process Them (if needed) ---
        if 'final_speaker_mapping' not in frontmatter:
            await self._substage3_process_results(filename, frontmatter, transcript)
        else:
            logger.info("Speaker matching results already processed for: %s", filename)

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
            
    async def _substage1_identify_speakers(self, filename: str, frontmatter: Dict, transcript: str) -> None:
        """Substage 1: Identify speakers using AI and save to frontmatter."""
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
        
        # Save the identified speakers to frontmatter immediately
        frontmatter['identified_speakers'] = speaker_mapping
        temp_content = frontmatter_to_text(frontmatter) + transcript
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(temp_content)
        os.utime(self.input_dir / filename, None)
        logger.info("Saved identified speakers to frontmatter for: %s", filename)
    
    async def _substage2_initiate_matching(self, filename: str, frontmatter: Dict, transcript: str) -> None:
        """Substage 2: Initiate matching UI service and send Discord notification."""
        speaker_mapping = frontmatter.get('identified_speakers', {})
        
        # Prepare payload for UI service
        speakers_payload = []
        for speaker_id, data in speaker_mapping.items():
            speaker_info = {
                "speaker_id": speaker_id,
                "description": data.get("reason", "No description available.")
            }
            # Only include extracted_name if it's not "unknown"
            if data.get("name") and data["name"].lower() != "unknown":
                speaker_info["extracted_name"] = data["name"]
            speakers_payload.append(speaker_info)
        
        meeting_id = filename
        meeting_context = f"Transcript from meeting: {filename}"
        
        # Parse transcript into segments for the API
        transcript_segments = self._parse_transcript_segments(transcript)
        
        payload = {
            "meeting_id": meeting_id,
            "meeting_context": meeting_context,
            "speakers": speakers_payload,
            "transcript": transcript_segments
        }
        
        # Call UI service
        try:
            logger.info("Calling speaker matcher UI service for: %s", filename)
            async with aiohttp.ClientSession() as session:
                async with session.post(SPEAKER_MATCHER_UI_URL, json=payload) as response:
                    response.raise_for_status()
                    response_data = await response.json()
                    
                    ui_url = response_data.get("ui_url")
                    results_url = response_data.get("results_url")
                    task_id = response_data.get("task_id")
                    
                    if not (ui_url and results_url and task_id):
                        error_msg = f"Incomplete response from UI service: {response_data}"
                        logger.error(error_msg)
                        raise SpeakerIdentificationError(error_msg)
                        
                    logger.info("Successfully called UI service for: %s", filename)
        except (aiohttp.ClientError, json.JSONDecodeError) as e:
            error_msg = f"Error calling UI service: {str(e)}"
            logger.error(error_msg)
            logger.info(f"Payload: {payload}")
            raise SpeakerIdentificationError(error_msg) from e
        
        # Send Discord notification
        try:
            logger.info("Sending Discord notification for: %s", filename)
            dm_text = f"Please help identify speakers for the meeting '{meeting_id}'.\n" \
                      f"Click here to start: {ui_url}"
            success = await self.discord_io.send_dm(TARGET_DISCORD_USER_ID, dm_text)
            
            if not success:
                error_msg = f"Failed to send Discord DM for: {filename}"
                logger.error(error_msg)
                raise SpeakerIdentificationError(error_msg)
                
            logger.info("Successfully sent Discord notification for: %s", filename)
        except Exception as e:
            error_msg = f"Error sending Discord notification: {str(e)}"
            logger.error(error_msg)
            raise SpeakerIdentificationError(error_msg) from e
        
        # Both API call and Discord notification succeeded, update frontmatter
        frontmatter['speaker_matcher_ui_url'] = ui_url
        frontmatter['speaker_matcher_results_url'] = results_url
        frontmatter['speaker_matcher_task_id'] = task_id
        
        # Save updated file
        full_content = frontmatter_to_text(frontmatter) + transcript
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(self.input_dir / filename, None)
        logger.info("Completed speaker matching UI initiation for: %s", filename)
    
    async def _clear_matching_session_fields_and_save(self, filename: str, frontmatter: Dict, transcript: str) -> None:
        """Clear session-specific matching fields and persist the file."""
        for key in (
            'speaker_matcher_ui_url',
            'speaker_matcher_results_url',
            'speaker_matcher_task_id',
        ):
            if key in frontmatter:
                del frontmatter[key]

        full_content = frontmatter_to_text(frontmatter) + transcript
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(self.input_dir / filename, None)
        logger.info("Cleared speaker matcher session fields for: %s", filename)

    async def _substage3_process_results(self, filename: str, frontmatter: Dict, transcript: str) -> None:
        """Substage 3: Poll for matching results and process when ready."""
        results_url = frontmatter.get('speaker_matcher_results_url')
        if not results_url:
            error_msg = f"Missing results URL in frontmatter for: {filename}"
            logger.error(error_msg)
            raise SpeakerIdentificationError(error_msg)
        
        logger.info("Polling for speaker matching results for: %s", filename)

        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        max_attempts = 3
        backoff_seconds = 1.0
        results = None

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for attempt in range(max_attempts):
                    try:
                        async with session.get(results_url) as response:
                            response.raise_for_status()
                            response_data = await response.json()

                            status = response_data.get("status")

                            if status == "PENDING":
                                logger.info("Results not ready yet for: %s. Will retry later.", filename)
                                raise ResultsNotReadyError(
                                    f"Results not ready for task: {response_data.get('task_id')}"
                                )

                            if status != "COMPLETE":
                                error_msg = f"Unexpected status from results endpoint: {status}"
                                logger.error(error_msg)
                                raise SpeakerIdentificationError(error_msg)

                            results = response_data.get("results", {})
                            if not results:
                                error_msg = f"Empty results received for: {filename}"
                                logger.error(error_msg)
                                raise SpeakerIdentificationError(error_msg)

                            logger.info("Successfully received matching results for: %s", filename)
                            logger.info("Speaker mapping from UI: %s", results)
                            break

                    except aiohttp.ClientResponseError as cre:
                        if cre.status in (404, 410):
                            logger.warning(
                                "Results endpoint gone (status %s) for %s. Clearing session fields to re-initiate.",
                                cre.status, filename,
                            )
                            await self._clear_matching_session_fields_and_save(filename, frontmatter, transcript)
                            return
                        logger.warning(
                            "HTTP error polling results (attempt %d/%d) for %s: %s",
                            attempt + 1, max_attempts, filename, cre,
                        )
                    except (aiohttp.ClientError, json.JSONDecodeError, asyncio.TimeoutError) as e:
                        logger.warning(
                            "Transient error polling results (attempt %d/%d) for %s: %s",
                            attempt + 1, max_attempts, filename, e,
                        )

                    if attempt < max_attempts - 1:
                        await asyncio.sleep(backoff_seconds * (2 ** attempt))
                        continue

                    logger.error(
                        "Exhausted retries polling results for %s. Clearing session fields to re-initiate.",
                        filename,
                    )
                    await self._clear_matching_session_fields_and_save(filename, frontmatter, transcript)
                    return
        except ResultsNotReadyError:
            raise
        
        # Process the results
        frontmatter_results = {}
        for speaker_id, speaker_data in results.items():
            frontmatter_speaker_data = dict(speaker_data)
            
            if 'person_id' in frontmatter_speaker_data:
                frontmatter_speaker_data['person_id'] = f"[[{frontmatter_speaker_data['person_id']}]]"
            
            if 'organisation' in frontmatter_speaker_data:
                frontmatter_speaker_data['organisation'] = f"[[{frontmatter_speaker_data['organisation']}]]"
            
            frontmatter_results[speaker_id] = frontmatter_speaker_data
        
        frontmatter['final_speaker_mapping'] = frontmatter_results
        
        if 'identified_speakers' in frontmatter:
            del frontmatter['identified_speakers']
        
        new_transcript = transcript
        for speaker_id, speaker_data in results.items():
            name = speaker_data.get("name", "Unknown")
            organization = speaker_data.get("organisation", "")
            
            if organization:
                replacement = f"{name} ({organization}):"
            else:
                replacement = f"{name}:"
            
            pattern = re.escape(f"{speaker_id}:")
            new_transcript = re.sub(pattern, replacement, new_transcript)
        
        full_content = frontmatter_to_text(frontmatter) + new_transcript
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

            current_transcript = read_text_from_content(content)
            transcript_to_save = current_transcript
            reverted = False

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
                final_mapping = frontmatter.get('final_speaker_mapping')
                if final_mapping:
                    logger.info(f"Detected new speaker format (dict) for {filename}. Reverting names.")
                    modified_transcript = current_transcript
                    logger.debug(f"Reverting transcript text based on mapping: {final_mapping}")
                    for speaker_id, speaker_data in final_mapping.items():
                        name = speaker_data.get("name", "Unknown")
                        organization = speaker_data.get("organisation", "").replace('[[', '').replace(']]', '')
                        original_label = f"{speaker_id}:"
                        if organization:
                            replaced_string = f"{name} ({organization}):"
                        else:
                            replaced_string = f"{name}:"
                        modified_transcript = modified_transcript.replace(replaced_string, original_label)
                    transcript_to_save = modified_transcript
                    reverted = True
                else:
                    logger.warning(f"Cannot revert transcript text for {filename}: Missing 'final_speaker_mapping' (new format) or 'identified_speakers' list (old format).")

            keys_to_remove = [
                'identified_speakers',
                'speaker_matcher_ui_url',
                'speaker_matcher_results_url',
                'speaker_matcher_task_id',
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

