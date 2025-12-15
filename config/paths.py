import os
import sys
from pathlib import Path
from dataclasses import dataclass, field


def get_default_google_drive_path() -> Path:
    """
    Detect the default Google Drive path based on the operating system.
    Returns the most likely path, which can be overridden by environment variables.
    """
    if sys.platform == "darwin":  # macOS
        home = Path.home()
        # Try newer Google Drive location first (Google Drive for Desktop)
        cloud_storage = home / "Library" / "CloudStorage"
        if cloud_storage.exists():
            # Look for any GoogleDrive-* folder
            gdrive_folders = list(cloud_storage.glob("GoogleDrive-*"))
            if gdrive_folders:
                return gdrive_folders[0] / "My Drive"
        # Try older Google Drive location
        old_gdrive = home / "Google Drive"
        if old_gdrive.exists():
            return old_gdrive
        # Default fallback for macOS
        return cloud_storage / "GoogleDrive" / "My Drive"
    
    elif sys.platform == "win32":  # Windows
        # Check for common Windows Google Drive mount points
        for drive_letter in ["G", "H", "I", "D", "E"]:
            gdrive = Path(f"{drive_letter}:/My Drive")
            if gdrive.exists():
                return gdrive
        # Default fallback for Windows
        return Path("G:/My Drive")
    
    else:  # Linux and others
        home = Path.home()
        # Google Drive is typically accessed via third-party tools on Linux
        gdrive = home / "google-drive"
        if gdrive.exists():
            return gdrive
        return home / "Google Drive"


def get_path_from_env(env_var: str, default: Path) -> Path:
    """Get a path from environment variable or use default."""
    env_value = os.environ.get(env_var)
    if env_value:
        return Path(env_value)
    return default


# Detect base paths
_default_gdrive = get_default_google_drive_path()


@dataclass
class Paths:
    # Base paths - can be overridden via environment variables
    noteflow_path: Path = field(default_factory=lambda: get_path_from_env(
        "NOTEFLOW_PATH",
        get_default_google_drive_path() / "KnowledgeBot"
    ))
    vault_path: Path = field(default_factory=lambda: get_path_from_env(
        "OBSIDIAN_VAULT_PATH",
        get_default_google_drive_path() / "Obsidian"  # Obsidian is inside My Drive
    ))
    runtime_path: Path = field(default_factory=lambda: Path("."))
    
    def __post_init__(self):
        """Initialize derived paths after base paths are set."""
        self.vault_noteflow_path = self.vault_path / "KnowledgeBot"
        
        # Audio processing paths
        self.audio_input = self.noteflow_path / "Audio" / "Incoming"
        self.audio_processed = self.noteflow_path / "Audio" / "Processed"
        self.transcriptions = self.vault_noteflow_path / "Transcriptions"
        
        # Note processing paths
        self.meditations = self.vault_noteflow_path / "Meditations"
        self.ideas = self.vault_noteflow_path / "Ideas"
        self.ideas_directory = self.vault_noteflow_path / "Ideas Directory.md"
        self.todo_directory = self.vault_noteflow_path / "Todo Directory.md"
        self.gdoc_path = self.vault_path / "gdoc"
        self.coda_path = self.vault_path / "coda"
        self.notion_path = self.vault_path / "notion"
        self.markdownload_path = self.vault_path / "MarkDownload"
        self.sources_path = self.vault_path / "Source"
        self.source_template_path = self.vault_path / "Templates" / "source.md"
        self.meetings = self.vault_path / "Meetings"
        self.meeting_template = self.vault_path / "Templates" / "meeting.md"
        self.conversations = self.vault_path / "Conversations"
        self.diary = self.vault_path / "Diary"
        self.people_path = self.vault_path / "People"
        
        self.scripts_folder = self.vault_path / "scripts"

        # prompts
        self.prompts_library = self.vault_path / "Prompts"

        # AI memory paths
        self.ai_memory = self.vault_path / "AI Memory"
        
        # LinkedIn paths
        self.linkedin_messages = self.vault_path / "LinkedIn Messages"

        # data
        self.data = self.runtime_path / "data"

        self.obsidian_vector_db = self.runtime_path / "data/obsidian_vector_db.sqlite"
        
        # Google Drive paths
        self.meetings_gdrive_folder_id = "13tFGdok5I-UTlE-3_We7W1Yym_iV7SK7"

        # Notion database URLs
        self.meetings_notion_database_url = "https://www.notion.so/pauseia/24d28fc94b7780d78c4ec96a7a29f5c6?v=24d28fc94b778072b94a000cd54e5004"

    def __iter__(self):
        """Allow iteration over all paths for directory creation."""
        return iter([
            self.audio_input,
            self.audio_processed,
            self.transcriptions,
            self.meditations,
            self.ideas,
            self.ideas_directory.parent,
            self.gdoc_path,
            self.coda_path,
            self.notion_path,
            self.markdownload_path,
            self.sources_path,
            self.meetings,
            self.conversations,
            self.diary,
            self.ai_memory,
            self.linkedin_messages,
            self.people_path
        ])
    

PATHS = Paths()

