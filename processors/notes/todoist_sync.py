"""
Todoist Sync Processor

Runs after a meeting has been summarised and pushes the action items that belong to
the user into Todoist, with an AI-chosen section, due date, urgency and labels.

The action items come from the *validated* meeting summary, so they have already been
through the human review gate in MeetingSummaryGenerator's Obsidian form. That is why
this stage writes to Todoist without asking for a second confirmation. Every task it
creates carries a marker label so AI-created tasks can be filtered and cleaned up.
"""

import asyncio
import calendar
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiofiles

from .base import NoteProcessor
from .meeting_summary_generator import MeetingSummaryGenerator
from ..common.frontmatter import (
    frontmatter_to_text,
    parse_frontmatter_from_content,
    read_text_from_content,
)
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from config.paths import PATHS
from config.secrets import TODOIST_API_TOKEN
from config.user_config import TODOIST_AI_LABEL, TODOIST_IGNORED_PROJECTS, USER_NAME
from integrations.todoist_integration import (
    URGENCY_TO_PRIORITY,
    TodoistClient,
    TodoistError,
)
from prompts.prompts import get_prompt

logger = setup_logger(__name__)


class TodoistSyncProcessor(NoteProcessor):
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

    **Filing**: the project and section are chosen from whatever exists in the account
    at the time, so projects added later are picked up with no config change.
        - confident project + section -> that section
        - confident project only      -> project root
        - no project fits            -> Inbox

    **Frontmatter fields**:
        - todoist_tasks: list of {id, content, action} for the tasks touched
    """

    stage_name = "todoist_synced"
    required_stage = MeetingSummaryGenerator.stage_name

    # Files dated before this are skipped unless tagged 'force_todoist_sync'.
    # Without this gate, adding the stage would fire on every already-summarised
    # transcript in the vault.
    START_DATE = "2026-07-24"

    # Cap the context handed to the model, so one huge account can't blow up the prompt.
    MAX_OPEN_TASKS = 150

    def __init__(self, input_dir: Path):
        super().__init__(input_dir)
        self.prompt_template = get_prompt("meeting_todoist_tasks")
        self.client: Optional[TodoistClient] = None

        if TODOIST_API_TOKEN:
            self.client = TodoistClient(TODOIST_API_TOKEN)
        else:
            logger.warning(
                "TODOIST_API_TOKEN not set — Todoist sync is disabled, meetings will "
                "stay unprocessed for stage '%s'", self.stage_name
            )

    # ===== Eligibility =====

    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        if not self.client:
            return False

        source_tags = frontmatter.get("source_tags", [])
        forced = "force_todoist_sync" in source_tags

        if frontmatter.get("category") != "meeting" and not forced:
            return False

        file_date = frontmatter.get("date")
        if file_date and not forced and self._to_date_str(file_date) < self.START_DATE:
            return False

        return True

    @staticmethod
    def _to_date_str(value: Any) -> str:
        """Coerce a date-like frontmatter value to 'YYYY-MM-DD'.

        Unquoted YAML dates parse as datetime.date, quoted ones as str.
        """
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

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

    # ===== Todoist context =====

    def _fetch_todoist_context(self) -> Dict[str, Any]:
        """Read the account state the model needs to make its decisions.

        Runs in a worker thread — every call here is blocking HTTP.
        """
        all_projects = self.client.get_projects()
        inbox = next((p for p in all_projects if p.get("inbox_project")), None)

        ignored = {name.strip().lower() for name in TODOIST_IGNORED_PROJECTS}
        ignored_ids = {
            p["id"] for p in all_projects if p.get("name", "").strip().lower() in ignored
        }
        # The Inbox is where tasks go when nothing fits, so it isn't offered as a choice.
        projects = [
            p for p in all_projects
            if not p.get("inbox_project") and p["id"] not in ignored_ids
        ]

        labels = self.client.get_labels()
        if TODOIST_AI_LABEL.lower() not in {label.get("name", "").lower() for label in labels}:
            logger.info("Creating Todoist label: %s", TODOIST_AI_LABEL)
            labels.append(self.client.create_label(TODOIST_AI_LABEL))

        # Every open task outside the ignored projects, so a commitment restated in a
        # different project is still recognised as a duplicate.
        open_tasks = [
            t for t in self.client.get_tasks() if t.get("project_id") not in ignored_ids
        ]

        return {
            "inbox_id": inbox.get("id") if inbox else None,
            "projects": projects,
            # One unscoped call returns the sections of every project.
            "sections": [
                s for s in self.client.get_sections()
                if s.get("project_id") not in ignored_ids
            ],
            "labels": labels,
            "open_tasks": open_tasks,
        }

    @staticmethod
    def _format_projects(projects: List[Dict], sections: List[Dict]) -> str:
        """Render the project/section tree the model chooses from."""
        if not projects:
            return "(no projects — everything goes to the Inbox)"

        lines = []
        for project in projects:
            names = [s["name"] for s in sections if s.get("project_id") == project["id"]]
            suffix = f" — sections: {', '.join(names)}" if names else " — no sections"
            lines.append(f"- {project['name']}{suffix}")
        return '\n'.join(lines)

    @staticmethod
    def _format_labels(labels: List[Dict]) -> str:
        # The marker label is applied unconditionally, so it isn't offered as a
        # topical choice.
        names = [
            label['name'] for label in labels
            if label['name'].lower() != TODOIST_AI_LABEL.lower()
        ]
        if not names:
            return "(no labels)"
        return '\n'.join(f"- {name}" for name in names)

    def _format_open_tasks(
        self, tasks: List[Dict], projects: List[Dict], sections: List[Dict]
    ) -> str:
        if not tasks:
            return "(no open tasks)"

        section_names = {s["id"]: s["name"] for s in sections}
        project_names = {p["id"]: p["name"] for p in projects}
        # Newest first, so the truncation drops the stalest tasks.
        tasks = sorted(tasks, key=lambda t: t.get("added_at") or "", reverse=True)

        lines = []
        for task in tasks[:self.MAX_OPEN_TASKS]:
            due = (task.get("due") or {}).get("date") or "no due date"
            location = project_names.get(task.get("project_id"), "Inbox")
            section = section_names.get(task.get("section_id"))
            if section:
                location = f"{location} / {section}"
            lines.append(
                f"- id={task['id']} | {task.get('content', '')} "
                f"| due: {due} | in: {location}"
            )
        if len(tasks) > self.MAX_OPEN_TASKS:
            lines.append(f"- ...({len(tasks) - self.MAX_OPEN_TASKS} older tasks omitted)")
        return '\n'.join(lines)

    # ===== AI call =====

    async def _decide_tasks(
        self,
        filename: str,
        meeting_date: str,
        summary: str,
        action_lines: List[str],
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Ask the model which action items are the user's, and how to file them."""
        meeting_context = '\n\n'.join(
            part for part in (
                f"## Summary\n{self._extract_section(summary, 'Summary')}",
                f"## Decisions Made\n{self._extract_section(summary, 'Decisions Made')}",
            ) if part.strip()
        )

        try:
            weekday = calendar.day_name[date.fromisoformat(meeting_date).weekday()]
        except ValueError:
            weekday = "unknown weekday"

        prompt = self.prompt_template.format(
            user_name=USER_NAME,
            meeting_title=filename.replace('.md', ''),
            meeting_date=meeting_date,
            weekday=weekday,
            today=date.today().isoformat(),
            summary_context=meeting_context,
            action_items='\n'.join(action_lines),
            projects=self._format_projects(context["projects"], context["sections"]),
            labels=self._format_labels(context["labels"]),
            open_tasks=self._format_open_tasks(
                context["open_tasks"], context["projects"], context["sections"]
            ),
        )

        message = Message(role="user", content=[MessageContent(type="text", text=prompt)])
        response = await asyncio.to_thread(self.ai_model.message, message)

        if response.error:
            raise RuntimeError(f"AI error deciding Todoist tasks: {response.error}")

        return self._parse_response(response.content or "")

    @staticmethod
    def _parse_response(response_text: str) -> List[Dict[str, Any]]:
        """Parse the model's JSON, tolerating a markdown fence around it."""
        text = response_text.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if not match:
            raise ValueError(f"No JSON object in AI response: {text[:300]}")

        data = json.loads(match.group())
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError(f"Expected 'tasks' to be a list, got {type(tasks).__name__}")
        return tasks

    # ===== Validation =====

    @staticmethod
    def _normalise(text: str) -> str:
        """Strip wikilinks, punctuation noise and casing for comparison."""
        text = re.sub(r'\[\[|\]\]', '', text)
        text = re.sub(r'[^a-z0-9]+', ' ', text.lower())
        return text.strip()

    def _validate_tasks(
        self,
        tasks: List[Dict[str, Any]],
        action_lines: List[str],
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Drop hallucinated tasks and coerce every field to something Todoist accepts."""
        known_lines = {self._normalise(line) for line in action_lines}
        label_names = {label["name"].lower(): label["name"] for label in context["labels"]}
        project_ids = {p["name"].strip().lower(): p["id"] for p in context["projects"]}
        open_task_ids = {str(task["id"]) for task in context["open_tasks"]}

        validated = []
        seen_contents = set()

        for task in tasks:
            content = (task.get("content") or "").strip()
            if not content:
                logger.warning("Dropping Todoist task with no content: %s", task)
                continue

            # The source line is the anti-hallucination check: a task the model made up
            # cannot quote a bullet that was never in the input.
            source = self._normalise(task.get("source_line") or "")
            if not source or not any(
                source == line or source in line or line in source for line in known_lines
            ):
                logger.warning(
                    "Dropping Todoist task %r — source_line %r not found in action items",
                    content, task.get("source_line"),
                )
                continue

            # A restated commitment can legitimately appear twice in one summary.
            content_key = self._normalise(content)
            if content_key in seen_contents:
                logger.info("Skipping duplicate task within response: %s", content)
                continue
            seen_contents.add(content_key)

            labels = [
                label_names[label.lower()]
                for label in (task.get("labels") or [])
                if isinstance(label, str) and label.lower() in label_names
            ]
            if TODOIST_AI_LABEL not in labels:
                labels.append(TODOIST_AI_LABEL)

            duplicate_of = task.get("duplicate_of")
            duplicate_of = str(duplicate_of) if duplicate_of else None
            if duplicate_of and duplicate_of not in open_task_ids:
                logger.warning(
                    "Ignoring unknown duplicate_of=%s for task %r", duplicate_of, content
                )
                duplicate_of = None

            project_id, section_id = self._resolve_location(task, project_ids, context)
            due_date, original_due = self._validate_due_date(task.get("due_date"), content)

            validated.append({
                "content": content,
                "description": (task.get("description") or "").strip(),
                "labels": labels,
                "priority": URGENCY_TO_PRIORITY.get(task.get("urgency"), 1),
                "due_date": due_date,
                "original_due_date": original_due,
                "project_id": project_id,
                "section_id": section_id,
                "duplicate_of": duplicate_of,
            })

        return validated

    def _resolve_location(
        self, task: Dict[str, Any], project_ids: Dict[str, str], context: Dict[str, Any]
    ) -> Tuple[Optional[str], Optional[str]]:
        """Resolve the AI's project/section names to ids.

        An unknown project name means the model guessed at something that doesn't exist,
        so the task falls back to the Inbox rather than being filed somewhere arbitrary.
        A section only counts if it actually belongs to the chosen project.
        """
        project_name = task.get("project")
        if not isinstance(project_name, str) or not project_name.strip():
            return context["inbox_id"], None

        project_id = project_ids.get(project_name.strip().lower())
        if not project_id:
            logger.warning(
                "Unknown Todoist project %r for task %r — filing in the Inbox",
                project_name, task.get("content"),
            )
            return context["inbox_id"], None

        section_name = task.get("section")
        if not isinstance(section_name, str) or not section_name.strip():
            return project_id, None

        section = next(
            (
                s for s in context["sections"]
                if s.get("project_id") == project_id
                and s["name"].strip().lower() == section_name.strip().lower()
            ),
            None,
        )
        if not section:
            logger.warning(
                "Section %r is not in project %r — filing task %r at the project root",
                section_name, project_name, task.get("content"),
            )
            return project_id, None

        return project_id, section["id"]

    def _validate_due_date(
        self, raw_due: Any, content: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return (due_date, original_due_date_if_clamped).

        Deadlines are resolved against the meeting date, so a transcript processed late
        can produce a date in the past. Those get pulled forward to today — an item that
        lands already overdue reads as a data error rather than as work to do — and the
        original deadline is kept for the description.
        """
        if not isinstance(raw_due, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw_due.strip()):
            if raw_due:
                logger.warning("Ignoring malformed due_date %r for task %r", raw_due, content)
            return None, None

        raw_due = raw_due.strip()
        try:
            parsed = date.fromisoformat(raw_due)
        except ValueError:
            logger.warning("Ignoring invalid due_date %r for task %r", raw_due, content)
            return None, None

        today = date.today()
        if parsed < today:
            return today.isoformat(), raw_due
        return raw_due, None

    # ===== Descriptions =====

    def _obsidian_uri(self, filename: str) -> str:
        vault = PATHS.vault_path.name
        try:
            relative = (self.input_dir / filename).relative_to(PATHS.vault_path)
            file_ref = str(relative.with_suffix(''))
        except ValueError:
            file_ref = filename.replace('.md', '')
        return f"obsidian://open?vault={quote(vault)}&file={quote(file_ref)}"

    def _build_description(
        self, task: Dict[str, Any], filename: str, meeting_date: str
    ) -> str:
        parts = []
        if task["description"]:
            parts.append(task["description"])
        if task["original_due_date"]:
            parts.append(f"Original deadline from the meeting: {task['original_due_date']}.")
        parts.append(
            f"Source: {filename.replace('.md', '')} ({meeting_date})\n"
            f"{self._obsidian_uri(filename)}"
        )
        return '\n\n'.join(parts)

    # ===== Todoist writes =====

    def _push_task(
        self,
        task: Dict[str, Any],
        context: Dict[str, Any],
        filename: str,
        meeting_date: str,
    ) -> Dict[str, Any]:
        """Create the task, or refresh the open task it duplicates.

        Runs in a worker thread.
        """
        if task["duplicate_of"]:
            existing = next(
                t for t in context["open_tasks"] if str(t["id"]) == task["duplicate_of"]
            )
            # Append rather than replace: the description may have been edited by hand.
            description = (existing.get("description") or "").rstrip()
            note = (
                f"Restated in {filename.replace('.md', '')} ({meeting_date})"
                + (f", now due {task['due_date']}." if task["due_date"] else ".")
                + f"\n{self._obsidian_uri(filename)}"
            )
            description = f"{description}\n\n{note}" if description else note

            labels = list(existing.get("labels") or [])
            if TODOIST_AI_LABEL not in labels:
                labels.append(TODOIST_AI_LABEL)

            self.client.update_task(
                task["duplicate_of"],
                description=description,
                due_date=task["due_date"],
                labels=labels,
            )
            logger.info(
                "Updated existing Todoist task %s (restated): %s",
                task["duplicate_of"], existing.get("content"),
            )
            return {
                "id": str(task["duplicate_of"]),
                "content": existing.get("content", task["content"]),
                "action": "updated",
            }

        created = self.client.create_task(
            content=task["content"],
            description=self._build_description(task, filename, meeting_date),
            project_id=task["project_id"],
            section_id=task["section_id"],
            due_date=task["due_date"],
            labels=task["labels"],
            priority=task["priority"],
        )
        logger.info("Created Todoist task %s: %s", created.get("id"), task["content"])
        return {
            "id": str(created.get("id")),
            "content": task["content"],
            "action": "created",
        }

    # ===== Main processing =====

    async def process_file(self, filename: str) -> None:
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)

        if not frontmatter:
            raise ValueError(f"No frontmatter found in: {filename}")

        summary = self._extract_summary(content)
        if summary is None:
            raise ValueError(f"No validated meeting summary found in: {filename}")

        action_lines = self._action_item_lines(self._extract_section(summary, 'Action Items'))
        if not action_lines:
            logger.info("No action items in %s — nothing to sync", filename)
            return

        # Cheap guard: if the user isn't named at all, there is nothing to own and no
        # reason to pay for an AI call.
        if not any(USER_NAME.lower() in line.lower() for line in action_lines):
            logger.info("No action items mention %s in %s — nothing to sync", USER_NAME, filename)
            return

        meeting_date = self._to_date_str(frontmatter.get('date', ''))
        context = await asyncio.to_thread(self._fetch_todoist_context)

        proposed = await self._decide_tasks(
            filename, meeting_date, summary, action_lines, context
        )
        tasks = self._validate_tasks(proposed, action_lines, context)

        if not tasks:
            logger.info("No tasks for %s survived validation — nothing to sync", filename)
            return

        # Push each task independently: one API failure shouldn't cost the others.
        # Whatever succeeded is recorded before re-raising, and the duplicate detection
        # above keeps the retry from creating the same task twice.
        records: List[Dict[str, Any]] = []
        failures: List[str] = []
        for task in tasks:
            try:
                records.append(
                    await asyncio.to_thread(
                        self._push_task, task, context, filename, meeting_date
                    )
                )
            except (TodoistError, StopIteration) as e:
                logger.error("Failed to push task %r: %s", task["content"], e)
                failures.append(task["content"])

        if records:
            await self._record_tasks(filename, records)

        if failures:
            raise TodoistError(
                f"{len(failures)} of {len(tasks)} tasks failed to sync: {failures}"
            )

        logger.info("Synced %d task(s) to Todoist from %s", len(records), filename)

    async def _record_tasks(self, filename: str, records: List[Dict[str, Any]]) -> None:
        """Record the touched tasks in frontmatter, for traceability and reset."""
        file_path = self.input_dir / filename
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content) or {}

        existing = frontmatter.get('todoist_tasks') or []
        known_ids = {entry.get('id') for entry in existing}
        frontmatter['todoist_tasks'] = existing + [
            record for record in records if record['id'] not in known_ids
        ]

        body = read_text_from_content(content)
        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
            await f.write(frontmatter_to_text(frontmatter) + body)
        os.utime(file_path, None)

    async def reset(self, filename: str) -> None:
        """Reset the stage for a file.

        Tasks already in Todoist are left alone — deleting them is the user's call.
        Their ids are logged so they can be found.
        """
        logger.info("Resetting Todoist sync for: %s", filename)

        file_path = self.input_dir / filename
        if not file_path.exists():
            logger.error("File not found: %s", filename)
            return

        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)
        if not frontmatter:
            return

        previous = frontmatter.pop('todoist_tasks', None)
        if previous:
            logger.info(
                "Leaving %d previously synced Todoist task(s) in place: %s",
                len(previous), [entry.get('id') for entry in previous],
            )

        stages = frontmatter.get('processing_stages', [])
        if self.stage_name in stages:
            stages.remove(self.stage_name)

        body = read_text_from_content(content)
        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
            await f.write(frontmatter_to_text(frontmatter) + body)
        os.utime(file_path, None)

        logger.info("Reset complete for: %s", filename)
