# NoteFlow

A document processing pipeline for audio transcription and note management.

## Features

### Audio Processing
- **Video to Audio Extraction** - Extracts audio from video files using FFmpeg
- **Audio Transcription** - Transcribes audio files using AssemblyAI with speaker diarization
- **Title Generation** - AI-generated titles for transcripts based on content
- **Quick Capture** - A global hotkey (⌃⌥Space) pops up a panel to dictate a todo or an idea straight into the pipeline, no files to move by hand

### Note Processing Pipeline
- **Transcript Classification** - Automatically categorizes transcripts (meeting, diary, idea, meditation, todo)
- **Speaker Identification** - AI-assisted speaker identification with inline Obsidian validation forms
- **Entity Resolution** - AI detection and resolution of named entities (people, orgs) to Obsidian wikilinks
- **Interaction Logging** - Generates meeting notes for participants and brief context logs for mentioned people
- **Todoist Sync** - Turns meeting action items, dictated todo memos and commitments buried in idea notes into Todoist tasks, with AI-chosen project, section, due date, urgency and labels

### Note Processors
| Processor | Description |
|-----------|-------------|
| `TranscriptClassifier` | Classifies transcripts into categories |
| `SpeakerIdentifier` | Identifies speakers using AI + inline Obsidian validation |
| `EntityResolver` | Resolves named entities to wikilinks using AI + inline Obsidian validation |
| `MeditationProcessor` | Processes meditation transcripts |
| `DiaryProcessor` | Formats diary entries |
| `IdeaProcessor` | Extracts and logs ideas to a directory |
| `IdeaCleanupProcessor` | Cleans up idea notes |
| `IdeaTaskProcessor` | Pulls commitments out of idea notes into Todoist (biased to the Inbox) |
| `TodoProcessor` | Turns dictated todo memos into Todoist tasks |
| `MeetingSummaryGenerator` | Generates meeting summaries with user validation and monthly index |
| `TodoistSyncProcessor` | Pushes the user's meeting action items to Todoist (AI picks project, section, due date, urgency, labels) |
| `InteractionLogger` | Logs interactions per person |
| `EmailDigestProcessor` | Fetches daily important emails from Gmail with AI filtering |
| `EmailSummaryGenerator` | Generates AI summaries for email digests and maintains monthly index |
| `InboxGenerator` | Generates inbox showing files awaiting user input (multi-directory) |
| `NotionUploadProcessor` | Uploads transcripts to Notion |

### External Content Processors
| Processor | Description |
|-----------|-------------|
| `GDocProcessor` | Syncs with Google Docs |
| `NotionProcessor` | Syncs with Notion pages |
| `CodaProcessor` | Syncs with Coda pages |
| `MarkdownloadProcessor` | Processes MarkDownload browser extension outputs |

## Installation

### Prerequisites
- Python 3.10+ (3.11+ recommended)
- FFmpeg (for video/audio processing)

### Setup

1. Clone the repository:
```bash
git clone <repository-url>
cd noteflow
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Install local packages:
```bash
pip install -e /path/to/ai_engine              # ai_core package
pip install -e /path/to/notion_md_converter    # notion_markdown_converter package
```

5. Configure environment variables:
```bash
cp .env.example .env
# Edit .env with your API keys
```

### Required Environment Variables

```
ASSEMBLY_AI_KEY=your_assemblyai_key
DISCORD_BOT_TOKEN=your_discord_bot_token
GOOGLE_API_KEY=your_google_gemini_key
ANTHROPIC_API_KEY=your_anthropic_key
OPENAI_API_KEY=your_openai_key
CODA_API_KEY=your_coda_key
NOTION_API_KEY=your_notion_key
TODOIST_API_TOKEN=your_todoist_token
```

### Todoist Sync

Three stages push tasks to Todoist, sharing all their machinery via
`processors/notes/todoist_base.py`:

| Stage | Source | Ownership | Provenance label |
|-------|--------|-----------|------------------|
| `todoist_synced` (`TodoistSyncProcessor`) | `## Action Items` from a validated meeting summary | AI decides which items the user owns | `from-meeting` |
| `todos_extracted` (`TodoProcessor`) | The body of a `category: todo` voice memo | All of them — the user dictated it | `from-voice-memo` |
| `idea_tasks_synced` (`IdeaTaskProcessor`) | The body of a `category: idea` monologue | All of them — but most notes yield none | `from-idea-note` |

