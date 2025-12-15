from pathlib import Path
from typing import Dict
import aiofiles
from datetime import datetime, timedelta, date
import calendar
from .base import NoteProcessor
from ..common.frontmatter import read_text_from_content, parse_frontmatter_from_content
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from .speaker_identifier import SpeakerIdentifier
from prompts.prompts import get_prompt

logger = setup_logger(__name__)

class TodoProcessor(NoteProcessor):
    """Processes todo transcripts and adds them to a todo directory."""
    stage_name = "todos_extracted"
    required_stage = SpeakerIdentifier.stage_name

    def __init__(self, input_dir: Path, directory_file: Path):
        super().__init__(input_dir)

        self.directory_file = directory_file
        self.directory_file.parent.mkdir(parents=True, exist_ok=True)

        # Initialize directory file if it doesn't exist
        if not self.directory_file.exists():
            self.directory_file.write_text("""---
tags:
  - todos
  - directory
---
# Todo Directory

""")
        self.prompt_todos = get_prompt("extract_todos")

    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        if frontmatter.get("category") != "todo":
            return False

        # Check if file is already referenced in directory
        directory_content = self.directory_file.read_text()
        return f"[[{filename}]]" not in directory_content

    async def process_file(self, filename: str) -> None:
        """Process todos from a note."""
        logger.info("Processing todos from: %s", filename)
        
        content = await self.read_file(filename)

        # Parse frontmatter and content
        frontmatter = parse_frontmatter_from_content(content)
        
        if not frontmatter:
            logger.warning("No frontmatter found in %s", filename)
            return

        transcript = read_text_from_content(content)
        date_str = frontmatter.get('date', '')
        if isinstance(date_str, date):
            recording_date = date_str
        else:
            recording_date = datetime.fromisoformat(date_str) if date_str else datetime.now()
        recording_date_str = recording_date.strftime('%Y-%m-%d')
        weekday = calendar.day_name[recording_date.weekday()]

        # Extract todos using AI
        todos_prompt = self.prompt_todos.format(recording_date_str=recording_date_str, weekday=weekday)
        message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=todos_prompt + "\n\nTranscript:\n" + transcript
            )]
        )
        todos_text = self.ai_model.message(message).content

        # Prepare the content to append
        append_content = f"\n## Todos from [[{filename}]] - {date_str}\n\n{todos_text}\n\n---\n"

        # Append to todo directory
        async with aiofiles.open(self.directory_file, "a", encoding='utf-8') as f:
            await f.write(append_content)

        logger.info("Processed todos from: %s", filename)

