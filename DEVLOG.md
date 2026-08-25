# NoteFlow Developer Log

A running log of technical discoveries, design decisions, and implementation notes.

---

## 2026-08-25: Quick Capture — a Hotkey Panel as a Pipeline Entry Point

### Problem
Dictating a todo meant recording it in some other app, finding the file, renaming it and
moving it into `Audio/Incoming`. Enough friction that the todo memo path went largely
unused despite the whole downstream pipeline already working.

### Investigation
The entry point turned out to already exist. Two properties of the pipeline make a capture
tool almost trivial:

1. `#tags` in a filename are extracted into `source_tags`
   (`processors/audio/transcriber.py`) and the classifier forces the category from them
   (`processors/notes/transcript_classifier.py`), so a memo can skip AI classification.
2. The scheduler polls every 30s, so pickup latency is irrelevant next to the AssemblyAI
   round trip.

So nothing in the pipeline needed changing — only something to put a correctly named file
in the folder. Two approaches were tried and abandoned first:

- **A shell script bound to a hotkey.** Worked from a terminal, but macOS Shortcuts (the
  only hotkey mechanism available without installing anything) runs "Run Shell Script" in a
  sandbox with no microphone access, and with `$USER` unset.
- **Shortcuts' own Record Audio action.** Viable — the action exists on macOS and outputs
  M4A — but the panel is a popup you must click Stop in, and Shortcuts cannot be authored
  programmatically, so the whole setup would have been manual and unversioned.

### Solution
`quickcapture/`, a separate process in this repo. A global hotkey (⌃⌥Space) shows a panel
listing capture actions; each runs from its shown key or a click. Recording writes an M4A
into `Audio/Incoming` named so the tag forces the category.

Verified end to end: hotkey → speech → transcript with `category: todo` (forced, not
guessed) → Todoist, in about two minutes, and the Todoist stage correctly recognised the
task as a restatement of an existing one rather than duplicating it.

### Key Design Decisions
- **Carbon `RegisterEventHotKey`, not `NSEvent` global monitors.** NSEvent monitors — and
  everything built on them, `pynput` included — need Accessibility permission, which never
  prompts usably for a launchd-started process. `RegisterEventHotKey` needs no permission
  at all; it is what the OS uses for its own shortcuts. Called through `ctypes`.
- **Its own process, not part of `main.py`.** A Cocoa run loop wants the main thread and
  the service is asyncio. Coupling is one file written into `Audio/Incoming`, so a UI fault
  cannot take processing down.
- **Actions auto-discovered from `quickcapture/actions/`.** Adding a capability is one
  file; the panel derives its rows, key hints and click handlers from the registry.
- **ffmpeg records from `:default`**, which follows the system input device, so headsets
  work with no configuration. Naming a device would have been actively wrong here: device
  index 0 on this machine is BlackHole, a loopback that records silence.
- **Memos land as a dotfile and are then renamed.** The transcriber skips dotfiles, so it
  cannot pick up a half-copied file — and this is a cross-filesystem copy into Google
  Drive, so the write is not atomic.
- **Several ways out of every panel state**, after a prototype left an unclosable
  borderless window on screen (see below): the hotkey toggles, Escape cancels, rows are
  clickable, and losing focus dismisses the menu.
- **A flock single-instance guard.** Two instances both register the hotkey and only one
  receives the keypress, which is indistinguishable from the app being broken.

### Key Learnings
- **`NSApp.stop_()` does nothing until the run loop handles an *event*, and a firing
  `NSTimer` is not one.** The app hung instead of quitting — and because the SIGTERM
  handler routed through the same path, the process also survived `pkill`, holding the
  global hotkey. Fixed by posting an application-defined event after `stop_()`, plus a
  hard-exit fallback so no future wedge can produce an unkillable process.
- **`pgrep -f "python -m quickcapture"` matches nothing**: the process is
  `.../Python.app/Contents/MacOS/Python`, capital P. This produced a false all-clear that
  hid three lingering instances and delayed finding the bug above. Case-sensitive process
  checks are a trap when the interpreter is a framework build.
- **A borderless `NSPanel` cannot become key window** unless `canBecomeKeyWindow` is
  overridden, and `addLocalMonitorForEventsMatchingMask` only delivers while the app is
  active — which together are exactly how a panel ends up on screen ignoring Escape.
- **The hyphen after the date in a filename is load-bearing.** `2026-08-25 16-52-01 #todo`
  silently loses its tag and gets AI-classified; `2026-08-25-Todo 16-52-01 #todo` does not.
  `tests/test_quickcapture.py` mirrors the transcriber's parsing so a change here fails
  loudly.
