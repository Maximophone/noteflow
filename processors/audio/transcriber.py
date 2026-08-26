from pathlib import Path
import json
import asyncio
import aiofiles
import assemblyai
from typing import Set, Dict
from datetime import datetime

from .utils import get_recording_date
from ..common.frontmatter import frontmatter_to_text

from ai_core import AI
from ai_core.types import Message, MessageContent
import re
import os
import asyncio
from config.logging_config import setup_logger
from config.services_config import BIG_MODEL

from prompts.prompts import get_prompt
from ..common import error_registry

logger = setup_logger(__name__)

class PermanentTranscriptionError(Exception):
    """A failure that retrying cannot fix — no speech, or an unsupported language.

    Separated from transient failures (network, timeouts) because those should
    keep retrying, while these must stop and be shown to the user instead of
    failing invisibly forever.
    """


class AudioTranscriber:
    """Handles the transcription of audio files to markdown and JSON."""

    # The pipeline stage this class performs, so a failure appears in the inbox
    # under the same label the note frontmatter would carry.
    stage_name = "transcribed"
    
    def __init__(
        self, 
        input_dir: Path,
        output_dir: Path,
        processed_dir: Path,
        api_key: str
    ):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.processed_dir = processed_dir
        # Where files that can never be transcribed are parked, alongside the
        # Incoming and Processed folders so they are easy to find and re-drop.
        self.failed_dir = input_dir.parent / "Failed"
        self.files_in_process: Set[str] = set()
        # filename -> time of last failure, used to back off before retrying
        self.failed_recently: Dict[str, datetime] = {}
        
        # Set up AssemblyAI
        assemblyai.settings.api_key = api_key
        assemblyai.settings.http_timeout = 3600  # 1 hour for very slow hotel connections
        self.transcriber = assemblyai.Transcriber()
        self.config = assemblyai.TranscriptionConfig(
            speaker_labels=True,
            language_detection=True,
            # Universal-3.5 Pro: ~34% lower WER on French than universal-2, plus
            # native code-switching for mixed FR/EN meetings. Covers 18 languages;
            # anything outside them errors rather than silently downgrading.
            speech_models=["universal-3-5-pro"],
            # word_boost is rejected by the Pro models; keyterms_prompt replaces it.
            keyterms_prompt=["Pause IA", "Pause AI", "Moiri"],
        )
        
        # Create necessary directories
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)

        # Add AI model for title generation
        self.ai_model = AI(BIG_MODEL)
        self.prompt_title = get_prompt("transcript_title")
        
        # Limit to 1 concurrent AssemblyAI call to avoid network overload
        self._transcribe_semaphore = asyncio.Semaphore(1)

    def generate_title(self, text: str) -> str:
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=self.prompt_title + text
            )]
        )
        return self.ai_model.message(message).content
        
    async def transcribe_audio_file(self, file_path: Path) -> assemblyai.Transcript:
        """Transcribe a single audio file using AssemblyAI."""
        # Limit to 1 concurrent call to avoid network overload on slow connections
        async with self._transcribe_semaphore:
            # Run in thread pool to avoid blocking the event loop (prevents Discord heartbeat issues)
            transcript = await asyncio.to_thread(
                self.transcriber.transcribe, str(file_path), self.config
            )
        return transcript
    
    async def delete_transcript_data(self, transcript_id: str) -> None:
        """Delete transcript and audio data from AssemblyAI's servers.
        
        This removes all data associated with the transcript, including the
        uploaded audio file if it was uploaded via AssemblyAI's /upload endpoint.
        """
        try:
            await asyncio.to_thread(
                assemblyai.Transcript.delete_by_id, transcript_id
            )
            logger.info("Deleted transcript data from AssemblyAI: %s", transcript_id)
        except Exception as e:
            logger.warning("Failed to delete transcript data from AssemblyAI: %s - %s", transcript_id, str(e))
    
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        # Skip hidden files (like .DS_Store on macOS)
        if filename.startswith('.'):
            return False
        _, ext = os.path.splitext(filename)
        # Only process audio files, skip video files and other non-audio files
        audio_extensions = ['.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.wma', '.aiff']
        video_extensions = ['.mkv', '.mp4', '.avi', '.mov', '.wmv', '.webm']
        excluded_extensions = ['.ini', '.txt', '.json', '.md']
        
        ext_lower = ext.lower()
        # If it's a known audio format, process it
        if ext_lower in audio_extensions:
            return True
        # If it's a video or excluded format, skip it
        if ext_lower in video_extensions or ext_lower in excluded_extensions:
            return False
        # For unknown extensions, skip to be safe
        return False
    
    async def process_single_file(self, filename: str) -> None:
        """Process a single audio file: transcribe and save outputs."""
        file_path = self.input_dir / filename
        
        try:
            # Get recording date
            recording_date = get_recording_date(file_path)
            date_str = recording_date.strftime("%Y-%m-%d")
            
            # Transcribe
            transcript = await self.transcribe_audio_file(file_path)

            # AssemblyAI reports a failed job as a completed call with an error
            # status, and utterances left as None. Iterating that raised
            # "'NoneType' object is not iterable" every cycle for hours, with the
            # real reason ("no spoken audio") never reaching the log or the user.
            if transcript.status == assemblyai.TranscriptStatus.error:
                raise PermanentTranscriptionError(transcript.error or "transcription failed")
            if transcript.utterances is None:
                raise PermanentTranscriptionError(
                    "no speech found in the audio (no utterances returned)"
                )

            # Process speaker labels with LeMUR
            text_with_speaker_labels = "\n".join(
                f"Speaker {utt.speaker}:\n{utt.text}\n" 
                for utt in transcript.utterances
            )
            
            # Store transcript data we need before deletion
            transcript_text = transcript.text
            transcript_json = transcript.json_response
            transcript_id = transcript.id
            
            # Delete transcript and audio data from AssemblyAI's servers
            await self.delete_transcript_data(transcript_id)
            
            title = None
            source_tags = [] # Initialize source_tags
            # Check if original filename starts with date pattern
            filename_without_ext = file_path.stem
            if filename_without_ext.startswith(date_str):
                # Extract everything after the date as title
                title_parts = filename_without_ext[len(date_str):].strip()
                if title_parts.startswith("-"):
                    raw_title = title_parts[1:].strip()
                    # Extract tags from the raw title
                    source_tags = re.findall(r"#([a-zA-Z0-9_]+)", raw_title)
                    # Remove tags from the title
                    cleaned_title = re.sub(r"#([a-zA-Z0-9_]+)", "", raw_title)
                    # Clean up extra hyphens and spaces
                    cleaned_title = re.sub(r'-+', '-', cleaned_title).strip('-').strip()
                    title = cleaned_title if cleaned_title else None # Assign cleaned title, or None if empty
            
            if title is None:
                # Generate new title if none found in filename or after cleaning
                title = self.generate_title(transcript_text)
            
            # Ensure title is not empty after potential cleaning, fallback if needed
            if not title:
                 logger.warning("Title became empty after tag removal for file %s. Using generated title.", filename)
                 title = self.generate_title(transcript_text)

            logger.info("Processing title: %s", title)
            logger.info("Extracted source tags: %s", source_tags)
            
            # Create safe filename base
            safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()
            base_filename = f"{date_str}-{safe_title}"
            
            # Save JSON response
            json_filename = f"{base_filename}.json"
            json_path = self.output_dir / json_filename
            async with aiofiles.open(json_path, "w") as f:
                await f.write(json.dumps(transcript_json, indent=2))
            
            logger.debug("Saved JSON: %s", json_filename)

            new_filename = date_str + "_" + filename

            # Prepare frontmatter
            frontmatter = {
                "tags": ["transcription"],
                "date": date_str,
                "original_file": new_filename,
                "title": title,
                "source_tags": source_tags, # Add extracted tags
                "json_data": json_filename,
                "AutoNoteMover": "disable",
                "processing_stages": ["transcribed"]  # Initialize as list
            }
            
            full_content = frontmatter_to_text(frontmatter) + text_with_speaker_labels

            md_filename = f"{base_filename}.md"
            md_path = self.output_dir / md_filename

            async with aiofiles.open(md_path, "w", encoding='utf-8') as f:
                await f.write(full_content)
            
            logger.debug("Saved MD: %s", md_filename)

            # Move original file to processed folder
            file_path.rename(self.processed_dir / new_filename)
            
            logger.info("Processed: %s -> %s", filename, md_filename)
            
        except PermanentTranscriptionError as e:
            # Retrying cannot help, so stop: move the file aside and surface it in
            # the inbox. Left in place it would re-queue every cycle indefinitely.
            logger.error("Cannot transcribe %s: %s", filename, e)
            self.failed_dir.mkdir(parents=True, exist_ok=True)
            parked = self.failed_dir / filename
            try:
                file_path.rename(parked)
            except OSError as move_error:
                logger.error("Could not move %s aside: %s", filename, move_error)
                parked = file_path
                self.failed_recently[filename] = datetime.now()
            error_registry.record_error(parked, self.stage_name, str(e))
        except Exception as e:
            logger.error("Error processing %s: %s", filename, str(e))
            # Record the failure to prevent immediate re-queueing
            self.failed_recently[filename] = datetime.now()
            raise
        finally:
            self.files_in_process.remove(filename)

    async def process_all(self) -> None:
        """Process all audio files in the input directory."""
        tasks = []
        for file_path in self.input_dir.iterdir():
            await asyncio.sleep(0)
            filename = file_path.name
            if not self.should_process(filename, None):
                continue
            # Skip if already being processed
            if filename in self.files_in_process:
                continue
            # Skip if failed recently (within last 5 minutes)
            if filename in self.failed_recently:
                last_fail = self.failed_recently[filename]
                if (datetime.now() - last_fail).total_seconds() < 300:
                    continue
                else:
                    del self.failed_recently[filename]
                    
            self.files_in_process.add(filename)
            logger.info("Queuing transcription: %s", filename)
            task = asyncio.create_task(self.process_single_file(filename))
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks)





