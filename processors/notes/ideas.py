from pathlib import Path
from typing import Dict
import aiofiles
from .base import NoteProcessor
from ..common.frontmatter import read_text_from_content, parse_frontmatter_from_content
from ..common.markdown import create_wikilink
from ai_core.types import Message, MessageContent
from prompts.prompts import get_prompt

from config.logging_config import setup_logger
from .speaker_identifier import SpeakerIdentifier

logger = setup_logger(__name__)


class IdeaProcessor(NoteProcessor):
    """Processes idea transcripts and adds them to an idea directory."""
    
    stage_name = "ideas_extracted"
    required_stage = SpeakerIdentifier.stage_name

    def __init__(self, input_dir: Path, directory_file: Path):
        super().__init__(input_dir)

        self.directory_file = directory_file
        self.directory_file.parent.mkdir(parents=True, exist_ok=True)

        self.prompt_ideas = get_prompt("idea_extract")
        
        # Initialize directory file if it doesn't exist
        if not self.directory_file.exists():
            self.directory_file.write_text("""---
tags:
  - ideas
  - directory
---
# Ideas Directory

""")
        
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        if frontmatter.get("category") != "idea":
            return False
            
        # Check if file is already referenced in directory
        directory_content = self.directory_file.read_text()
        return f"[[{filename}]]" not in directory_content
        
    async def process_file(self, filename: str) -> None:
        """Process ideas from a note."""
        logger.info("Processing ideas from: %s", filename)
        
        content = await self.read_file(filename)
        
        # Parse frontmatter and content
        frontmatter = parse_frontmatter_from_content(content)
        
        if not frontmatter:
            logger.warning("No frontmatter found in %s", filename)
            return
            
        transcript = read_text_from_content(content)
        
        # Extract ideas using AI
        ideas_prompt = self.prompt_ideas + "\n\nTranscript:\n" + transcript
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=ideas_prompt + transcript
            )]
        )
        ideas_text = self.ai_model.message(message).content
        
        # Prepare the content to append
        date_str = frontmatter.get('date', '')
        append_content = f"\n## Ideas from [[{filename}]] - {date_str}\n\n{ideas_text}\n\n---\n"
        
        # Append to ideas directory
        async with aiofiles.open(self.directory_file, "a", encoding='utf-8') as f:
            await f.write(append_content)
            
        logger.info("Processed ideas from: %s", filename)





