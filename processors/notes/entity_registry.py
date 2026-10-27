"""
Entity Registry

What the entity resolver knows about every entity it has linked before, learnt
from the user's past resolutions:

- every alias the entity was detected as, with how often each one mapped to it
- the people it appears alongside (attendees and other entities in the same meeting)
- how many meetings it appeared in and when it was last seen
- a one-line description

Aliases the user rejected (left with no link) are counted too, so a word that is
usually a false detection is recognised as such.

The registry is the evidence behind each resolution and behind its confidence
score (see entity_confidence.py). It is stored as JSON in the vault and updated
after every resolution. ``rebuild`` recreates it from the ``resolved_entities``
of past transcripts.
"""

import difflib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from config.logging_config import setup_logger

logger = setup_logger(__name__)


# ===== Normalisation =====

def normalize_alias(text: str) -> str:
    """Lowercase, strip accents and punctuation: 'Jérémy  Duval.' -> 'jeremy duval'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(text.split())


def link_target(link: str) -> str:
    """'[[People/Maxime Fournes|Max]]' -> 'Maxime Fournes'."""
    match = re.match(r"\s*\[\[([^\]|#]+)", link or "")
    if not match:
        return ""
    return match.group(1).split("/")[-1].strip()


def entity_key(link: str) -> str:
    """Key under which a link is stored, so [[People/X]] and [[X]] are one entity."""
    return normalize_alias(link_target(link))


def _tokens(text: str) -> List[str]:
    return normalize_alias(text).split()


# ===== Candidates =====

@dataclass
class Candidate:
    """An existing entity that a mention might refer to, with the evidence for it."""
    key: str
    link: str
    entity_type: str
    alias_count: int = 0          # times this exact alias resolved to this entity
    similarity: float = 0.0       # best string similarity to any of its aliases
    token_match: bool = False     # every word of the mention is a word of its name
    is_attendee: bool = False
    companion_score: float = 0.0  # share of the other attendees it has met before
    meetings: int = 0


@dataclass
class AliasEvidence:
    """Everything the registry knows about one detected name."""
    alias: str
    total: int = 0                # times this alias was resolved, any outcome
    rejected: int = 0             # times the user left it unlinked
    candidates: List[Candidate] = field(default_factory=list)

    def history_top(self) -> Optional[Candidate]:
        """The entity this exact alias resolved to most often, if any."""
        seen = [c for c in self.candidates if c.alias_count]
        return max(seen, key=lambda c: c.alias_count) if seen else None


# ===== Registry =====

class EntityRegistry:
    """JSON-backed store of entity profiles, alias statistics and co-occurrences."""

    VERSION = 1
    # Companions kept per entity; the rest add little and bloat the file
    MAX_COMPANIONS = 40
    # Similarity below which an alias is not considered a fuzzy match
    MIN_SIMILARITY = 0.72
    # Looser bar for the meeting's own attendees, who are likely referents
    MIN_ATTENDEE_SIMILARITY = 0.7
    MAX_CANDIDATES = 8

    def __init__(self, path: Path, user_link: str = ""):
        self.path = path
        # The user attends every meeting, so co-occurring with them says nothing
        self.user_key = entity_key(user_link)
        self.entities: Dict[str, Dict] = {}
        self.rejected: Dict[str, int] = {}
        self.processed: Dict[str, str] = {}   # filename -> meeting date
        # Descriptions the user can edit (Entity Descriptions.md), by entity key.
        # They take precedence over descriptions learnt from meetings.
        self.curated: Dict[str, str] = {}
        self._alias_index: Optional[Dict[str, Dict[str, int]]] = None
        self.load()

    # ----- persistence -----

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.error("Could not read entity registry %s: %s", self.path, e)
            return
        self.entities = data.get("entities", {})
        self.rejected = data.get("rejected", {})
        self.processed = data.get("processed", {})
        self._alias_index = None

    def save(self) -> None:
        data = {
            "version": self.VERSION,
            "entities": self.entities,
            "rejected": self.rejected,
            "processed": self.processed,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # ----- learning -----

    def record_meeting(
        self,
        filename: str,
        meeting_date: str,
        resolved_entities: Iterable[Dict],
        attendees: Iterable[str] = (),
        descriptions: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Learn from one finished resolution. Returns False if already recorded."""
        if filename in self.processed:
            return False
        self.processed[filename] = str(meeting_date or "")
        descriptions = descriptions or {}

        present = set()
        for link in attendees:
            key = entity_key(link)
            if key:
                self._entity(key, link, "people")
                present.add(key)

        for item in resolved_entities:
            alias = normalize_alias(item.get("detected_name", ""))
            if not alias:
                continue
            link = item.get("resolved_link") or ""
            key = entity_key(link)
            if not key:
                self.rejected[alias] = self.rejected.get(alias, 0) + 1
                continue
            entity = self._entity(key, link, item.get("entity_type") or "people")
            entity["aliases"][alias] = entity["aliases"].get(alias, 0) + 1
            description = descriptions.get(link) or descriptions.get(key)
            if description and not entity.get("description"):
                entity["description"] = description.strip()[:200]
            present.add(key)

        for key in present:
            entity = self.entities[key]
            entity["meetings"] = entity.get("meetings", 0) + 1
            if str(meeting_date) > entity.get("last_seen", ""):
                entity["last_seen"] = str(meeting_date)
            companions = entity.setdefault("companions", {})
            for other in present - {key, self.user_key}:
                companions[other] = companions.get(other, 0) + 1
            if len(companions) > self.MAX_COMPANIONS:
                kept = sorted(companions.items(), key=lambda kv: -kv[1])[:self.MAX_COMPANIONS]
                entity["companions"] = dict(kept)

        self._alias_index = None
        return True

    def _entity(self, key: str, link: str, entity_type: str) -> Dict:
        entity = self.entities.get(key)
        if entity is None:
            entity = self.entities[key] = {
                "link": link,
                "type": entity_type if entity_type in ("people", "org") else "people",
                "aliases": {},
                "companions": {},
                "meetings": 0,
                "last_seen": "",
                "description": "",
            }
        # Prefer the plain [[Name]] form over a path-prefixed one
        if "/" in link_target_raw(entity["link"]) and "/" not in link_target_raw(link):
            entity["link"] = link
        return entity

    # ----- lookup -----

    def _index(self) -> Dict[str, Dict[str, int]]:
        """alias -> {entity key: count}; the canonical name counts as an alias."""
        if self._alias_index is None:
            index: Dict[str, Dict[str, int]] = {}
            for key, entity in self.entities.items():
                index.setdefault(key, {}).setdefault(key, 0)
                for alias, count in entity["aliases"].items():
                    index.setdefault(alias, {})[key] = index.get(alias, {}).get(key, 0) + count
            self._alias_index = index
        return self._alias_index

    def evidence(self, detected_name: str, attendees: Iterable[str] = ()) -> AliasEvidence:
        """Collect the candidate entities for a mention and the evidence for each."""
        alias = normalize_alias(detected_name)
        index = self._index()
        attendee_keys = {entity_key(a) for a in attendees} - {""}
        others = attendee_keys - {self.user_key}

        exact = index.get(alias, {})
        rejected = self.rejected.get(alias, 0)
        ev = AliasEvidence(alias=alias, total=sum(exact.values()) + rejected, rejected=rejected)

        # Fuzzy matches on every known alias, which is what catches transcription
        # errors like "Ani" for Annie
        scores: Dict[str, float] = {k: 1.0 for k in exact}
        mention_tokens = set(alias.split())
        matcher = difflib.SequenceMatcher(autojunk=False)
        matcher.set_seq2(alias)
        for known, keys in index.items():
            if known == alias:
                continue
            matcher.set_seq1(known)
            if matcher.real_quick_ratio() < self.MIN_SIMILARITY and not (mention_tokens & set(known.split())):
                continue
            sim = matcher.ratio()
            if mention_tokens and mention_tokens <= set(known.split()):
                sim = max(sim, 0.8)
            if sim < self.MIN_SIMILARITY:
                continue
            for key in keys:
                scores[key] = max(scores.get(key, 0.0), sim)
        # Attendees are likely referents even when misheard, but only if the
        # spelling is close: misheard names score about 0.75 and up ("Ezra"
        # for Esra), while unrelated words can reach 0.55 against one part of
        # a name ("Google" against Cooke)
        for key in attendee_keys:
            if key in self.entities and key not in scores:
                sim = max([self._similarity(alias, key)] + [self._similarity(alias, t) for t in key.split()])
                if sim >= self.MIN_ATTENDEE_SIMILARITY:
                    scores[key] = sim

        ranked = sorted(
            scores.items(),
            key=lambda kv: (exact.get(kv[0], 0), kv[0] in attendee_keys, kv[1]),
            reverse=True,
        )[:self.MAX_CANDIDATES]

        for key, sim in ranked:
            entity = self.entities.get(key)
            if not entity:
                continue
            companions = entity.get("companions", {})
            ev.candidates.append(Candidate(
                key=key,
                link=entity["link"],
                entity_type=entity["type"],
                alias_count=exact.get(key, 0),
                similarity=max(sim, self._similarity(alias, key)),
                token_match=bool(mention_tokens) and mention_tokens <= set(key.split()),
                is_attendee=key in attendee_keys,
                companion_score=(sum(1 for o in others if o in companions) / len(others)) if others else 0.0,
                meetings=entity.get("meetings", 0),
            ))
        return ev

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()

    def lookup(self, link: str) -> Optional[Dict]:
        return self.entities.get(entity_key(link))

    def profile(self, link: str, max_aliases: int = 6, max_companions: int = 5) -> str:
        """One-line description of an entity for the AI prompt."""
        entity = self.lookup(link)
        if not entity:
            return f"{link} (new)"
        aliases = sorted(entity["aliases"].items(), key=lambda kv: -kv[1])[:max_aliases]
        companions = sorted(entity.get("companions", {}).items(), key=lambda kv: -kv[1])[:max_companions]
        parts = [f"{entity['link']} ({entity['type']}, {entity.get('meetings', 0)} meetings, last {entity.get('last_seen') or '?'})"]
        description = self.curated.get(entity_key(link)) or entity.get("description")
        if description:
            parts.append(description)
        if aliases:
            parts.append("heard as: " + ", ".join(f"{a} x{n}" for a, n in aliases))
        if companions:
            names = [self.entities[k]["link"] if k in self.entities else k for k, _ in companions]
            parts.append("usually with: " + ", ".join(names))
        return " — ".join(parts)

    def reference_text(self, max_aliases: int = 4) -> str:
        """Every known entity with its commonest spellings, for the detection prompt."""
        sections = []
        for entity_type, title in [("people", "People"), ("org", "Organisations")]:
            entities = [e for e in self.entities.values() if e["type"] == entity_type]
            if not entities:
                continue
            lines = [f"## {title}"]
            for entity in sorted(entities, key=lambda e: link_target(e["link"]).lower()):
                aliases = sorted(entity["aliases"].items(), key=lambda kv: -kv[1])[:max_aliases]
                heard = f": {', '.join(a for a, _ in aliases)}" if aliases else ""
                lines.append(f"- {entity['link']}{heard}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    # ----- rebuild -----

    @classmethod
    def rebuild(cls, path: Path, user_link: str, meetings: Iterable[Tuple[str, str, List[Dict], List[str]]]) -> "EntityRegistry":
        """Recreate the registry from (filename, date, resolved_entities, attendees) tuples."""
        registry = cls.__new__(cls)
        registry.path = path
        registry.user_key = entity_key(user_link)
        registry.entities, registry.rejected, registry.processed = {}, {}, {}
        registry.curated = {}
        registry._alias_index = None
        for filename, meeting_date, resolved, attendees in sorted(meetings, key=lambda m: (str(m[1]), m[0])):
            registry.record_meeting(filename, meeting_date, resolved, attendees)
        return registry


def link_target_raw(link: str) -> str:
    match = re.match(r"\s*\[\[([^\]|#]+)", link or "")
    return match.group(1) if match else ""


# ===== Curated descriptions =====

DESCRIPTIONS_HEADER = """# Entity Descriptions

One line per entity, shown to the entity resolver next to each candidate so it can tell
similar names apart (two people called Tom, a national chapter and the global
organisation). Edit or add rows freely: the resolver reads this file every time.
Prefilled by scripts/prefill_entity_descriptions.py, which keeps existing rows.

| Entity | Description |
|---|---|
"""


def load_descriptions(path: Path) -> Dict[str, str]:
    """Entity key -> description, from the descriptions table."""
    return {key: text for key, (_, text) in read_description_rows(path).items()}


def read_description_rows(path: Path) -> Dict[str, Tuple[str, str]]:
    rows: Dict[str, Tuple[str, str]] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*\|\s*(\[\[[^\]]+\]\])\s*\|(.*)\|\s*$", line)
        if match and match.group(2).strip():
            link = match.group(1)
            rows[entity_key(link)] = (link, match.group(2).strip())
    return rows


def write_descriptions(path: Path, rows: Dict[str, Tuple[str, str]]) -> None:
    """Write {key: (link, description)} as the descriptions table, sorted by name."""
    lines = [DESCRIPTIONS_HEADER.rstrip("\n")]
    for link, text in sorted(rows.values(), key=lambda r: link_target(r[0]).lower()):
        lines.append(f"| {link} | {text.replace('|', '/')} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
