# NoteFlow Developer Log

A running log of technical discoveries, design decisions, and implementation notes.

---

## 2025-12-28: Email Summary Generator & Entity Resolution for Emails

### Problem
Email digests were being created but had no structured summaries or monthly index like meetings. Entity resolution also only worked for meeting transcripts, not email digests.

### Solution

**1. EmailSummaryGenerator Processor**
New processor that auto-generates summaries (no user validation form) and maintains monthly index:
- AI-powered summarization proportional to email volume
- Extracts participants (From/To wikilinks) and mentioned entities
- Creates monthly index files (`YYYY-MM Email Index.md`)
- Uses H2 headings only (no H1) for cleaner integration

**2. EntityResolver for Emails**
Extended EntityResolver to process email digests:
- Added `category: 'email'` acceptance in `should_process()`
- Set instance-level `required_stage = "email_digest_created"` (vs class default `speaker_identified`)
- EmailDigestProcessor now adds `email_digest_created` to processing_stages
- Base class now checks instance attributes before class attributes for `required_stage`

**3. InboxGenerator Multi-Directory Support**
Extended to scan multiple directories:
- New `scan_dirs` parameter (list of paths)
- Now scans both Transcriptions and Email Digests for pending forms

### Key Learnings

**1. Instance vs Class Attributes for Flexibility**
- Changed base class to check `getattr(self, 'required_stage', None)` before `self.__class__.required_stage`
- Enables per-instance configuration without subclassing

**2. Index Parsing with Mixed H1 Headers**
- Original regex `^# ` split on ALL H1 headers, breaking when AI generated `# Email Digest` in summaries
- Fixed: Split only on date-pattern headers `^# (\d{4}-\d{2}-\d{2})\s*-\s*`
- Captures groups allow cleaner parsing of date and title

**3. Scheduler Job ID Conflicts**
- Two EntityResolver instances had same `stage_name`, causing `ConflictingIdError`
- Solution: Use dict key (e.g., `_entity_resolver_emails`) for underscore-prefixed processors

### Files Created
- `processors/notes/email_summary_generator.py` - Main processor (396 lines)
- `prompts/email_summary.md` - AI prompt with H2-only instruction
- `tests/test_email_summary_generator.py` - 17 unit tests

### Files Modified
- `processors/notes/entity_resolver.py` - Accept email category
- `processors/notes/email_digest.py` - Add `email_digest_created` stage
- `processors/notes/inbox_generator.py` - Multi-directory support
- `processors/notes/base.py` - Instance attribute check for required_stage
- `main.py` - Add EmailSummaryGenerator, configure email EntityResolver
- `tests/test_email_digest.py` - Update for tuple returns

---

## 2025-12-28: Email Digest Processor

### Problem
Need to capture important daily emails for later processing (entity resolution, interaction logging) without flooding digests with marketing/automated content.

### Solution
New `EmailDigestProcessor` that:
1. Fetches all emails (sent + received) since last run via Gmail API
2. Pre-filters: skips Gmail categories (Promotions, Social, Updates, Forums) + automated email detection
3. AI scores remaining emails 1-10 for importance, includes only ≥5
4. Creates daily digest files with embedded thread context (last 10 messages)

### Key Design Decisions
- **Self-contained files**: Each daily digest embeds thread context (vs. separate thread index) for simpler downstream processing
- **State-based scheduling**: Uses interval scheduler but skips if already run today, handles multi-day catchup
- **Reuses OAuth**: Leverages existing `token.pickle` from Google Docs integration
- **Two-stage filtering**: Pre-filter (fast, rule-based) + AI scoring (accurate, expensive)

### Files Created
- `integrations/gmail_utils.py` - Gmail API wrapper
- `processors/notes/email_digest.py` - Main processor
- `prompts/email_importance.md` - AI importance scoring prompt
- `tests/test_email_digest.py` - 17 unit tests

---

## 2025-12-27: Meeting Summary Generator

### Problem
After meetings are transcribed, speakers identified, and entities resolved, there was no automated way to generate and validate structured summaries or maintain a searchable index.

### Solution
New `MeetingSummaryGenerator` processor with 3-stage workflow:
1. **AI Generation**: Uses tiny model with rich context (transcript + frontmatter, monthly index, attendee People notes)
2. **User Validation**: Obsidian inline form for review/editing
3. **Processing**: Appends validated summary to dated monthly index (e.g., `2025-12 Meetings.md`)

### Key Design Decisions
- **Fallback for sparse indexes**: When current month has <100 lines, also includes previous month's content
- **Dated index files**: `YYYY-MM Meetings.md` format instead of single perpetual file
- **No checkbox in action items**: Action items use `@[[Person]] Task` format (not for ticking)
- **Summary stays in note body**: Uses callout format like EntityResolver, not frontmatter

### Pipeline Impact
- Runs after EntityResolver, before InteractionLogger
- Updated InteractionLogger's `required_stage` to chain properly

### Files Created
- `prompts/meeting_summary_ai.md` - Rich context prompt
- `processors/notes/meeting_summary_generator.py` - Multi-substage processor
- `tests/test_meeting_summary_generator.py` - 11 unit tests

---


## 2025-12-27: Mention Logging Enhancement

### Problem
InteractionLogger only logged people who participated in meetings (speakers). People who were *mentioned* in discussions got no record.

### Solution
Extended InteractionLogger to also log mentions:
- New `category: mention` vs existing `category: meeting`
- **Batch processing**: Single AI call for all mentions (returns JSON), not per-person
- Captures both "why mentioned" and "information learned about this person"
- Sources mentions from `resolved_entities` (Entity Resolution output)
- Filters out speakers to avoid duplicate logs

### Key Learnings
- Entity Resolution data (`resolved_entities`) can be reused for downstream features
- Separating `logged_interactions` and `logged_mentions` in frontmatter allows independent progress tracking
- Batch JSON prompts are far more efficient than per-item calls

### Files Changed
- `prompts/mention_log.md` - New lightweight prompt
- `processors/notes/interaction_logger.py` - Added mention processing loop

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
