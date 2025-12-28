#!/usr/bin/env python3
"""
NoteFlow - A document processing pipeline for audio transcription and note management.

This service handles:
- Audio/video transcription via AssemblyAI
- Speaker identification with human-in-the-loop confirmation
- Automatic note classification and processing
- Integration with Notion, Coda, and Google Docs
"""

import asyncio
import argparse
import logging
import sys
import io
import os
from config import SLOW_REPEAT_INTERVAL
from config.paths import PATHS
from config.secrets import ASSEMBLY_AI_KEY, DISCORD_BOT_TOKEN
from config.logging_config import set_default_log_level, setup_logger
from typing import Dict, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Import processor classes
from processors.audio.transcriber import AudioTranscriber
from processors.notes.meditation import MeditationProcessor
from processors.notes.ideas import IdeaProcessor
from processors.notes.gdoc import GDocProcessor
from processors.notes.coda import CodaProcessor
from processors.notes.notion import NotionProcessor
from processors.notes.markdownload import MarkdownloadProcessor
from processors.notes.speaker_identifier import SpeakerIdentifier
from processors.notes.meeting import MeetingProcessor
from processors.notes.meeting_summary_generator import MeetingSummaryGenerator
from processors.notes.transcript_classifier import TranscriptClassifier
from processors.notes.conversation import ConversationProcessor
from processors.notes.diary import DiaryProcessor
from processors.notes.idea_cleanup import IdeaCleanupProcessor
from processors.notes.todo import TodoProcessor
from processors.notes.interaction_logger import InteractionLogger
from processors.audio.video_to_audio import VideoToAudioProcessor
from processors.notes.base import NoteProcessor
from processors.notes.base import NoteProcessor
from processors.notes.notion_uploader import NotionUploadProcessor
from processors.notes.entity_resolver import EntityResolver
from processors.notes.inbox_generator import InboxGenerator
from processors.notes.email_digest import EmailDigestProcessor
from processors.notes.email_summary_generator import EmailSummaryGenerator

from integrations.discord import DiscordIOCore

# Ensure stdout/stderr use UTF-8 on Windows
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    else:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

logger = setup_logger(__name__)

scheduler = AsyncIOScheduler()


def instantiate_all_processors(discord_io: DiscordIOCore) -> Dict[str, Any]:
    """Instantiate all processor classes and return a dictionary mapping stage_name to instance."""
    processors = {}
    logger.info("Instantiating processors...")

    # Audio processors
    transcriber = AudioTranscriber(
        input_dir=PATHS.audio_input,
        output_dir=PATHS.transcriptions,
        processed_dir=PATHS.audio_processed,
        api_key=ASSEMBLY_AI_KEY
    )

    video_to_audio_processor = VideoToAudioProcessor(
        input_dir=PATHS.audio_input,
        output_dir=PATHS.audio_input,
        processed_dir=PATHS.audio_processed
    )

    # Note processor classes
    note_processor_classes = [
        MeditationProcessor,
        IdeaProcessor,
        GDocProcessor,
        CodaProcessor,
        NotionProcessor,
        MarkdownloadProcessor,
        SpeakerIdentifier,
        MeetingProcessor,
        MeetingSummaryGenerator,
        TranscriptClassifier,
        ConversationProcessor,
        DiaryProcessor,
        IdeaCleanupProcessor,
        TodoProcessor,
        InteractionLogger,
        InteractionLogger,
        NotionUploadProcessor,
        EntityResolver
    ]

    for cls in note_processor_classes:
        if not issubclass(cls, NoteProcessor):
            continue

        if not cls.stage_name:
            logger.warning(f"Processor class {cls.__name__} missing stage_name attribute.")
            continue

        try:
            if cls is MeditationProcessor:
                instance = cls(input_dir=PATHS.transcriptions, output_dir=PATHS.meditations)
            elif cls is IdeaProcessor:
                instance = cls(input_dir=PATHS.transcriptions, directory_file=PATHS.ideas_directory)
            elif cls is GDocProcessor:
                instance = cls(input_dir=PATHS.gdoc_path)
            elif cls is CodaProcessor:
                instance = cls(input_dir=PATHS.coda_path)
            elif cls is NotionProcessor:
                instance = cls(input_dir=PATHS.notion_path)
            elif cls is MarkdownloadProcessor:
                instance = cls(input_dir=PATHS.markdownload_path, output_dir=PATHS.sources_path, template_path=PATHS.source_template_path)
            elif cls is SpeakerIdentifier:
                instance = cls(input_dir=PATHS.transcriptions, discord_io=discord_io)
            elif cls is MeetingProcessor:
                instance = cls(input_dir=PATHS.transcriptions, output_dir=PATHS.meetings, template_path=PATHS.meeting_template)
            elif cls is MeetingSummaryGenerator:
                instance = cls(input_dir=PATHS.transcriptions, discord_io=discord_io)
            elif cls is TranscriptClassifier:
                instance = cls(input_dir=PATHS.transcriptions)
            elif cls is ConversationProcessor:
                instance = cls(input_dir=PATHS.conversations)
            elif cls is DiaryProcessor:
                instance = cls(input_dir=PATHS.transcriptions, output_dir=PATHS.diary)
            elif cls is IdeaCleanupProcessor:
                instance = cls(input_dir=PATHS.transcriptions, output_dir=PATHS.ideas)
            elif cls is TodoProcessor:
                instance = cls(input_dir=PATHS.transcriptions, directory_file=PATHS.todo_directory)
            elif cls is InteractionLogger:
                instance = cls(input_dir=PATHS.transcriptions)
            elif cls is NotionUploadProcessor:
                instance = cls(input_dir=PATHS.transcriptions, database_url=PATHS.meetings_notion_database_url)
            elif cls is EntityResolver:
                instance = cls(input_dir=PATHS.transcriptions, discord_io=discord_io)

            processors[cls.stage_name] = instance

        except Exception as e:
            logger.error(f"Error instantiating {cls.__name__}: {e}", exc_info=True)

    # Add audio processors
    processors["_transcriber"] = transcriber
    processors["_video_to_audio"] = video_to_audio_processor
    
    # Add inbox generator (scans multiple directories)
    inbox_generator = InboxGenerator(
        scan_dirs=[PATHS.transcriptions, PATHS.email_digests],
        inbox_path=PATHS.inbox_path,
        vault_path=PATHS.vault_path
    )
    processors["_inbox_generator"] = inbox_generator

    # Add email digest processor
    email_digest_processor = EmailDigestProcessor(
        output_dir=PATHS.email_digests,
        state_file=PATHS.email_state,
        overwrite_existing=False
    )
    processors["_email_digest"] = email_digest_processor
    
    # Add entity resolver for email digests (separate from transcript resolver)
    email_entity_resolver = EntityResolver(
        input_dir=PATHS.email_digests, discord_io=discord_io
    )
    email_entity_resolver.required_stage = "email_digest_created"  # Override class default
    processors["_entity_resolver_emails"] = email_entity_resolver
    
    # Add email summary generator
    email_summary_generator = EmailSummaryGenerator(
        input_dir=PATHS.email_digests,
        index_dir=PATHS.email_digests,
    )
    processors["_email_summary_generator"] = email_summary_generator

    logger.info(f"Instantiated {len(processors)} processors.")
    return processors


