# NoteFlow

A document processing pipeline for audio transcription and note management.

## Features

### Audio Processing
- **Video to Audio Extraction** - Extracts audio from video files using FFmpeg
- **Audio Transcription** - Transcribes audio files using AssemblyAI with speaker diarization
- **Title Generation** - AI-generated titles for transcripts based on content

### Note Processing Pipeline
- **Transcript Classification** - Automatically categorizes transcripts (meeting, diary, idea, meditation, todo)
- **Speaker Identification** - AI-assisted speaker identification with inline Obsidian validation forms
- **Entity Resolution** - AI detection and resolution of named entities (people, orgs) to Obsidian wikilinks
- **Interaction Logging** - Generates meeting notes for participants and brief context logs for mentioned people

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
| `TodoProcessor` | Turns dictated todo memos into Todoist tasks |
| `MeetingSummaryGenerator` | Generates meeting summaries with user validation and monthly index |
| `TodoistSyncProcessor` | Pushes the user's meeting action items to Todoist (AI picks section, due date, urgency, labels) |
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

Two stages push tasks to Todoist, sharing all their machinery via
`processors/notes/todoist_base.py`:

| Stage | Source | Ownership |
|-------|--------|-----------|
| `todoist_synced` (`TodoistSyncProcessor`) | `## Action Items` from a validated meeting summary | AI decides which items the user owns |
| `todos_extracted` (`TodoProcessor`) | The body of a `category: todo` voice memo | All of them — the user dictated it |

For meetings, the action items come from the *validated* summary, so the human review gate
in the Obsidian summary form is the only approval step — the sync itself is automatic.

- One AI call per note decides wording, due date, urgency, project, section
  and labels, using the live Todoist project/section/label lists and open tasks as context.
- Every task returned by the AI must quote its source verbatim — a summary bullet, or a
  phrase from the memo — or it is discarded. This keeps invented tasks out of Todoist.
- A commitment restated in a later note **updates the existing open task** (refreshed due
  date, appended note and link) instead of creating a duplicate. Both recurring meeting
  action items and re-dictated memos hit this often.
- The project and section are chosen from whatever exists in the account at the time, so
  projects added later need no config change. Anything that doesn't clearly fit a project —
  including personal matters — goes to the Inbox rather than being filed at a guess.
- Labels are whitelist-only: the AI can only apply labels that already exist.
- Projects listed in `TODOIST_IGNORED_PROJECTS` are never filed into, and their tasks are
  left out of duplicate detection (Todoist's onboarding project is there by default).
- Every task carries the `TODOIST_AI_LABEL` marker label, so AI-created tasks can be
  filtered and cleaned up in bulk.
- Deadlines resolve against the note's own date, not today; a resulting past date is pulled
  forward to today, with the original kept in the description.
- Each stage's `START_DATE` gates eligibility; older notes are skipped unless tagged
  `force_todoist_sync`.
- Without `TODOIST_API_TOKEN` both stages are inert (log a warning, process nothing).

## Usage

### Running the Service

```bash
python main.py
```

With custom log level:
```bash
python main.py --log-level DEBUG
```

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

All output (stdout and stderr) is written to `logs/noteflow.log`:
```bash
# Follow logs in real-time
tail -f logs/noteflow.log

# View last 50 lines
tail -50 logs/noteflow.log
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
category: meeting
---
```

## License

[Your license here]





