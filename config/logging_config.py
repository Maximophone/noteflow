import logging
import sys

# Create formatters
DEFAULT_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

# Default logging level - can be overridden
DEFAULT_LOG_LEVEL = 'INFO'

# Example of how to set different levels for different components
LOGGER_LEVELS = {
    'services.file_watcher': 'INFO',
    'services.repeater': 'INFO',
    # Add more components as needed
}

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
    
    # Only add handler if logger doesn't already have handlers
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
        logger.addHandler(handler)
    
    return logger