- **TCC and the window server both work for a launchd agent.** Tested before committing to
  the setup: a launchd-spawned process captured room audio at −70.5 dB (against −91 dB for
  digital silence, the tell for a silently denied microphone) and drew its panel.

### Files Created
- `quickcapture/hotkey.py` - Carbon hotkey wrapper, combo parsing and display formatting
- `quickcapture/recorder.py` - ffmpeg capture, the filename contract, delivery to Incoming
- `quickcapture/panel.py` - the popup and its menu/recording/message states
- `quickcapture/app.py` - hotkey, panel and recorder wiring; lifecycle
- `quickcapture/actions/{record_todo,record_idea}.py` - the two shipped actions
- `com.maximefournes.noteflow.quickcapture.plist` - launchd agent
- `tests/test_quickcapture.py` - 27 tests

### Files Modified
- `requirements.txt` - `pyobjc-framework-Cocoa`, macOS only
- `README.md`

### Files Deleted
- `scripts/record_memo.sh` - the abandoned shell-script approach

### Verification
- 224 tests pass (27 new).
- Idle cost measured over a 60s window: ~10ms CPU per minute, ~28-37MB resident, flat.
- Lifecycle: clean exit, SIGTERM honoured, `KeepAlive` restart confirmed by `kill -9`,
  duplicate instance refused.

---

## 2026-08-01: Extracting Commitments from Idea Notes

### Problem
Todos were being extracted from meetings and `todo` memos, but not from `idea` notes. The
question was whether they should be — the classifier defines `idea` as "a monologue about
new ideas, projects, or creative thoughts", explicitly the *non*-actionable category, and
there are 148 of them against 30 todo memos.

### Investigation
Rather than reason about it, the existing memo extractor was run over the 8 most recent
idea notes with no writes, to see what would actually land:

| note | proposed |
|------|----------|
| Peaceful AI Safety Advocacy | 0 |
| Shaping the AI Safety Movement | 0 |
| Strength Without Breaking | 0 |
| Le temple du progrès | 0 |
| The Russian Doll Reality | 0 |
| Pause AI Article Brainstorming | 2 (1 a duplicate of an open task) |
| driving | 1 |
| **matilda-rui** | **4** (1 a duplicate) |

The prediction going in — that idea notes would flood the inflow — was **wrong**. Five of
eight produced nothing; the extractor stays quiet on reflective notes.

The case for the stage rests on `2026-04-20-matilda-rui.md`, which is classified `idea`
but is plainly a working session: review Ben's Germany application, work on the UK funding
application, review Matilda's SFF application, discuss rebranding. Real commitments that
were going nowhere. The classifier calls anything monologue-shaped an `idea`, so 1:1s and
thinking-aloud working sessions land there next to philosophy — and only the philosophy is
genuinely task-free.

### Solution
`IdeaTaskProcessor`, a third `TodoistTaskSync` subclass, on `category: idea`.

The prompt differs from the memo one in two ways that matter:

1. **An empty result is framed as the normal outcome**, with an explicit bar for what
   counts: a decision to do a specific concrete thing. Reasoning through whether to do
   something is not deciding to do it; content being drafted inside the note is the note's
   subject, not a todo.
2. **Filing biases hard to the Inbox.** A rambling note is much weaker evidence of where a
   task belongs than a meeting or a dictated todo, so `project` defaults to null and is
   only chosen for concrete professional work that unambiguously belongs to one. This was
   the user's call: "most of them should land in the inbox, but some of them will
   correspond to pause ai work and projects".

### Key Design Decisions
- **A new stage name means the gate is the only thing holding back 148 notes.** Unlike the
  todo memos, which kept `todos_extracted` and were therefore already all marked done,
  `idea_tasks_synced` is new. `START_DATE = 2026-07-31` is load-bearing here, not just
  belt-and-braces.
- **Its own provenance label**, `from-idea-note`, so the lowest-confidence source can be
  reviewed and bulk-cleaned separately from the other two.
- **A larger transcript cap** (20000 vs 8000 chars): idea notes ramble, and one recent
  note runs to 25 KB.
- **Hangs off `speakers_identified`**, not `ideas_extracted`, so it does not couple to
  IdeaProcessor. The note body does not change after speaker identification.

### Key Learnings
- **Test the hypothesis against the real pipeline before designing around it.** The
  volume argument against this stage was intuitive, plausible, and wrong. Eight AI calls
  settled what no amount of reading the classifier prompt would have.
- **A classifier boundary is not a semantic one.** "Idea" here means "monologue", not
  "not actionable" — which is exactly why the actionable content in those notes was
  invisible.

