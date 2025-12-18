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

logger = setup_logger(__name__)

class AudioTranscriber:
    """Handles the transcription of audio files to markdown and JSON."""
    
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
        self.files_in_process: Set[str] = set()
        
        # Set up AssemblyAI
        assemblyai.settings.api_key = api_key
        self.transcriber = assemblyai.Transcriber()
        self.config = assemblyai.TranscriptionConfig(
            speaker_labels=True,
            language_detection=True,
            word_boost=["Pause IA", "Pause AI", "Moiri"],
        )
        
        # Create necessary directories
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)

        # Add AI model for title generation
        self.ai_model = AI(BIG_MODEL)
        self.prompt_title = get_prompt("transcript_title")

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
        # TODO: Make this properly async when AssemblyAI supports it
        transcript = self.transcriber.transcribe(str(file_path), self.config)
        return transcript
    
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
            
            # Process speaker labels with LeMUR
            text_with_speaker_labels = "\n".join(
                f"Speaker {utt.speaker}:\n{utt.text}\n" 
                for utt in transcript.utterances
            )
            
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
                title = self.generate_title(transcript.text)
            
            # Ensure title is not empty after potential cleaning, fallback if needed
            if not title:
                 logger.warning("Title became empty after tag removal for file %s. Using generated title.", filename)
                 title = self.generate_title(transcript.text)

            logger.info("Processing title: %s", title)
            logger.info("Extracted source tags: %s", source_tags)
            
            # Create safe filename base
            safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()
            base_filename = f"{date_str}-{safe_title}"
            
            # Save JSON response
            json_filename = f"{base_filename}.json"
            json_path = self.output_dir / json_filename
            async with aiofiles.open(json_path, "w") as f:
                await f.write(json.dumps(transcript.json_response, indent=2))
            
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
            
        except Exception as e:
            logger.error("Error processing %s: %s", filename, str(e))
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
            self.files_in_process.add(filename)
            logger.info("Queuing transcription: %s", filename)
            task = asyncio.create_task(self.process_single_file(filename))
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks)





