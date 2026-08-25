"""Quick capture: a global hotkey that pops up a panel of capture actions.

Runs as its own process rather than inside main.py: a Cocoa run loop wants the
main thread, and the pipeline is an asyncio service. The two talk through the
entry point that already exists — a file landing in Audio/Incoming — so a crash
here can never take processing down with it.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import tempfile
import threading
from pathlib import Path
from typing import List, Optional

import AppKit
import objc
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSApp, NSEvent,
    NSEventMaskKeyDown, NSPasteboard, NSPasteboardTypeString,
)
from Foundation import NSOperationQueue, NSPoint, NSTimer

from config.logging_config import setup_logger
from config.paths import PATHS

from .actions import Action, actions
from .hotkey import HotKey, HotKeyError, format_combo
from .livetranscript import LiveTranscriber, LiveTranscriptError
from .panel import CapturePanel
from .recorder import Recorder, RecorderError

logger = setup_logger(__name__)

DEFAULT_HOTKEY = "ctrl+opt+space"
KEY_ESCAPE = 53
KEY_RETURN = 36
KEY_ENTER = 76  # numeric keypad
SAVED_FLASH_SECONDS = 1.6
TICK_INTERVAL = 0.2
_EVENT_APPLICATION_DEFINED = getattr(AppKit, "NSEventTypeApplicationDefined", 15)
# If the run loop somehow refuses to unwind, leave anyway rather than becoming a
# process that ignores SIGTERM and holds the global hotkey hostage.
HARD_EXIT_GRACE = 2.0


def _copy_to_clipboard(text: str) -> None:
    pasteboard = NSPasteboard.generalPasteboard()
    pasteboard.clearContents()
    pasteboard.setString_forType_(text, NSPasteboardTypeString)


def _acquire_single_instance_lock():
    """Hold a lock for the lifetime of the process, or return None.

    Two instances both register the hotkey and only one of them gets the
    keypress, which looks exactly like the app being broken. Cheaper to refuse
    to start.
    """
    path = Path(tempfile.gettempdir()) / f"noteflow-quickcapture-{os.getuid()}.lock"
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


class QuickCapture:
    """Wires the hotkey, the panel and the recorder together."""

    def __init__(
        self,
        incoming_dir=None,
        *,
        hotkey: str = DEFAULT_HOTKEY,
        quit_after: Optional[float] = None,
        show_on_start: bool = False,
    ):
        self.recorder = Recorder(incoming_dir or PATHS.audio_input)
        self.live: Optional[LiveTranscriber] = None
        self.panel = CapturePanel(on_resign_key=self._on_resign_key)
        self.hotkey = HotKey(hotkey, self._on_hotkey)
        self.hotkey_label = format_combo(hotkey)
        self.quit_after = quit_after
        self.show_on_start = show_on_start

        self._actions: List[Action] = actions()
        self._tick_timer = None
        self._flash_timer = None
        self._key_monitor = None
        self._should_quit = False
        self._quitting = False
        self._shutdown_done = False

    # ------------------------------------------------------------------ context
    # (the CaptureContext an action receives)

    def start_recording(self, tag: str, label: str) -> None:
        try:
            self.recorder.start(tag)
        except RecorderError as exc:
            logger.error("could not start recording: %s", exc)
            self.show_error(str(exc))
            return

        logger.info("recording %s", tag)
        self.panel.show_recording(f"Recording {tag}", [
            ("⏎", "Save and process", self._stop_and_save),
            ("esc", "Discard", self._discard),
        ])
        self._start_tick()

    def start_live_transcript(self, label: str) -> None:
        """Dictate to the clipboard: text streams in, then becomes editable."""
        transcriber = LiveTranscriber(
            on_update=lambda text: self._on_main(
                lambda: self.panel.update_transcript(text)
            ),
            on_error=lambda message: self._on_main(
                lambda: self._live_failed(message)
            ),
        )
        try:
            transcriber.start()
        except LiveTranscriptError as exc:
            logger.error("could not start live transcription: %s", exc)
            self.show_error(str(exc))
            return

        self.live = transcriber
        self.panel.show_transcript(
            "Listening…", "",
            [("⏎", "Stop and copy", self._stop_live_transcript),
             ("esc", "Discard", self._discard_live_transcript)],
            editable=False,
            hint="text appears as you speak; you can edit it after stopping",
        )

    def dismiss(self) -> None:
        """Hide the panel and hand focus back to the app the user was in."""
        self._stop_tick()
        self.panel.hide()
        NSApp.hide_(None)

    def show_error(self, message: str) -> None:
        self._stop_tick()
        self.panel.show_message(
            "Couldn't record", message, error=True,
            rows=[("esc", "Dismiss", self.dismiss)],
        )

    # ------------------------------------------------------------------ panel flow

    def show_menu(self) -> None:
        rows = [
            (action.key.upper(), action.label, self._runner(action))
            for action in self._actions
        ]
        rows.append(("esc", "Cancel", self.dismiss))
        self.panel.show_menu("NoteFlow Quick Capture", rows,
                             footer=f"{self.hotkey_label} shows and hides this panel")

    def _runner(self, action: Action):
        def run():
            self._run_action(action)
        return run

    def _run_action(self, action: Action) -> None:
        logger.info("action: %s", type(action).__name__)
        try:
            action.run(self)
        except Exception as exc:  # noqa: BLE001 - an action must not kill the app
            logger.exception("action %s failed", type(action).__name__)
            self.show_error(f"{action.label} failed: {exc}")

    # ------------------------------------------------------------------ recording

    def _stop_and_save(self) -> None:
        if not self.recorder.is_recording:
            return
        self._stop_tick()
        self.panel.show_message("Saving…", "")

        def worker():
            with objc.autorelease_pool():
                try:
                    dest = self.recorder.stop()
                except RecorderError as exc:
                    self._on_main(lambda: self.show_error(str(exc)))
                    return
                self._on_main(lambda: self._saved(dest.name))

        threading.Thread(target=worker, daemon=True, name="capture-stop").start()

    def _saved(self, filename: str) -> None:
        logger.info("memo saved: %s", filename)
        self.panel.show_message("Saved", filename)
        self._flash_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            SAVED_FLASH_SECONDS, False, lambda timer: self.dismiss()
        )

    def _discard(self) -> None:
        self._stop_tick()
        self.recorder.cancel()
        self.dismiss()

    # ------------------------------------------------------------------ dictation

    def _stop_live_transcript(self) -> None:
        if self.live is None:
            return
        text = self.live.stop()
        self.live = None
        if not text.strip():
            self.show_error("Nothing was transcribed — was anything said?")
            return

        # Copy straight away so the common case (speak, paste) needs no extra
        # keystroke; closing re-copies, in case the text was edited.
        _copy_to_clipboard(text)
        logger.info("transcript copied to clipboard (%d chars)", len(text))
        self.panel.show_transcript(
            "Transcript — copied to clipboard", text,
            [("esc", "Copy and close", self._close_live_transcript)],
            editable=True,
            hint="edit freely; ⌘C copies your selection, esc copies everything and closes",
        )

    def _close_live_transcript(self) -> None:
        """Re-copy on the way out so the clipboard matches what is on screen."""
        text = self.panel.transcript_text
        if text.strip():
            _copy_to_clipboard(text)
        self.dismiss()

    def _discard_live_transcript(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None
        logger.info("live transcript discarded")
        self.dismiss()

    def _live_failed(self, message: str) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None
        self.show_error(message)

    def _start_tick(self) -> None:
        self._stop_tick()
        self._tick_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            TICK_INTERVAL, True, lambda timer: self._tick()
        )

    def _stop_tick(self) -> None:
        if self._tick_timer is not None:
            self._tick_timer.invalidate()
            self._tick_timer = None

    def _tick(self) -> None:
        """While recording: update the clock and notice if ffmpeg died."""
        if not self.recorder.is_recording:
            self._stop_tick()
            return
        failure = self.recorder.failure()
        if failure:
            logger.error("recording failed: %s", failure)
            self.recorder.cancel()
            self.show_error(failure)
            return
        self.panel.update_elapsed(self.recorder.elapsed)

    # ------------------------------------------------------------------ input

    def _on_hotkey(self) -> None:
        """Toggle: finish whatever is in progress, or dismiss, or show the menu."""
        if self.recorder.is_recording:
            self._stop_and_save()
        elif self.live is not None:
            self._stop_live_transcript()
        elif self.panel.state == "transcript":
            self._close_live_transcript()
        elif self.panel.is_visible:
            self.dismiss()
        else:
            self.show_menu()

    def _on_key(self, event):
        if not self.panel.is_visible:
            return event

        code = event.keyCode()
        chars = (event.charactersIgnoringModifiers() or "").lower()

        if code == KEY_ESCAPE:
            if self.recorder.is_recording:
                self._discard()
            elif self.live is not None:
                self._discard_live_transcript()
            elif self.panel.state == "transcript":
                self._close_live_transcript()
            else:
                self.dismiss()
            return None

        # Editing a finished transcript: everything except Escape belongs to the
        # text view, including ⌘C and every character typed.
        if self.panel.wants_raw_keys:
            return event

        if code in (KEY_RETURN, KEY_ENTER):
            if self.recorder.is_recording:
                self._stop_and_save()
            elif self.live is not None:
                self._stop_live_transcript()
            return None

        if self.panel.state == "menu":
            for action in self._actions:
                if action.key.lower() == chars:
                    self._run_action(action)
                    return None

        return None  # the panel is modal while up; swallow everything else

    def _on_resign_key(self) -> None:
        """Clicking elsewhere dismisses the menu only.

        A recording or a live session keeps going, and a finished transcript
        stays up — closing it silently would throw away edits the user made.
        """
        if self.recorder.is_recording or self.live is not None:
            return
        if self.panel.state in ("menu", "message"):
            self.dismiss()

    @staticmethod
    def _on_main(callable_) -> None:
        NSOperationQueue.mainQueue().addOperationWithBlock_(callable_)

    # ------------------------------------------------------------------ lifecycle

    def run(self) -> int:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        try:
            self.hotkey.register()
        except HotKeyError as exc:
            logger.error("%s", exc)
            print(f"error: {exc}")
            return 1

        self._key_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, self._on_key
        )

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._on_signal)
        # Cocoa's run loop blocks Python's signal handling; this timer gives the
        # interpreter a chance to run handlers (so ctrl-C works), and notices
        # when one has asked us to quit. One second keeps idle wakeups low —
        # this process spends months doing nothing.
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            1.0, True, lambda timer: self._quit() if self._should_quit else None
        )
        if self.quit_after:
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                self.quit_after, False, lambda timer: self._quit()
            )

        logger.info(
            "quick capture ready: %s (%d actions: %s)",
            self.hotkey_label, len(self._actions),
            ", ".join(f"{a.key}={a.label}" for a in self._actions),
        )
        print(f"NoteFlow quick capture ready — press {self.hotkey_label} "
              f"({len(self._actions)} actions). Ctrl-C to quit.")

        if self.show_on_start:
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                0.4, False, lambda timer: self.show_menu()
            )

        app.run()
        self._shutdown()
        return 0

    def _on_signal(self, signum, frame) -> None:
        self._should_quit = True

    def _quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        logger.info("quick capture shutting down")
        self.panel.hide()

        NSApp.stop_(None)
        # stop_ is only noticed when the run loop next handles an *event*, and a
        # firing NSTimer is not one — without this the app hangs here, ignoring
        # SIGTERM because the handler below routes through this method.
        NSApp.postEvent_atStart_(
            NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                _EVENT_APPLICATION_DEFINED, NSPoint(0, 0), 0, 0, 0, None, 0, 0, 0
            ),
            True,
        )
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            HARD_EXIT_GRACE, False, lambda timer: self._hard_exit()
        )

    def _hard_exit(self) -> None:
        logger.warning("run loop did not unwind; exiting the hard way")
        self._shutdown()
        os._exit(0)

    def _shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._stop_tick()
        if self._key_monitor is not None:
            NSEvent.removeMonitor_(self._key_monitor)
            self._key_monitor = None
        if self.recorder.is_recording:
            # Never silently drop audio the user already spoke.
            try:
                dest = self.recorder.stop()
                logger.info("saved in-progress recording on shutdown: %s", dest.name)
            except RecorderError as exc:
                logger.error("lost in-progress recording on shutdown: %s", exc)
        if self.live is not None:
            text = self.live.stop()
            self.live = None
            if text.strip():
                _copy_to_clipboard(text)
                logger.info("copied in-progress transcript on shutdown")
        self.hotkey.unregister()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m quickcapture",
        description="Global hotkey panel for capturing todos and ideas by voice.",
    )
    parser.add_argument("--hotkey", default=DEFAULT_HOTKEY,
                        help=f"key combination (default: {DEFAULT_HOTKEY})")
    parser.add_argument("--incoming", default=None,
                        help="override the Audio/Incoming folder (for testing)")
    parser.add_argument("--quit-after", type=float, default=None, metavar="SECONDS",
                        help="quit automatically; useful when testing")
    parser.add_argument("--show-on-start", action="store_true",
                        help="show the panel immediately, without the hotkey")
    args = parser.parse_args(argv)

    lock = _acquire_single_instance_lock()
    if lock is None:
        print("error: quick capture is already running (only one instance can own "
              "the hotkey)")
        return 1

    return QuickCapture(
        args.incoming,
        hotkey=args.hotkey,
        quit_after=args.quit_after,
        show_on_start=args.show_on_start,
    ).run()
