"""
Tests for TodoistSyncProcessor.
"""

import json
from datetime import date, timedelta

import pytest

from processors.notes.todoist_sync import TodoistSyncProcessor
from integrations.todoist_integration import TodoistError


# ===== Fakes =====

class FakeTodoistClient:
    """In-memory stand-in for TodoistClient."""

    def __init__(self, sections=None, labels=None, open_tasks=None, project_name="PauseAI"):
        self.project_name = project_name
        self.sections = sections if sections is not None else [
            {"id": "sec-legal", "name": "Legal", "project_id": "proj-work"},
            {"id": "sec-comms", "name": "Communications", "project_id": "proj-work"},
            {"id": "sec-politics", "name": "Politics", "project_id": "proj-work"},
            {"id": "sec-other-legal", "name": "Legal", "project_id": "proj-france"},
            {"id": "sec-tutorial", "name": "Todoist 101", "project_id": "proj-onboarding"},
        ]
        self.labels = labels if labels is not None else [
            {"id": "lab-1", "name": "fundraising"},
            {"id": "lab-2", "name": "ai-generated"},
        ]
        self.open_tasks = open_tasks if open_tasks is not None else []
        self.created = []
        self.updated = []
        self.created_labels = []
        self.fail_on_create = False

    def get_projects(self):
        return [
            {"id": "proj-work", "name": self.project_name},
            {"id": "proj-france", "name": "PauseAI France"},
            {"id": "proj-onboarding", "name": "Getting Started 👋"},
            {"id": "proj-inbox", "name": "Inbox", "inbox_project": True},
        ]

    def create_label(self, name):
        label = {"id": f"lab-new-{len(self.created_labels) + 1}", "name": name}
        self.created_labels.append(name)
        self.labels.append(label)
        return label

    def get_sections(self, project_id=None):
        return self.sections

    def get_labels(self):
        return self.labels

    def get_tasks(self, project_id=None, label=None):
        if label:
            return [t for t in self.open_tasks if label in (t.get("labels") or [])]
        return list(self.open_tasks)

    def create_task(self, **kwargs):
        if self.fail_on_create:
            raise TodoistError("boom")
        task = {"id": f"new-{len(self.created) + 1}", **kwargs}
        self.created.append(kwargs)
        return task

    def update_task(self, task_id, **fields):
        self.updated.append({"id": task_id, **fields})
        return {"id": task_id, **fields}


TRANSCRIPT_TEMPLATE = """---
category: meeting
date: '{date}'
processing_stages:
- transcribed
- classified
- speakers_identified
- entities_resolved
- meeting_summarized
---
<!-- summary:meeting_summary:start -->

> [!success] Meeting Summary Complete — See [[2026-07 Meetings]]

## Summary
The team reviewed fundraising status and legal agreements.

## Decisions Made
- **Anonymous Donations**: Agreed to accept anonymous donations.

## Action Items
{action_items}

## Key Topics
#fundraising #legal

<!-- summary:meeting_summary:end -->

Speaker A: Hello everyone.
"""


