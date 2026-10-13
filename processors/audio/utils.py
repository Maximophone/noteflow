from datetime import datetime
from pathlib import Path
from mutagen import File
from config.logging_config import setup_logger

logger = setup_logger(__name__)

def get_recording_date(file_path: Path) -> datetime:
    """
    Extract the original recording date from an audio file.
    
    Attempts to get date from:
    1. Audio file metadata
    2. Filename (if in YYYY-MM-DD format)
    3. File modification time as fallback
    
    Args:
        file_path: Path to the audio file
    Returns:
        datetime object of the recording date
    """
    try:
        audio = File(str(file_path))
        
        # Try to get date from metadata
        if audio is not None and audio.tags:
            if 'date' in audio.tags:
                date_str = str(audio.tags['date'][0])
                return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
            elif 'creation_time' in audio.tags:
                return datetime.strptime(str(audio.tags['creation_time'][0]), "%Y-%m-%dT%H:%M:%S")
        
        # Try to parse from filename
        filename = file_path.name
        date_part = filename.split('-')[:3]  # Assumes format like "2024-05-01-pause-ai-france.m4a"
        if len(date_part) == 3:
            try:
                return datetime.strptime('-'.join(date_part), "%Y-%m-%d")
            except ValueError:
                pass
        
        # Fallback to file modification time
        return datetime.fromtimestamp(file_path.stat().st_mtime)
    
    except Exception as e:
        logger.error("Error extracting date from %s: %s", file_path, e)
        return datetime.fromtimestamp(file_path.stat().st_mtime)


def unused_stem(stem: str, *candidates) -> str:
    """The first of stem, stem-2, stem-3... for which no candidate path exists.

    Each candidate is a function from a stem to a path, so several files that
    must share one name (a transcript's .md and .json) are checked together.

    Recordings are named by hand, so two from the same day often share a name.
    Moving or writing onto that name used to replace the earlier recording and
    its transcript without a trace.
    """
    n = 1
    name = stem
    while any(candidate(name).exists() for candidate in candidates):
        n += 1
        name = f"{stem}-{n}"
    return name


def unused_path(path: Path) -> Path:
    """path itself if nothing is there yet, otherwise the next free -2, -3... variant."""
    stem = unused_stem(path.stem, lambda s: path.with_name(s + path.suffix))
    return path.with_name(stem + path.suffix)
