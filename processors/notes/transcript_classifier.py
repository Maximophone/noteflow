from pathlib import Path
from typing import Dict, Optional
import aiofiles
from .base import NoteProcessor
from ..common.frontmatter import read_frontmatter_from_file, set_frontmatter_in_file, read_text_from_content
from prompts.prompts import get_prompt
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger

logger = setup_logger(__name__)

# Valid categories that can be forced via source_tags
VALID_CATEGORIES = {"meeting", "idea", "todo", "meditation", "diary", "unsorted"}

class TranscriptClassifier(NoteProcessor):
    """Classifies transcripts using AI based on content."""
    stage_name = "classified"
    required_stage = "transcribed" # Assuming transcription is needed first

    def __init__(self, input_dir: Path):
        super().__init__(input_dir)
        self.prompt_classify = get_prompt("classify_transcript")
        
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        # Process if it's a transcription and hasn't been classified
        return "transcription" in frontmatter.get("tags", [])
        
    def classify(self, text: str) -> str:
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=self.prompt_classify + text
            )]
        )
        return self.tiny_ai_model.message(message).content
        
    def _get_forced_category(self, frontmatter: Dict) -> Optional[str]:
        """Check if a category is forced via source_tags."""
        source_tags = frontmatter.get("source_tags", [])
        for tag in source_tags:
            if tag.lower() in VALID_CATEGORIES:
                return tag.lower()
        return None

    async def process_file(self, filename: str) -> None:
        """Process a transcript file."""
        logger.info("Classifying transcript: %s", filename)
        
        # Read file content
        content = await self.read_file(filename)
        file_path = self.input_dir / filename
        frontmatter = read_frontmatter_from_file(file_path)
        
        # Check for forced category via source_tags
        forced_category = self._get_forced_category(frontmatter)
        if forced_category:
            category = forced_category
            logger.info("Using forced category from source_tags: %s", category)
        else:
            # Get text after frontmatter and classify with AI
            text = read_text_from_content(content)
            category = self.classify(text)
            logger.info("AI classified as: %s", category)
        
        # Update frontmatter
        frontmatter["category"] = category
        if "tags" not in frontmatter:
            frontmatter["tags"] = []
        frontmatter["tags"].append(category)
        
        set_frontmatter_in_file(file_path, frontmatter)
        logger.info("Updated classification for: %s", filename)





