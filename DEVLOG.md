# NoteFlow Developer Log

A running log of technical discoveries, design decisions, and implementation notes.

---

## 2025-12-27: Entity Resolution Implementation

### Problem
Needed to detect and resolve named entities (people, organizations) in transcripts to Obsidian wikilinks.

### Key Learnings

**1. Gemini 3.0 Flash "Thought Tokens"**
- Gemini 3 Flash uses hidden "thought tokens" for internal reasoning that count toward output limits
- Even short JSON outputs can hit `MAX_TOKENS` if the model is "thinking" heavily
- Solution: Either use a very high `max_tokens` (65k) or switch to a model without this behavior (e.g., Opus)

**2. Safe Text Replacement for Wikilinks**
- Naive iterative `re.sub` can corrupt already-replaced text (e.g., "Irina" inside `[[Irina Tavera]]`)
- Solution: Single-pass regex with callback:
  ```python
  # Pattern matches existing wikilinks (to skip) OR target terms (to replace)
  pattern = r"(\[\[.*?\]\])|(\b(?:Term1|Term2)\b)"
  
  def replace_callback(match):
      if match.group(1):  # Existing wikilink - skip
          return match.group(0)
      return replacements.get(match.group(2), match.group(0))
  ```
- Also sort replacement keys by length (descending) to handle substrings correctly

**3. Multi-Stage Processor Pattern**
- For human-in-the-loop workflows, use frontmatter flags (e.g., `entity_resolution_pending: true`)
- Raise `ResultsNotReadyError` after form creation to prevent base class from marking stage complete
- On next run, check the flag and process user input if "Finished" checkbox is checked

**4. AI Model Selection**
- Flash models: Fast, cheap, good for simple extraction
- Opus/Claude: Better reasoning, more reliable JSON, worth the cost for complex tasks
- Final choice for entity resolution: `opus4.5`

### Files Created
- `processors/notes/entity_resolver.py` - Main processor
- `prompts/detect_entities.md` - AI prompt for entity detection
- `tests/test_entity_resolver.py` - Unit tests

---

## Template for New Entries

```markdown
## YYYY-MM-DD: Feature/Fix Title

### Problem
Brief description of what needed to be solved.

### Key Learnings
1. **Topic**: What was learned
2. **Topic**: What was learned

### Files Changed
- `path/to/file.py` - Description
```
