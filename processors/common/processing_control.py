"""
Per-note switches that stop the pipeline.

A note can say how far it wants to travel, in its own frontmatter:

    processing_stopped: true           # nothing else runs on this note
    skip_stages: [interactions_logged] # everything else still runs

Both are read-only as far as the pipeline is concerned — nothing rewrites
them, and nothing else in the note is touched — so clearing the flag lets
processing resume exactly where it left off, form and all.

`processing_stopped` also hides the note from the NoteFlow Inbox. A note
held at a form the user has decided not to fill in is not awaiting input;
it is finished, and leaving it in the inbox is how the inbox stops being
worth reading.
"""

from typing import Any, Dict, Optional

# Obsidian's property editor renders a boolean as a checkbox, so this is a
# click in the sidebar rather than a hand-edited YAML line.
STOP_KEY = "processing_stopped"

# The original name for the same idea, honoured so notes already carrying it
# keep working.
LEGACY_STOP_KEY = "abandoned"

SKIP_KEY = "skip_stages"


def is_processing_stopped(frontmatter: Optional[Dict[str, Any]]) -> bool:
    """True if the note has asked the pipeline to leave it alone."""
    if not frontmatter:
        return False
    return bool(frontmatter.get(STOP_KEY) or frontmatter.get(LEGACY_STOP_KEY))


def is_stage_skipped(frontmatter: Optional[Dict[str, Any]], stage_name: Optional[str]) -> bool:
    """True if `stage_name` is listed in the note's `skip_stages`.

    Accepts a single string as well as a list, since that is what a hand-typed
    `skip_stages: interactions_logged` parses to.
    """
    if not frontmatter or not stage_name:
        return False

    skipped = frontmatter.get(SKIP_KEY)
    if not skipped:
        return False
    if isinstance(skipped, str):
        skipped = [skipped]
    if not isinstance(skipped, (list, tuple, set)):
        return False

    wanted = stage_name.strip().lower()
    return any(
        isinstance(entry, str) and entry.strip().lower() == wanted
        for entry in skipped
    )