### Files Created
- `processors/notes/idea_tasks.py` - `IdeaTaskProcessor`
- `prompts/idea_todoist_tasks.md` - high-bar extraction, Inbox-biased filing
- `tests/test_idea_tasks.py` - 12 tests

### Files Modified
- `config/user_config.py` - `TODOIST_LABEL_FROM_IDEA_NOTE`, added to the provenance list
- `main.py` - registered the processor
- `README.md`

---

## 2026-07-31: Todoist Sync for Meeting Action Items and Todo Memos

### Problem
Two kinds of commitment were being captured but never reaching the list actually worked
from:

1. **Meeting action items** lived only in the `## Action Items` section of a summary, in
   the transcript and the monthly index. Acting on them meant re-reading meeting notes.
2. **Dictated todo memos** (`category: todo`) were appended to an Obsidian
   `Todo Directory.md` note that was never opened.

A survey of July 2026 showed the scale and the main hazard: 49 Maxime-owned action items
across 22 meetings, but with heavy restatement. "Brief the national chapter leads on the
Pause AI US situation" appeared in 4 separate meetings (07-16, 07-20, and both 07-22
meetings); "Send the resignation letter to Holly Elmore" in 3; "Send Gabriel Alfour
recommendations of aligned advocacy organisations" in 3. A naive sync would have created
several copies of each single commitment.

### Solution

**Source of truth: the validated summary, not the raw transcript.** The action items are
read from the summary callout that `MeetingSummaryGenerator` writes after the user checks
"Finished" in its Obsidian form. That form is already a human review gate — the user can
edit or delete items before validating — so the Todoist write needs no second
confirmation.

**Pre-fetched account state instead of tool-calling.** One HTTP round fetches projects,
sections, labels and open tasks, all injected into the prompt. `ai_core` supports tools,
but the catalogs are small and static, so a prefetch is cheaper, deterministic, and has
no loop failure modes. It also supplies duplicate detection for free.

**One AI call per note** decides ownership (meetings only), wording, description, due
date, urgency, project, section, labels, and whether each item duplicates an open task.

**Shared base class.** `todoist_base.TodoistTaskSync` holds everything both stages need;
subclasses supply only `should_process()` and a `_source_material()` hook returning the
text to triage plus its prompt fields.

### Key Design Decisions

- **Restatements update, they don't duplicate.** When the model marks `duplicate_of`, the
  existing open task is updated — due date refreshed, a "Restated in ..." note and
  Obsidian link appended to the description rather than replacing it (the description may
  have been hand-edited). This is the single most important behaviour given the
  restatement rate above.
- **`source_line` as an anti-hallucination check.** Every returned task must quote its
  source verbatim — a bullet line for a summary, a phrase from the body for a memo — or it
  is dropped and logged. A task the model invented cannot quote input that never existed.
- **Whitelist everything the model names.** Labels must already exist; projects and
  sections must exist, and a section must belong to the chosen project. Anything
  unresolved falls back to the Inbox rather than being filed at a guess — misfiling is
  worse than leaving a task to triage.
- **The AI picks the project, not config.** Projects and sections come from whatever is in
  the account at that moment, so a project added later is picked up with no code change.
  This was confirmed live: a "Pause IA (France)" project created mid-session was chosen
  correctly on the next run.
- **Deadlines anchor to the note's date, not today.** "by next week" in a 2026-07-27
  meeting means the Friday after that meeting. A resulting past date is pulled forward to
  today with the original kept in the description — an item that lands already overdue
  reads as a data error rather than as work.
- **Provenance labels stamp origin, not mentions.** `from-meeting` / `from-voice-memo` are
  applied on creation only. A task restated later keeps its original provenance, which
  also keeps the label honest on the user's own manual tasks that the sync merely updates.
  Named after the artifact rather than the medium, since meeting transcripts are dictated
  audio too — "from-audio-note" would not have distinguished them.
- **`START_DATE` gates eligibility.** 416 transcripts already carried
  `meeting_summarized`; without the gate the new stage would have fired on all of them.
  `force_todoist_sync` in `source_tags` overrides it.
- **The todo stage kept its old `todos_extracted` stage name.** All 29 existing memos
  already carried it, so switching the destination backfilled nothing.
- **Partial failures record what succeeded, then raise.** The stage is not marked done, so
  it retries; duplicate detection stops the retry from re-creating what already landed.

### Key Learnings

- **Survey the real data before designing.** The restatement rate was invisible from the
  code and was the thing that most shaped the design. Reading two weeks of actual
  summaries was worth more than any amount of reasoning about the schema.
