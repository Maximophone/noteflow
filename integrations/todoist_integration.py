"""
Todoist integration.

Thin synchronous client over the Todoist API v1 (https://api.todoist.com/api/v1).
Processors call it from a worker thread (asyncio.to_thread) to stay off the event loop.

Endpoints used:
    GET  /projects            list projects (inbox_project flags the Inbox)
    GET  /sections            list sections, optionally scoped to a project
    GET  /labels              list personal labels
    POST /labels              create a label
    GET  /tasks               list open tasks (completed tasks are not returned)
    POST /tasks               create a task
    POST /tasks/{id}          update a task

All list endpoints are cursor-paginated: {"results": [...], "next_cursor": "..."}.
"""

import time
from typing import Any, Dict, List, Optional

import requests

from config.logging_config import setup_logger

logger = setup_logger(__name__)

BASE_URL = "https://api.todoist.com/api/v1"

_MAX_RETRIES = 3
_RETRY_DELAY = 5
_PAGE_LIMIT = 200
_TIMEOUT = 30

# Todoist priority is inverted relative to its own UI: 4 is p1 (urgent), 1 is p4 (none).
URGENCY_TO_PRIORITY = {
    "urgent": 4,
    "high": 3,
    "medium": 2,
    "normal": 1,
}


class TodoistError(Exception):
    """Raised when the Todoist API cannot satisfy a request."""
    pass


class TodoistClient:
    """Minimal Todoist API v1 client covering the reads and writes NoteFlow needs."""

    def __init__(self, api_token: str):
        if not api_token:
            raise TodoistError("No Todoist API token configured (TODOIST_API_TOKEN)")
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    # ===== Transport =====

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """Issue a request, retrying on 429 and 5xx."""
        url = f"{BASE_URL}{path}"
        last_error = None

        for attempt in range(_MAX_RETRIES):
            try:
                response = requests.request(
                    method, url, headers=self.headers, timeout=_TIMEOUT, **kwargs
                )
                response.raise_for_status()
                if not response.content:
                    return None
                return response.json()
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code
                # 4xx other than rate limiting means the request itself is wrong;
                # retrying would just repeat it.
                if status != 429 and status < 500:
                    raise TodoistError(
                        f"{method} {path} failed ({status}): {e.response.text[:300]}"
                    ) from e
                last_error = e
            except requests.exceptions.RequestException as e:
                last_error = e

            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_DELAY * (attempt + 1)
                logger.warning(
                    "Todoist %s %s failed (%s), retrying in %ss", method, path, last_error, delay
                )
                time.sleep(delay)

        raise TodoistError(f"{method} {path} failed after {_MAX_RETRIES} attempts: {last_error}")

    def _get_paginated(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict]:
        """Follow next_cursor until the collection is exhausted."""
        params = dict(params or {})
        params["limit"] = _PAGE_LIMIT
        results: List[Dict] = []
        cursor = None

        while True:
            if cursor:
                params["cursor"] = cursor
            payload = self._request("GET", path, params=params)
            results.extend(payload.get("results", []))
            cursor = payload.get("next_cursor")
            if not cursor:
                return results

    # ===== Reads =====

    def get_projects(self) -> List[Dict]:
        return self._get_paginated("/projects")

    def get_sections(self, project_id: Optional[str] = None) -> List[Dict]:
        params = {"project_id": project_id} if project_id else None
        sections = self._get_paginated("/sections", params)
        return [s for s in sections if not s.get("is_archived") and not s.get("is_deleted")]

    def get_labels(self) -> List[Dict]:
        return self._get_paginated("/labels")

    def get_tasks(
        self, project_id: Optional[str] = None, label: Optional[str] = None
    ) -> List[Dict]:
        """List open (uncompleted) tasks, optionally scoped to a project or label."""
        params = {}
        if project_id:
            params["project_id"] = project_id
        if label:
            params["label"] = label
        return self._get_paginated("/tasks", params or None)

    # ===== Writes =====

    def create_label(self, name: str) -> Dict:
        return self._request("POST", "/labels", json={"name": name})

    def create_task(
        self,
        content: str,
        description: Optional[str] = None,
        project_id: Optional[str] = None,
        section_id: Optional[str] = None,
        due_date: Optional[str] = None,
        labels: Optional[List[str]] = None,
        priority: Optional[int] = None,
    ) -> Dict:
        """Create a task. due_date must be an absolute YYYY-MM-DD string."""
        payload: Dict[str, Any] = {"content": content}
        if description:
            payload["description"] = description
        if project_id:
            payload["project_id"] = project_id
        if section_id:
            payload["section_id"] = section_id
        if due_date:
            payload["due_date"] = due_date
        if labels:
            payload["labels"] = labels
        if priority:
            payload["priority"] = priority
        return self._request("POST", "/tasks", json=payload)

    def update_task(self, task_id: str, **fields) -> Dict:
        """Update a task. Only the fields passed are touched."""
        payload = {k: v for k, v in fields.items() if v is not None}
        return self._request("POST", f"/tasks/{task_id}", json=payload)
