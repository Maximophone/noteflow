"""Tests for the quick-capture entry point (no GUI involved).

The filename convention is the contract between this module and the rest of the
pipeline, so that is what is worth pinning down: get it wrong and memos are
transcribed but silently classified by AI instead of being forced to the
category the user asked for.
"""

import re
from datetime import datetime

import pytest

from quickcapture.actions import Action, actions, register
from quickcapture.hotkey import HotKeyError, format_combo, parse_combo
from quickcapture.recorder import Recorder, RecorderError, build_filename


WHEN = datetime(2026, 8, 25, 16, 19, 55)


def extract_title_and_tags(stem: str, date_str: str):
    """The transcriber's own parsing, mirrored.

    Copied from processors/audio/transcriber.py (process_single_file) so a
    change to the naming here fails loudly instead of quietly losing tags.
    """
    title, source_tags = None, []
    if stem.startswith(date_str):
        title_parts = stem[len(date_str):].strip()
        if title_parts.startswith("-"):
            raw_title = title_parts[1:].strip()
            source_tags = re.findall(r"#([a-zA-Z0-9_]+)", raw_title)
            cleaned = re.sub(r"#([a-zA-Z0-9_]+)", "", raw_title)
            cleaned = re.sub(r"-+", "-", cleaned).strip("-").strip()
            title = cleaned or None
    return title, source_tags


class TestFilename:
    def test_shape(self):
        assert build_filename("todo", WHEN) == "2026-08-25-Todo 16-19-55 #todo.m4a"

    @pytest.mark.parametrize("tag", ["todo", "idea", "meeting", "diary", "meditation"])
    def test_tag_survives_the_transcriber(self, tag):
        name = build_filename(tag, WHEN)
        title, tags = extract_title_and_tags(name.removesuffix(".m4a"), "2026-08-25")
        assert tags == [tag], f"{name} lost its tag"
        assert title, "transcriber would fall back to an AI-generated title"

    def test_hyphen_immediately_after_date(self):
        # A space here is silently fatal: the transcriber stops parsing and the
        # forced category is lost.
        assert build_filename("todo", WHEN)[10] == "-"

    def test_names_are_unique_per_second(self):
        later = WHEN.replace(second=WHEN.second + 1)
        assert build_filename("todo", WHEN) != build_filename("todo", later)

    def test_tag_is_lowercased(self):
        assert build_filename("TODO", WHEN).endswith("#todo.m4a")


class TestDelivery:
    def test_deliver_moves_file_and_leaves_no_temp(self, tmp_path):
        incoming = tmp_path / "Incoming"
        incoming.mkdir()
        temp = tmp_path / "capture.m4a"
        temp.write_bytes(b"fake audio")

        recorder = Recorder(incoming)
        dest = recorder._deliver(temp, "todo", WHEN)

        assert dest.name == "2026-08-25-Todo 16-19-55 #todo.m4a"
        assert dest.read_bytes() == b"fake audio"
        assert not temp.exists()
        # Nothing half-written left behind for the transcriber to trip over.
        assert list(incoming.glob(".*")) == []

    def test_deliver_reports_a_missing_incoming_folder(self, tmp_path):
        temp = tmp_path / "capture.m4a"
        temp.write_bytes(b"fake audio")
        recorder = Recorder(tmp_path / "does-not-exist")

        with pytest.raises(RecorderError, match="Incoming folder not found"):
            recorder._deliver(temp, "todo", WHEN)

    def test_state_is_clean_before_recording(self, tmp_path):
        recorder = Recorder(tmp_path)
        assert not recorder.is_recording
        assert recorder.elapsed == 0.0
        assert recorder.failure() is None
        with pytest.raises(RecorderError, match="not recording"):
            recorder.stop()


class TestActionRegistry:
    def test_ships_todo_and_idea(self):
        by_key = {a.key: a for a in actions()}
        assert set(by_key) >= {"t", "i"}
        assert by_key["t"].label == "Record todo"
        assert by_key["i"].label == "Record idea"

    def test_keys_are_unique(self):
        keys = [a.key for a in actions()]
        assert len(keys) == len(set(keys))

    def test_ordered_for_display(self):
        found = actions()
        assert found == sorted(found, key=lambda a: (a.order, a.label))

    def test_registering_without_a_key_is_rejected(self):
        with pytest.raises(ValueError, match="single character"):
            @register
            class NoKey(Action):
                label = "Broken"

    def test_registering_without_a_label_is_rejected(self):
        with pytest.raises(ValueError, match="label"):
            @register
            class NoLabel(Action):
                key = "z"


class TestHotkeyParsing:
    def test_default_combo(self):
        code, mods = parse_combo("ctrl+opt+space")
        assert code == 49
        assert mods == 0x1000 | 0x0800

    def test_case_and_separator_insensitive(self):
        assert parse_combo("Ctrl-Opt-Space") == parse_combo("ctrl+opt+space")

    @pytest.mark.parametrize("combo,rendered", [
        ("ctrl+opt+space", "⌃⌥Space"),
        ("cmd+shift+t", "⇧⌘T"),
        ("ctrl+opt+return", "⌃⌥Return"),
    ])
    def test_rendered_for_display(self, combo, rendered):
        assert format_combo(combo) == rendered

    @pytest.mark.parametrize("combo,message", [
        ("space", "no modifiers"),
        ("ctrl+opt", "no non-modifier key"),
        ("ctrl+opt+nope", "unknown key"),
        ("ctrl+a+b", "more than one"),
        ("", "empty hotkey"),
    ])
    def test_rejects_nonsense(self, combo, message):
        with pytest.raises(HotKeyError, match=message):
            parse_combo(combo)