Idea notes are the loosest source: they are thinking out loud, and most contain no tasks
at all, so that prompt sets a high bar and treats an empty result as the normal outcome.
It exists because the classifier files anything monologue-shaped as an `idea`, so working
sessions land there alongside pure reflection. Because a rambling note is weak evidence of
where a task belongs, that stage biases hard toward the Inbox — it only picks a project
for concrete professional work that clearly belongs to one.

Adding a further source (email, say) means a new subclass with its own prompt, plus a
`from-email` constant added to `TODOIST_PROVENANCE_LABELS` in `config/user_config.py`.

For meetings, the action items come from the *validated* summary, so the human review gate
in the Obsidian summary form is the only approval step — the sync itself is automatic.

- One AI call per note decides wording, due date, urgency, project, section
  and labels, using the live Todoist project/section/label lists and open tasks as context.
- Every task returned by the AI must quote its source verbatim — a summary bullet, or a
  phrase from the note body — or it is discarded. This keeps invented tasks out of Todoist.
- A commitment restated in a later note **updates the existing open task** (refreshed due
  date, appended note and link) instead of creating a duplicate. Both recurring meeting
  action items and re-dictated memos hit this often.
- The project and section are chosen from whatever exists in the account at the time, so
  projects added later need no config change. Anything that doesn't clearly fit a project —
  including personal matters — goes to the Inbox rather than being filed at a guess.
- Labels are whitelist-only: the AI can only apply labels that already exist, and never
  a reserved one — the `ai-generated` marker, any provenance label, or anything in
  `TODOIST_RESERVED_LABELS` (`human-approved` by default, since a review marker the AI
  could stamp would mean nothing).
- Each stage stamps a **provenance label** recording where the task came from:
  `from-meeting`, `from-voice-memo` or `from-idea-note`, created automatically if absent.
  It goes on at creation only — a task restated in a later note keeps its original
  provenance.