def write_transcript(directory, name, action_items, meeting_date="2026-07-29"):
    path = directory / name
    path.write_text(
        TRANSCRIPT_TEMPLATE.format(date=meeting_date, action_items=action_items),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def processor(mock_ai, monkeypatch, tmp_path):
    """A processor with a fake Todoist client and a mocked AI."""
    monkeypatch.setattr("processors.notes.todoist_base.TODOIST_API_TOKEN", "fake-token")
    input_dir = tmp_path / "transcriptions"
    input_dir.mkdir(parents=True)

    proc = TodoistSyncProcessor(input_dir)
    proc.client = FakeTodoistClient()
    return proc


def ai_response(*tasks):
    return json.dumps({"tasks": list(tasks)})


def task_payload(**overrides):
    payload = {
        "source_line": "- [[Maxime Fournes]] Update the SFF application (by next week).",
        "content": "Update the SFF application",
        "description": "Matilda is waiting on this before submission.",
        "due_date": "2026-08-07",
        "urgency": "high",
        "project": "PauseAI",
        "section": "Legal",
        "labels": ["fundraising"],
        "duplicate_of": None,
    }
    payload.update(overrides)
    return payload


# ===== Eligibility =====

class TestShouldProcess:

    def test_skips_files_before_start_date(self, processor):
        assert not processor.should_process("old.md", {
            "category": "meeting", "date": "2026-07-01",
        })

    def test_processes_files_on_or_after_start_date(self, processor):
        assert processor.should_process("new.md", {
            "category": "meeting", "date": TodoistSyncProcessor.START_DATE,
        })

    def test_handles_unquoted_yaml_dates(self, processor):
        assert not processor.should_process("old.md", {
            "category": "meeting", "date": date(2026, 7, 1),
        })

    def test_skips_non_meetings(self, processor):
        assert not processor.should_process("diary.md", {
            "category": "diary", "date": "2026-07-29",
        })

    def test_force_tag_overrides_date_and_category(self, processor):
        assert processor.should_process("old.md", {
            "category": "diary",
            "date": "2024-01-01",
            "source_tags": ["force_todoist_sync"],
        })

    def test_disabled_without_token(self, mock_ai, monkeypatch, tmp_path):
        monkeypatch.setattr("processors.notes.todoist_base.TODOIST_API_TOKEN", None)
        proc = TodoistSyncProcessor(tmp_path)
        assert proc.client is None
        assert not proc.should_process("new.md", {
            "category": "meeting", "date": "2026-07-29",
        })


# ===== Summary parsing =====

class TestSummaryParsing:

    def test_extracts_action_items_section(self, processor, tmp_path):
        content = TRANSCRIPT_TEMPLATE.format(
            date="2026-07-29",
            action_items="- [[Maxime Fournes]] Do the thing\n- [[Someone Else]] Do another thing",
        )
        summary = processor._extract_summary(content)
        items = processor._extract_section(summary, "Action Items")

        assert "Do the thing" in items
        assert "#fundraising" not in items  # stopped at the next heading

    def test_returns_none_without_summary_block(self, processor):
        assert processor._extract_summary("---\ncategory: meeting\n---\nJust a transcript.") is None

    def test_splits_bullets(self, processor):
        lines = processor._action_item_lines(
            "- [[A]] First task\n\n- [[B]] Second task\nnot a bullet\n"
        )
        assert lines == ["- [[A]] First task", "- [[B]] Second task"]


# ===== Todoist context =====

class TestTodoistContext:

    def test_excludes_inbox_and_ignored_projects(self, processor):
        processor.client.open_tasks = [
            {"id": "t-work", "content": "Real task", "project_id": "proj-work",
             "added_at": "2026-07-29T10:00:00Z"},
            {"id": "t-tutorial", "content": "Capture: add your first task",
             "project_id": "proj-onboarding", "added_at": "2026-07-01T10:00:00Z"},
        ]

        context = processor._fetch_todoist_context()

        names = [p["name"] for p in context["projects"]]
        assert names == ["PauseAI", "PauseAI France"]
        assert context["inbox_id"] == "proj-inbox"
        # Tutorial noise stays out of both the section list and duplicate detection.
        assert "sec-tutorial" not in [s["id"] for s in context["sections"]]
        assert [t["id"] for t in context["open_tasks"]] == ["t-work"]

    def test_creates_managed_labels_if_absent(self, processor):
        processor.client.labels = [{"id": "lab-1", "name": "fundraising"}]

        context = processor._fetch_todoist_context()

        assert processor.client.created_labels == ["ai-generated", "from-meeting"]
        names = [label["name"] for label in context["labels"]]
        assert "ai-generated" in names
        assert "from-meeting" in names

    def test_managed_labels_not_offered_as_a_choice(self, processor):
        processor.client.labels.append({"id": "lab-3", "name": "from-meeting"})

        rendered = processor._format_labels(processor.client.labels)

        assert "fundraising" in rendered
        assert "ai-generated" not in rendered
        assert "from-meeting" not in rendered


# ===== Validation =====

class TestValidation:

    @staticmethod
    def _context(client):
        return {
            "inbox_id": "proj-inbox",
            "projects": [p for p in client.get_projects() if not p.get("inbox_project")],
            "sections": client.sections,
            "labels": client.labels,
            "open_tasks": client.open_tasks,
        }

    def test_drops_task_whose_source_line_is_invented(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(source_line="- [[Maxime Fournes]] Buy a yacht")],
            action_lines,
            self._context(processor.client),
        )
        assert tasks == []

    def test_keeps_task_with_matching_source_line(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload()], action_lines, self._context(processor.client)
        )
        assert len(tasks) == 1
        assert tasks[0]["content"] == "Update the SFF application"
        assert tasks[0]["project_id"] == "proj-work"
        assert tasks[0]["section_id"] == "sec-legal"
        assert tasks[0]["priority"] == 3  # high

    def test_strips_unknown_labels_and_always_adds_managed_ones(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(labels=["fundraising", "invented-label"])],
            action_lines,
            self._context(processor.client),
        )
        assert tasks[0]["labels"] == ["fundraising", "ai-generated", "from-meeting"]

    def test_null_project_goes_to_inbox(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(project=None, section="Legal")],
            action_lines,
            self._context(processor.client),
        )
        assert tasks[0]["project_id"] == "proj-inbox"
        assert tasks[0]["section_id"] is None

    def test_unknown_project_falls_back_to_inbox(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(project="Invented Project", section="Legal")],
            action_lines,
            self._context(processor.client),
        )
        assert tasks[0]["project_id"] == "proj-inbox"
        assert tasks[0]["section_id"] is None

    def test_picks_project_the_ai_chose(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(project="PauseAI France", section="Legal")],
            action_lines,
            self._context(processor.client),
        )
        assert tasks[0]["project_id"] == "proj-france"
        # The section resolved is France's Legal, not the one in PauseAI.
        assert tasks[0]["section_id"] == "sec-other-legal"

    def test_unknown_section_falls_back_to_project_root(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(section="Nonexistent Section")],
            action_lines,
            self._context(processor.client),
        )
        assert tasks[0]["project_id"] == "proj-work"
        assert tasks[0]["section_id"] is None

    def test_section_from_another_project_is_rejected(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(project="PauseAI France", section="Communications")],
            action_lines,
            self._context(processor.client),
        )
        assert tasks[0]["project_id"] == "proj-france"
        assert tasks[0]["section_id"] is None

    def test_ignores_unknown_duplicate_id(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(duplicate_of="does-not-exist")],
            action_lines,
            self._context(processor.client),
        )
        assert tasks[0]["duplicate_of"] is None

    def test_collapses_duplicates_within_response(self, processor):
        action_lines = [
            "- [[Maxime Fournes]] Update the SFF application (by next week).",
            "- [[Matilda da Rui]] and [[Maxime Fournes]] Update the SFF application.",
        ]
        tasks = processor._validate_tasks(
            [
                task_payload(),
                task_payload(source_line=action_lines[1]),
            ],
            action_lines,
            self._context(processor.client),
        )
        assert len(tasks) == 1

    def test_malformed_due_date_is_dropped(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(due_date="next Tuesday")],
            action_lines,
            self._context(processor.client),
        )
        assert tasks[0]["due_date"] is None

    def test_past_due_date_is_clamped_to_today(self, processor):
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(due_date="2020-01-01")],
            action_lines,
            self._context(processor.client),
        )
        assert tasks[0]["due_date"] == date.today().isoformat()
        assert tasks[0]["original_due_date"] == "2020-01-01"

    def test_future_due_date_is_kept(self, processor):
        future = (date.today() + timedelta(days=10)).isoformat()
        action_lines = ["- [[Maxime Fournes]] Update the SFF application (by next week)."]
        tasks = processor._validate_tasks(
            [task_payload(due_date=future)],
            action_lines,
            self._context(processor.client),
        )
        assert tasks[0]["due_date"] == future
        assert tasks[0]["original_due_date"] is None


