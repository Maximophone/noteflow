"""
Pushes a meeting's raw transcript -- never the summary -- to the shared
Meeting Transcripts database in Notion.

Opt-in per note, from its own frontmatter:

    notion_push: true          # a checkbox in Obsidian's property editor
    notion_access:             # who else gets to see it, by People note
      - "[[Irina Tavera]]"

Access in that database is granted row by row: whoever is in the row's
"Participants (Can View and Edit)" column can open it. So the one failure
that matters here is putting someone in that column who should not be
there, and every rule below is there to make that impossible rather than
merely unlikely:

- Only people named in `notion_access` are shared with, plus the user. Being
  a speaker in the meeting, or being mentioned in it, grants nothing by
  default -- some meetings are about a participant as much as with them.
  Sharing with the speakers takes the explicit keyword `speakers` in the
  list (see below).
- A name reaches Notion only through an explicit `notion_email` property on
  that person's note in People/. No fuzzy matching, no name lookup, no
  fallback to their ordinary `email` -- an address they use elsewhere is
  not necessarily the one their Notion account is under, and several
  people here have more than one account.
- Members are looked up in the workspace's user list. Guests are not in
  it -- the API hides them -- so an address not found there is looked for
  among the accounts in the people columns, authors and editors of the
  pages the integration can see, where a guest's account does appear with
  its email. A guest who shows up in none of those cannot be found, and
  blocks the push.
- Any name that cannot be resolved stops the whole push. Nothing is created
  with a partial list, because the fix (add the mapping) is quick and a
  silently dropped person is a surprise later.
- The participant list is set in the same call that creates the page, and
  read back afterwards. If Notion holds anything other than what was asked
  for, the page goes to the trash and the stage fails.

An empty or missing `notion_access` is valid and shares with the user
only. Under-sharing is the safe direction to fail in: anyone can be added
in Notion afterwards.

`notion_access: speakers` (or `speakers` as one entry alongside names)
adds every identified speaker. Speakers are often outside the workspace,
so one with no People note or no `notion_email` is left out rather than
blocking the push, and is listed under `notion_not_shared_with`. A
`notion_email` that is present but matches no Notion user still stops the
push: that is a broken mapping, not an outsider.
"""

import asyncio
import os
import re
import time
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .base import NoteProcessor
from .entity_resolver import EntityResolver
from ..common.notion_push import ACCESS_KEY, PUSH_KEY
from ..common.frontmatter import (
    parse_frontmatter_from_content,
    read_text_from_content,
    set_frontmatter_in_file,
)
from config.logging_config import setup_logger
from config.paths import PATHS
from config.user_config import NOTION_TRANSCRIPT_OWNER
from integrations.notion_integration import NotionClient

logger = setup_logger(__name__)

# On a person's note in People/: the address of their Notion account.
PERSON_EMAIL_KEY = "notion_email"

# Written back once the push succeeds.
URL_KEY = "notion_transcript_url"
SHARED_WITH_KEY = "notion_shared_with"
NOT_SHARED_KEY = "notion_not_shared_with"

# In `notion_access`, stands for every identified speaker. Only as a bare
# word: "[[Speakers]]" would be a person.
SPEAKERS_KEYWORD = "speakers"

# Column names in the Meeting Transcripts database.
TITLE_PROPERTY = "Name"
DATE_PROPERTY = "Date"
PARTICIPANTS_PROPERTY = "Participants (Can View and Edit)"

# How long the scan of every visible page for guest accounts is reused. It
# is slow (thousands of pages), and a blocked note retries every cycle.
GUEST_SCAN_TTL_SECONDS = 15 * 60

# The API rejects any single text run over 2000 characters.
MAX_TEXT_RUN = 2000

