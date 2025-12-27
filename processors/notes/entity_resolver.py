"""
Entity Resolution Processor

Detects named entities in transcripts and resolves them to wikilinks,
using AI detection and user validation via inline Obsidian forms.
"""

from pathlib import Path
from typing import Dict, Any, Optional, List
import aiofiles
import os
import re
import asyncio

from .base import NoteProcessor
from .speaker_identifier import SpeakerIdentifier
from ..common.frontmatter import parse_frontmatter_from_content, frontmatter_to_text, read_text_from_content
from ..common.obsidian_form import validate_wikilink_field, validate_choice_field, insert_error_in_section
from ai_core import AI
from ai_core.types import Message, MessageContent
from config.logging_config import setup_logger
from config.paths import PATHS
from config.user_config import TARGET_DISCORD_USER_ID
from integrations.discord import DiscordIOCore
from prompts.prompts import get_prompt

logger = setup_logger(__name__)


class EntityResolutionError(Exception):
    """Exception raised when entity resolution encounters an error."""
    pass


class ResultsNotReadyError(Exception):
    """Raised when user input is not yet available."""
    pass


class EntityResolver(NoteProcessor):
    """Resolves named entities in transcripts to wikilinks via AI + user validation.
    
    This processor implements a multi-substage workflow:
    
    **Substage 1: AI Entity Detection**
        - Reads transcript and Entity Reference file
        - AI detects named entities (people, organisations, other)
        - Looks up existing mappings in reference file
    
    **Substage 2: Form Generation**
        - Creates inline form with one section per entity
        - Prepopulates Link and Type fields with AI suggestions
        - Sends Discord notification
    
    **Substage 3: Processing**
        - Validates input (type must be: people, org, other)
        - Replaces entity occurrences in transcript
        - Updates Entity Reference file with new mappings
    
    **Frontmatter Fields**:
        - entity_resolution_pending: True while waiting for user input
        - resolved_entities: Dict of entity mappings after completion
    """
    stage_name = "entities_resolved"
    required_stage = SpeakerIdentifier.stage_name
    
    # Entity types
    ENTITY_TYPES = {"people", "org", "other"}
    
    # Form markers
    FORM_START = "<!-- form:entity_resolution:start -->"
    FORM_END = "<!-- form:entity_resolution:end -->"
    SUMMARY_START = "<!-- summary:entity_resolution:start -->"
    SUMMARY_END = "<!-- summary:entity_resolution:end -->"
    
    def __init__(self, input_dir: Path, discord_io: DiscordIOCore):
        super().__init__(input_dir)
        super().__init__(input_dir)
        self.discord_io = discord_io
        self.entity_reference_path = PATHS.vault_path / "Entity Reference.md"
        # Use a more powerful model for entity resolution as per user request
        self.entity_model = AI("opus4.5")
    
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
        
        # Only process meeting category files
        if frontmatter.get('category') != 'meeting':
            return False
            
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

