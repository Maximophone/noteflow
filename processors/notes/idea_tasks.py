"""
Idea Task Processor

Pulls commitments out of transcripts classified as `idea` and pushes them to Todoist.

Idea notes are monologues — thinking out loud, working through a problem. Most contain
no tasks, and the prompt is written so an empty result is the normal outcome. The reason
to run this at all is that the classifier calls anything monologue-shaped an `idea`, so
working sessions land here alongside pure reflection, and their commitments were
otherwise going nowhere.

Because a thinking-aloud note is weak evidence of where a task belongs, this stage biases
hard toward the Inbox: a project is chosen only for concrete professional work that
clearly belongs to it.
"""

from typing import Dict, Optional

from .speaker_identifier import SpeakerIdentifier
from .todoist_base import SourceMaterial, TodoistTaskSync
from ..common.frontmatter import read_text_from_content
from config.logging_config import setup_logger
from config.user_config import TODOIST_LABEL_FROM_IDEA_NOTE

logger = setup_logger(__name__)


class IdeaTaskProcessor(TodoistTaskSync):
    """Pushes commitments found in idea notes to Todoist.

    **Pipeline position**: runs on classified transcripts once speakers are known
    (``required_stage = speakers_identified``), on ``category: idea``. Independent of
    IdeaProcessor and IdeaCleanupProcessor, which handle the note itself.
    """

    stage_name = "idea_tasks_synced"
    required_stage = SpeakerIdentifier.stage_name
    prompt_name = "idea_todoist_tasks"
    provenance_label = TODOIST_LABEL_FROM_IDEA_NOTE

    # This stage is new, so unlike the todo memos there is no existing stage marker to
    # keep the 148 historical idea notes out. The gate is what keeps them out.
    START_DATE = "2026-07-31"

    # Idea notes ramble and run much longer than a dictated memo.
    MAX_TRANSCRIPT_CHARS = 20000

    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        if not self.client:
            return False

        if self._is_forced(frontmatter):
            return True

        if frontmatter.get("category") != "idea":
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
                "Idea note %s is %d chars, truncating to %d for task extraction",
                filename, len(transcript), self.MAX_TRANSCRIPT_CHARS,
            )
            transcript = transcript[:self.MAX_TRANSCRIPT_CHARS]

        return SourceMaterial(
            references=[transcript],
            fields={"transcript": transcript},
        )
