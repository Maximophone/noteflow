"""
E2E tests for TodoProcessor — dictated todo memos pushed to Todoist.
"""

import json
from datetime import date, timedelta

import pytest

from processors.notes.todo import TodoProcessor
from tests.test_todoist_sync import FakeTodoistClient


TRANSCRIPT = """---
date: '{date}'
category: todo
processing_stages:
- transcribed
- classified
- speakers_identified
---
Maxime Fournes ([[Pause IA]]):
{body}
"""


@pytest.fixture
def processor(mock_ai, monkeypatch, tmp_path):
    monkeypatch.setattr("processors.notes.todoist_base.TODOIST_API_TOKEN", "fake-token")
    input_dir = tmp_path / "transcriptions"
    input_dir.mkdir(parents=True)

    proc = TodoProcessor(input_dir=input_dir)
    proc.client = FakeTodoistClient()
    return proc


def write_memo(processor, name, body, memo_date="2026-07-29"):
    path = processor.input_dir / name
    path.write_text(TRANSCRIPT.format(date=memo_date, body=body), encoding="utf-8")
    return path


def ai_response(*tasks):
    return json.dumps({"tasks": list(tasks)})


class TestShouldProcess:

    def test_processes_todo_category(self, processor):
        assert processor.should_process("test.md", {
            "category": "todo", "date": "2026-07-29",
        })

    def test_skips_non_todo_category(self, processor):
        assert not processor.should_process("test.md", {
            "category": "meeting", "date": "2026-07-29",
        })

    def test_skips_memos_before_start_date(self, processor):
        assert not processor.should_process("old.md", {
            "category": "todo", "date": "2026-06-11",
        })

    def test_force_tag_overrides_the_date_gate(self, processor):
        assert processor.should_process("old.md", {
            "category": "todo",
            "date": "2024-01-01",
            "source_tags": ["force_todoist_sync"],
        })

    def test_disabled_without_token(self, mock_ai, monkeypatch, tmp_path):
        monkeypatch.setattr("processors.notes.todoist_base.TODOIST_API_TOKEN", None)
        proc = TodoProcessor(input_dir=tmp_path)
        assert proc.client is None
        assert not proc.should_process("test.md", {
            "category": "todo", "date": "2026-07-29",
        })


class TestSourceMaterial:

    def test_uses_the_whole_body_as_the_reference(self, processor):
        content = TRANSCRIPT.format(date="2026-07-29", body="I need to pay Matilda today.")

        source = processor._source_material("memo.md", {"category": "todo"}, content)

        assert source.references == [source.fields["transcript"]]
        assert "pay Matilda" in source.fields["transcript"]
        # Frontmatter is not part of the transcript handed to the model.
        assert "category: todo" not in source.fields["transcript"]

    def test_empty_body_is_a_noop(self, processor):
        content = TRANSCRIPT.format(date="2026-07-29", body="")

        # The speaker prefix line alone is not empty, so blank it out entirely.
        content = content.split("Maxime Fournes")[0]
        assert processor._source_material("memo.md", {"category": "todo"}, content) is None

    def test_long_transcript_is_truncated(self, processor):
        body = "x" * (processor.MAX_TRANSCRIPT_CHARS + 500)
        content = TRANSCRIPT.format(date="2026-07-29", body=body)

        source = processor._source_material("memo.md", {"category": "todo"}, content)

        assert len(source.fields["transcript"]) == processor.MAX_TRANSCRIPT_CHARS


class TestProcessFile:

    async def test_creates_tasks_from_a_multi_item_memo(self, processor, mock_ai):
        future = (date.today() + timedelta(days=5)).isoformat()
        write_memo(
            processor, "2026-07-29-todos.md",
            "First to do is to pay Matilda. The second is to send Ramon the storage numbers.",
        )
        mock_ai.add_response("voice memo", ai_response(
            {
                "source_line": "First to do is to pay Matilda.",
                "content": "Pay Matilda",
                "description": "",
                "due_date": future,
                "urgency": "high",
                "project": "PauseAI",
                "section": "Legal",
                "labels": [],
                "duplicate_of": None,
            },
            {
                "source_line": "The second is to send Ramon the storage numbers.",
                "content": "Send Ramon the storage capacity numbers",
                "description": "",
                "due_date": None,
                "urgency": "normal",
                "project": None,
                "section": None,
                "labels": [],
                "duplicate_of": None,
            },
        ))

        await processor.process_file("2026-07-29-todos.md")

        assert len(processor.client.created) == 2
        paid, ramon = processor.client.created
        assert paid["content"] == "Pay Matilda"
        assert paid["project_id"] == "proj-work"
        assert paid["section_id"] == "sec-legal"
        assert paid["due_date"] == future
        assert "ai-generated" in paid["labels"]
        # No project chosen -> Inbox, not filed at a guess.
        assert ramon["project_id"] == "proj-inbox"
        assert "obsidian://open" in ramon["description"]

        content = (processor.input_dir / "2026-07-29-todos.md").read_text()
        assert "todoist_tasks:" in content
        assert content.count("action: created") == 2

    async def test_drops_a_task_not_traceable_to_the_transcript(self, processor, mock_ai):
        write_memo(processor, "2026-07-29-todos.md", "I need to pay Matilda.")
        mock_ai.add_response("voice memo", ai_response({
            "source_line": "buy a yacht",
            "content": "Buy a yacht",
            "description": "",
            "due_date": None,
            "urgency": "normal",
            "project": None,
            "section": None,
            "labels": [],
            "duplicate_of": None,
        }))

        await processor.process_file("2026-07-29-todos.md")

        assert processor.client.created == []

    async def test_redictated_memo_updates_the_open_task(self, processor, mock_ai):
        future = (date.today() + timedelta(days=5)).isoformat()
        processor.client.open_tasks = [{
            "id": "task-99",
            "content": "Send Ramon the storage capacity numbers",
            "description": "From an earlier memo.",
            "labels": [],
            "project_id": "proj-work",
            "due": {"date": "2026-07-20"},
            "added_at": "2026-07-20T09:00:00Z",
        }]
        write_memo(processor, "2026-07-30-todos.md",
                   "Still need to send Ramon the storage numbers.", memo_date="2026-07-30")
        mock_ai.add_response("voice memo", ai_response({
            "source_line": "Still need to send Ramon the storage numbers.",
            "content": "Send Ramon the storage capacity numbers",
            "description": "",
            "due_date": future,
            "urgency": "high",
            "project": "PauseAI",
            "section": None,
            "duplicate_of": "task-99",
            "labels": [],
        }))

        await processor.process_file("2026-07-30-todos.md")

        assert processor.client.created == []
        update = processor.client.updated[0]
        assert update["id"] == "task-99"
        assert update["due_date"] == future
        assert "From an earlier memo." in update["description"]
        assert "Restated in 2026-07-30-todos" in update["description"]
