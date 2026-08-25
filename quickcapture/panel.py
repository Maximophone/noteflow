"""The quick-capture popup.

Dumb rendering only: it draws whatever state it is told to draw and reports
clicks and focus loss back to the controller.

Every state has at least two ways out — a click target, the Escape key, and the
global hotkey itself — because a borderless panel with no escape route is
genuinely unclosable if it ever loses focus.
"""

from __future__ import annotations

import warnings
from typing import Callable, Optional, Sequence, Tuple

import AppKit
import objc
from AppKit import (
    NSColor, NSFont, NSMakeRect, NSObject, NSPanel, NSScreen, NSTextField,
    NSTrackingArea, NSView, NSVisualEffectView,
)

# PyObjC warns each time a CGColorRef crosses the bridge (layer background
# colours). The wrapper behaves correctly; the warning is just log noise.
warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)

# Constants PyObjC does not export uniformly across versions.
_STYLE_BORDERLESS = 0
_LEVEL_STATUS = 25
_BACKING_BUFFERED = 2
_MATERIAL_HUD = getattr(AppKit, "NSVisualEffectMaterialHUDWindow", 13)
_BLENDING_BEHIND_WINDOW = 0
_VISUAL_EFFECT_STATE_ACTIVE = 1
_TRACK_ENTER_EXIT = getattr(AppKit, "NSTrackingMouseEnteredAndExited", 0x01)
_TRACK_ACTIVE_ALWAYS = getattr(AppKit, "NSTrackingActiveAlways", 0x80)
_TRACK_IN_VISIBLE_RECT = getattr(AppKit, "NSTrackingInVisibleRect", 0x200)
_ALIGN_RIGHT = getattr(AppKit, "NSTextAlignmentRight", 1)
_ALIGN_CENTER = getattr(AppKit, "NSTextAlignmentCenter", 2)
_TRUNCATE_MIDDLE = getattr(AppKit, "NSLineBreakByTruncatingMiddle", 5)
# Join every Space, and float above full-screen windows.
_COLLECTION_CAN_JOIN_ALL_SPACES = 1 << 0
_COLLECTION_FULLSCREEN_AUXILIARY = 1 << 8

PANEL_WIDTH = 460.0
PAD_X = 22.0
PAD_TOP = 16.0
PAD_BOTTOM = 14.0
HEADER_H = 30.0
ROW_H = 38.0
FOOTER_H = 26.0
LABEL_X = 74.0        # where row labels start, leaving room for the key cap
CORNER_RADIUS = 16.0

# Row: (key hint, label, callback or None)
Row = Tuple[str, str, Optional[Callable[[], None]]]


def _label(
    text: str,
    frame,
    size: float = 14.0,
    color=None,
    bold: bool = False,
    mono: bool = False,
) -> NSTextField:
    field = NSTextField.alloc().initWithFrame_(frame)
    field.setStringValue_(text)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setTextColor_(color or NSColor.labelColor())
    if mono:
        field.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(size, 0.0))
    elif bold:
        field.setFont_(NSFont.boldSystemFontOfSize_(size))
    else:
        field.setFont_(NSFont.systemFontOfSize_(size))
    return field


def _key_cap(text: str, x: float, row_height: float) -> NSView:
    """A small key-cap badge, so a shortcut reads as a key and not as a word."""
    width = max(26.0, 15.0 + 8.5 * len(text))
    height = 22.0
    cap = NSView.alloc().initWithFrame_(
        NSMakeRect(x, (row_height - height) / 2, width, height)
    )
    cap.setWantsLayer_(True)
    cap.layer().setCornerRadius_(6.0)
    cap.layer().setBackgroundColor_(
        NSColor.labelColor().colorWithAlphaComponent_(0.15).CGColor()
    )
    field = _label(
        text, NSMakeRect(0, 2, width, 17), size=12.0, bold=True,
        color=NSColor.labelColor().colorWithAlphaComponent_(0.8),
    )
    field.setAlignment_(_ALIGN_CENTER)
    cap.addSubview_(field)
    return cap


class _Panel(NSPanel):
    """Borderless panels refuse key status unless this is overridden."""

    def canBecomeKeyWindow(self) -> bool:  # noqa: N802 - ObjC selector
        return True


