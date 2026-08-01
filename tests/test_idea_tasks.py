"""
Tests for IdeaTaskProcessor — commitments pulled out of idea notes.
"""

import json
from datetime import date

import pytest

from processors.notes.idea_tasks import IdeaTaskProcessor
from tests.test_todoist_sync import FakeTodoistClient


NOTE = """---
date: '{date}'
category: idea
processing_stages:
- transcribed
- classified
- idea_cleaned
- speakers_identified
- ideas_extracted
---
Maxime Fournes ([[Pause IA]]):
{body}
"""


@pytest.fixture
def processor(mock_ai, monkeypatch, tmp_path):
    monkeypatch.setattr("processors.notes.todoist_base.TODOIST_API_TOKEN", "fake-token")
    input_dir = tmp_path / "transcriptions"
    input_dir.mkdir(parents=True)

    proc = IdeaTaskProcessor(input_dir=input_dir)
    proc.client = FakeTodoistClient()
    return proc


def write_note(processor, name, body, note_date="2026-08-01"):
    path = processor.input_dir / name
    path.write_text(NOTE.format(date=note_date, body=body), encoding="utf-8")
    return path


def ai_response(*tasks):
    return json.dumps({"tasks": list(tasks)})


class TestShouldProcess:

    def test_processes_idea_category(self, processor):
        assert processor.should_process("note.md", {
            "category": "idea", "date": "2026-08-01",
        })

    def test_skips_other_categories(self, processor):
        for category in ("meeting", "todo", "diary", "meditation"):
            assert not processor.should_process("note.md", {
                "category": category, "date": "2026-08-01",
            }), category

    def test_skips_the_historical_backlog(self, processor):
        """148 idea notes predate this stage; only the gate keeps them out."""
        assert not processor.should_process("old.md", {
            "category": "idea", "date": "2026-04-20",
        })

    def test_force_tag_overrides_the_gate(self, processor):
        assert processor.should_process("old.md", {
            "category": "idea",
            "date": "2024-01-01",
            "source_tags": ["force_todoist_sync"],
        })

    def test_disabled_without_token(self, mock_ai, monkeypatch, tmp_path):
        monkeypatch.setattr("processors.notes.todoist_base.TODOIST_API_TOKEN", None)
        proc = IdeaTaskProcessor(input_dir=tmp_path)
        assert proc.client is None
        assert not proc.should_process("note.md", {
            "category": "idea", "date": "2026-08-01",
        })


class TestStageWiring:

    def test_has_its_own_stage_and_label(self, processor):
        assert processor.stage_name == "idea_tasks_synced"
        assert processor.provenance_label == "from-idea-note"
        assert processor.managed_labels == ["ai-generated", "from-idea-note"]

    def test_other_provenance_labels_are_not_offered(self, processor):
        processor.client.labels = [
            {"id": "l1", "name": "fundraising"},
            {"id": "l2", "name": "from-meeting"},
            {"id": "l3", "name": "from-idea-note"},
        ]

        rendered = processor._format_labels(processor.client.labels)

        assert "fundraising" in rendered
        assert "from-meeting" not in rendered
        assert "from-idea-note" not in rendered

    def test_allows_a_longer_transcript_than_a_dictated_memo(self, processor):
        from processors.notes.todo import TodoProcessor
        assert processor.MAX_TRANSCRIPT_CHARS > TodoProcessor.MAX_TRANSCRIPT_CHARS


class TestProcessFile:

    async def test_a_reflective_note_produces_nothing(self, processor, mock_ai):
        """The common case: an idea note with no commitments in it."""
        write_note(processor, "2026-08-01-Strength.md",
                   "You are strong. That means you can carve a way into reality.")
        mock_ai.add_response("idea note", ai_response())

        await processor.process_file("2026-08-01-Strength.md")

        assert processor.client.created == []
        assert processor.client.updated == []
        content = (processor.input_dir / "2026-08-01-Strength.md").read_text()
        assert "todoist_tasks" not in content

    async def test_a_working_session_produces_tasks(self, processor, mock_ai):
        write_note(
            processor, "2026-08-01-matilda.md",
            "I need to review Ben's work on the Germany application and send it to Liron. "
            "And I should work on the UK funding application.",
        )
        mock_ai.add_response("idea note", ai_response(
            {
                "source_line": "I need to review Ben's work on the Germany application "
                               "and send it to Liron.",
                "content": "Review Ben's work on the Germany application and send to Liron",
                "description": "",
                "due_date": None,
                "urgency": "normal",
                "project": "PauseAI",
                "section": "Fundraising",
                "labels": [],
                "duplicate_of": None,
            },
            {
                "source_line": "And I should work on the UK funding application.",
                "content": "Work on the UK funding application",
                "description": "",
                "due_date": None,
                "urgency": "normal",
                "project": None,
                "section": None,
                "labels": [],
                "duplicate_of": None,
            },
        ))

        await processor.process_file("2026-08-01-matilda.md")

        assert len(processor.client.created) == 2
        germany, uk = processor.client.created
        # Concrete project work may be filed.
        assert germany["project_id"] == "proj-work"
        assert germany["labels"] == ["ai-generated", "from-idea-note"]
        # Everything else defaults to the Inbox for hand-triage.
        assert uk["project_id"] == "proj-inbox"
        assert uk["section_id"] is None

    async def test_thinking_about_tracked_work_updates_rather_than_duplicates(
        self, processor, mock_ai
    ):
        processor.client.open_tasks = [{
            "id": "task-7",
            "content": "Write a piece of thought leadership",
            "description": "",
            "labels": [],
            "project_id": "proj-work",
            "due": {"date": "2026-08-05"},
            "added_at": "2026-07-25T09:00:00Z",
        }]
        write_note(processor, "2026-08-01-article.md",
                   "The piece I am writing should be called We Must Rise to the Challenge.")
        mock_ai.add_response("idea note", ai_response({
            "source_line": "The piece I am writing should be called We Must Rise to the "
                           "Challenge.",
            "content": "Write the thought leadership piece",
            "description": "",
            "due_date": None,
            "urgency": "normal",
            "project": "PauseAI",
            "section": None,
            "labels": [],
            "duplicate_of": "task-7",
        }))

        await processor.process_file("2026-08-01-article.md")

        assert processor.client.created == []
        assert processor.client.updated[0]["id"] == "task-7"
        # Revisiting a task in a note doesn't make the note its origin.
        assert "from-idea-note" not in processor.client.updated[0]["labels"]

    async def test_a_task_not_in_the_note_is_dropped(self, processor, mock_ai):
        write_note(processor, "2026-08-01-musing.md", "Thinking about strength today.")
        mock_ai.add_response("idea note", ai_response({
            "source_line": "I should buy a yacht",
            "content": "Buy a yacht",
            "description": "",
            "due_date": None,
            "urgency": "normal",
            "project": None,
            "section": None,
            "labels": [],
            "duplicate_of": None,
        }))

        await processor.process_file("2026-08-01-musing.md")

        assert processor.client.created == []
