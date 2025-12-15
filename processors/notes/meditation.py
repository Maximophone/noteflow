from pathlib import Path
from typing import Dict
import aiofiles
from .base import NoteProcessor
from ..common.frontmatter import read_text_from_content, parse_frontmatter_from_content, frontmatter_to_text
from ..common.markdown import sanitize_filename
from prompts.prompts import get_prompt

from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from .speaker_identifier import SpeakerIdentifier

logger = setup_logger(__name__)

class MeditationProcessor(NoteProcessor):
    """Processes meditation transcripts into structured notes."""
    stage_name = "meditation_processed"
    required_stage = SpeakerIdentifier.stage_name

    def __init__(self, input_dir: Path, output_dir: Path):
        super().__init__(input_dir)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.meditation_prompt = get_prompt("process_meditation")
        
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        if frontmatter.get("category") != "meditation":
            return False
        # Check if output file exists
        return not (self.output_dir / filename).exists()
        
    async def process_file(self, filename: str) -> None:
        """Process a meditation note."""
        logger.info("Processing meditation: %s", filename)

            
        content = await self.read_file(filename)
        
        # Parse frontmatter and content
        frontmatter = parse_frontmatter_from_content(content)
        if not frontmatter:
            logger.warning("No frontmatter found in %s", filename)
            return
            
        transcript = read_text_from_content(content)
        
        # Generate meditation summary
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=self.meditation_prompt + transcript
            )]
        )
        ai_response = self.ai_model.message(message).content
        
        # Create audio link
        original_file = frontmatter.get('original_file', '')
        sanitized_filename = original_file.replace(" ", "%20")
        audio_link = f"G:/My Drive/NoteFlow/Audio/Processed/{sanitized_filename}"
        
        # Create new frontmatter
        new_frontmatter = {
            "title": frontmatter.get("title", ""),
            "date": frontmatter.get("date", ""),
            "tags": ["meditation"],
            "original_transcript": f"[[Transcriptions/{filename}]]",
            "audio_file": audio_link,
            "category": "meditation"
        }
        
        # Combine into final markdown
        final_content = (
            frontmatter_to_text(new_frontmatter) +
            ai_response +
            "\n\n## Original Transcription\n" +
            transcript
        )
        
        # Save to output directory
        output_path = self.output_dir / filename
        async with aiofiles.open(output_path, 'w', encoding='utf-8') as f:
            await f.write(final_content)
            
        logger.info("Saved meditation note: %s", filename)

