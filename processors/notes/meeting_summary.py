from pathlib import Path
from typing import Dict
import aiofiles
from .base import NoteProcessor
from ..common.frontmatter import read_frontmatter_from_file
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from prompts.prompts import get_prompt

from .meeting import MeetingProcessor

logger = setup_logger(__name__)


class MeetingSummaryProcessor(NoteProcessor):
    """Adds AI-generated summaries to meeting notes based on transcripts."""
    
    stage_name = "meeting_summary_generated"
    required_stage = MeetingProcessor.stage_name

    def __init__(self, input_dir: Path, transcript_dir: Path):
        super().__init__(input_dir)
        self.transcript_dir = transcript_dir
        
    def should_process(self, filename: str, frontmatter: Dict) -> bool:            
        transcript_path = self.transcript_dir / filename
        if not transcript_path.exists():
            return False

        transcript_frontmatter = read_frontmatter_from_file(transcript_path)
        if "speakers_identified" not in transcript_frontmatter.get("processing_stages", []):
            return False

        return True
        
    async def process_file(self, filename: str) -> None:
        """Generate a meeting summary."""
        logger.info("Generating meeting summary for: %s", filename)
        
        note_content = await self.read_file(filename)
        
        pre_meeting_notes = ""
        meeting_notes = ""
        
        if "# Pre-Meeting Notes" in note_content:
            pre_meeting_parts = note_content.split("# Pre-Meeting Notes", 1)
            if len(pre_meeting_parts) > 1:
                next_section = pre_meeting_parts[1].find("\n# ")
                if next_section != -1:
                    pre_meeting_notes = pre_meeting_parts[1][:next_section].strip()
                else:
                    pre_meeting_notes = pre_meeting_parts[1].strip()
        
        if "# Meeting Notes" in note_content:
            meeting_parts = note_content.split("# Meeting Notes", 1)
            if len(meeting_parts) > 1:
                next_section = meeting_parts[1].find("\n# ")
                if next_section != -1:
                    meeting_notes = meeting_parts[1][:next_section].strip()
                else:
                    meeting_notes = meeting_parts[1].strip()
        
        context = ""
        if pre_meeting_notes:
            context += f"""Pre-Meeting Notes:
{pre_meeting_notes}

"""
        if meeting_notes:
            context += f"""Meeting Notes:
{meeting_notes}

"""

        transcript_path = self.transcript_dir / filename
        transcript_content = transcript_path.read_text()
        
        summary_prompt = get_prompt("meeting_summary").format(context=context)
        
        summary_message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=summary_prompt + "Transcript:\n" + transcript_content
            )]
        )
        summary = self.ai_model.message(summary_message).content
        
        next_steps_prompt = get_prompt("meeting_next_steps").format(context=context)
        
        next_steps_message = Message(
            role="user",
            content=[MessageContent(
                type="text",
                text=next_steps_prompt + "Transcript:\n" + transcript_content
            )]
        )
        next_steps = self.ai_model.message(next_steps_message).content
        
        new_summary = f"""## Executive Summary

### Key Points
{summary}

### Next Steps
{next_steps}
"""
        
        parts = note_content.split('## Executive Summary')
        if len(parts) != 2:
            logger.warning("Could not find Executive Summary section in %s", filename)
            return
            
        rest = parts[1]
        next_section_match = rest.find('\n## ')
        if next_section_match != -1:
            new_content = parts[0] + new_summary + rest[next_section_match:]
        else:
            new_content = parts[0] + new_summary
            
        async with aiofiles.open(self.input_dir / filename, 'w', encoding='utf-8') as f:
            await f.write(new_content)
            
        logger.info("Added summary to meeting note: %s", filename)

