"""Global hotkeys on macOS, via Carbon's RegisterEventHotKey.

NSEvent's global monitors — and everything built on them, pynput included —
need Accessibility permission, which never prompts usably for a process
started outside a normal app bundle. RegisterEventHotKey needs no permission
at all: it is the mechanism behind the OS's own keyboard shortcuts.

Registering more hotkeys later costs one HotKey() call each; the shared Carbon
handler dispatches to the right callback by id.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
from typing import Callable, Dict, Optional

from config.logging_config import setup_logger

logger = setup_logger(__name__)


class HotKeyError(RuntimeError):
    """Raised when a hotkey cannot be parsed or registered."""


def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode())[0]


_EVENT_CLASS_KEYBOARD = _fourcc("keyb")
_EVENT_HOTKEY_PRESSED = 5
_PARAM_DIRECT_OBJECT = _fourcc("----")
_TYPE_EVENT_HOTKEY_ID = _fourcc("hkid")
_SIGNATURE = _fourcc("nflw")

# Carbon modifier masks (Events.h).
MODIFIERS: Dict[str, int] = {
    "cmd": 0x0100, "command": 0x0100, "⌘": 0x0100,
    "shift": 0x0200, "⇧": 0x0200,
    "opt": 0x0800, "option": 0x0800, "alt": 0x0800, "⌥": 0x0800,
    "ctrl": 0x1000, "control": 0x1000, "⌃": 0x1000,
}

# Virtual key codes (Events.h kVK_*).
KEY_CODES: Dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
    "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38,
    "k": 40, "n": 45, "m": 46,
    "return": 36, "tab": 48, "space": 49, "escape": 53, "esc": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}


def parse_combo(combo: str) -> tuple[int, int]:
    """Turn "ctrl+opt+space" into (key_code, modifier_mask)."""
    parts = [p.strip().lower() for p in combo.replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise HotKeyError(f"empty hotkey: {combo!r}")

    mods = 0
    key_code = None
    for part in parts:
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif part in KEY_CODES:
            if key_code is not None:
                raise HotKeyError(f"more than one non-modifier key in {combo!r}")
            key_code = KEY_CODES[part]
        else:
            raise HotKeyError(f"unknown key {part!r} in {combo!r}")

    if key_code is None:
        raise HotKeyError(f"no non-modifier key in {combo!r}")
    if not mods:
        raise HotKeyError(f"{combo!r} has no modifiers; that would swallow a plain keypress")
    return key_code, mods


_SYMBOLS = {0x1000: "⌃", 0x0800: "⌥", 0x0200: "⇧", 0x0100: "⌘"}
_MOD_ORDER = (0x1000, 0x0800, 0x0200, 0x0100)  # macOS renders them ⌃⌥⇧⌘
_KEY_NAMES = {49: "Space", 36: "Return", 53: "Esc", 48: "Tab"}


def format_combo(combo: str) -> str:
    """Render "ctrl+opt+space" the way macOS shows it: ⌃⌥Space."""
    key_code, mods = parse_combo(combo)
    text = "".join(_SYMBOLS[mod] for mod in _MOD_ORDER if mods & mod)
    name = _KEY_NAMES.get(key_code)
    if name is None:
        name = next(
            (k.upper() for k, v in KEY_CODES.items() if v == key_code and len(k) == 1),
            "?",
        )
    return text + name


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_HANDLER_PROTO = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)

_carbon = None
_handler_installed = False
_handler_ref = None  # must outlive registration; ctypes callbacks are GC'd otherwise
_callbacks: Dict[int, Callable[[], None]] = {}
_next_id = 1


def _lib():
    global _carbon
    if _carbon is not None:
        return _carbon

    path = ctypes.util.find_library("Carbon")
    if not path:
        raise HotKeyError("Carbon framework not found; global hotkeys need macOS")
    lib = ctypes.CDLL(path)

    lib.GetApplicationEventTarget.restype = ctypes.c_void_p
    lib.InstallEventHandler.argtypes = [
        ctypes.c_void_p, _HANDLER_PROTO, ctypes.c_uint32,
        ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.InstallEventHandler.restype = ctypes.c_int32
    lib.RegisterEventHotKey.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.RegisterEventHotKey.restype = ctypes.c_int32
    lib.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
    lib.UnregisterEventHotKey.restype = ctypes.c_int32
    lib.GetEventParameter.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
    ]
    lib.GetEventParameter.restype = ctypes.c_int32

    _carbon = lib
    return _carbon


def _dispatch(next_handler, event, user_data) -> int:
    """Shared Carbon callback: look up which hotkey fired and run its callback."""
    try:
        hk = _EventHotKeyID()
        _lib().GetEventParameter(
            event, _PARAM_DIRECT_OBJECT, _TYPE_EVENT_HOTKEY_ID,
            None, ctypes.sizeof(hk), None, ctypes.byref(hk),
        )
        callback = _callbacks.get(hk.id)
        if callback is None:
            logger.warning("hotkey id %s fired with no callback registered", hk.id)
            return 0
        callback()
    except Exception:
        # Never let an exception cross back into Carbon.
        logger.exception("error handling hotkey")
    return 0


def _install_handler() -> None:
    global _handler_installed, _handler_ref
    if _handler_installed:
        return

    lib = _lib()
    _handler_ref = _HANDLER_PROTO(_dispatch)
    spec = _EventTypeSpec(_EVENT_CLASS_KEYBOARD, _EVENT_HOTKEY_PRESSED)
    out = ctypes.c_void_p()
    status = lib.InstallEventHandler(
        ctypes.c_void_p(lib.GetApplicationEventTarget()), _handler_ref,
        1, ctypes.byref(spec), None, ctypes.byref(out),
    )
    if status != 0:
        raise HotKeyError(f"InstallEventHandler failed (OSStatus {status})")
    _handler_installed = True


class HotKey:
    """A registered system-wide hotkey.

    The callback runs on the main thread, inside the Cocoa run loop.
    """

    def __init__(self, combo: str, callback: Callable[[], None]):
        global _next_id
        self.combo = combo
        self.key_code, self.modifiers = parse_combo(combo)
        self._callback = callback
        self._id = _next_id
        _next_id += 1
        self._ref: Optional[ctypes.c_void_p] = None

    def register(self) -> None:
        if self._ref is not None:
            return

        _install_handler()
        _callbacks[self._id] = self._callback

        ref = ctypes.c_void_p()
        status = _lib().RegisterEventHotKey(
            self.key_code, self.modifiers, _EventHotKeyID(_SIGNATURE, self._id),
            ctypes.c_void_p(_lib().GetApplicationEventTarget()), 0, ctypes.byref(ref),
        )
        if status != 0:
            _callbacks.pop(self._id, None)
            # -9878 is eventHotKeyExistsErr: another app already owns the combo.
            hint = " (already taken by another app?)" if status == -9878 else ""
            raise HotKeyError(f"could not register {self.combo}: OSStatus {status}{hint}")

        self._ref = ref
        logger.info("registered global hotkey %s", self.combo)

    def unregister(self) -> None:
        if self._ref is None:
            return
        _lib().UnregisterEventHotKey(self._ref)
        self._ref = None
        _callbacks.pop(self._id, None)
        logger.info("unregistered global hotkey %s", self.combo)
