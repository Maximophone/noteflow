"""
Todo Processor

Turns transcripts classified as `todo` — voice memos where the user dictates his own
todos — into Todoist tasks.

These previously landed in an Obsidian "Todo Directory" note. That file is left in
place as a historical record; new memos go to Todoist instead, through the same
machinery the meeting action items use (todoist_base.py): AI-chosen project, section,
due date, urgency and labels, restatements folded into the existing open task, and a
marker label on everything so AI-created tasks can be filtered.

Unlike a meeting, there is no ownership question here — the user dictated the memo, so
every task in it is his.
"""

from typing import Dict, Optional

from .speaker_identifier import SpeakerIdentifier
from .todoist_base import SourceMaterial, TodoistTaskSync
from ..common.frontmatter import read_text_from_content
from config.logging_config import setup_logger
from config.user_config import TODOIST_LABEL_FROM_VOICE_MEMO

logger = setup_logger(__name__)


class TodoProcessor(TodoistTaskSync):
    """Pushes dictated todo memos to Todoist.

    **Pipeline position**: runs on classified transcripts once speakers are known
    (``required_stage = speakers_identified``), on ``category: todo``.

    The stage name is unchanged from when this wrote to Obsidian, so the memos already
    processed then stay processed — only new ones reach Todoist.
    """

    stage_name = "todos_extracted"
    required_stage = SpeakerIdentifier.stage_name
    prompt_name = "transcript_todoist_tasks"
    provenance_label = TODOIST_LABEL_FROM_VOICE_MEMO

    # Every existing todo transcript already carries this stage, so nothing is
    # backfilled. The gate is here for consistency with the meeting stage: it stops a
    # reset of an old memo from silently re-creating tasks years later.
    START_DATE = "2026-07-24"

    # A dictated memo is short; anything much longer than this was misclassified.
    MAX_TRANSCRIPT_CHARS = 8000

    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        if not self.client:
            return False

        if self._is_forced(frontmatter):
            return True

        if frontmatter.get("category") != "todo":
            return False

        return self._within_start_date(frontmatter)

    def _source_material(
        self, filename: str, frontmatter: Dict, content: str
    ) -> Optional[SourceMaterial]:
        transcript = read_text_from_content(content).strip()

        if not transcript:
            logger.info("Empty transcript in %s — nothing to sync", filename)
            return None

        if len(transcript) > self.MAX_TRANSCRIPT_CHARS:
            logger.warning(
                "Transcript %s is %d chars, truncating to %d for task extraction",
                filename, len(transcript), self.MAX_TRANSCRIPT_CHARS,
            )
            transcript = transcript[:self.MAX_TRANSCRIPT_CHARS]

        # The whole transcript is the reference text: a dictated memo has no bullets, so
        # a task's source_line is validated as a verbatim quote from the body.
        return SourceMaterial(
            references=[transcript],
            fields={"transcript": transcript},
        )
