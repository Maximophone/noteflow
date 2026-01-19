# NoteFlow Developer Log

A running log of technical discoveries, design decisions, and implementation notes.

---

## 2026-01-19: Email Digest Error Handling Fixes

### Problem
Email digest processor had two critical issues:
1. **Broken pipe errors** from Gmail API were caught but not re-raised, causing silent failures that appeared as "no emails found"
2. **State marked as completed** even when email fetching failed, because the error path fell through to the "no emails" case which updated state

Example error logs:
```
ERROR - Error fetching emails with query 'after:1768608107 in:inbox': [Errno 32] Broken pipe
ERROR - Error fetching emails with query 'after:1768608107 in:sent': [Errno 32] Broken pipe
```

The processor would then update state and skip the day's emails permanently.

### Root Cause
**Issue 1**: In `GmailUtils.get_emails_since()`, exceptions were caught and logged but not re-raised:
```python
except Exception as e:
    logger.error(...)
    # No raise! Execution continues, returns []
```

**Issue 2**: With silent failures returning `[]`, the calling code interpreted this as "successfully fetched 0 emails" and updated state.

**Issue 3**: Broken pipe errors occur when the Gmail API connection becomes stale between runs (credential refresh issues, network timeouts).

### Solution

**1. Added retry logic with connection reset** (`gmail_utils.py`):
- New `_handle_broken_pipe()` method detects broken pipe errors and resets `self.service = None`
- `get_emails_since()` wraps API calls in retry loop (max 2 attempts)
- On broken pipe, gets fresh service connection and retries
- All exceptions now properly re-raised after logging

**2. Fixed error propagation** (`email_digest.py`):
- Added clarifying comments showing state only updates in success paths
- With re-raised exceptions, failed fetches now trigger exception handler where state is NOT updated
- Failed runs will retry on next scheduled execution

### Key Learnings
- **Silent error handling is dangerous**: Always re-raise after logging unless you have a specific recovery strategy
- **Distinguish "no results" from "failure to fetch"**: Empty results can be valid, but shouldn't be returned on errors
- **Persistent connections need refresh logic**: Long-lived API service objects can become stale

### Files Modified
- `integrations/gmail_utils.py` - Added `_handle_broken_pipe()`, retry logic, error re-raising
- `processors/notes/email_digest.py` - Added clarifying comments about state update logic

### Verification
All 17 existing tests passed:
```
tests/test_email_digest.py::TestGmailUtils::* - 7/7 passed
tests/test_email_digest.py::TestEmailDigestProcessor::* - 6/6 passed  
tests/test_email_digest.py::TestEmailDigestProcessorAsync::* - 4/4 passed
```

---

## 2026-01-13: Entity Resolution Bug Fixes

### Problem
Entity resolution for email digests failed silently overnight:
1. **Wrong AI model**: Hardcoded `opus4.5` instead of using `tiny_ai_model` (`gemini3.0flash`) from base class
2. **Silent failure on JSON parse errors**: When AI response was truncated, code returned empty list instead of raising an error, causing stage to be marked complete with 0 entities

### Root Cause
Large email digest (33 emails, 25 threads) caused AI to generate 86+ entities, hitting token limits. The JSON response was truncated mid-string (`"[[EU` instead of `"[[EU AI Act]]"`), causing parse failure.

### Solution
1. Removed custom `self.entity_model = AI("opus4.5")` - now uses inherited `self.tiny_ai_model` from base class
2. Fixed duplicate `super().__init__()` call
3. Changed `return []` to `raise EntityResolutionError(...)` on JSON parse failure

### Key Learning
- Always use centralized AI model config (`services_config.py`) via base class, never hardcode model names in processors
- The old DEVLOG entry mentioning Opus for entity resolution was outdated - Flash models work fine for this use case

### Files Modified
- `processors/notes/entity_resolver.py` - Model fix, error handling fix

---

## 2026-01-12: Discord Notification Consolidation

### Problem
Multiple processors (`SpeakerIdentifier`, `EntityResolver`, `MeetingSummaryGenerator`) each sent individual Discord notifications when creating validation forms. This caused notification spam when multiple processors triggered for the same file, and scattered Discord logic across the codebase.

### Solution
Consolidated all Discord notifications into `InboxGenerator`:
- Removed `discord_io` parameter from all three processors
- `InboxGenerator` now tracks pending items across runs using in-memory state
- Sends **one consolidated notification** when new pending items are detected
- Batches multiple files into a single message (up to 5 shown, rest summarized)

### Key Design Decisions
- **State tracking in memory**: Uses `_known_pending_items` dict to compare current vs previous scan
- **No persistent state file**: State resets on service restart (acceptable tradeoff for simplicity)
- **Error notifications removed**: The inbox markdown file already shows ⚠️ status for errors

### Notification Format
Single file:
```
📝 **NoteFlow: Action Required**
File: `meeting.md`
Pending: Speaker ID
```

Multiple files:
```
📝 **NoteFlow: 3 files need your attention**
• `file1.md` — Speaker ID
• `file2.md` — Entity Resolution ⚠️
• `file3.md` — Meeting Summary
```

### Files Modified
- `processors/notes/speaker_identifier.py` - Removed Discord logic
- `processors/notes/entity_resolver.py` - Removed Discord logic
- `processors/notes/meeting_summary_generator.py` - Removed Discord logic
- `processors/notes/inbox_generator.py` - Added Discord notification with state tracking
- `main.py` - Updated processor instantiations
- `tests/test_*.py` - Updated fixtures for new API

---

## 2025-12-31: Email Interaction Logging & Digest Fixes

### Features Added

**1. Email Interaction Logging**
Extended `InteractionLogger` to handle email digests:
- Uses `email_participant` and `email_mention` categories
- Batched AI calls (2 total per digest): one for participants, one for mentions
- Prompts emphasize NEW messages only (not quoted thread history)
- Uses `tiny_ai_model` for cost efficiency

**2. Quote Stripping in Email Digests**
Added `_strip_quoted_content()` to `EmailDigestProcessor`:
- Detects `On X wrote:` patterns and cuts content after
- Removes `>` quoted lines
- Dramatically reduces digest file sizes (2-email thread: 2800→~200 lines)

**3. Day Completion Logic**
Fixed critical bug where same-day emails were lost:
- **Problem**: If NoteFlow ran in morning, afternoon emails dated same day were skipped (file existed)
- **Solution**: Only process completed days - today's emails deferred to next run
- Log: `"Deferring X emails from today (YYYY-MM-DD) to next run"`

### Bug Fixes

**1. Async AI Calls Blocking Event Loop**
- Wrapped all AI calls with `asyncio.to_thread()` to prevent Discord heartbeat timeouts
- Fixed 4 occurrences of `response` vs `response_text` variable naming after refactor

**2. JSON Template Escaping**
- Escaped `{}` in prompt files to `{{}}` for Python `.format()` compatibility

### Files Created
- `prompts/email_participant_log_batch.md` - Batch prompt for correspondents
- `prompts/email_mention_log.md` - Batch prompt for mentioned people

### Files Modified
- `processors/notes/interaction_logger.py` - Email support, async AI calls
- `processors/notes/email_digest.py` - Quote stripping, day completion logic
- `main.py` - Added email InteractionLogger instance

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