- **The live account contradicts assumptions.** Todoist's "Getting Started 👋" onboarding
  project was being offered as a filing destination and its 16 tutorial tasks were
  drowning the duplicate-detection context; hence `TODOIST_IGNORED_PROJECTS`.
- **A label whose meaning depends on a human must be unusable by the AI.** The user had
  added a `human-approved` label to mark reviewed tasks. Nothing stopped the model
  applying it, which would have made the entire review step meaningless. Hence
  `TODOIST_RESERVED_LABELS`.
- **Per-stage exclusion lists leak.** Filtering only *this* stage's provenance label from
  the offered list let the meeting stage apply `from-voice-memo`. Reserved labels have to
  be a global set.
- **The Todoist API v1 spec is fetchable even when the docs page isn't.** The rendered
  reference is JS-heavy and unreadable to a fetcher, but
  `https://developer.todoist.com/openapi.json` gives exact request/response shapes.

### Files Created
- `integrations/todoist_integration.py` - API v1 client (cursor pagination, retries on
  429/5xx only)
- `processors/notes/todoist_base.py` - `TodoistTaskSync`, shared by both stages
- `processors/notes/todoist_sync.py` - `TodoistSyncProcessor`, meeting action items
- `prompts/meeting_todoist_tasks.md` - triage prompt, includes ownership rules
- `prompts/transcript_todoist_tasks.md` - memo prompt, includes splitting rules
- `tests/test_todoist_sync.py`, extended `tests/test_todo.py` - 49 tests

### Files Modified
- `processors/notes/todo.py` - rewritten to push to Todoist instead of `Todo Directory.md`
- `config/user_config.py` - label and ignored-project settings
- `config/secrets.py`, `.env.example` - `TODOIST_API_TOKEN`
- `config/paths.py` - noted `todo_directory` is now historical
- `main.py` - registered `TodoistSyncProcessor`, dropped `TodoProcessor`'s `directory_file`
- `.gitignore` - widened `.env` to `.env.*` (backups hold live secrets too)
- `README.md`

### Files Deleted
- `prompts/extract_todos.md` - dead with the code that used it

### Verification
Backfilled 10 meetings from 2026-07-24 against the live account: **10 tasks created, 4
existing tasks updated** rather than duplicated — including four of the user's own
manually-written tasks ("Find orgs for Gabe", "Chase Rob Wiblin", "Produce impact report
for Gabe", "Sort out legal things for CRM"). One task created early in the run was matched
and updated by a later meeting in the same run. Date clamping fired on the two 2026-07-27
deadlines.

The memo stage then fired unattended in production on `2026-07-31-Matildas July Payment`,
splitting it into the personal expense and the PauseAI salary as two separate tasks.

### Open Question
When a meeting restates a commitment with an *earlier* deadline than one the user set by
hand, the meeting currently wins ("Chase Rob Wiblin" moved 2026-08-07 → 2026-08-03). The
user confirmed this is desired; the alternative is to only ever extend a deadline.

---

## 2026-07-31: Test Runs Polluting the Service Log

### Problem
`logs/noteflow.log` is a single shared file, and `setup_logger()` attaches a
`RotatingFileHandler` to it at import time. Running the test suite therefore appended to
the same log the live service writes to. Fixture data then read as real activity — fake
Todoist ids like `new-1` and `task-42` interleaved with genuine task ids — which made the
log actively misleading when debugging the Todoist work.

### Root Cause
Handlers are built once, lazily, by `_get_shared_handlers()`, and the file handler was
unconditional. `LOG_FILE` is resolved relative to `config/logging_config.py`, so a run
from a git worktree writes to *that* worktree's `logs/`, which is also why out-of-band
script runs seemed to log nowhere.

### Solution
The file handler is skipped when `NOTEFLOW_LOG_TO_FILE=0`. `tests/conftest.py` sets it at
module top level, before any import can reach `logging_config` — a fixture would run too
late, since the handlers are already built by then. With no file handler, the existing
stdout fallback takes over, and pytest captures it.

### Key Learnings
- **Lazily-built module state constrains where you can configure it.** The env var had to
  be set at conftest import time, not in a fixture.
- **Silence is not proof.** The first attempt appeared to work only because the merge had
  not yet run; verifying meant comparing line counts across a full suite run.

### Files Created
- `tests/test_logging_config.py` - asserts the suite itself has no file handler attached,
  so this cannot silently regress

### Files Modified
- `config/logging_config.py` - `LOG_TO_FILE_ENV_VAR` gate
- `tests/conftest.py` - sets the var before other imports
- `README.md`

### Verification
A full suite run took the worktree log from 568 lines to 568 lines, and the service log on
`main` from 81724 to 81724.

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
