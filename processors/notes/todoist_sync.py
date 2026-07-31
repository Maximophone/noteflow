"""
Todoist Sync Processor (meetings)

Runs after a meeting has been summarised and pushes the action items that belong to
the user into Todoist, with an AI-chosen project, section, due date, urgency and labels.

The action items come from the *validated* meeting summary, so they have already been
through the human review gate in MeetingSummaryGenerator's Obsidian form. That is why
this stage writes to Todoist without asking for a second confirmation. Every task it
creates carries a marker label so AI-created tasks can be filtered and cleaned up.

The Todoist machinery itself lives in todoist_base.py, shared with the dictated-todo
stage in todo.py.
"""

import re
from typing import Dict, List, Optional

from .meeting_summary_generator import MeetingSummaryGenerator
from .todoist_base import SourceMaterial, TodoistTaskSync
from config.logging_config import setup_logger
from config.user_config import TODOIST_LABEL_FROM_MEETING, USER_NAME

logger = setup_logger(__name__)


class TodoistSyncProcessor(TodoistTaskSync):
    """Pushes the user's meeting action items to Todoist.

    **Pipeline position**: runs on validated meeting summaries
    (``required_stage = meeting_summarized``).

    **Flow**:
        1. Extract the ``## Action Items`` block from the validated summary
        2. Skip early (no AI call) if the user is not named anywhere in it
        3. Fetch live Todoist state: projects, sections, labels, open tasks
        4. One AI call decides ownership, wording, due date, urgency, project, section,
           labels and whether each item duplicates an already-open task
        5. Validate the response against the input, then create or update tasks
    """

    stage_name = "todoist_synced"
    required_stage = MeetingSummaryGenerator.stage_name
    prompt_name = "meeting_todoist_tasks"
    provenance_label = TODOIST_LABEL_FROM_MEETING

    # Meetings dated before this are skipped unless forced. Without this gate, adding
    # the stage would have fired on every already-summarised transcript in the vault.
    START_DATE = "2026-07-24"

    # ===== Eligibility =====

    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        if not self.client:
            return False

        if self._is_forced(frontmatter):
            return True

        if frontmatter.get("category") != "meeting":
            return False

        return self._within_start_date(frontmatter)

    # ===== Summary parsing =====

    def _extract_summary(self, content: str) -> Optional[str]:
        """Pull the validated summary out of its callout block."""
        start = content.find(MeetingSummaryGenerator.SUMMARY_START)
        end = content.find(MeetingSummaryGenerator.SUMMARY_END)
        if start == -1 or end == -1:
            return None
        return content[start + len(MeetingSummaryGenerator.SUMMARY_START):end]

    @staticmethod
    def _extract_section(summary: str, heading: str) -> str:
        """Return the body of a '## <heading>' section of the summary."""
        match = re.search(
            rf'^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s|\Z)',
            summary,
            re.MULTILINE | re.DOTALL,
        )
        return match.group(1).strip() if match else ""

    @staticmethod
    def _action_item_lines(action_items: str) -> List[str]:
        """Split the action items block into individual bullet lines."""
        lines = []
        for line in action_items.split('\n'):
            stripped = line.strip()
            if stripped.startswith(('-', '*')) and len(stripped) > 2:
                lines.append(stripped)
        return lines

    # ===== Source material =====

    def _source_material(
        self, filename: str, frontmatter: Dict, content: str
    ) -> Optional[SourceMaterial]:
        summary = self._extract_summary(content)
        if summary is None:
            raise ValueError(f"No validated meeting summary found in: {filename}")

        action_lines = self._action_item_lines(self._extract_section(summary, 'Action Items'))
        if not action_lines:
            logger.info("No action items in %s — nothing to sync", filename)
            return None

        # Cheap guard: if the user isn't named at all, there is nothing to own and no
        # reason to pay for an AI call.
        if not any(USER_NAME.lower() in line.lower() for line in action_lines):
            logger.info("No action items mention %s in %s — nothing to sync", USER_NAME, filename)
            return None

        meeting_context = '\n\n'.join(
            part for part in (
                f"## Summary\n{self._extract_section(summary, 'Summary')}",
                f"## Decisions Made\n{self._extract_section(summary, 'Decisions Made')}",
            ) if part.strip()
        )

        return SourceMaterial(
            references=action_lines,
            fields={
                "summary_context": meeting_context,
                "action_items": '\n'.join(action_lines),
            },
        )
