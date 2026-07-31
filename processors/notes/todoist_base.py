"""
Shared machinery for processors that push tasks to Todoist.

Two stages use this: meeting action items (``todoist_sync.py``) and dictated todo
memos (``todo.py``). They differ only in where the candidate tasks come from and
what the model is told about them; everything downstream — choosing a project,
section, due date and labels, catching restatements of already-open tasks,
validating the model's answer, and the writes themselves — is identical.

Subclasses supply:
    - ``stage_name``, ``required_stage``, ``prompt_name``, ``START_DATE``
    - ``should_process()``
    - ``_source_material()``, returning the text to triage plus its prompt fields
"""

import asyncio
import calendar
import json
import os
import re
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiofiles

from .base import NoteProcessor
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

# Source tag that forces a sync regardless of category or date.
FORCE_TAG = "force_todoist_sync"


@dataclass
class SourceMaterial:
    """What a subclass extracted from a note for the model to work from.

    Attributes:
        references: Verbatim texts a task's ``source_line`` must be traceable to.
            This is the anti-hallucination check, so it must be the raw input the
            model was shown — bullet lines for a summary, the body for a transcript.
        fields: Prompt placeholders specific to this kind of note.
    """
    references: List[str]
    fields: Dict[str, str] = field(default_factory=dict)


class TodoistTaskSync(NoteProcessor):
    """Base for stages that turn note content into Todoist tasks.

    **Filing**: the project and section are chosen from whatever exists in the
    account at the time, so projects added later are picked up with no config
    change.
        - confident project + section -> that section
        - confident project only      -> project root
        - no project fits             -> Inbox

    **Frontmatter fields**:
        - todoist_tasks: list of {id, content, action} for the tasks touched
    """

    # Name of the prompt file (without .md) in prompts/.
    prompt_name: Optional[str] = None

    # Notes dated before this are skipped unless tagged with FORCE_TAG.
    START_DATE = "2026-07-24"

    # Cap the context handed to the model, so one huge account can't blow up the prompt.
    MAX_OPEN_TASKS = 150

    def __init__(self, input_dir: Path):
        super().__init__(input_dir)
        if not self.prompt_name:
            raise NotImplementedError("Todoist processors must define prompt_name")
        self.prompt_template = get_prompt(self.prompt_name)
        self.client: Optional[TodoistClient] = None

        if TODOIST_API_TOKEN:
            self.client = TodoistClient(TODOIST_API_TOKEN)
        else:
            logger.warning(
                "TODOIST_API_TOKEN not set — Todoist sync is disabled, notes will "
                "stay unprocessed for stage '%s'", self.stage_name
            )

    # ===== Hooks for subclasses =====

    @abstractmethod
    def _source_material(
        self, filename: str, frontmatter: Dict, content: str
    ) -> Optional[SourceMaterial]:
        """Extract what the model should triage.

        Return None when there is nothing to sync — the stage is then marked done
        without an AI call. Raise for a note that should have been syncable but
        isn't, so it surfaces in the error registry.
        """
        raise NotImplementedError

    # ===== Eligibility helpers =====

    def _is_forced(self, frontmatter: Dict) -> bool:
        return FORCE_TAG in (frontmatter.get("source_tags") or [])

    def _within_start_date(self, frontmatter: Dict) -> bool:
        file_date = frontmatter.get("date")
        if not file_date:
            return True
        return self._to_date_str(file_date) >= self.START_DATE

    @staticmethod
    def _to_date_str(value: Any) -> str:
        """Coerce a date-like frontmatter value to 'YYYY-MM-DD'.

        Unquoted YAML dates parse as datetime.date, quoted ones as str.
        """
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

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
        source_date: str,
        source: SourceMaterial,
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Ask the model which tasks to create, and how to file them."""
        try:
            weekday = calendar.day_name[date.fromisoformat(source_date).weekday()]
        except ValueError:
            weekday = "unknown weekday"

        fields = {
            "user_name": USER_NAME,
            "source_title": filename.replace('.md', ''),
            "source_date": source_date,
            "weekday": weekday,
            "today": date.today().isoformat(),
            "projects": self._format_projects(context["projects"], context["sections"]),
            "labels": self._format_labels(context["labels"]),
            "open_tasks": self._format_open_tasks(
                context["open_tasks"], context["projects"], context["sections"]
            ),
            **source.fields,
        }
        prompt = self.prompt_template.format(**fields)

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
        references: List[str],
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Drop hallucinated tasks and coerce every field to something Todoist accepts."""
        known = {self._normalise(reference) for reference in references}
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
            # cannot quote text that was never in the input.
            source = self._normalise(task.get("source_line") or "")
            if not source or not any(
                source == ref or source in ref or ref in source for ref in known
            ):
                logger.warning(
                    "Dropping Todoist task %r — source_line %r not found in the source",
                    content, task.get("source_line"),
                )
                continue

            # The same commitment can legitimately be stated twice in one note.
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

        Deadlines are resolved against the note's date, so a note processed late can
        produce a date in the past. Those get pulled forward to today — an item that
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
        self, task: Dict[str, Any], filename: str, source_date: str
    ) -> str:
        parts = []
        if task["description"]:
            parts.append(task["description"])
        if task["original_due_date"]:
            parts.append(f"Original deadline from the note: {task['original_due_date']}.")
        parts.append(
            f"Source: {filename.replace('.md', '')} ({source_date})\n"
            f"{self._obsidian_uri(filename)}"
        )
        return '\n\n'.join(parts)

    # ===== Todoist writes =====

    def _push_task(
        self,
        task: Dict[str, Any],
        context: Dict[str, Any],
        filename: str,
        source_date: str,
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
                f"Restated in {filename.replace('.md', '')} ({source_date})"
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
            description=self._build_description(task, filename, source_date),
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

        source = self._source_material(filename, frontmatter, content)
        if source is None:
            return

        source_date = self._to_date_str(frontmatter.get('date', ''))
        context = await asyncio.to_thread(self._fetch_todoist_context)

        proposed = await self._decide_tasks(filename, source_date, source, context)
        tasks = self._validate_tasks(proposed, source.references, context)

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
                        self._push_task, task, context, filename, source_date
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
