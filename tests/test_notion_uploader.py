"""
Tests for NotionUploadProcessor: pushing a raw transcript to Notion, shared
only with the people the note names.
"""

import pytest

from processors.common.frontmatter import read_frontmatter_from_file
from processors.notes.notion_uploader import (
    AccessError,
    NotionUploadProcessor,
    PARTICIPANTS_PROPERTY,
    extract_transcript,
    parse_access_list,
    transcript_blocks,
)

OWNER_ID = "user-maxime"
IRINA_ID = "user-irina"
JOSEPH_ID = "user-joseph"


class FakeNotion:
    """In-memory stand-in for NotionClient."""

    def __init__(self):
        self.users = [
            {"id": OWNER_ID, "name": "Maxime Fournes", "email": "maxime@example.org"},
            {"id": IRINA_ID, "name": "Irina Tavera", "email": "irina@example.org"},
            {"id": JOSEPH_ID, "name": "Joseph Miller", "email": "joseph@example.org"},
        ]
        self.schema = {
            "id": "ds-1",
            "properties": {
                "Name": {"type": "title"},
                "Date": {"type": "date"},
                PARTICIPANTS_PROPERTY: {"type": "people"},
            },
        }
        # Guests: absent from list_people, visible only in pages' people columns.
        self.seen_in_pages = [
            {"id": "user-clemence", "name": "Clémence Peyrot", "email": "clemence@example.org"},
            {"id": OWNER_ID, "name": "Maxime Fournes", "email": "maxime@example.org"},
        ]
        self.scans = 0
        self.existing_pages = []
        self.created = []
        self.trashed = []
        # Lets a test make Notion report back a different participant list.
        self.stored_people_override = None

    def list_people(self):
        return list(self.users)

    def list_people_seen_in_pages(self):
        self.scans += 1
        return list(self.seen_in_pages)

    def get_single_data_source(self, database_url):
        return self.schema

    def own_user_id(self):
        return "bot-noteflow"

    def find_pages_by_title(self, data_source_id, title_property, title):
        return [p for p in self.existing_pages if p["title"] == title]

    def create_page_in_data_source(self, data_source_id, properties, blocks):
        page = {"id": f"page-{len(self.created) + 1}", "url": "https://notion.so/page",
                "properties": properties, "blocks": blocks}
        self.created.append(page)
        return page

    def get_page(self, page_id):
        page = next(p for p in self.created if p["id"] == page_id)
        people = page["properties"][PARTICIPANTS_PROPERTY]["people"]
        if self.stored_people_override is not None:
            people = [{"id": i} for i in self.stored_people_override]
        return {"properties": {PARTICIPANTS_PROPERTY: {"people": people}}}

    def trash_page(self, page_id):
        self.trashed.append(page_id)


NOTE = """---
category: meeting
date: '2026-09-29'
final_speaker_mapping:
  Speaker A:
    name: Maxime Fournes
    person_id: '[[Maxime Fournes]]'
  Speaker B:
    name: Joseph Miller
    person_id: '[[Joseph Miller]]'
processing_stages:
- transcribed
- classified
- speakers_identified
- entities_resolved
{extra}---
<!-- summary:meeting_summary:start -->

> [!success] Meeting Summary Complete

## Summary
Something confidential about [[Irina Tavera]].

<!-- summary:meeting_summary:end -->
<!-- summary:speaker_identification:start -->

**Speakers:** [[Maxime Fournes]], [[Joseph Miller]]

<!-- summary:speaker_identification:end -->
Maxime Fournes:
Hi [[Joseph Miller|Joe]], thanks for coming.

[[Joseph Miller]]:
Happy to be here.
"""


