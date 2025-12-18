"""
External services configuration settings.
Contains URLs, endpoints, and other settings for external services used by the application.
"""

# AI Model Configuration
# These are the standard models used across the application.
# Update these values when better models become available.
BIG_MODEL = "gemini3.0pro"      # Primary model for complex tasks
SMALL_MODEL = "gemini3.0flash"  # Fast model for simple tasks (classification, name extraction, etc.)

# Speaker Matcher UI service URL
SPEAKER_MATCHER_UI_URL = "http://127.0.0.1:5000/match/request"

# Add other service URLs as needed 
GOOGLE_SCOPES = [
    'https://mail.google.com/',
    'https://www.googleapis.com/auth/apps.groups.migration',
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/drive.file',
]





