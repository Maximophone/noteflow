"""
Entity Resolution Processor

Detects named entities in transcripts and resolves them to wikilinks,
using AI detection and user validation via inline Obsidian forms.
"""

from pathlib import Path
from typing import Dict, Any, Optional, List
import aiofiles
import difflib
import json
import os
import re
import asyncio

from .base import NoteProcessor
from .speaker_identifier import SpeakerIdentifier
from ..common.frontmatter import parse_frontmatter_from_content, frontmatter_to_text, read_text_from_content
from ..common.obsidian_form import validate_wikilink_field, validate_choice_field, insert_error_in_section
from . import entity_confidence
from .entity_registry import (
    AliasEvidence, EntityRegistry, entity_key, load_descriptions, load_tracked_orgs, normalize_alias,
)
from ai_core import AI
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from config.paths import PATHS
from config.user_config import USER_NAME
from prompts.prompts import get_prompt

logger = setup_logger(__name__)


class EntityResolutionError(Exception):
    """Exception raised when entity resolution encounters an error."""
    pass


class ResultsNotReadyError(Exception):
    """Raised when user input is not yet available."""
    pass


class EntityResolver(NoteProcessor):
    """Resolves named entities in transcripts to wikilinks, asking the user only when unsure.
    
    **Substage 1: Detection**
        - AI lists the people and known organisations mentioned in the transcript
    
    **Substage 2: Resolution**
        - For each mention, the entity registry supplies candidates and the
          evidence for them (past spellings, attendees, usual companions)
        - AI picks a link for each mention (an existing note, a new one, or none)
        - Each answer gets a calibrated probability of being right (entity_confidence)
    
    **Substage 3: Review** (only when something is uncertain)
        - Answers at or above AUTO_ACCEPT_THRESHOLD are applied without asking
        - Otherwise a form lists the uncertain ones first, least certain on top,
          with context and alternatives; the confident ones follow, editable
    
    **Substage 4: Finalisation**
        - Replaces entity occurrences in transcript
        - Teaches the registry (and the Entity Reference file) the outcome
    
    **Frontmatter Fields**:
        - entity_resolution_pending: True while waiting for user input
        - detected_entities: the AI's answers while the form is open
        - resolved_entities: final mappings, with the AI's suggestion and confidence
          kept alongside so calibration can be checked against what the user changed
    """
    stage_name = "entities_resolved"
    required_stage = SpeakerIdentifier.stage_name
    
    # Entity types
    ENTITY_TYPES = {"people", "org"}
    
    # Answers at least this likely to be right are applied without review.
    # In the 2026-10-27 replay (scripts/entity_resolution_eval.py) this auto-accepted
    # 70% of entities, of which 0.8% disagreed with the user's past answer. 0.97
    # would auto-accept 64% with 0.5% disagreeing.
    AUTO_ACCEPT_THRESHOLD = 0.95
    
    # Form markers
    FORM_START = "<!-- form:entity_resolution:start -->"
    FORM_END = "<!-- form:entity_resolution:end -->"
    SUMMARY_START = "<!-- summary:entity_resolution:start -->"
    SUMMARY_END = "<!-- summary:entity_resolution:end -->"
    
    def __init__(self, input_dir: Path):
        super().__init__(input_dir)
        self.entity_reference_path = PATHS.vault_path / "Entity Reference.md"
        self.registry_path = PATHS.vault_path / "KnowledgeBot" / "entity_registry.json"
        # User-editable descriptions; see scripts/prefill_entity_descriptions.py
        self.descriptions_path = PATHS.vault_path / "KnowledgeBot" / "Entity Descriptions.md"
        # The only organisations that get linked; see load_tracked_orgs
        self.tracked_orgs_path = PATHS.vault_path / "KnowledgeBot" / "Tracked Organisations.md"
    
    # Start date for automatic processing (YYYY-MM-DD)
    # Files before this date will be skipped unless they have 'force_entity_resolution' tag
    START_DATE = "2025-12-15"
    
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        """Additional criteria for processing."""
        source_tags = frontmatter.get("source_tags", [])
        
        # Skip if noentity tag is set
        if "noentity" in source_tags:
            return False
            
        # Always process if force tag is present
        if "force_entity_resolution" in source_tags:
            return True
            
        # Always process if already pending user input
        if frontmatter.get('entity_resolution_pending'):
            return True
        
        # Only process meeting or email category files
        category = frontmatter.get('category')
        if category not in ('meeting', 'email'):
            return False
        
        # For emails, skip the required_stage check (no speaker identification needed)
        # For meetings, the base class already checked required_stage
        # This is handled here because base._should_process checks required_stage
        # before calling should_process, so we override that by accepting emails here
        if category == 'email':
            # Check that entity_resolution hasn't already been done
            stages = frontmatter.get('processing_stages', [])
            if self.__class__.stage_name in stages:
                return False
            # Accept email for processing
            return True
            
        # Check date for automatic processing
        file_date = frontmatter.get('date')
        if file_date:
            # Handle both string and date objects
            date_str = str(file_date)
            if date_str < self.START_DATE:
                return False
                
        return True
    
    # ===== Entity Reference File =====
    
    def _ensure_entity_reference_exists(self) -> None:
        """Create Entity Reference file if it doesn't exist."""
        if self.entity_reference_path.exists():
            return
        
        template = """# Entity Resolution Reference

## People Aliases
| Detected Name | Resolved Link |
|---------------|---------------|

## Organisation Aliases
| Detected Name | Resolved Link |
|---------------|---------------|
"""
        self.entity_reference_path.write_text(template, encoding='utf-8')
        logger.info("Created Entity Reference file at: %s", self.entity_reference_path)
    
    def _parse_entity_reference(self) -> Dict[str, Dict[str, str]]:
        """Parse Entity Reference file into lookup dict.
        
        Returns:
            Dict with structure: {
                "people": {"maxime": "[[Maxime Fournes]]", ...},
                "org": {"pause ai": "[[Pause IA]]", ...},
            }

        Sections other than People/Organisation are ignored.
        """
        self._ensure_entity_reference_exists()
        
        content = self.entity_reference_path.read_text(encoding='utf-8')
        
        result = {"people": {}, "org": {}}
        current_type = None
        
        for line in content.split('\n'):
            line = line.strip()
            
            # Detect section headers
            if "## People" in line:
                current_type = "people"
            elif "## Organisation" in line:
                current_type = "org"
            elif line.startswith("##"):
                current_type = None
            elif current_type and line.startswith("|") and "---" not in line and "Detected" not in line:
                # Parse table row: | Name | Link |
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 2:
                    detected_name = parts[0].lower()
                    resolved_link = parts[1]
                    result[current_type][detected_name] = resolved_link
        
        return result
    
    def _update_entity_reference(self, entities: List[Dict[str, str]]) -> None:
        """Update Entity Reference file with new entity mappings.
        
        Args:
            entities: List of dicts with 'detected_name', 'resolved_link', 'entity_type'
        """
        reference = self._parse_entity_reference()
        
        # Add new entries
        for entity in entities:
            if not entity.get('resolved_link'):
                continue
            
            entity_type = entity.get('entity_type')
            if entity_type not in self.ENTITY_TYPES:
                # Anything else has no section in the reference file, so it would
                # be dropped on the next rebuild. Skip it loudly instead.
                logger.warning(
                    "Not adding %r to Entity Reference: unsupported type %r",
                    entity.get('detected_name'), entity_type
                )
                continue

            detected_name = entity['detected_name'].lower()

            # Only add if not already present
            if detected_name not in reference[entity_type]:
                reference[entity_type][detected_name] = entity['resolved_link']
        
        # Rebuild file content
        lines = ["# Entity Resolution Reference", ""]
        
        for section_name, section_type in [("People Aliases", "people"), 
                                            ("Organisation Aliases", "org")]:
            lines.extend([
                f"## {section_name}",
                "| Detected Name | Resolved Link |",
                "|---------------|---------------|",
            ])
            
            for detected, resolved in sorted(reference.get(section_type, {}).items()):
                lines.append(f"| {detected.title()} | {resolved} |")
            
            lines.append("")
        
        self.entity_reference_path.write_text('\n'.join(lines), encoding='utf-8')
        logger.info("Updated Entity Reference file")
    
    # ===== Entity Registry =====

    def _load_registry(self) -> EntityRegistry:
        """Load the registry, building it from past resolutions the first time.

        Loaded fresh on every use: the transcript and email resolvers share it.
        """
        if self.registry_path.exists():
            registry = EntityRegistry(self.registry_path, f"[[{USER_NAME}]]")
        else:
            registry = self._build_registry()
            registry.save()
        registry.curated = load_descriptions(self.descriptions_path)
        registry.tracked_orgs = load_tracked_orgs(self.tracked_orgs_path)
        return registry

    def _build_registry(self) -> EntityRegistry:
        """Build the registry from the resolved_entities of every past note."""
        logger.info("Building entity registry from past resolutions")
        meetings = []
        for directory in {self.input_dir, PATHS.transcriptions}:
            for path in sorted(Path(directory).glob("*.md")):
                try:
                    content = path.read_text(encoding="utf-8")
                except OSError as e:
                    logger.warning("Skipping %s while building registry: %s", path.name, e)
                    continue
                if "resolved_entities:" not in content:
                    continue
                frontmatter = parse_frontmatter_from_content(content) or {}
                if frontmatter.get("resolved_entities"):
                    meetings.append((
                        path.name,
                        str(frontmatter.get("date", "")),
                        frontmatter["resolved_entities"],
                        self._attendees(frontmatter),
                    ))
        registry = EntityRegistry.rebuild(self.registry_path, f"[[{USER_NAME}]]", meetings)
        logger.info("Built entity registry: %d entities from %d notes", len(registry.entities), len(meetings))
        return registry

    @staticmethod
    def _attendees(frontmatter: Dict) -> List[str]:
        mapping = frontmatter.get("final_speaker_mapping") or {}
        return sorted({
            v["person_id"] for v in mapping.values()
            if isinstance(v, dict) and v.get("person_id")
        })

    # ===== Form Generation =====

    @staticmethod
    def _context_snippet(transcript: str, name: str, width: int = 90) -> str:
        """The text around the first mention of `name` in the conversation, on one line.

        Callouts left by earlier stages (speaker list, summaries) are skipped:
        they name people too, but say nothing about who is meant.
        """
        transcript = re.sub(
            r"<!--\s*(summary|form):([\w-]+):start\s*-->.*?<!--\s*\1:\2:end\s*-->",
            "", transcript, flags=re.S,
        )
        match = re.search(r"\b" + re.escape(name) + r"\b", transcript)
        if not match:
            return ""
        start = max(0, match.start() - width)
        end = min(len(transcript), match.end() + width)
        snippet = " ".join(transcript[start:end].split())
        return f"…{snippet}…"

    @staticmethod
    def _percent(confidence: float) -> int:
        # Rounded down, so nothing below the auto-accept threshold reads as above it
        return int(confidence * 100)

    def _generate_form(self, entities: List[Dict[str, Any]], transcript: str = "") -> str:
        """Generate the entity resolution form.
        
        Entities needing review come first, each with its context and the
        alternatives considered; auto-accepted ones follow with only a link field.
        Field indices follow the order of `entities`, which is also the order
        stored in the detected_entities frontmatter.

        Args:
            entities: dicts with 'detected_name', 'suggested_link', 'entity_type',
                and optionally 'confidence', 'alternatives', 'auto_accepted'
        """
        review = [(i, e) for i, e in enumerate(entities) if not e.get('auto_accepted')]
        accepted = [(i, e) for i, e in enumerate(entities) if e.get('auto_accepted')]

        lines = [
            self.FORM_START,
            "",
            f"> [!info] Entity Resolution — {len(review)} to review"
            + (f", {len(accepted)} auto-accepted" if accepted else ""),
            "> Fix the link if wrong, or clear it to leave the name unlinked.",
            "",
        ]
        
        for i, entity in review:
            detected = entity['detected_name']
            suggested = entity.get('suggested_link', '')
            # Blank rather than a placeholder: validation then tells the user the
            # field is required instead of rejecting an invented type.
            entity_type = entity.get('entity_type', '')
            confidence = entity.get('confidence')
            heading = f"## {detected}"
            if confidence is not None:
                heading += f" — {self._percent(confidence)}% sure"
            lines.append(heading)
            snippet = self._context_snippet(transcript, detected) if transcript else ""
            if snippet:
                lines.append(f"> {snippet}")
            if entity.get('alternatives'):
                lines.append(f"Alternatives: {' · '.join(entity['alternatives'])}")
            lines.extend([
                f"**Link:** <!-- input:entity_{i}_link -->{suggested}",
                f"**Type:** <!-- input:entity_{i}_type -->{entity_type}",
                "",
                "---",
                "",
            ])

        if accepted:
            lines.extend(["## Auto-accepted — edit only if wrong", ""])
            for i, entity in accepted:
                confidence = entity.get('confidence')
                pct = f" ({self._percent(confidence)}%)" if confidence is not None else ""
                lines.append(
                    f"- {entity['detected_name']}{pct}: "
                    f"<!-- input:entity_{i}_link -->{entity.get('suggested_link', '')}"
                )
            lines.extend(["", "---", ""])
        
        lines.extend([
            "- [ ] Finished <!-- input:finished -->",
            "",
            self.FORM_END,
            "",
        ])
        
        return '\n'.join(lines)
    
    def _parse_form(self, content: str) -> Optional[Dict[str, Any]]:
        """Parse the entity resolution form from content.
        
        Returns:
            Dict with:
                - 'entities': List of dicts with 'link', 'type' (type is None
                  for auto-accepted entries, which have no type field)
                - 'finished': Boolean
            Returns None if form not found.
        """
        start_idx = content.find(self.FORM_START)
        end_idx = content.find(self.FORM_END)
        
        if start_idx == -1 or end_idx == -1:
            return None
        
        section = content[start_idx:end_idx]
        
        result = {
            'entities': [],
            'finished': False,
        }
        
        # Parse entity inputs
        link_pattern = r'<!-- input:entity_(\d+)_link -->([^\n]*)'
        type_pattern = r'<!-- input:entity_(\d+)_type -->([^\n]*)'
        
        links = {}
        types = {}
        
        for match in re.finditer(link_pattern, section):
            idx = int(match.group(1))
            links[idx] = match.group(2).strip()
        
        for match in re.finditer(type_pattern, section):
            idx = int(match.group(1))
            types[idx] = match.group(2).strip()
        
        # Combine into entity list
        max_idx = max(list(links.keys()) + list(types.keys()) + [-1])
        for i in range(max_idx + 1):
            result['entities'].append({
                'link': links.get(i, ''),
                'type': types.get(i),
            })
        
        # Parse finished checkbox
        finished_pattern = r'\[(x|X)\]\s+Finished\s+<!-- input:finished -->'
        result['finished'] = bool(re.search(finished_pattern, section))
        
        return result
    
    def _remove_form_section(self, content: str) -> str:
        """Remove form or summary section from content."""
        for start_marker, end_marker in [
            (self.FORM_START, self.FORM_END),
            (self.SUMMARY_START, self.SUMMARY_END),
        ]:
            start_idx = content.find(start_marker)
            end_idx = content.find(end_marker)
            
            if start_idx != -1 and end_idx != -1:
                end_line_idx = content.find('\n', end_idx)
                if end_line_idx == -1:
                    end_line_idx = len(content)
                else:
                    end_line_idx += 1
                
                return content[:start_idx] + content[end_line_idx:]
        
        return content
    
    def _generate_summary(self, entities: List[Dict[str, str]], reviewed: Optional[int] = None) -> str:
        """Generate completion summary.

        Args:
            reviewed: how many entities the user was asked to review, if known
        """
        resolved_count = sum(1 for e in entities if e.get('resolved_link'))
        if reviewed is None:
            detail = ""
        elif reviewed == 0:
            detail = ", all auto-accepted"
        else:
            detail = f", {reviewed} reviewed"
        
        lines = [
            self.SUMMARY_START,
            "",
            f"> [!success] Entity resolution complete ({resolved_count} entities resolved{detail})",
            "",
        ]
        
        # Group by type — only the supported types get a section
        by_type = {"people": [], "org": []}
        for e in entities:
            if not e.get('resolved_link'):
                continue

            entity_type = e.get('entity_type')
            if entity_type not in by_type:
                logger.warning(
                    "Omitting %r from summary: unsupported type %r",
                    e.get('detected_name'), entity_type
                )
                continue

            if e['resolved_link'] not in by_type[entity_type]:
                by_type[entity_type].append(e['resolved_link'])
        
        for type_name, links in by_type.items():
            if links:
                lines.append(f"**{type_name.title()}:** {', '.join(links)}")
                lines.append("")
        
        lines.extend([
            self.SUMMARY_END,
            "",
        ])
        
        return '\n'.join(lines)
    
    # ===== Resolution =====

    MAX_PROMPT_CANDIDATES = 5
    # Attendees come from the speaker identification the user has already
    # confirmed, so a close spelling of one of them is all but certain. The
    # model cannot see that for someone at their first meeting, whom it
    # treats as an unknown new name.
    ATTENDEE_CONFIDENCE = 0.99
    ATTENDEE_MIN_SIMILARITY = 0.8
    RESOLUTION_ATTEMPTS = 2

    def _format_mentions(
        self, registry: EntityRegistry, names: List[str], evidences: List[AliasEvidence]
    ) -> str:
        """Describe each mention and its candidates for the resolution prompt."""
        blocks = []
        for i, (name, ev) in enumerate(zip(names, evidences)):
            past = [f"{c.link} x{c.alias_count}" for c in ev.candidates if c.alias_count]
            if ev.rejected:
                past.append(f"left unlinked x{ev.rejected}")
            history = ", ".join(past) if past else "never seen"
            lines = [f'[{i}] "{name}" (history: {history})']
            candidates = ev.candidates[:self.MAX_PROMPT_CANDIDATES]
            if candidates:
                lines.append("    candidates:")
                for c in candidates:
                    tag = " [attendee]" if c.is_attendee else ""
                    lines.append(f"    - {registry.profile(c.link)}{tag}")
            else:
                lines.append("    candidates: none")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    @staticmethod
    def _parse_json(text: str) -> Dict:
        text = (text or "").strip()
        if "```" in text:
            text = text.split("```json")[-1] if "```json" in text else text.split("```")[1]
            text = text.split("```")[0]
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start:end + 1])

    async def _resolve_mentions(
        self,
        registry: EntityRegistry,
        transcript: str,
        attendees: List[str],
        mentions: List[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        """Pick a link for each detected mention and score how likely it is right.

        Args:
            mentions: dicts with 'detected_name' and optionally 'entity_type'

        Returns one dict per mention with 'detected_name', 'resolved_link'
        ('' for no link), 'entity_type', 'confidence', 'llm_probability',
        'alternatives' and 'description'.
        """
        names = [m['detected_name'] for m in mentions]
        evidences = [registry.evidence(n, attendees) for n in names]

        prompt = get_prompt("resolve_entities")
        for placeholder, value in [
            ("{user_name}", USER_NAME),
            ("{attendees}", "\n".join(f"- {registry.profile(a)}" for a in attendees) or "Unknown"),
            ("{mentions}", self._format_mentions(registry, names, evidences)),
            ("{transcript}", transcript),
        ]:
            prompt = prompt.replace(placeholder, value)

        message = Message(role="user", content=[MessageContent(type="text", text=prompt)])
        # The answer is long JSON, which is occasionally malformed: ask once more
        for attempt in range(self.RESOLUTION_ATTEMPTS):
            response = await asyncio.to_thread(self.ai_model.message, message, max_tokens=16000)
            if response.error and "MAX_TOKENS" not in str(response.error):
                raise EntityResolutionError(f"AI error in entity resolution: {response.error}")
            try:
                answers = {
                    int(item.get("id", -1)): item
                    for item in self._parse_json(response.content).get("resolutions", [])
                }
                break
            except (ValueError, TypeError, AttributeError) as e:
                if attempt + 1 == self.RESOLUTION_ATTEMPTS:
                    raise EntityResolutionError(f"Could not parse entity resolution response: {e}")
                logger.warning("Unparseable entity resolution response, retrying: %s", e)

        results, dropped = [], []
        for i, (mention, ev) in enumerate(zip(mentions, evidences)):
            answer = answers.get(i, {})
            choice = str(answer.get("choice") or "").strip()
            if choice.upper() == "NEW":
                link = self._as_wikilink(answer.get("new_link", "")) or self._as_wikilink(mention['detected_name'])
            elif choice.upper() == "NONE" or not choice:
                link = ""
            else:
                link = self._as_wikilink(choice)
            try:
                llm_probability = float(answer.get("probability", 0.5))
            except (TypeError, ValueError):
                llm_probability = 0.5
            if i not in answers:
                # The AI skipped it: fall back to the history, with low confidence
                top = ev.history_top()
                link, llm_probability = (top.link if top else ""), 0.3

            entity = registry.lookup(link) if link else None
            entity_type = answer.get("entity_type") or (entity or {}).get("type") or mention.get("entity_type") or ""
            if entity_type not in self.ENTITY_TYPES:
                entity_type = ""

            if self._is_untracked_org(registry, link, entity_type):
                # Not an organisation the user wants linked: drop it altogether
                dropped.append(mention['detected_name'])
                continue

            feats = entity_confidence.features(link, ev, llm_probability)
            confidence = entity_confidence.probability(feats)
            if self._names_attendee(mention['detected_name'], link, attendees):
                confidence = max(confidence, self.ATTENDEE_CONFIDENCE)
            results.append({
                'detected_name': mention['detected_name'],
                'resolved_link': link,
                'entity_type': entity_type,
                'confidence': round(confidence, 3),
                'llm_probability': llm_probability,
                'alternatives': [c.link for c in ev.candidates if entity_key(c.link) != entity_key(link)][:3],
                'description': str(answer.get("description") or "").strip(),
                'features': feats,
            })
        if dropped:
            logger.info("Ignored %d untracked organisations: %s", len(dropped), ", ".join(dropped))
        return results

    @staticmethod
    def _is_untracked_org(registry: EntityRegistry, link: str, entity_type: str) -> bool:
        if registry.tracked_orgs is None:
            return False
        key = entity_key(link)
        entity = registry.entities.get(key) if key else None
        is_org = entity_type == "org" or bool(entity and entity["type"] == "org")
        return is_org and key not in registry.tracked_orgs

    @classmethod
    def _names_attendee(cls, detected_name: str, link: str, attendees: List[str]) -> bool:
        """True if `link` is an attendee and `detected_name` is close to their name."""
        key = entity_key(link)
        if not key or key not in {entity_key(a) for a in attendees}:
            return False
        name = normalize_alias(detected_name)
        if not name:
            return False
        if set(name.split()) <= set(key.split()):
            return True
        return difflib.SequenceMatcher(None, name, key).ratio() >= cls.ATTENDEE_MIN_SIMILARITY

    @staticmethod
    def _as_wikilink(text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        if text.startswith("[[") and text.endswith("]]"):
            return text
        return f"[[{text.strip('[]')}]]"

    # ===== Main Processing =====
    
    async def process_file(self, filename: str) -> None:
        """Main entry point for processing a file."""
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)
        
        if not frontmatter:
            raise EntityResolutionError(f"No frontmatter found in: {filename}")
        
        transcript = read_text_from_content(content)
        
        if frontmatter.get('entity_resolution_pending'):
            await self._substage3_process_results(filename, frontmatter, content)
            return

        registry = self._load_registry()
        mentions = await self._substage1_detect_entities(filename, frontmatter, transcript, registry)
        if not mentions:
            logger.info("No entities detected in: %s", filename)
            return

        entities = await self._resolve_mentions(
            registry, transcript, self._attendees(frontmatter), mentions
        )
        for entity in entities:
            entity['suggested_link'] = entity['resolved_link']
            # A missing type would fail validation, so it always needs review
            entity['auto_accepted'] = (
                entity['confidence'] >= self.AUTO_ACCEPT_THRESHOLD and bool(entity['entity_type'])
            )
        # Least certain first: that is where the user's attention is needed
        entities.sort(key=lambda e: (e['auto_accepted'], e['confidence']))

        to_review = sum(1 for e in entities if not e['auto_accepted'])
        logger.info(
            "Resolved %d entities in %s: %d auto-accepted, %d to review",
            len(entities), filename, len(entities) - to_review, to_review
        )

        if to_review == 0:
            await self._finalize(filename, frontmatter, transcript, entities, reviewed=0)
            return

        await self._substage2_create_form(filename, frontmatter, transcript, entities)
        raise ResultsNotReadyError(f"Form created, waiting for user input: {filename}")
    
    async def _substage1_detect_entities(
        self, filename: str, frontmatter: Dict, transcript: str,
        registry: Optional[EntityRegistry] = None,
    ) -> List[Dict[str, str]]:
        """Substage 1: Use AI to detect named entities.

        Returns dicts with 'detected_name' and 'entity_type'.
        """
        logger.info("Detecting entities in: %s", filename)
        
        registry = registry or self._load_registry()
        references_text = registry.reference_text() or "No existing references."
        
        # Prepare prompt
        prompt_template = get_prompt("detect_entities")
        prompt = prompt_template.replace("{entity_references}", references_text)
        prompt = prompt.replace("{transcript}", transcript)
        
        message = Message(
            role="user",
            content=[MessageContent(type="text", text=prompt)]
        )
        
        # Call AI
        try:

            unique_entities = {}
            
            response = await asyncio.to_thread(self.tiny_ai_model.message, message)
            
            if response.error:
                if "MAX_TOKENS" in str(response.error):
                    logger.warning("AI hit token limit (MAX_TOKENS). Attempting to parse available content.")
                else:
                    logger.error("AI error in entity detection: %s", response.error)
                    return []
            
            if not response.content:
                logger.warning("Empty response from AI for entity detection")
                return []
            
            try:
                data = self._parse_json(response.content)
                entities = data.get("entities", [])
                
                for item in entities:
                    detected = item.get("detected_name")
                    if not detected:
                        continue

                    # The prompt asks for people/org only. Keep the entity if the
                    # AI says otherwise, but leave the type blank so the form asks
                    # the user for it rather than pre-filling something invalid.
                    entity_type = item.get("entity_type", "")
                    if entity_type not in self.ENTITY_TYPES:
                        logger.warning(
                            "AI returned unsupported entity_type %r for %r; leaving blank",
                            entity_type, detected
                        )
                        entity_type = ""

                    unique_entities[detected] = {
                        "detected_name": detected,
                        "entity_type": entity_type
                    }
                        
            except (ValueError, TypeError) as e:
                logger.error("Failed to parse JSON response: %s. Content: %s", e, response.content)
                raise EntityResolutionError(f"Failed to parse AI response as JSON: {e}")
                
            return list(unique_entities.values())
            
        except Exception as e:
            logger.error("Error in entity detection AI call: %s", e)
            return []
    
    # Kept per entity in frontmatter while the form is open
    _PENDING_FIELDS = ('detected_name', 'entity_type', 'suggested_link', 'confidence', 'description')

    async def _substage2_create_form(
        self, filename: str, frontmatter: Dict, transcript: str, entities: List[Dict]
    ) -> None:
        """Substage 2: Create form for user validation."""
        logger.info("Creating entity resolution form for: %s", filename)
        
        # Generate form
        form_content = self._generate_form(entities, transcript)
        
        # Update frontmatter
        frontmatter['entity_resolution_pending'] = True
        
        # Store the AI's answers; form fields are matched to them by index
        frontmatter['detected_entities'] = [
            {k: e[k] for k in self._PENDING_FIELDS if k in e}
            for e in entities
        ]
        
        # Save file
        full_content = frontmatter_to_text(frontmatter) + form_content + transcript
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(self.input_dir / filename, None)
        
        logger.info("Created entity resolution form for: %s", filename)
    
    async def _substage3_process_results(
        self, filename: str, frontmatter: Dict, content: str
    ) -> None:
        """Substage 3: Process user input from form."""
        logger.info("Processing entity resolution form for: %s", filename)
        
        form_data = self._parse_form(content)
        
        if form_data is None:
            raise EntityResolutionError(f"Could not parse form in: {filename}")
        
        if not form_data['finished']:
            raise ResultsNotReadyError(f"User has not checked 'Finished' in: {filename}")
        
        detected_entities = frontmatter.get('detected_entities', [])

        # Validate type fields. Auto-accepted entries have no type field: they keep
        # the type that was detected.
        errors = []
        for i, entity in enumerate(form_data['entities']):
            if entity['type'] is None:
                entity['type'] = detected_entities[i].get('entity_type', '') if i < len(detected_entities) else ''
            error = validate_choice_field(
                entity['type'], 
                self.ENTITY_TYPES, 
                f"Entity {i+1} Type"
            )
            if error:
                errors.append(error)
            
            # Validate link if provided
            if entity['link']:
                link_error = validate_wikilink_field(entity['link'], f"Entity {i+1} Link")
                if link_error:
                    errors.append(link_error)
        
        if errors:
            logger.warning("Validation errors in %s: %s", filename, [e.message for e in errors])
            
            updated_content = insert_error_in_section(content, errors, self.FORM_START)
            
            async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
                await f.write(updated_content)
            os.utime(self.input_dir / filename, None)
            
            raise ResultsNotReadyError(f"Validation errors in: {filename}")
        
        # Build resolved entities list
        resolved_entities = []
        reviewed = 0
        for i, entity in enumerate(form_data['entities']):
            if i < len(detected_entities):
                detected = detected_entities[i]
                if 'confidence' in detected and detected['confidence'] < self.AUTO_ACCEPT_THRESHOLD:
                    reviewed += 1
                resolved_entities.append({
                    **{k: v for k, v in detected.items() if k in self._PENDING_FIELDS},
                    'resolved_link': entity['link'],
                    'entity_type': entity['type'],
                })
        
        transcript = read_text_from_content(self._remove_form_section(content))
        del frontmatter['entity_resolution_pending']
        frontmatter.pop('detected_entities', None)
        await self._finalize(filename, frontmatter, transcript, resolved_entities, reviewed=reviewed)

    async def _finalize(
        self, filename: str, frontmatter: Dict, transcript: str,
        entities: List[Dict[str, Any]], reviewed: Optional[int] = None,
    ) -> None:
        """Link the resolved entities in the transcript and learn from the outcome."""
        resolved_entities = [
            {
                'detected_name': e['detected_name'],
                'entity_type': e.get('entity_type', ''),
                'resolved_link': e.get('resolved_link', ''),
                # What the AI proposed and how sure it was, to check calibration later
                **({'suggested_link': e['suggested_link']} if 'suggested_link' in e else {}),
                **({'confidence': e['confidence']} if 'confidence' in e else {}),
            }
            for e in entities
        ]

        # Prepare replacements mapping
        replacements = {}
        for entity in resolved_entities:
            if entity['resolved_link'] and entity['detected_name']:
                replacements[entity['detected_name']] = entity['resolved_link']
        
        if replacements:
            # Sort keys by length (descending) to match longest triggers first
            sorted_keys = sorted(replacements.keys(), key=len, reverse=True)
            
            # Create a regex that matches either:
            # 1. An existing wikilink (to ignore it)
            # 2. One of our target terms (to replace it)
            # We use word boundaries \b for the target terms to avoid partial matches inside words
            pattern_string = r"(\[\[.*?\]\])|(\b(?:" + "|".join(re.escape(k) for k in sorted_keys) + r")\b)"
            
            def replace_callback(match):
                full_match = match.group(0)
                # If group 1 match (existing link), return as is
                if match.group(1):
                    return full_match
                # If group 2 match (target term), replace it
                detected_text = match.group(2)
                if detected_text:
                    return replacements.get(detected_text, full_match)
                return full_match

            transcript = re.sub(pattern_string, replace_callback, transcript)
        
        # Teach the registry and update the Entity Reference file
        registry = self._load_registry()
        descriptions = {
            e['resolved_link']: e['description']
            for e in entities if e.get('resolved_link') and e.get('description')
        }
        if registry.record_meeting(
            filename, str(frontmatter.get('date', '')), resolved_entities,
            self._attendees(frontmatter), descriptions,
        ):
            registry.save()
        self._update_entity_reference(resolved_entities)
        
        # Update frontmatter
        frontmatter['resolved_entities'] = resolved_entities
        
        # Generate summary
        summary = self._generate_summary(resolved_entities, reviewed=reviewed)
        
        # Save file
        full_content = frontmatter_to_text(frontmatter) + summary + transcript
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(self.input_dir / filename, None)
        
        logger.info("Completed entity resolution for: %s", filename)
    
    async def reset(self, filename: str) -> None:
        """Reset entity resolution for a file."""
        logger.info(f"Resetting entity resolution for: {filename}")
        
        file_path = self.input_dir / filename
        if not file_path.exists():
            logger.error(f"File not found: {filename}")
            return
        
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)
        
        if not frontmatter:
            return
        
        # Remove form/summary section
        content_without_form = self._remove_form_section(content)
        transcript = read_text_from_content(content_without_form)
        
        # TODO: Revert entity replacements if resolved_entities exists
        
        # Clean frontmatter
        keys_to_remove = [
            'entity_resolution_pending',
            'detected_entities', 
            'resolved_entities',
        ]
        cleaned_frontmatter = {k: v for k, v in frontmatter.items() if k not in keys_to_remove}
        
        # Remove stage from processing_stages
        if self.stage_name in cleaned_frontmatter.get('processing_stages', []):
            cleaned_frontmatter['processing_stages'].remove(self.stage_name)
        
        # Save
        full_content = frontmatter_to_text(cleaned_frontmatter) + transcript
        async with aiofiles.open(file_path, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(file_path, None)
        
        logger.info(f"Reset complete for: {filename}")
