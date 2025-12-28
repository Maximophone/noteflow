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
| `TodoProcessor` | Extracts todo items from transcripts |
| `MeetingProcessor` | Creates meeting notes from templates |
| `MeetingSummaryGenerator` | Generates meeting summaries with user validation and monthly index |
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
```

## Usage

### Running the Service

```bash
python main.py
```

With custom log level:
```bash
python main.py --log-level DEBUG
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





