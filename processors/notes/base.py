from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Dict
import aiofiles
from ..common.frontmatter import read_frontmatter_from_file, set_frontmatter_in_file, parse_frontmatter_from_content
import traceback
from ai_core import AI
import os
import asyncio
from config.logging_config import setup_logger
from config.services_config import BIG_MODEL, SMALL_MODEL

logger = setup_logger(__name__)

class NoteProcessor(ABC):
    """Base class for all note processors in Obsidian vault."""
    stage_name: Optional[str] = None
    required_stage: Optional[str] = None

    def __init__(self, input_dir: Path):
        self.input_dir = input_dir
        self.files_in_process = set()
        self.ai_model = AI(BIG_MODEL)
        self.tiny_ai_model = AI(SMALL_MODEL)
    
    def _should_process(self, filename: str) -> bool:
        """Base implementation of should_process with pipeline logic."""
        # Ensure stage_name is defined in subclasses
        if not self.__class__.stage_name:
            raise NotImplementedError("Processors must define stage_name as a class attribute")
        
        if not filename.endswith('.md'):
            return False
            
        # Check pipeline stage requirements
        file_path = self.input_dir.joinpath(filename)
        try:
            frontmatter = read_frontmatter_from_file(file_path)
        except Exception as e:
            logger.error("Error reading frontmatter for %s in stage %s: %s", filename, self.__class__.stage_name, str(e))
            return False

        # Skip if "abandoned" flag is set in frontmatter
        if frontmatter.get('abandoned', False):
            return False

        stages = frontmatter.get('processing_stages', [])
        
        # Skip if already processed
        if self.__class__.stage_name in stages:
            return False
            
        # Check required stage if specified (check instance attr first, then class)
        required = getattr(self, 'required_stage', None) or self.__class__.required_stage
        if required and required not in stages:
            return False

        # Additional validation specific to the processor
        if not self.should_process(filename, frontmatter):
            return False
            
        return True

    @abstractmethod
    def should_process(self, filename: str, frontmatter: Dict) -> bool:
        """Determine if a file should be processed."""
        pass
        
    async def _process_file(self, filename: str) -> None:
        """Wrapper for file processing that handles stage tracking."""
        try:
            logger.info(f"Processing file {filename} for stage {self.__class__.stage_name}")
            # Process the file
            await self.process_file(filename)
            
            # Update processing stages
            file_path = self.input_dir / filename
            frontmatter = read_frontmatter_from_file(file_path)
            if 'processing_stages' not in frontmatter:
                frontmatter['processing_stages'] = []
            if self.__class__.stage_name not in frontmatter['processing_stages']:
                frontmatter['processing_stages'].append(self.__class__.stage_name)
            set_frontmatter_in_file(file_path, frontmatter)
            os.utime(file_path, None)
            
        except Exception as e:
            logger.error("Error in %s processing %s: %s", self.__class__.__name__, filename, str(e))
            raise
    
    @abstractmethod
    async def process_file(self, filename: str) -> None:
        """Process a single file."""
        pass

    async def read_file(self, filename: str) -> str:
        """Helper method to read file content."""
        async with aiofiles.open(self.input_dir / filename, 'r', encoding='utf-8') as f:
            return await f.read()

    async def process_all(self) -> None:
        """Process all eligible files in the input directory."""
        logger.debug(f"Processing all eligible files for stage {self.__class__.stage_name}")
        for file_path in self.input_dir.iterdir():
            await asyncio.sleep(0)
            filename = file_path.name
            
            if filename in self.files_in_process:
                continue
                
            if not self._should_process(filename):
                continue
                
            try:
                self.files_in_process.add(filename)
                await self._process_file(filename)
            except Exception as e:
                logger.error("Error processing %s: %s", filename, str(e))
                traceback.print_exc()
            finally:
                self.files_in_process.remove(filename)
        logger.debug(f"Finished processing all eligible files for stage {self.__class__.stage_name}")