## Other Aliases
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
                "other": {...}
            }
        """
        self._ensure_entity_reference_exists()
        
        content = self.entity_reference_path.read_text(encoding='utf-8')
        
        result = {"people": {}, "org": {}, "other": {}}
        current_type = None
        
        for line in content.split('\n'):
            line = line.strip()
            
            # Detect section headers
            if "## People" in line:
                current_type = "people"
            elif "## Organisation" in line:
                current_type = "org"
            elif "## Other" in line:
                current_type = "other"
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
            
            entity_type = entity.get('entity_type', 'other')
            detected_name = entity['detected_name'].lower()
            resolved_link = entity['resolved_link']
            
            # Only add if not already present
            if detected_name not in reference.get(entity_type, {}):
                if entity_type not in reference:
                    reference[entity_type] = {}
                reference[entity_type][detected_name] = resolved_link
        
        # Rebuild file content
        lines = ["# Entity Resolution Reference", ""]
        
        for section_name, section_type in [("People Aliases", "people"), 
                                            ("Organisation Aliases", "org"),
                                            ("Other Aliases", "other")]:
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
    
    # ===== Form Generation =====
    
    def _generate_form(self, entities: List[Dict[str, str]]) -> str:
        """Generate the entity resolution form.
        
        Args:
            entities: List of dicts with 'detected_name', 'suggested_link', 'entity_type'
        """
        lines = [
            self.FORM_START,
            "",
            "> [!info] Entity Resolution — Review and confirm entity mappings",
            "",
        ]
        
        for i, entity in enumerate(entities):
            detected = entity['detected_name']
            suggested = entity.get('suggested_link', '')
            entity_type = entity.get('entity_type', 'other')
            
            lines.extend([
                f"## {detected}",
                f"**Link:** <!-- input:entity_{i}_link -->{suggested}",
                f"**Type:** <!-- input:entity_{i}_type -->{entity_type}",
                "",
                "---",
                "",
            ])
        
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
                - 'entities': List of dicts with 'link', 'type'
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
                'type': types.get(i, 'other'),
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
    
    def _generate_summary(self, entities: List[Dict[str, str]]) -> str:
        """Generate completion summary."""
        resolved_count = sum(1 for e in entities if e.get('resolved_link'))
        
        lines = [
            self.SUMMARY_START,
            "",
            f"> [!success] Entity resolution complete ({resolved_count} entities resolved)",
            "",
        ]
        
        # Group by type
        by_type = {"people": [], "org": [], "other": []}
        for e in entities:
            if e.get('resolved_link'):
                by_type[e.get('entity_type', 'other')].append(e['resolved_link'])
        
        for type_name, links in by_type.items():
            if links:
                lines.append(f"**{type_name.title()}:** {', '.join(links)}")
                lines.append("")
        
        lines.extend([
            self.SUMMARY_END,
            "",
        ])
        
        return '\n'.join(lines)
    
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
        else:
            entities = await self._substage1_detect_entities(filename, frontmatter, transcript)
            if entities:
                await self._substage2_create_form(filename, frontmatter, transcript, entities)
                raise ResultsNotReadyError(f"Form created, waiting for user input: {filename}")
            else:
                logger.info("No entities detected in: %s", filename)
    
    async def _substage1_detect_entities(
        self, filename: str, frontmatter: Dict, transcript: str
    ) -> List[Dict[str, str]]:
        """Substage 1: Use AI to detect named entities."""
        logger.info("Detecting entities in: %s", filename)
        
        # Load existing reference
        reference = self._parse_entity_reference()
        
        # Format references for prompt
        ref_sections = []
        for type_key, type_name in [("people", "People"), ("org", "Organisations"), ("other", "Other")]:
            items = reference.get(type_key, {})
            if items:
                ref_sections.append(f"## {type_name}")
                for name, link in sorted(items.items()):
                    ref_sections.append(f"- {name} -> {link}")
                ref_sections.append("")
        
        references_text = "\n".join(ref_sections) if ref_sections else "No existing references."
        
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
            
            response = await asyncio.to_thread(self.entity_model.message, message)
            
            if response.error:
                if "MAX_TOKENS" in str(response.error):
                    logger.warning("AI hit token limit (MAX_TOKENS). Attempting to parse available content.")
                else:
                    logger.error("AI error in entity detection: %s", response.error)
                    return []
            
            if not response.content:
                logger.warning("Empty response from AI for entity detection")
                return []
            
            # Parse JSON
            content = response.content.strip()
            # Remove markdown code blocks if present
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            import json
            try:
                data = json.loads(content)
                entities = data.get("entities", [])
                
                for item in entities:
                    detected = item.get("detected_name")
                    if detected:
                        unique_entities[detected] = {
                            "detected_name": detected,
                            "suggested_link": item.get("suggested_link", ""),
                            "entity_type": item.get("entity_type", "other")
                        }
                        
            except json.JSONDecodeError as e:
                logger.error("Failed to parse JSON response: %s. Content: %s", e, content)
                return []
                
            return list(unique_entities.values())
            
        except Exception as e:
            logger.error("Error in entity detection AI call: %s", e)
            return []
    
    async def _substage2_create_form(
        self, filename: str, frontmatter: Dict, transcript: str, entities: List[Dict]
    ) -> None:
        """Substage 2: Create form for user validation."""
        logger.info("Creating entity resolution form for: %s", filename)
        
        # Generate form
        form_content = self._generate_form(entities)
        
        # Update frontmatter
        frontmatter['entity_resolution_pending'] = True
        
        # Store detected entities for later reference
        frontmatter['detected_entities'] = [
            {'detected_name': e['detected_name'], 'entity_type': e.get('entity_type', 'other')}
            for e in entities
        ]
        
        # Save file
        full_content = frontmatter_to_text(frontmatter) + form_content + transcript
        async with aiofiles.open(self.input_dir / filename, "w", encoding='utf-8') as f:
            await f.write(full_content)
        os.utime(self.input_dir / filename, None)
        
        # Send Discord notification
        try:
            logger.info("Sending Discord notification for: %s", filename)
            dm_text = (
                f"📝 **Entity Resolution Required**\n"
                f"File: `{filename}`\n"
                f"Entities detected: {len(entities)}\n"
                f"Please review and confirm entity mappings in Obsidian."
            )
            success = await self.discord_io.send_dm(TARGET_DISCORD_USER_ID, dm_text)
            
            if not success:
                logger.warning("Failed to send Discord DM for: %s", filename)
            else:
                logger.info("Successfully sent Discord notification for: %s", filename)
        except Exception as e:
            logger.warning("Error sending Discord notification for %s: %s", filename, e)
    
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
        
        # Validate type fields
        errors = []
        for i, entity in enumerate(form_data['entities']):
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
            
            try:
                error_summary = "; ".join(e.message for e in errors)
                dm_text = (
                    f"⚠️ **Entity Resolution Validation Errors**\n"
                    f"File: `{filename}`\n"
                    f"Errors: {error_summary}\n"
                    f"Please fix and check Finished again."
                )
                await self.discord_io.send_dm(TARGET_DISCORD_USER_ID, dm_text)
            except Exception as e:
                logger.warning("Failed to send Discord notification: %s", e)
            
            raise ResultsNotReadyError(f"Validation errors in: {filename}")
        
        # Build resolved entities list
        detected_entities = frontmatter.get('detected_entities', [])
        resolved_entities = []
        
        for i, entity in enumerate(form_data['entities']):
            if i < len(detected_entities):
                resolved_entities.append({
                    'detected_name': detected_entities[i]['detected_name'],
                    'resolved_link': entity['link'],
                    'entity_type': entity['type'],
                })
        
        # Get transcript and replace entities
        transcript = read_text_from_content(self._remove_form_section(content))
        
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
        
        # Update Entity Reference file
        self._update_entity_reference(resolved_entities)
        
        # Update frontmatter
        frontmatter['resolved_entities'] = resolved_entities
        del frontmatter['entity_resolution_pending']
        if 'detected_entities' in frontmatter:
            del frontmatter['detected_entities']
        
        # Generate summary
        summary = self._generate_summary(resolved_entities)
        
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