# ===== End to end =====

class TestProcessFile:

    async def test_creates_task_and_records_it(self, processor, mock_ai):
        future = (date.today() + timedelta(days=10)).isoformat()
        write_transcript(
            processor.input_dir, "2026-07-29-funding.md",
            "- [[Maxime Fournes]] Update the SFF application (by next week).\n"
            "- [[Irina Tavera]] Book her train travel.",
        )
        mock_ai.add_response("triaging the action items", ai_response(
            task_payload(due_date=future)
        ))

        await processor.process_file("2026-07-29-funding.md")

        assert len(processor.client.created) == 1
        created = processor.client.created[0]
        assert created["content"] == "Update the SFF application"
        assert created["project_id"] == "proj-work"
        assert created["section_id"] == "sec-legal"
        assert created["due_date"] == future
        assert created["labels"] == ["fundraising", "ai-generated", "from-meeting"]
        assert "obsidian://open" in created["description"]
        assert "2026-07-29-funding" in created["description"]

        content = (processor.input_dir / "2026-07-29-funding.md").read_text()
        assert "todoist_tasks:" in content
        assert "action: created" in content

    async def test_creates_managed_labels_when_missing(self, processor, mock_ai):
        processor.client.labels = [{"id": "lab-1", "name": "fundraising"}]
        write_transcript(
            processor.input_dir, "2026-07-29-funding.md",
            "- [[Maxime Fournes]] Update the SFF application (by next week).",
        )
        mock_ai.add_response("triaging the action items", ai_response(
            task_payload(due_date=None)
        ))

        await processor.process_file("2026-07-29-funding.md")

        assert processor.client.created_labels == ["ai-generated", "from-meeting"]
        assert "from-meeting" in processor.client.created[0]["labels"]

    async def test_unfiled_task_goes_to_inbox(self, processor, mock_ai):
        write_transcript(
            processor.input_dir, "2026-07-29-funding.md",
            "- [[Maxime Fournes]] Update the SFF application (by next week).",
        )
        mock_ai.add_response("triaging the action items", ai_response(
            task_payload(project=None, section=None, due_date=None)
        ))

        await processor.process_file("2026-07-29-funding.md")

        assert processor.client.created[0]["project_id"] == "proj-inbox"
        assert processor.client.created[0]["section_id"] is None

    async def test_duplicate_updates_existing_task_instead_of_creating(self, processor, mock_ai):
        future = (date.today() + timedelta(days=10)).isoformat()
        processor.client.open_tasks = [{
            "id": "task-42",
            "content": "Update the SFF application",
            "description": "Original context.",
            "labels": ["fundraising"],
            "section_id": "sec-legal",
            "due": {"date": "2026-08-01"},
            "added_at": "2026-07-20T10:00:00Z",
        }]
        write_transcript(
            processor.input_dir, "2026-07-30-pausecon.md",
            "- [[Maxime Fournes]] Update the SFF application (by next week).",
            meeting_date="2026-07-30",
        )
        mock_ai.add_response("triaging the action items", ai_response(
            task_payload(duplicate_of="task-42", due_date=future)
        ))

        await processor.process_file("2026-07-30-pausecon.md")

        assert processor.client.created == []
        assert len(processor.client.updated) == 1
        update = processor.client.updated[0]
        assert update["id"] == "task-42"
        assert update["due_date"] == future
        # The existing description is preserved, not overwritten.
        assert "Original context." in update["description"]
        assert "Restated in 2026-07-30-pausecon" in update["description"]
        assert "obsidian://open" in update["description"]
        # Restating a task in a meeting doesn't make the meeting its origin.
        assert update["labels"] == ["fundraising", "ai-generated"]
        assert "from-meeting" not in update["labels"]

        content = (processor.input_dir / "2026-07-30-pausecon.md").read_text()
        assert "action: updated" in content

    async def test_skips_ai_call_when_user_not_mentioned(self, processor, mock_ai):
        write_transcript(
            processor.input_dir, "2026-07-29-funding.md",
            "- [[Irina Tavera]] Book her train travel.\n"
            "- [[Jonathan Moody]] Draft the podcast list.",
        )

        await processor.process_file("2026-07-29-funding.md")

        assert mock_ai.call_log == []
        assert processor.client.created == []

    async def test_no_action_items_is_a_noop(self, processor, mock_ai):
        write_transcript(processor.input_dir, "2026-07-29-funding.md",
                         "No explicit action items recorded.")

        await processor.process_file("2026-07-29-funding.md")

        assert mock_ai.call_log == []
        assert processor.client.created == []

    async def test_missing_summary_block_raises(self, processor):
        path = processor.input_dir / "bare.md"
        path.write_text("---\ncategory: meeting\ndate: '2026-07-29'\n---\nSpeaker A: hi\n")

        with pytest.raises(ValueError, match="No validated meeting summary"):
            await processor.process_file("bare.md")

    async def test_partial_failure_records_successes_and_raises(self, processor, mock_ai):
        write_transcript(
            processor.input_dir, "2026-07-29-funding.md",
            "- [[Maxime Fournes]] Update the SFF application (by next week).",
        )
        mock_ai.add_response("triaging the action items", ai_response(task_payload()))
        processor.client.fail_on_create = True

        with pytest.raises(TodoistError, match="failed to sync"):
            await processor.process_file("2026-07-29-funding.md")

        content = (processor.input_dir / "2026-07-29-funding.md").read_text()
        assert "todoist_tasks" not in content

    async def test_reset_clears_stage_and_keeps_tasks(self, processor, mock_ai):
        future = (date.today() + timedelta(days=10)).isoformat()
        write_transcript(
            processor.input_dir, "2026-07-29-funding.md",
            "- [[Maxime Fournes]] Update the SFF application (by next week).",
        )
        mock_ai.add_response("triaging the action items", ai_response(
            task_payload(due_date=future)
        ))
        await processor.process_file("2026-07-29-funding.md")

        await processor.reset("2026-07-29-funding.md")

        content = (processor.input_dir / "2026-07-29-funding.md").read_text()
        assert "todoist_tasks" not in content
        assert "todoist_synced" not in content
        assert "meeting_summarized" in content
