from pathlib import Path
from typing import Dict
import aiofiles
from .base import NoteProcessor
from ..common.frontmatter import parse_frontmatter_from_content, frontmatter_to_text
from integrations.coda_integration import CodaClient
import os
from config.secrets import CODA_API_KEY
from config.logging_config import setup_logger

logger = setup_logger(__name__)

class CodaProcessor(NoteProcessor):
    """Processes Coda pages by pulling their content and converting to markdown."""
    
    stage_name = "coda_synced"
    
    def __init__(self, input_dir: Path):
        super().__init__(input_dir)
        self.coda_client = CodaClient(CODA_API_KEY)
        
    def should_process(self, filename: str, frontmatter: Dict) -> bool:        
        if not frontmatter:
            return False
        
        # Process if it has a URL and it's a Coda URL
        url = frontmatter.get("url")
        if not url:
            return False
            
        return "coda.io" in url
        
    async def process_file(self, filename: str) -> None:
        """Process a Coda page."""
        logger.info("Processing coda page: %s", filename)
        
        # Read the file
        content = await self.read_file(filename)
        frontmatter = parse_frontmatter_from_content(content)
        
        try:
            # Extract doc and page IDs from URL
            doc_id, page_id = self.coda_client.extract_doc_and_page_id(frontmatter["url"])
            
            # Get page content directly in markdown format
            coda_content_md = self.coda_client.get_page_content(doc_id, page_id, output_format="markdown")
            
            # Update frontmatter and save
            final_content = frontmatter_to_text(frontmatter) + coda_content_md.decode('utf-8')
            
            # Write back to same file
            async with aiofiles.open(self.input_dir / filename, 'w', encoding='utf-8') as f:
                await f.write(final_content)
            os.utime(self.input_dir / filename, None)

            logger.info("Processed coda page: %s", filename)
            
        except Exception as e:
            logger.error("Error processing Coda page %s: %s", filename, str(e))
            raise

