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
# Projects the AI never files into, and whose tasks are left out of duplicate
# detection. Todoist's onboarding project is pure noise in both. Matched by name,
# case-insensitively.
TODOIST_IGNORED_PROJECTS = ["Getting Started 👋"]

# Any other user-specific information can be added here 




