"""
The frontmatter properties a meeting note uses to ask for its transcript to
be pushed to Notion. Kept apart from the push stage itself so the
classifier can put them on new meeting notes without importing the stage.
"""

from typing import Dict

PUSH_KEY = "notion_push"
ACCESS_KEY = "notion_access"


def add_push_defaults(frontmatter: Dict) -> None:
    """Put the opt-in properties on a meeting note, switched off.

    Purely so they are there to be found in Obsidian's property panel --
    their names are not something anyone remembers. Values already set are
    left alone.
    """
    frontmatter.setdefault(PUSH_KEY, False)
    frontmatter.setdefault(ACCESS_KEY, [])
