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

# Any other user-specific information can be added here 

# Sound notification settings
ENABLE_AI_COMPLETION_SOUND = True  # Enable/disable sound when AI finishes processing

# AI completion sound can be:
# 1. System sound: "SystemAsterisk", "SystemExclamation", etc.
# 2. WAV file path: "sounds/gentle-bell.wav" or "C:/Users/fourn/sounds/success.wav"
# 3. Beep pattern: [[frequency1, duration1], [frequency2, duration2]] 
# 4. None for simple beep (800Hz, 200ms)

# Examples of pleasant beep patterns:
# AI_COMPLETION_SOUND = [[523, 100], [659, 100], [784, 150]]  # C-E-G chord
# AI_COMPLETION_SOUND = [[440, 200]]  # Single A note
# AI_COMPLETION_SOUND = "sounds/notification.wav"  # Custom WAV file

# Default: Pleasant two-tone chime
AI_COMPLETION_SOUND = [[523, 120], [784, 150]]