- Projects listed in `TODOIST_IGNORED_PROJECTS` are never filed into, and their tasks are
  left out of duplicate detection (Todoist's onboarding project is there by default).
- Every task carries the `TODOIST_AI_LABEL` marker label, so AI-created tasks can be
  filtered and cleaned up in bulk.
- Deadlines resolve against the note's own date, not today; a resulting past date is pulled
  forward to today, with the original kept in the description.
- Each stage's `START_DATE` gates eligibility; older notes are skipped unless tagged
  `force_todoist_sync`.
- Without `TODOIST_API_TOKEN` all three stages are inert (log a warning, process nothing).

## Usage

### Running the Service

```bash
python main.py
```

With custom log level:
```bash
python main.py --log-level DEBUG
```

### Quick Capture (macOS)

A separate little app that turns a keypress into a voice memo the pipeline
already knows how to handle. Start it in its own terminal:

```bash
python -m quickcapture
```

Press **⌃⌥Space** and a panel appears: `T` records a todo, `I` records an idea,
`esc` cancels. The rows are clickable too. While recording, `⏎` saves and `esc`
discards; pressing ⌃⌥Space again also saves. The memo lands in `Audio/Incoming`
named so the transcriber picks it up and the classifier is forced to the right
category, and from there it is the normal pipeline — transcription, then
Todoist.

Useful flags: `--hotkey cmd+shift+space` to rebind, `--incoming DIR` to point at
a scratch folder while testing, `--quit-after SECONDS` as a safety valve.

It runs as its own process on purpose: a Cocoa event loop wants the main
thread, and this way a GUI problem can never take the processing service down.
It talks to the pipeline only by writing a file into `Audio/Incoming`.

Adding another capture action is one file in `quickcapture/actions/` — subclass
`Action`, decorate it with `@register`, and the panel picks up the row, the key
hint and the click handler by itself.

#### Microphone permission

The first recording triggers a microphone prompt. Because the recorder shells
out to ffmpeg, macOS attributes the request to the Python binary, so grant it
once while running from a terminal. If recordings come back empty, check
System Settings › Privacy & Security › Microphone.

### Running as a Background Service (macOS)

NoteFlow can run automatically in the background using macOS launchd. A plist file is included in the repository.

#### Installation

1. Copy the plist to LaunchAgents:
```bash
cp com.maximefournes.noteflow.plist ~/Library/LaunchAgents/
```

2. Load the service:
```bash
launchctl load ~/Library/LaunchAgents/com.maximefournes.noteflow.plist
```

The service will now start automatically on login and restart if it crashes.

#### Managing the Service

```bash
# Check if running (shows PID if active)
launchctl list | grep noteflow

# Stop the service
launchctl unload ~/Library/LaunchAgents/com.maximefournes.noteflow.plist

# Start the service
launchctl load ~/Library/LaunchAgents/com.maximefournes.noteflow.plist

# Restart the service (after code changes)
launchctl unload ~/Library/LaunchAgents/com.maximefournes.noteflow.plist && launchctl load ~/Library/LaunchAgents/com.maximefournes.noteflow.plist
```

#### Viewing Logs

All output (stdout and stderr) is written to `logs/noteflow.log`, rotated by Python at
50 MB with 5 backups (launchd does not rotate what it redirects):
```bash
# Follow logs in real-time
tail -f logs/noteflow.log

# View last 50 lines
tail -50 logs/noteflow.log
```

There is only one log file, so anything run alongside the service would interleave with
it. Set `NOTEFLOW_LOG_TO_FILE=0` to log to stdout only — the test suite sets this for
itself, and it is worth setting for one-off scripts:
```bash
NOTEFLOW_LOG_TO_FILE=0 python some_script.py
```

### Directory Structure

The service expects the following directory structure (configurable via environment variables):

```
NoteFlow/                    # NOTEFLOW_PATH
  Audio/
    Incoming/                # Audio files to process
    Processed/               # Processed audio files
    
Obsidian/                    # OBSIDIAN_VAULT_PATH
  NoteFlow/
    Transcriptions/          # Generated transcripts
    Meditations/             # Meditation notes
    Ideas/                   # Idea notes
  gdoc/                      # Google Doc synced notes
  coda/                      # Coda synced notes
  notion/                    # Notion synced notes
  Meetings/                  # Meeting notes
  Diary/                     # Diary entries
  People/                    # People notes (for interaction logging)
  KnowledgeBot/
    Email Digests/           # Daily email digests from Gmail
```

## Architecture

### Processing Pipeline

1. **Video → Audio**: VideoToAudioProcessor extracts audio from video files
2. **Audio → Transcript**: AudioTranscriber creates markdown transcripts
3. **Classification**: TranscriptClassifier categorizes the transcript
4. **Speaker ID**: SpeakerIdentifier identifies speakers (AI detection + inline Obsidian form for human validation)
5. **Entity Resolution**: EntityResolver detects/resolves entities (AI detection + inline Obsidian form for human validation)
6. **Processing**: Category-specific processors handle the rest
7. **Task Sync**: For meetings, MeetingSummaryGenerator produces a summary the user validates, then TodoistSyncProcessor pushes his action items to Todoist. For `todo` memos, TodoProcessor pushes them directly (see [Todoist Sync](#todoist-sync))

### Email Processing Pipeline

1. **Email Fetch**: EmailDigestProcessor fetches emails from Gmail API
2. **Pre-filter**: Removes promotional categories and automated emails
3. **AI Scoring**: Scores emails 1-10 for importance, keeps ≥5
4. **Digest Creation**: Creates daily digest files with `email_digest_created` stage
5. **Entity Resolution**: EntityResolver processes email digests (same as transcripts)
6. **Summary Generation**: EmailSummaryGenerator creates AI summaries and updates monthly index

### Obsidian Form System

The `processors/common/obsidian_form.py` module provides a reusable text-based form system for Obsidian:
- Validates user input (e.g., wikilink format)
- Shows error callouts for invalid data  
- Unchecks completion checkbox on validation errors
- Sends Discord notifications for user feedback

### Scheduler

All processors run on an interval schedule (default 30 seconds) using APScheduler. Each processor's `process_all()` method scans its input directory and processes eligible files.

### Frontmatter-Based Pipeline

Files track their processing state via YAML frontmatter:
```yaml
---
processing_stages:
  - transcribed
  - classified
  - speakers_identified
  - entities_resolved
  - meeting_summarized
  - todoist_synced
category: meeting
# Written by the Todoist stages, for traceability and reset
todoist_tasks:
  - id: '1234567890'
    content: Review the draft and send feedback
    action: created
---
```

A stage runs only once per file: the base class skips any file whose
`processing_stages` already lists it. Several stages also honour a
`force_todoist_sync` / `force_meeting_summary` entry in `source_tags` to override
their eligibility rules.

## License

[Your license here]