def write_person(people_dir, name, notion_email=None):
    lines = ["---", f"name: {name}", "email: other@example.org"]
    if notion_email:
        lines.append(f"notion_email: {notion_email}")
    lines += ["---", "", "## Log", ""]
    (people_dir / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def setup(mock_ai, tmp_path):
    transcripts = tmp_path / "Transcriptions"
    people = tmp_path / "People"
    transcripts.mkdir()
    people.mkdir()
    write_person(people, "Maxime Fournes", "maxime@example.org")
    write_person(people, "Irina Tavera", "Irina@Example.org")
    write_person(people, "Joseph Miller", "joseph@example.org")
    notion = FakeNotion()
    proc = NotionUploadProcessor(transcripts, "https://www.notion.so/db", people_dir=people, notion=notion)

    def write_note(extra="", name="2026-09-29-meeting.md"):
        path = transcripts / name
        path.write_text(NOTE.replace("{extra}", extra), encoding="utf-8")
        return name

    return proc, notion, people, write_note


def shared_ids(page):
    return [p["id"] for p in page["properties"][PARTICIPANTS_PROPERTY]["people"]]


class TestOptIn:
    def test_not_pushed_without_checkbox(self, setup):
        proc, _, _, write_note = setup
        assert not proc._should_process(write_note())

    def test_string_true_is_not_consent(self, setup):
        proc, _, _, write_note = setup
        assert not proc._should_process(write_note("notion_push: 'true'\n"))

    def test_pushed_with_checkbox(self, setup):
        proc, _, _, write_note = setup
        assert proc._should_process(write_note("notion_push: true\n"))

    def test_waits_for_entity_resolution(self, setup):
        proc, _, _, write_note = setup
        name = write_note("notion_push: true\n")
        path = proc.input_dir / name
        path.write_text(path.read_text().replace("- entities_resolved\n", ""))
        assert not proc._should_process(name)


class TestAccess:
    @pytest.mark.asyncio
    async def test_owner_only_when_no_access_list(self, setup):
        proc, notion, _, write_note = setup
        await proc.process_file(write_note("notion_push: true\n"))
        assert shared_ids(notion.created[0]) == [OWNER_ID]

    @pytest.mark.asyncio
    async def test_speakers_are_not_shared_with(self, setup):
        proc, notion, _, write_note = setup
        # Joseph speaks in the meeting; only Irina is named.
        await proc.process_file(write_note("notion_push: true\nnotion_access:\n- '[[Irina Tavera]]'\n"))
        assert shared_ids(notion.created[0]) == [OWNER_ID, IRINA_ID]

    @pytest.mark.asyncio
    async def test_records_who_it_was_shared_with(self, setup):
        proc, _, _, write_note = setup
        name = write_note("notion_push: true\nnotion_access:\n- '[[Irina Tavera]]'\n")
        await proc.process_file(name)
        fm = read_frontmatter_from_file(proc.input_dir / name)
        assert fm["notion_shared_with"] == ["[[Maxime Fournes]]", "[[Irina Tavera]]"]
        assert fm["notion_transcript_url"] == "https://notion.so/page"

    @pytest.mark.asyncio
    async def test_unknown_person_blocks_push(self, setup):
        proc, notion, _, write_note = setup
        with pytest.raises(AccessError, match="Nobody"):
            await proc.process_file(write_note("notion_push: true\nnotion_access:\n- '[[Nobody]]'\n"))
        assert notion.created == []

    @pytest.mark.asyncio
    async def test_missing_notion_email_blocks_push(self, setup):
        proc, notion, people, write_note = setup
        write_person(people, "Irina Tavera")  # has `email`, but no `notion_email`
        with pytest.raises(AccessError, match="notion_email"):
            await proc.process_file(write_note("notion_push: true\nnotion_access:\n- '[[Irina Tavera]]'\n"))
        assert notion.created == []

    @pytest.mark.asyncio
    async def test_email_not_in_workspace_blocks_push(self, setup):
        proc, notion, people, write_note = setup
        write_person(people, "Irina Tavera", "irina@elsewhere.org")
        with pytest.raises(AccessError, match="0 Notion users"):
            await proc.process_file(write_note("notion_push: true\nnotion_access:\n- '[[Irina Tavera]]'\n"))
        assert notion.created == []

    @pytest.mark.asyncio
    async def test_owner_mapping_is_required_too(self, setup):
        proc, notion, people, write_note = setup
        write_person(people, "Maxime Fournes")
        with pytest.raises(AccessError, match="Maxime Fournes"):
            await proc.process_file(write_note("notion_push: true\n"))
        assert notion.created == []

    @pytest.mark.asyncio
    async def test_page_trashed_when_notion_reports_other_people(self, setup):
        proc, notion, _, write_note = setup
        notion.stored_people_override = [OWNER_ID, JOSEPH_ID]
        name = write_note("notion_push: true\n")
        with pytest.raises(AccessError, match="trashed"):
            await proc.process_file(name)
        assert notion.trashed == ["page-1"]
        assert "notion_transcript_url" not in read_frontmatter_from_file(proc.input_dir / name)

    @pytest.mark.asyncio
    async def test_schema_change_blocks_push(self, setup):
        proc, notion, _, write_note = setup
        del notion.schema["properties"][PARTICIPANTS_PROPERTY]
        with pytest.raises(ValueError, match="Participants"):
            await proc.process_file(write_note("notion_push: true\n"))
        assert notion.created == []


class TestLeftovers:
    @pytest.mark.asyncio
    async def test_trashes_own_leftovers_but_not_hand_made_pages(self, setup):
        proc, notion, _, write_note = setup
        notion.existing_pages = [
            {"id": "old-bot", "title": "2026-09-29-meeting", "created_by": {"id": "bot-noteflow"}},
            {"id": "old-human", "title": "2026-09-29-meeting", "created_by": {"id": OWNER_ID}},
            {"id": "other", "title": "2026-09-30-other", "created_by": {"id": "bot-noteflow"}},
        ]
        await proc.process_file(write_note("notion_push: true\n"))
        assert notion.trashed == ["old-bot"]
        assert len(notion.created) == 1


class TestGuests:
    @pytest.mark.asyncio
    async def test_members_need_no_scan(self, setup):
        proc, notion, _, write_note = setup
        await proc.process_file(write_note("notion_push: true\nnotion_access:\n- '[[Irina Tavera]]'\n"))
        assert notion.scans == 0

    @pytest.mark.asyncio
    async def test_guest_found_in_page_columns(self, setup):
        proc, notion, people, write_note = setup
        write_person(people, "Clemence Peyrot", "clemence@example.org")
        await proc.process_file(write_note("notion_push: true\nnotion_access:\n- '[[Clemence Peyrot]]'\n"))
        assert shared_ids(notion.created[0]) == [OWNER_ID, "user-clemence"]

    @pytest.mark.asyncio
    async def test_guest_nowhere_blocks_push_and_scan_is_cached(self, setup):
        proc, notion, people, write_note = setup
        write_person(people, "Ella Hughes", "ella@example.org")
        name = write_note("notion_push: true\nnotion_access:\n- '[[Ella Hughes]]'\n")
        for _ in range(2):
            with pytest.raises(AccessError, match="guest"):
                await proc.process_file(name)
        assert notion.created == []
        assert notion.scans == 1

    @pytest.mark.asyncio
    async def test_one_email_on_two_accounts_blocks_push(self, setup):
        proc, notion, people, write_note = setup
        notion.seen_in_pages.append({"id": "user-irina-2", "name": "Irina", "email": "irina@example.org"})
        write_person(people, "Clemence Peyrot", "clemence@example.org")  # forces the scan
        with pytest.raises(AccessError, match="2 Notion users"):
            await proc.process_file(write_note(
                "notion_push: true\nnotion_access:\n- '[[Irina Tavera]]'\n- '[[Clemence Peyrot]]'\n"))
        assert notion.created == []


class TestSpeakersKeyword:
    @pytest.mark.asyncio
    async def test_shares_with_identified_speakers(self, setup):
        proc, notion, _, write_note = setup
        await proc.process_file(write_note("notion_push: true\nnotion_access: speakers\n"))
        assert shared_ids(notion.created[0]) == [OWNER_ID, JOSEPH_ID]

    @pytest.mark.asyncio
    async def test_combines_with_named_people(self, setup):
        proc, notion, _, write_note = setup
        await proc.process_file(write_note("notion_push: true\nnotion_access:\n- speakers\n- '[[Irina Tavera]]'\n"))
        assert shared_ids(notion.created[0]) == [OWNER_ID, IRINA_ID, JOSEPH_ID]

    @pytest.mark.asyncio
    async def test_unmapped_speaker_is_left_out_not_blocking(self, setup):
        proc, notion, people, write_note = setup
        write_person(people, "Joseph Miller")  # an outsider: no notion_email
        name = write_note("notion_push: true\nnotion_access: speakers\n")
        await proc.process_file(name)
        assert shared_ids(notion.created[0]) == [OWNER_ID]
        fm = read_frontmatter_from_file(proc.input_dir / name)
        assert fm["notion_not_shared_with"] == ["[[Joseph Miller]]"]

    @pytest.mark.asyncio
    async def test_unidentified_speaker_is_ignored(self, setup):
        proc, notion, _, write_note = setup
        name = write_note("notion_push: true\nnotion_access: speakers\n")
        path = proc.input_dir / name
        path.write_text(path.read_text().replace("'[[Joseph Miller]]'", "''"))
        await proc.process_file(name)
        assert shared_ids(notion.created[0]) == [OWNER_ID]

    @pytest.mark.asyncio
    async def test_broken_speaker_mapping_still_blocks(self, setup):
        proc, notion, people, write_note = setup
        write_person(people, "Joseph Miller", "joseph@typo.org")
        with pytest.raises(AccessError, match="Joseph Miller"):
            await proc.process_file(write_note("notion_push: true\nnotion_access: speakers\n"))
        assert notion.created == []

    @pytest.mark.asyncio
    async def test_explicitly_named_speaker_is_required(self, setup):
        proc, notion, people, write_note = setup
        write_person(people, "Joseph Miller")
        with pytest.raises(AccessError, match="Joseph Miller"):
            await proc.process_file(write_note("notion_push: true\nnotion_access:\n- speakers\n- '[[Joseph Miller]]'\n"))
        assert notion.created == []


class TestParseAccessList:
    def test_accepts_links_aliases_paths_and_bare_names(self):
        assert parse_access_list(["[[Irina Tavera|Irina]]", "[[People/Joseph Miller]]", "Ana"]) == (
            ["Irina Tavera", "Joseph Miller", "Ana"], False)

    def test_single_string(self):
        assert parse_access_list("[[Irina Tavera]]") == (["Irina Tavera"], False)

    def test_deduplicates(self):
        assert parse_access_list(["[[A]]", "A"]) == (["A"], False)

    def test_unquoted_wikilink_yaml_shape(self):
        # What YAML makes of an unquoted `- [[Chay Morris]]`.
        assert parse_access_list(["speakers", [["Chay Morris"]]]) == (["Chay Morris"], True)

    @pytest.mark.asyncio
    async def test_unquoted_wikilink_in_note(self, setup):
        proc, notion, _, write_note = setup
        await proc.process_file(write_note("notion_push: true\nnotion_access:\n- [[Irina Tavera]]\n"))
        assert shared_ids(notion.created[0]) == [OWNER_ID, IRINA_ID]

    def test_speakers_keyword(self):
        assert parse_access_list("speakers") == ([], True)
        assert parse_access_list(["Speakers", "[[A]]"]) == (["A"], True)

    def test_linked_speakers_is_a_person_not_the_keyword(self):
        assert parse_access_list(["[[speakers]]"]) == (["speakers"], False)

    @pytest.mark.parametrize("bad", [{"a": 1}, [None], [""], ["[[broken"], 3,
                                     [[["A", "B"]]], [[[["A"]]]], [["A"]]])
    def test_rejects_anything_else(self, bad):
        with pytest.raises(AccessError):
            parse_access_list(bad)


class TestTranscript:
    def test_keeps_only_the_transcript(self):
        body = NOTE.split("---\n", 2)[2]
        text = extract_transcript(body)
        assert "confidential" not in text
        assert "Speakers:" not in text
        assert text.startswith("Maxime Fournes:\nHi Joe, thanks for coming.")
        assert "Joseph Miller:\nHappy to be here." in text
        assert "[[" not in text

    def test_one_paragraph_per_turn_with_bold_speaker(self):
        blocks = transcript_blocks("Maxime Fournes:\nHello.\n\nIrina Tavera:\nHi.")
        assert len(blocks) == 2
        runs = blocks[0]["paragraph"]["rich_text"]
        assert runs[0]["text"]["content"] == "Maxime Fournes\n"
        assert runs[0]["annotations"] == {"bold": True}
        assert runs[1]["text"]["content"] == "Hello."

    def test_long_turns_split_under_api_limit(self):
        blocks = transcript_blocks("Maxime Fournes:\n" + "word " * 1000)
        runs = blocks[0]["paragraph"]["rich_text"]
        assert len(runs) > 2
        assert all(len(r["text"]["content"]) <= 2000 for r in runs)
        assert "".join(r["text"]["content"] for r in runs[1:]) == ("word " * 1000).strip()