class _Row(NSView):
    """A clickable, hover-highlighting row."""

    def initWithFrame_(self, frame):  # noqa: N802 - ObjC selector
        self = objc.super(_Row, self).initWithFrame_(frame)
        if self is None:
            return None
        self._callback = None
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(8.0)
        self.addTrackingArea_(
            NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                self.bounds(),
                _TRACK_ENTER_EXIT | _TRACK_ACTIVE_ALWAYS | _TRACK_IN_VISIBLE_RECT,
                self,
                None,
            )
        )
        return self

    def set_callback(self, callback: Optional[Callable[[], None]]) -> None:
        self._callback = callback

    def mouseEntered_(self, event):  # noqa: N802 - ObjC selector
        if self._callback is not None:
            self.layer().setBackgroundColor_(
                NSColor.labelColor().colorWithAlphaComponent_(0.12).CGColor()
            )

    def mouseExited_(self, event):  # noqa: N802 - ObjC selector
        self.layer().setBackgroundColor_(None)

    def mouseUp_(self, event):  # noqa: N802 - ObjC selector
        if self._callback is not None:
            self._callback()


class _Delegate(NSObject):
    """Reports focus loss so the controller can dismiss the panel."""

    def initWithHandler_(self, handler):  # noqa: N802 - ObjC selector
        self = objc.super(_Delegate, self).init()
        if self is None:
            return None
        self._handler = handler
        return self

    def windowDidResignKey_(self, notification):  # noqa: N802 - ObjC selector
        if self._handler is not None:
            self._handler()


