import logging
import time
from typing import Any, Callable, Dict, List, Optional

import httpx
from notion_client.errors import APIResponseError, RequestTimeoutError
from notion_markdown_converter import fetch_page_as_markdown, create_page_from_markdown, create_notion_client, extract_page_id

logger = logging.getLogger(__name__)

# The API takes at most 100 blocks per create or append call.
MAX_BLOCKS_PER_REQUEST = 100

# Notion drops connections now and then ("Server disconnected without
# sending a response"), and rate-limits with 429. Both pass if retried.
RETRY_DELAYS_SECONDS = (1, 3, 8)
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _is_transient(error: Exception) -> bool:
    if isinstance(error, (httpx.TransportError, RequestTimeoutError)):
        return True
    return isinstance(error, APIResponseError) and error.status in _RETRY_STATUSES


def _retrying(call: Callable[[], Any], what: str) -> Any:
    """Run a call that is safe to repeat, retrying transient failures."""
    for delay in RETRY_DELAYS_SECONDS + (None,):
        try:
            return call()
        except Exception as e:
            if delay is None or not _is_transient(e):
                raise
            logger.warning("Notion %s failed (%s), retrying in %ss", what, e, delay)
            time.sleep(delay)


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
            response = _retrying(lambda: self.client.users.list(**kwargs), "user list")
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
        # Authors and editors come as bare ids; looked up once the scan is done.
        bare_ids = set()
        cursor = None
        while True:
            kwargs = {"page_size": 100, "filter": {"property": "object", "value": "page"}}
            if cursor:
                kwargs["start_cursor"] = cursor
            response = _retrying(lambda: self.client.search(**kwargs), "search")
            for page in response.get("results", []):
                for key in ("created_by", "last_edited_by"):
                    user_id = (page.get(key) or {}).get("id")
                    if user_id:
                        bare_ids.add(user_id)
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
                break
            cursor = response.get("next_cursor")

        for user_id in bare_ids - set(found):
            try:
                user = _retrying(lambda: self.client.users.retrieve(user_id=user_id), "user lookup")
            except Exception:
                # Deleted accounts and placeholder ids: nothing to share with.
                continue
            if user.get("type") == "person":
                found[user_id] = {
                    "id": user_id,
                    "name": user.get("name"),
                    "email": (user.get("person") or {}).get("email"),
                }
        return list(found.values())

    def get_single_data_source(self, database_url: str) -> Dict[str, Any]:
        """The schema of a database's data source.

        Refuses databases with several data sources: which one a page lands
        in would then be a guess.
        """
        database = _retrying(
            lambda: self.client.databases.retrieve(database_id=extract_page_id(database_url)),
            "database lookup")
        sources = database.get("data_sources") or []
        if len(sources) != 1:
            raise ValueError(
                f"Expected exactly one data source in database {database_url}, found {len(sources)}"
            )
        return _retrying(
            lambda: self.client.data_sources.retrieve(data_source_id=sources[0]["id"]),
            "data source lookup")

    def own_user_id(self) -> str:
        """The integration's own bot user: the author of every page it creates."""
        return _retrying(lambda: self.client.users.me(), "own user lookup")["id"]

    def find_pages_by_title(self, data_source_id: str, title_property: str, title: str) -> List[Dict[str, Any]]:
        """Pages in a data source whose title is exactly `title`."""
        response = _retrying(lambda: self.client.data_sources.query(
            data_source_id=data_source_id,
            filter={"property": title_property, "title": {"equals": title}},
        ), "query")
        return response.get("results", [])

    def create_page_in_data_source(self, data_source_id: str, properties: Dict[str, Any],
                                   blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create a page with its properties set, then fill in its content.

        Properties go in with the create call itself, so a page never exists
        without them -- even if appending the content fails partway. The
        create itself is not retried: a dropped response may hide a page
        that was made, and a blind retry would make a second one.
        """
        page = self.client.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
            children=blocks[:MAX_BLOCKS_PER_REQUEST],
        )
        try:
            self._append_blocks(page["id"], blocks[MAX_BLOCKS_PER_REQUEST:],
                                already=min(len(blocks), MAX_BLOCKS_PER_REQUEST))
        except Exception:
            # A half-written page is worse than none; the caller retries from scratch.
            logger.error("Filling Notion page %s failed; moving it to the trash", page["id"])
            self.trash_page(page["id"])
            raise
        return page

    def _append_blocks(self, page_id: str, blocks: List[Dict[str, Any]], already: int) -> None:
        """Append in batches. A batch whose response was lost is checked before it is resent.

        Resending blindly could add the same paragraphs twice, so after a
        failure the page's block count says whether the batch landed.
        """
        expected = already
        for start in range(0, len(blocks), MAX_BLOCKS_PER_REQUEST):
            chunk = blocks[start:start + MAX_BLOCKS_PER_REQUEST]
            for delay in RETRY_DELAYS_SECONDS + (None,):
                try:
                    self.client.blocks.children.append(block_id=page_id, children=chunk)
                    break
                except Exception as e:
                    if delay is None or not _is_transient(e):
                        raise
                    logger.warning("Notion append failed (%s), checking the page", e)
                    time.sleep(delay)
                    count = self._count_blocks(page_id)
                    if count == expected + len(chunk):
                        break  # it landed; only the response was lost
                    if count != expected:
                        raise RuntimeError(
                            f"Notion page {page_id} has {count} blocks, expected "
                            f"{expected} or {expected + len(chunk)}; not retrying") from e
            expected += len(chunk)

    def _count_blocks(self, page_id: str) -> int:
        count, cursor = 0, None
        while True:
            kwargs = {"block_id": page_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            response = _retrying(lambda: self.client.blocks.children.list(**kwargs), "block list")
            count += len(response.get("results", []))
            if not response.get("has_more"):
                return count
            cursor = response.get("next_cursor")

    def get_page(self, page_id: str) -> Dict[str, Any]:
        return _retrying(lambda: self.client.pages.retrieve(page_id=page_id), "page lookup")

    def trash_page(self, page_id: str) -> None:
        """Move a page to the trash, where it can still be restored."""
        _retrying(lambda: self.client.pages.update(page_id=page_id, in_trash=True), "trash")
