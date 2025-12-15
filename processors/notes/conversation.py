from pathlib import Path
from typing import Dict
import aiofiles
from .base import NoteProcessor
from ..common.frontmatter import read_text_from_content, parse_frontmatter_from_content, frontmatter_to_text
from prompts.prompts import get_prompt

from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger

logger = setup_logger(__name__)

class ConversationProcessor(NoteProcessor):
    """Processes conversation notes and reformats them with AI-generated summaries."""
    stage_name = "conversation_processed"
    # No required_stage specified

    def __init__(self, input_dir: Path):
        super().__init__(input_dir)
        self.prompt_format = get_prompt("conversation_format")
        self.prompt_summary = get_prompt("conversation_summary")
        
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        return True
        
    async def process_file(self, filename: str) -> None:
        """Process a conversation transcript."""
        logger.info("Processing conversation: %s", filename)
        
        # Read source file
        content = await self.read_file(filename)
        
        # Parse frontmatter if it exists
        has_frontmatter = content.startswith('---')
        if has_frontmatter:
            frontmatter = parse_frontmatter_from_content(content)
            text = read_text_from_content(content)
        else:
            frontmatter = {
                'processing_stages': []
            }
            text = content.strip()
        
        # Format the conversation using AI
        format_message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=self.prompt_format + "\n\nTranscript:\n" + text
            )]
        )
        formatted_conversation = self.ai_model.message(format_message).content
        
        # Generate summary
        summary_message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=self.prompt_summary + "\n\nTranscript:\n" + text
            )]
        )
        summary = self.ai_model.message(summary_message).content
        
        # Update frontmatter
        frontmatter['tags'] = frontmatter.get('tags', [])
        if "conversation" not in frontmatter["tags"]:
            frontmatter['tags'].append("conversation")

        # Combine into final content
        final_content = (
            frontmatter_to_text(frontmatter) +
            "## Summary\n" +
            summary + "\n\n" +
            "## Conversation\n" +
            formatted_conversation
        )
        
        # Save back to same file
        async with aiofiles.open(self.input_dir / filename, 'w', encoding='utf-8') as f:
            await f.write(final_content)
            
        logger.info("Processed conversation: %s", filename)

