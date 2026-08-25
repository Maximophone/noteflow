"""Action registry — the panel is built entirely from what is registered here.

Adding a capability is one file in this package:

    from . import Action, register

    @register
    class OpenInbox(Action):
        key = "o"
        label = "Open inbox"
        order = 30

        def run(self, ctx):
            ...

Modules in this package are imported automatically, so the new action shows up
in the panel — with its key hint and a clickable row — without touching the UI.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Callable, List, Protocol, Type


class CaptureContext(Protocol):
    """What an action is handed when it runs (implemented by app.QuickCapture)."""

    def start_recording(self, tag: str, label: str) -> None: ...

    def dismiss(self) -> None: ...

    def show_error(self, message: str) -> None: ...


class Action:
    """One row in the quick-capture panel."""

    key: str = ""          # single character that triggers it while the panel is up
    label: str = ""        # shown in the panel
    order: int = 100       # lower sorts first

    def run(self, ctx: CaptureContext) -> None:
        raise NotImplementedError


_REGISTRY: List[Type[Action]] = []
_discovered = False


def register(cls: Type[Action]) -> Type[Action]:
    """Class decorator that adds an action to the panel."""
    if not cls.key or len(cls.key) != 1:
        raise ValueError(f"{cls.__name__}.key must be a single character")
    if not cls.label:
        raise ValueError(f"{cls.__name__}.label must be set")
    _REGISTRY.append(cls)
    return cls


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    for module in pkgutil.iter_modules(__path__):
        if not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")
    _discovered = True


def actions() -> List[Action]:
    """Every registered action, in display order."""
    _discover()
    instances = [cls() for cls in _REGISTRY]
    instances.sort(key=lambda a: (a.order, a.label))

    seen: dict[str, str] = {}
    for action in instances:
        key = action.key.lower()
        if key in seen:
            raise ValueError(
                f"duplicate action key {key!r}: {seen[key]} and {type(action).__name__}"
            )
        seen[key] = type(action).__name__
    return instances
