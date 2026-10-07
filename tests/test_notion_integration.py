"""
Tests for NotionClient's page writing: transient failures must neither
duplicate content nor leave half-written pages behind.
"""

import httpx
import pytest

import integrations.notion_integration as notion_integration
from integrations.notion_integration import NotionClient


class FakeApi:
    """Just enough of notion_client.Client for page creation."""

    def __init__(self, append_failures):
        # One entry per append call: None succeeds; "lost" stores the
        # blocks but drops the response; "dropped" stores nothing.
        self.append_failures = list(append_failures)
        self.blocks = []
        self.trashed = []
        self.pages = self
        self.blocks_api = self

    # pages
    def create(self, parent, properties, children):
        self.blocks.extend(children)
        return {"id": "page-1"}

    def update(self, page_id, in_trash):
        self.trashed.append(page_id)

    # blocks.children
    def append(self, block_id, children):
        outcome = self.append_failures.pop(0) if self.append_failures else None
        if outcome != "dropped":
            self.blocks.extend(children)
        if outcome:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    def list(self, block_id, page_size, start_cursor=None):
        start = int(start_cursor or 0)
        chunk = self.blocks[start:start + page_size]
        more = start + page_size < len(self.blocks)
        return {"results": chunk, "has_more": more, "next_cursor": str(start + page_size) if more else None}


def make_client(append_failures, monkeypatch):
    monkeypatch.setattr(notion_integration.time, "sleep", lambda s: None)
    api = FakeApi(append_failures)
    client = NotionClient.__new__(NotionClient)
    client.client = type("C", (), {})()
    client.client.pages = api
    client.client.blocks = type("B", (), {"children": api})()
    return client, api


BLOCKS = [{"n": i} for i in range(250)]


def test_all_blocks_once_when_nothing_fails(monkeypatch):
    client, api = make_client([], monkeypatch)
    client.create_page_in_data_source("ds", {}, BLOCKS)
    assert api.blocks == BLOCKS


def test_lost_response_is_not_resent(monkeypatch):
    client, api = make_client(["lost"], monkeypatch)
    client.create_page_in_data_source("ds", {}, BLOCKS)
    assert api.blocks == BLOCKS
    assert api.trashed == []


def test_dropped_batch_is_resent(monkeypatch):
    client, api = make_client(["dropped", "dropped"], monkeypatch)
    client.create_page_in_data_source("ds", {}, BLOCKS)
    assert api.blocks == BLOCKS


def test_page_trashed_when_retries_run_out(monkeypatch):
    client, api = make_client(["dropped"] * 10, monkeypatch)
    with pytest.raises(httpx.RemoteProtocolError):
        client.create_page_in_data_source("ds", {}, BLOCKS)
    assert api.trashed == ["page-1"]


def test_trashing_an_already_trashed_page_is_fine(monkeypatch):
    from notion_client.errors import APIResponseError

    client, api = make_client([], monkeypatch)

    def already_trashed(page_id, in_trash):
        raise APIResponseError(
            httpx.Response(400, request=httpx.Request("PATCH", "https://api.notion.com")),
            "Can't edit block that is archived. You must unarchive the block before editing.",
            "validation_error",
        )

    api.update = already_trashed
    client.trash_page("page-1")