async def main():
    # Create all required directories
    for path in PATHS:
        if hasattr(path, 'parent') and path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.suffix and not path.exists():
            path.mkdir(parents=True, exist_ok=True)
    logger.info("Ensured all necessary directories exist.")

    # Initialize Discord I/O Core
    logger.info("Initializing Discord...")
    discord_io = DiscordIOCore(token=DISCORD_BOT_TOKEN)
    discord_task = asyncio.create_task(discord_io.start_bot())
    logger.info("Discord task created.")

    # Instantiate all processors
    all_processors = instantiate_all_processors(discord_io)

    # Schedule processors
    logger.info("Scheduling processor jobs...")
    interval = SLOW_REPEAT_INTERVAL
    logger.info(f"Using scheduler interval: {interval} seconds")
    scheduled_count = 0
    
    for name, processor in all_processors.items():
        if hasattr(processor, 'process_all') and callable(processor.process_all):
            # Use dict key for special processors (underscore prefix), stage_name for regular ones
            if name.startswith('_'):
                job_id = name
            elif isinstance(processor, NoteProcessor) and processor.stage_name:
                job_id = processor.stage_name
            else:
                job_id = name

            logger.debug(f"Scheduling job: {job_id} with interval {interval}s")
            try:
                scheduler.add_job(processor.process_all, 'interval', seconds=interval, id=job_id, name=job_id, jitter=5)
                scheduled_count += 1
            except Exception as e:
                logger.error(f"Error scheduling job {job_id}: {e}", exc_info=True)
        else:
            logger.warning(f"Processor with key '{name}' has no process_all method, skipping scheduling.")
    
    logger.info(f"Scheduled {scheduled_count} processor jobs.")

    try:
        logger.info("Starting scheduler...")
        scheduler.start()
        logger.info("Scheduler started.")

        logger.info("NoteFlow service running...")
        await asyncio.gather(
            discord_task,
            asyncio.Event().wait()  # Keep the main loop alive
        )
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received.")
    except Exception as e:
        logger.error(f"An error occurred in the main gather loop: {e}", exc_info=True)
    finally:
        logger.info("Shutting down scheduler...")
        if scheduler.running:
            scheduler.shutdown()
            logger.info("Scheduler shut down.")
        else:
            logger.info("Scheduler was not running.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='NoteFlow - Document Processing Pipeline')
    parser.add_argument('--log-level',
                        type=str,
                        default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        help='Set the default logging level (default: INFO)')
    args = parser.parse_args()

    set_default_log_level(args.log_level)
    logger.info(f"Logging level set to {args.log_level}")

    # Silence APScheduler logs
    logging.getLogger('apscheduler.executors.default').setLevel(logging.ERROR)
    logging.getLogger('apscheduler.scheduler').setLevel(logging.ERROR)

    try:
        logger.info("Starting NoteFlow service...")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("NoteFlow service interrupted by user.")
    except Exception as e:
        logger.critical(f"NoteFlow service exited unexpectedly: {e}", exc_info=True)
    finally:
        logger.info("NoteFlow service stopped.")





