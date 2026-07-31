import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Create formatters
DEFAULT_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

# Default logging level - can be overridden
DEFAULT_LOG_LEVEL = 'INFO'

# Rotating log file: Python owns rotation because launchd never rotates the
# files it redirects stdout/stderr into (the plist only captures uncaught
# output in logs/launchd.log).
LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "noteflow.log"
LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per file
LOG_BACKUP_COUNT = 5  # noteflow.log.1 ... noteflow.log.5

# Set NOTEFLOW_LOG_TO_FILE=0 to log to stdout only. There is a single shared log
# file, so anything running alongside the service — the test suite, a one-off
# script — otherwise interleaves its output with the real service's log, which
# makes the log misleading to read (fake ids from tests look like real activity).
LOG_TO_FILE_ENV_VAR = "NOTEFLOW_LOG_TO_FILE"

# Example of how to set different levels for different components
LOGGER_LEVELS = {
    'services.file_watcher': 'INFO',
    'services.repeater': 'INFO',
    # Add more components as needed
}

_shared_handlers = None


def _get_shared_handlers() -> list:
    """Build the handlers shared by all loggers (created once).

    Writes to the rotating log file unless NOTEFLOW_LOG_TO_FILE=0; also echoes to
    stdout when attached to a terminal (interactive runs). Falls back to
    stdout-only if the log file is disabled or cannot be opened.
    """
    global _shared_handlers
    if _shared_handlers is not None:
        return _shared_handlers

    formatter = logging.Formatter(DEFAULT_FORMAT)
    handlers = []

    if os.environ.get(LOG_TO_FILE_ENV_VAR, "1") != "0":
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding='utf-8',
            )
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except OSError as e:
            print(f"WARNING: could not open log file {LOG_FILE}: {e}", file=sys.stderr)

    if not handlers or (hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()):
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    _shared_handlers = handlers
    return _shared_handlers


def set_default_log_level(level: str):
    """
    Set the default logging level for all loggers.
    """
    global DEFAULT_LOG_LEVEL
    DEFAULT_LOG_LEVEL = level.upper()


def setup_logger(name: str, level: str = None) -> logging.Logger:
    """
    Creates a logger with the given name and level.
    Usage: logger = setup_logger(__name__)
    """
    logger = logging.getLogger(name)

    # Prevent logging propagation to avoid duplicate logs
    logger.propagate = False

    if level is None:
        # Use the global default level, but check for component-specific overrides
        level = DEFAULT_LOG_LEVEL
        for logger_name, logger_level in LOGGER_LEVELS.items():
            if name.startswith(logger_name):
                level = logger_level
                break
    logger.setLevel(getattr(logging, level.upper()))

    # Only add handlers if logger doesn't already have handlers
    if not logger.handlers:
        for handler in _get_shared_handlers():
            logger.addHandler(handler)

    return logger