# Forms and status callouts the pipeline writes into a note, wrapped in
# start/end markers. None of it is transcript.
_GENERATED_BLOCK = re.compile(
    r"<!--\s*(form|summary):([\w-]+):start\s*-->.*?<!--\s*\1:\2:end\s*-->",
    re.DOTALL,
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
# A speaker turn opens with a line of the form "Maxime Fournes:".
_SPEAKER_LINE = re.compile(r"^([^:\n]{1,80}):\s*$")


class AccessError(Exception):
    """The access list cannot be turned into Notion users with certainty."""


def extract_transcript(body: str) -> str:
    """The note body reduced to the spoken transcript.

    Drops the pipeline's forms and summaries, and turns wikilinks back into
    plain names: they would be dead links in Notion.
    """
    text = _GENERATED_BLOCK.sub("", body)
    text = _HTML_COMMENT.sub("", text)
    text = _WIKILINK.sub(lambda m: (m.group(2) or m.group(1).split("/")[-1]).strip(), text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_access_list(value: Any) -> Tuple[List[str], bool]:
    """Person names from the `notion_access` property, in order, deduplicated,
    and whether it asks for the speakers too.

    Takes wikilinks or bare names. Anything that is not a plain list of
    names is rejected outright rather than interpreted.
    """
    if value is None:
        return [], False
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise AccessError(f"'{ACCESS_KEY}' must be a list of people, got: {value!r}")

    names, speakers = [], False
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise AccessError(f"'{ACCESS_KEY}' has an entry that is not a name: {entry!r}")
        if entry.strip().lower() == SPEAKERS_KEYWORD:
            speakers = True
            continue
        name = person_name(entry)
        if not name:
            raise AccessError(f"'{ACCESS_KEY}' has an entry that is not a name: {entry!r}")
        if name not in names:
            names.append(name)
    return names, speakers


def speaker_names(frontmatter: Dict) -> List[str]:
    """Names of the identified speakers. Unidentified ones have no person_id."""
    mapping = frontmatter.get("final_speaker_mapping")
    if not isinstance(mapping, dict):
        raise AccessError(
            f"'{ACCESS_KEY}' asks for '{SPEAKERS_KEYWORD}' but the note has no speaker mapping"
        )
    names = []
    for speaker in mapping.values():
        person_id = speaker.get("person_id") if isinstance(speaker, dict) else None
        name = person_name(person_id) if isinstance(person_id, str) and person_id.strip() else ""
        if name and name not in names:
            names.append(name)
    return names


def person_name(entry: str) -> str:
    """'[[People/Irina Tavera|Irina]]' -> 'Irina Tavera'. Bare names pass through."""
    entry = entry.strip()
    match = _WIKILINK.fullmatch(entry)
    if match:
        entry = match.group(1)
    elif "[" in entry or "]" in entry:
        return ""
    return entry.split("/")[-1].strip()


def transcript_blocks(transcript: str) -> List[Dict[str, Any]]:
    """One paragraph per speaker turn, the speaker's name in bold."""
    blocks = []
    for turn in re.split(r"\n\s*\n", transcript):
        lines = turn.strip().split("\n")
        if not lines or not lines[0]:
            continue
        runs = []
        speaker = _SPEAKER_LINE.match(lines[0])
        if speaker and len(lines) > 1:
            runs.append(_text_run(speaker.group(1) + "\n", bold=True))
            lines = lines[1:]
        for chunk in _chunks("\n".join(lines), MAX_TEXT_RUN):
            runs.append(_text_run(chunk))
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": runs},
        })
    return blocks


def _text_run(content: str, bold: bool = False) -> Dict[str, Any]:
    run = {"type": "text", "text": {"content": content}}
    if bold:
        run["annotations"] = {"bold": True}
    return run


def _chunks(text: str, size: int) -> List[str]:
    """Split text into pieces of at most `size`, preferring to break at a space."""
    pieces = []
    while len(text) > size:
        cut = text.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        pieces.append(text[:cut])
        text = text[cut:]
    pieces.append(text)
    return pieces


def _index_by_email(users: List[Dict]) -> Dict[str, Dict[str, Dict]]:
    """email (lowercased) -> {user id: user}. Two ids under one email would be ambiguous."""
    index: Dict[str, Dict[str, Dict]] = {}
    for user in users:
        if user.get("email") and user.get("id"):
            index.setdefault(user["email"].strip().lower(), {})[user["id"]] = user
    return index


