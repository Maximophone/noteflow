from pathlib import Path
from typing import Dict
import aiofiles
from .base import NoteProcessor
from ..common.frontmatter import read_text_from_content, parse_frontmatter_from_content, frontmatter_to_text
from ai_core import AI
from prompts.prompts import get_prompt

from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from .transcript_classifier import TranscriptClassifier

logger = setup_logger(__name__)

class DiaryProcessor(NoteProcessor):
    """Processes diary transcripts into clean, well-formatted entries."""
    stage_name = "diary_processed"
    required_stage = TranscriptClassifier.stage_name

    def __init__(self, input_dir: Path, output_dir: Path):
        super().__init__(input_dir)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_format = get_prompt("diary_format")
        
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        if frontmatter.get("category") != "diary":
            return False
            
        # Check if diary entry already exists
        output_path = self.output_dir / filename
        return not output_path.exists()
        
    async def process_file(self, filename: str) -> None:
        """Process a diary entry."""
        logger.info("Processing diary entry: %s", filename)
        
        # Read source transcript
        content = await self.read_file(filename)
        
        # Parse frontmatter and content
        frontmatter = parse_frontmatter_from_content(content)
        
        if not frontmatter:
            logger.warning("No frontmatter found in %s", filename)
            return
            
        transcript = read_text_from_content(content)
        
        # Format the entry using AI
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=self.prompt_format + "\n\nEntry:\n" + transcript
            )]
        )
        formatted_entry = self.ai_model.message(message).content
        
        # Create new frontmatter
        new_frontmatter = {
            "title": frontmatter.get("title", ""),
            "date": frontmatter.get("date", ""),
            "tags": ["diary"],
            "original_transcript": f"[[Transcriptions/{filename}]]",
        }
        
        # Combine into final content
        final_content = (
            frontmatter_to_text(new_frontmatter) +
            "# Diary Entry\n\n" +
            formatted_entry +
            "\n\n## Original Transcription\n" +
            transcript
        )
        
        # Save to output directory
        output_path = self.output_dir / filename
        async with aiofiles.open(output_path, 'w', encoding='utf-8') as f:
            await f.write(final_content)
            
        logger.info("Processed diary entry: %s", filename)

