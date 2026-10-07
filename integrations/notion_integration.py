from notion_markdown_converter import fetch_page_as_markdown, create_page_from_markdown, create_notion_client, extract_page_id
from typing import Dict, Any, List, Optional

# The API takes at most 100 blocks per create or append call.
MAX_BLOCKS_PER_REQUEST = 100


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

    def list_people(self) -> List[Dict[str, Any]]:
        """Every human user in the workspace, as {id, name, email}.

        Bots are left out. `email` is None when the integration lacks the
        "read user information including email addresses" capability.
        """
        people = []
        cursor = None
        while True:
            kwargs = {"page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            response = self.client.users.list(**kwargs)
            for user in response.get("results", []):
                if user.get("type") != "person":
                    continue
                people.append({
                    "id": user["id"],
                    "name": user.get("name"),
                    "email": (user.get("person") or {}).get("email"),
                })
            if not response.get("has_more"):
                return people
            cursor = response.get("next_cursor")

    def list_people_seen_in_pages(self) -> List[Dict[str, Any]]:
        """Users found in the people columns of every page this integration can see.

        The users endpoint lists members only; guests appear nowhere else in
        the API. A guest who sits in some people column is found here, with
        the email of their account. One who sits in none is not.
        """
        found: Dict[str, Dict[str, Any]] = {}
        cursor = None
        while True:
            kwargs = {"page_size": 100, "filter": {"property": "object", "value": "page"}}
            if cursor:
                kwargs["start_cursor"] = cursor
            response = self.client.search(**kwargs)
            for page in response.get("results", []):
                for prop in (page.get("properties") or {}).values():
                    if not isinstance(prop, dict) or prop.get("type") != "people":
                        continue
                    for user in prop.get("people") or []:
                        if user.get("type") == "person" and user.get("id"):
                            found[user["id"]] = {
                                "id": user["id"],
                                "name": user.get("name"),
                                "email": (user.get("person") or {}).get("email"),
                            }
            if not response.get("has_more"):
                return list(found.values())
            cursor = response.get("next_cursor")

    def get_single_data_source(self, database_url: str) -> Dict[str, Any]:
        """The schema of a database's data source.

        Refuses databases with several data sources: which one a page lands
        in would then be a guess.
        """
        database = self.client.databases.retrieve(database_id=extract_page_id(database_url))
        sources = database.get("data_sources") or []
        if len(sources) != 1:
            raise ValueError(
                f"Expected exactly one data source in database {database_url}, found {len(sources)}"
            )
        return self.client.data_sources.retrieve(data_source_id=sources[0]["id"])

    def create_page_in_data_source(self, data_source_id: str, properties: Dict[str, Any],
                                   blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create a page with its properties set, then fill in its content.

        Properties go in with the create call itself, so a page never exists
        without them -- even if appending the content fails partway.
        """
        page = self.client.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
            children=blocks[:MAX_BLOCKS_PER_REQUEST],
        )
        for start in range(MAX_BLOCKS_PER_REQUEST, len(blocks), MAX_BLOCKS_PER_REQUEST):
            self.client.blocks.children.append(
                block_id=page["id"],
                children=blocks[start:start + MAX_BLOCKS_PER_REQUEST],
            )
        return page

    def get_page(self, page_id: str) -> Dict[str, Any]:
        return self.client.pages.retrieve(page_id=page_id)

    def trash_page(self, page_id: str) -> None:
        """Move a page to the trash, where it can still be restored."""
        self.client.pages.update(page_id=page_id, in_trash=True)
