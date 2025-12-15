from notion_markdown_converter import fetch_page_as_markdown, create_page_from_markdown, create_notion_client, extract_page_id
from typing import Dict, Any, Optional

class NotionClient:
    def __init__(self):
        self.client = create_notion_client()

    def create_page_from_markdown(self, markdown_content: str, parent_url: str, title: Optional[str] = None, parent_type="page") -> Dict[str, Any]:
        """
        Create a Notion page from Markdown content.
        
        Args:
            markdown_content: The Markdown content
            parent_url: Parent page or database ID
            title: Optional page title. If None, extracts from first # heading
            parent_type: The type of parent ("page" or "database")
        Returns:
            The created page response from Notion API
        """
        parent_id = extract_page_id(parent_url)
        page_id = create_page_from_markdown(markdown_content, parent_id=parent_id, title=title, parent_type=parent_type, client=self.client)
        return page_id
    
    def fetch_page_as_markdown(self, page_url: str) -> str:
        """
        Fetch a Notion page as Markdown content.
        
        Args:
            page_url: The URL of the Notion page
            
        Returns:
            The Markdown content of the page
        """
        page_id = extract_page_id(page_url)
        markdown_content = fetch_page_as_markdown(page_id, self.client)
        return markdown_content