class CapturePanel:
    """The popup window and its three states: menu, recording, message."""

    def __init__(self, *, on_resign_key: Optional[Callable[[], None]] = None):
        self._panel: Optional[_Panel] = None
        self._delegate = None
        self._on_resign_key = on_resign_key
        self._time_field: Optional[NSTextField] = None
        self.state: str = "hidden"

    # ------------------------------------------------------------------ window

    def _ensure_panel(self, height: float):
        if self._panel is None:
            self._panel = _Panel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, PANEL_WIDTH, height), _STYLE_BORDERLESS,
                _BACKING_BUFFERED, False,
            )
            self._panel.setLevel_(_LEVEL_STATUS)
            self._panel.setOpaque_(False)
            self._panel.setBackgroundColor_(NSColor.clearColor())
            self._panel.setHasShadow_(True)
            self._panel.setMovableByWindowBackground_(True)
            self._panel.setCollectionBehavior_(
                _COLLECTION_CAN_JOIN_ALL_SPACES | _COLLECTION_FULLSCREEN_AUXILIARY
            )
            self._delegate = _Delegate.alloc().initWithHandler_(self._handle_resign_key)
            self._panel.setDelegate_(self._delegate)

        panel = self._panel
        screen = (NSScreen.mainScreen() or NSScreen.screens()[0]).frame()
        x = screen.origin.x + (screen.size.width - PANEL_WIDTH) / 2
        y = screen.origin.y + (screen.size.height - height) / 2 + 140
        panel.setFrame_display_(NSMakeRect(x, y, PANEL_WIDTH, height), False)

        blur = NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, PANEL_WIDTH, height)
        )
        blur.setMaterial_(_MATERIAL_HUD)
        blur.setBlendingMode_(_BLENDING_BEHIND_WINDOW)
        blur.setState_(_VISUAL_EFFECT_STATE_ACTIVE)
        blur.setWantsLayer_(True)
        blur.layer().setCornerRadius_(CORNER_RADIUS)
        blur.layer().setMasksToBounds_(True)
        panel.setContentView_(blur)
        return panel

    def _handle_resign_key(self) -> None:
        if self._on_resign_key is not None:
            self._on_resign_key()

    def _present(self) -> None:
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self._panel.makeKeyAndOrderFront_(None)

    def _add_rows(self, content, rows: Sequence[Row], y: float) -> float:
        for key_hint, text, callback in rows:
            y -= ROW_H
            row = _Row.alloc().initWithFrame_(
                NSMakeRect(PAD_X - 10, y, PANEL_WIDTH - 2 * (PAD_X - 10), ROW_H)
            )
            row.set_callback(callback)
            row.addSubview_(_key_cap(key_hint, 10, ROW_H))
            row.addSubview_(_label(
                text, NSMakeRect(LABEL_X, (ROW_H - 22) / 2, 300, 22), size=15.0,
            ))
            content.addSubview_(row)
        return y

    # ------------------------------------------------------------------ states

    def show_menu(self, title: str, rows: Sequence[Row], footer: str = "") -> None:
        height = (
            PAD_TOP + HEADER_H + ROW_H * len(rows)
            + (FOOTER_H if footer else 0) + PAD_BOTTOM
        )
        panel = self._ensure_panel(height)
        content = panel.contentView()

        y = height - PAD_TOP - HEADER_H
        content.addSubview_(_label(
            title, NSMakeRect(PAD_X, y + 4, PANEL_WIDTH - 2 * PAD_X, 22),
            size=13.0, color=NSColor.secondaryLabelColor(), bold=True,
        ))
        y = self._add_rows(content, rows, y)

        if footer:
            content.addSubview_(_label(
                footer, NSMakeRect(PAD_X, PAD_BOTTOM - 2, PANEL_WIDTH - 2 * PAD_X, 18),
                size=11.0, color=NSColor.tertiaryLabelColor(),
            ))

        self.state = "menu"
        self._time_field = None
        self._present()

    def show_recording(self, title: str, rows: Sequence[Row]) -> None:
        height = PAD_TOP + HEADER_H + 14 + ROW_H * len(rows) + PAD_BOTTOM
        panel = self._ensure_panel(height)
        content = panel.contentView()

        y = height - PAD_TOP - HEADER_H
        dot = NSView.alloc().initWithFrame_(NSMakeRect(PAD_X, y + 9, 11, 11))
        dot.setWantsLayer_(True)
        dot.layer().setCornerRadius_(5.5)
        dot.layer().setBackgroundColor_(NSColor.systemRedColor().CGColor())
        content.addSubview_(dot)

        content.addSubview_(_label(
            title, NSMakeRect(PAD_X + 22, y + 3, 240, 24), size=15.0, bold=True,
        ))
        self._time_field = _label(
            "0:00", NSMakeRect(PANEL_WIDTH - PAD_X - 70, y + 3, 70, 24),
            size=15.0, color=NSColor.secondaryLabelColor(), mono=True,
        )
        self._time_field.setAlignment_(_ALIGN_RIGHT)
        content.addSubview_(self._time_field)

        self._add_rows(content, rows, y - 14)
        self.state = "recording"
        self._present()

    def update_elapsed(self, seconds: float) -> None:
        if self._time_field is None:
            return
        self._time_field.setStringValue_(f"{int(seconds) // 60}:{int(seconds) % 60:02d}")

    def show_message(
        self,
        title: str,
        detail: str = "",
        *,
        error: bool = False,
        rows: Sequence[Row] = (),
    ) -> None:
        height = (
            PAD_TOP + HEADER_H + (24 if detail else 0)
            + ROW_H * len(rows) + PAD_BOTTOM
        )
        panel = self._ensure_panel(height)
        content = panel.contentView()

        y = height - PAD_TOP - HEADER_H
        content.addSubview_(_label(
            title, NSMakeRect(PAD_X, y + 3, PANEL_WIDTH - 2 * PAD_X, 24),
            size=15.0, bold=True,
            color=NSColor.systemRedColor() if error else NSColor.labelColor(),
        ))
        if detail:
            y -= 24
            field = _label(
                detail, NSMakeRect(PAD_X, y, PANEL_WIDTH - 2 * PAD_X, 20),
                size=12.0, color=NSColor.secondaryLabelColor(),
            )
            field.setLineBreakMode_(_TRUNCATE_MIDDLE)
            content.addSubview_(field)

        self._add_rows(content, rows, y)
        self.state = "message"
        self._time_field = None
        self._present()

    def hide(self) -> None:
        if self._panel is not None:
            self._panel.orderOut_(None)
        self.state = "hidden"
        self._time_field = None

    @property
    def is_visible(self) -> bool:
        return self.state != "hidden"
