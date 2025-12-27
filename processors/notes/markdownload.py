from pathlib import Path
from typing import Dict
import aiofiles
from .base import NoteProcessor
from ..common.frontmatter import parse_frontmatter_from_content
from ai_core import AI
from prompts.prompts import get_prompt

from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger

logger = setup_logger(__name__)


class MarkdownloadProcessor(NoteProcessor):
    """Processes downloaded web pages and creates source notes with summaries."""
    
    stage_name = "markdownload_processed"
    # No required_stage needed

    def __init__(self, input_dir: Path, output_dir: Path, template_path: Path):
        super().__init__(input_dir)
        self.output_dir = output_dir
        self.template_path = template_path
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_summary = get_prompt("summarise_markdownload")
        
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        if not (filename.startswith("markdownload_") and filename.endswith(".md")):
            return False
            
        # Check if output file exists
        new_filename = filename[13:]  # Remove "markdownload_" prefix
        return not (self.output_dir / new_filename).exists()
        
    async def process_file(self, filename: str) -> None:
        """Process a markdownload file."""
        logger.info("Processing markdownload: %s", filename)
        
        # Read source file
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)
        
        # Generate summary
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=self.prompt_summary + "\n\nContent:\n" + content
            )]
        )
        summary = self.ai_model.message(message).content
        
        # Read template
        async with aiofiles.open(self.template_path, 'r', encoding='utf-8') as f:
            template = await f.read()
        
        # Prepare new filename and content
        new_filename = filename[13:]  # Remove "markdownload_" prefix
        fname = filename.split(".")[0]
        
        if frontmatter:
            url = frontmatter.get("url", "")
            template = template.replace("url: ", f"url: {url}")
            template = template.replace("{{title}}", new_filename.split(".")[0])
            template = template.replace("markdownload:", f'markdownload: "[[{fname}]]"')
        
        # Save to output directory
        output_path = self.output_dir / new_filename
        async with aiofiles.open(output_path, 'w', encoding='utf-8') as f:
            await f.write(template + "\n" + summary)
            
        logger.info("Processed markdownload: %s", filename)





