"""
User-specific configuration settings.
Contains personal user details that are used across the application.
"""

# Discord User ID for the primary user who will receive notifications
TARGET_DISCORD_USER_ID = 252771041464680449  # Replace with your actual Discord User ID

# User's name and other personal details if needed
USER_NAME = "Maxime Fournes"
USER_EMAIL = "maxime@pauseia.fr"
USER_ORGANIZATION = "Pause IA"

# Todoist
# The project and section for each task are chosen by the AI from whatever projects
# exist at the time, so new projects are picked up with no config change. Tasks that
# don't fit any project land in the Inbox.
# Every task NoteFlow creates carries this label, so AI-created tasks can be
# filtered and cleaned up in bulk. Created automatically if it doesn't exist.
TODOIST_AI_LABEL = "ai-generated"

# Provenance labels: which kind of note a task came from. Named after the artifact
# rather than the medium, since meeting transcripts are dictated audio too. Each is
# created automatically if it doesn't exist, and is applied on task creation only —
# a label says where a task came from, not everywhere it has since been mentioned.
TODOIST_LABEL_FROM_MEETING = "from-meeting"
TODOIST_LABEL_FROM_VOICE_MEMO = "from-voice-memo"
TODOIST_LABEL_FROM_IDEA_NOTE = "from-idea-note"

# All of them, so no stage offers another stage's provenance label to the model as a
# topical choice. Add any new one here as well as on its processor.
TODOIST_PROVENANCE_LABELS = [
    TODOIST_LABEL_FROM_MEETING,
    TODOIST_LABEL_FROM_VOICE_MEMO,
    TODOIST_LABEL_FROM_IDEA_NOTE,
]

# Labels the AI must never apply, beyond the managed ones above. "human-approved" is
# the user's own review marker — if the AI could stamp it, reviewing would mean
# nothing. Add any label whose meaning depends on a human having applied it.
TODOIST_RESERVED_LABELS = [
    "human-approved",
]
# Projects the AI never files into, and whose tasks are left out of duplicate
# detection. Todoist's onboarding project is pure noise in both. Matched by name,
# case-insensitively.
TODOIST_IGNORED_PROJECTS = ["Getting Started 👋"]

# Any other user-specific information can be added here 




