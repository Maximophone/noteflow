import os
from pathlib import Path
from moviepy import VideoFileClip
import asyncio
from config.logging_config import setup_logger
import shutil

logger = setup_logger(__name__)

class VideoToAudioProcessor:
    """Extracts audio from video files and replaces the original file with the audio-only version."""

    def __init__(self, input_dir: Path, output_dir: Path, processed_dir: Path):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.processed_dir = processed_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)

    async def process_all(self) -> None:
        """Process all video files in the input directory."""
        for file_path in self.input_dir.iterdir():
            filename = file_path.name
            
            # Skip hidden files (like .DS_Store on macOS)
            if filename.startswith('.'):
                continue

            # Check if the file is a video file
            _, ext = os.path.splitext(filename)
            if ext.lower() not in ['.mkv', '.mp4', '.avi', '.mov', '.webm', '.wmv']:
                continue  # Skip non-video files, don't exit the loop!

            await self.process_single_file(filename)
            await asyncio.sleep(0)

    async def process_single_file(self, filename: str) -> None:
        """Process a single video file: extract audio and replace the original file."""
        logger.info("Extracting audio from: %s", filename)
        
        try:
            input_path = self.input_dir / filename
            output_path = self.output_dir / f"{os.path.splitext(filename)[0]}.m4a"
            
            # Extract audio using ffmpeg
            await self._extract_audio(input_path, output_path)
            
            # Move original file to processed directory
            processed_path = self.processed_dir / filename
            shutil.move(str(input_path), str(processed_path))
            
            logger.info("Extracted audio: %s", output_path)
            
        except Exception as e:
            logger.error("Error processing %s: %s", filename, str(e))
            raise

    async def _extract_audio(self, input_path: Path, output_path: Path) -> None:
        # Extract audio from the video file
        video = VideoFileClip(str(input_path))
        audio = video.audio
        audio.write_audiofile(str(output_path), codec="aac")
        video.close()
        audio.close()

