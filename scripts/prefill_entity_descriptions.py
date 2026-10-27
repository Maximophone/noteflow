"""
Prefill descriptions for the entities that are easiest to confuse.

The entity resolver shows each candidate's description next to its name, which is
what lets it tell, say, two people called Tom apart from what the meeting is about.
This picks the entities that recur under ambiguous names (a spelling that resolved to
several entities, or a first name shared with other people), reads each one's note in
the vault, and asks the AI for a one-line description that sets it apart.

Descriptions are written to ``Entity Descriptions.md`` in the vault, a table the user
can edit. Rows already in the file are kept, so edits survive a rerun; pass --refresh
to regenerate them.

    python scripts/prefill_entity_descriptions.py --limit 300
    python scripts/prefill_entity_descriptions.py --cutoff 2026-09-15 --out /tmp/x.md

--cutoff ignores dated log entries from that day on, so a replay of later meetings
(scripts/entity_resolution_eval.py --descriptions) cannot see their outcome.
"""

import argparse
import asyncio
import collections
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_core import AI
from ai_core.types import Message, MessageContent

from config.paths import PATHS
from config.services_config import SMALL_MODEL
from config.user_config import USER_NAME
from processors.notes.entity_registry import (
    EntityRegistry, read_description_rows, link_target, write_descriptions,
)
from processors.notes.entity_resolver import EntityResolver
from prompts.prompts import get_prompt

MAX_NOTE_CHARS = 8000


def confusable_entities(registry: EntityRegistry, min_meetings: int = 2):
    """Entity keys ranked by how often they are involved in an ambiguity.

    Returns (ranked keys, {key: similar keys}).
    """
    weight = collections.Counter()
    similar = collections.defaultdict(set)

    for alias, keys in registry._index().items():
        real = {k: c for k, c in keys.items() if c > 0}
        if len(real) > 1:
            for k in real:
                weight[k] += sum(real.values())
                similar[k] |= set(real) - {k}

    by_first_name = collections.defaultdict(list)
    for key, entity in registry.entities.items():
        if entity["type"] == "people":
            by_first_name[key.split()[0]].append(key)
    for keys in by_first_name.values():
        if len(keys) > 1:
            for k in keys:
                weight[k] += registry.entities[k].get("meetings", 0)
                similar[k] |= set(keys) - {k}

    ranked = [k for k in weight if registry.entities[k].get("meetings", 0) >= min_meetings]
    ranked.sort(key=lambda k: -weight[k])
    return ranked, similar


def vault_notes() -> dict:
    """Lowercased note name -> path, for every note in the vault."""
    notes = {}
    for root, dirs, files in os.walk(PATHS.vault_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.endswith(".md"):
                notes.setdefault(name[:-3].lower(), Path(root) / name)
    return notes


def note_text(path: Path, cutoff: str = "") -> str:
    """The note without query blocks, and without dated sections from `cutoff` on."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"```dataview.*?```", "", text, flags=re.S)
    if cutoff:
        sections = re.split(r"(?m)^(?=#{1,3} \d{4}-\d{2}-\d{2})", text)
        text = "".join(
            s for s in sections
            if not (m := re.match(r"#{1,3} (\d{4}-\d{2}-\d{2})", s)) or m.group(1) < cutoff
        )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:MAX_NOTE_CHARS]


async def describe(ai: AI, link: str, similar_links, note: str) -> str:
    prompt = get_prompt("describe_entity")
    for placeholder, value in [
        ("{user_name}", USER_NAME),
        ("{link}", link),
        ("{similar}", "\n".join(f"- {s}" for s in similar_links) or "(none)"),
        ("{note}", note),
    ]:
        prompt = prompt.replace(placeholder, value)
    message = Message(role="user", content=[MessageContent(type="text", text=prompt)])
    response = await asyncio.to_thread(ai.message, message, max_tokens=400)
    if response.error:
        raise RuntimeError(response.error)
    return str(EntityResolver._parse_json(response.content).get("description", "")).strip()


async def run(args) -> None:
    resolver = EntityResolver(PATHS.transcriptions)
    # Built in memory: this script must not create the registry the service owns
    registry = resolver._build_registry()
    ranked, similar = confusable_entities(registry)
    notes = vault_notes()

    existing = read_description_rows(args.out)
    todo = []
    for key in ranked:
        link = registry.entities[key]["link"]
        path = notes.get(link_target(link).lower())
        if path is None:
            continue
        if len(todo) >= args.limit:
            break
        if key in existing and not args.refresh:
            continue
        todo.append((key, link, path))
    print(f"{len(ranked)} confusable entities; describing {len(todo)}")

    ai = AI(SMALL_MODEL)
    sem = asyncio.Semaphore(args.concurrency)
    results = dict(existing)

    async def one(key, link, path):
        async with sem:
            try:
                note = note_text(path, args.cutoff)
                similar_links = sorted(registry.entities[k]["link"] for k in similar[key] if k in registry.entities)[:10]
                text = await describe(ai, link, similar_links, note)
            except Exception as e:
                print(f"  failed {link}: {e}")
                return
            if text:
                results[key] = (link, text)
                print(f"  {link}: {text}")

    await asyncio.gather(*(one(*item) for item in todo))
    write_descriptions(args.out, results)
    print(f"Wrote {len(results)} descriptions to {args.out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--cutoff", default="", help="ignore dated log entries from this YYYY-MM-DD on")
    parser.add_argument("--out", type=Path, default=PATHS.vault_path / "KnowledgeBot" / "Entity Descriptions.md")
    parser.add_argument("--refresh", action="store_true", help="regenerate rows already in the file")
    parser.add_argument("--concurrency", type=int, default=8)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