class NotionUploadProcessor(NoteProcessor):
    """Pushes a meeting transcript to Notion, shared only with the people the note names."""
    stage_name = "notion_transcript_pushed"
    required_stage = EntityResolver.stage_name

    def __init__(self, input_dir: Path, database_url: str, people_dir: Optional[Path] = None,
                 notion: Optional[NotionClient] = None):
        super().__init__(input_dir)
        self.database_url = database_url
        self.people_dir = people_dir or PATHS.people_path
        self._notion = notion
        self._guest_scan: Optional[Tuple[float, List[Dict]]] = None

    @property
    def notion(self) -> NotionClient:
        # Built on first use, so a missing token only fails the notes that
        # actually asked to be pushed.
        if self._notion is None:
            self._notion = NotionClient()
        return self._notion

    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        # A checkbox is a real boolean; a string like "false" is not consent.
        if frontmatter.get(PUSH_KEY) is not True:
            return False
        return frontmatter.get("category") == "meeting"

    async def process_file(self, filename: str) -> None:
        file_path = self.input_dir / filename
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content) or {}

        meeting_date = frontmatter.get("date")
        if isinstance(meeting_date, (date, datetime)):
            meeting_date = meeting_date.isoformat()
        if not meeting_date:
            raise ValueError("No 'date' in frontmatter")

        transcript = extract_transcript(read_text_from_content(content))
        if not transcript:
            raise ValueError("No transcript text found in the note")

        # Everything that could stop the push is checked before anything is
        # created in Notion.
        named, with_speakers = parse_access_list(frontmatter.get(ACCESS_KEY))
        required = [person_name(NOTION_TRANSCRIPT_OWNER)]
        required += [n for n in named if n not in required]
        optional = [n for n in speaker_names(frontmatter) if n not in required] if with_speakers else []
        names, user_ids, left_out = await self._resolve_people(required, optional)

        schema = self.notion.get_single_data_source(self.database_url)
        self._check_schema(schema)

        title = file_path.stem
        properties = {
            TITLE_PROPERTY: {"title": [{"text": {"content": title}}]},
            DATE_PROPERTY: {"date": {"start": str(meeting_date)}},
            PARTICIPANTS_PROPERTY: {"people": [{"id": uid} for uid in user_ids]},
        }

        logger.info("Pushing transcript %s to Notion, shared with: %s", filename, ", ".join(names))
        page = self.notion.create_page_in_data_source(
            schema["id"], properties, transcript_blocks(transcript)
        )
        self._verify_participants(page["id"], user_ids)

        # Re-read: the note may have been edited while the push ran.
        fresh = parse_frontmatter_from_content(await self.read_file(filename)) or frontmatter
        fresh[URL_KEY] = page.get("url")
        fresh[SHARED_WITH_KEY] = [f"[[{n}]]" for n in names]
        if left_out:
            fresh[NOT_SHARED_KEY] = [f"[[{n}]]" for n in left_out]
        set_frontmatter_in_file(file_path, fresh)
        os.utime(file_path, None)
        logger.info("Pushed transcript %s to Notion: %s", filename, page.get("url"))

    async def _resolve_people(self, required: List[str],
                              optional: List[str]) -> Tuple[List[str], List[str], List[str]]:
        """Notion user ids for these names, through their People notes.

        Returns the names shared with, their user ids, and the optional names
        left out. A required name that cannot be resolved stops the push; an
        optional one (a speaker) is left out only when it has no mapping at
        all. Every problem found is reported at once.
        """
        emails: Dict[str, str] = {}
        names, ids, left_out, problems = [], [], [], []
        for name in required + optional:
            is_optional = name in optional
            email = self._person_email(name, [] if is_optional else problems)
            if email is not None:
                emails[name] = email
            elif is_optional:
                left_out.append(name)

        directory = _index_by_email(self.notion.list_people())
        if not directory:
            raise AccessError(
                "Notion returned no user emails; the integration needs the "
                "'Read user information including email addresses' capability"
            )
        if any(e.lower() not in directory for e in emails.values()):
            for email, users in _index_by_email(await self._guests()).items():
                directory.setdefault(email, {}).update(users)

        for name, email in emails.items():
            matches = list(directory.get(email.lower(), {}))
            if len(matches) != 1:
                hint = (" -- if they are a guest, put them in a people column on any "
                        "page the integration can see, so it can find their account "
                        "(new accounts are picked up within 15 minutes)"
                        if not matches else "")
                problems.append(
                    f"[[{name}]]: {PERSON_EMAIL_KEY} {email} matches "
                    f"{len(matches)} Notion users, expected exactly 1{hint}"
                )
                continue
            if matches[0] not in ids:
                names.append(name)
                ids.append(matches[0])

        if problems:
            raise AccessError("Not pushed to Notion. " + "; ".join(problems))
        if left_out:
            logger.info("Speakers left out of the Notion share (no notion_email): %s", ", ".join(left_out))
        return names, ids, left_out

    async def _guests(self) -> List[Dict]:
        """Accounts seen in visible pages' people columns, from a recent scan if there is one."""
        if self._guest_scan and time.monotonic() - self._guest_scan[0] < GUEST_SCAN_TTL_SECONDS:
            return self._guest_scan[1]
        logger.info("Scanning visible Notion pages for guest accounts")
        # Off the event loop: it is thousands of pages, and every other stage waits on this loop.
        users = await asyncio.to_thread(self.notion.list_people_seen_in_pages)
        self._guest_scan = (time.monotonic(), users)
        return users

    def _person_email(self, name: str, problems: List[str]) -> Optional[str]:
        person_file = self.people_dir / f"{name}.md"
        if not person_file.is_file():
            problems.append(f"[[{name}]] has no note in {self.people_dir.name}/")
            return None
        try:
            person_fm = parse_frontmatter_from_content(person_file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            problems.append(f"[[{name}]]: could not read note ({e})")
            return None
        email = person_fm.get(PERSON_EMAIL_KEY)
        if not isinstance(email, str) or "@" not in email:
            problems.append(f"[[{name}]] has no '{PERSON_EMAIL_KEY}' property")
            return None
        return email.strip()

    @staticmethod
    def _check_schema(schema: Dict) -> None:
        props = schema.get("properties") or {}
        expected = {
            TITLE_PROPERTY: "title",
            DATE_PROPERTY: "date",
            PARTICIPANTS_PROPERTY: "people",
        }
        wrong = [
            f"'{name}' ({kind})" for name, kind in expected.items()
            if (props.get(name) or {}).get("type") != kind
        ]
        if wrong:
            raise ValueError(
                "Notion database no longer has the expected columns: " + ", ".join(wrong)
            )

    def _verify_participants(self, page_id: str, expected_ids: List[str]) -> None:
        """Read the participant list back; trash the page if it is not exactly as sent."""
        try:
            page = self.notion.get_page(page_id)
            people = page["properties"][PARTICIPANTS_PROPERTY]["people"]
            actual = {p["id"] for p in people}
        except Exception as e:
            actual = None
            reason = f"could not read it back ({e})"
        else:
            reason = f"expected {sorted(expected_ids)}, got {sorted(actual)}"

        if actual == set(expected_ids):
            return
        logger.error("Participant check failed for Notion page %s: %s", page_id, reason)
        self.notion.trash_page(page_id)
        raise AccessError(f"Participant list on the new Notion page was wrong ({reason}); page trashed")

    async def reset(self, filename: str) -> None:
        """Forget the push so it runs again. Delete the Notion page first, or it will be duplicated."""
        file_path = self.input_dir / filename
        if not file_path.exists():
            logger.error(f"File not found during reset: {filename}")
            return
        try:
            frontmatter = parse_frontmatter_from_content(await self.read_file(filename))
            if not frontmatter:
                logger.warning(f"No frontmatter found in {filename}. Cannot reset stage.")
                return
            frontmatter.pop(URL_KEY, None)
            frontmatter.pop(SHARED_WITH_KEY, None)
            frontmatter.pop(NOT_SHARED_KEY, None)
            stages = frontmatter.get("processing_stages", [])
            if self.stage_name in stages:
                stages.remove(self.stage_name)
            set_frontmatter_in_file(file_path, frontmatter)
            os.utime(file_path, None)
            logger.info(f"Reset stage '{self.stage_name}' for: {filename}")
        except Exception as e:
            logger.error(f"Error resetting stage '{self.stage_name}' for {filename}: {e}")
            logger.error(traceback.format_exc())
