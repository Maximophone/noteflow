"""Tests for the frontmatter parse cache.

The cache exists for speed, but the ways it could go wrong are correctness
issues: serving a stale parse after a note changes, or handing every caller the
same mutable dict.
"""

import time

import pytest

from processors.common.frontmatter import (
    frontmatter_cache_stats, invalidate_frontmatter_cache,
    read_frontmatter_from_file, set_frontmatter_in_file,
)


@pytest.fixture(autouse=True)
def clean_cache():
    invalidate_frontmatter_cache()
    yield
    invalidate_frontmatter_cache()


def write(path, body="text\n", **fields):
    lines = "".join(f"{k}: {v}\n" for k, v in fields.items())
    path.write_text(f"---\n{lines}---\n{body}")
    return path


class TestCaching:
    def test_second_read_is_a_hit(self, tmp_path):
        note = write(tmp_path / "a.md", category="todo")
        first = read_frontmatter_from_file(note)
        before = frontmatter_cache_stats()["hits"]
        second = read_frontmatter_from_file(note)
        assert first == second == {"category": "todo"}
        assert frontmatter_cache_stats()["hits"] == before + 1

    def test_a_changed_note_is_re_read(self, tmp_path):
        note = write(tmp_path / "a.md", category="todo")
        assert read_frontmatter_from_file(note) == {"category": "todo"}
        time.sleep(0.01)
        write(note, category="idea")
        assert read_frontmatter_from_file(note) == {"category": "idea"}

    def test_same_size_rewrite_is_still_noticed(self, tmp_path):
        # The dangerous case: an edit that leaves the file exactly as long.
        note = write(tmp_path / "a.md", category="todo")
        assert read_frontmatter_from_file(note)["category"] == "todo"
        time.sleep(0.01)
        write(note, category="idea")  # same length as "todo"
        assert read_frontmatter_from_file(note)["category"] == "idea"

    def test_set_frontmatter_invalidates(self, tmp_path):
        note = write(tmp_path / "a.md", category="todo")
        read_frontmatter_from_file(note)
        set_frontmatter_in_file(note, {"category": "meeting"})
        assert read_frontmatter_from_file(note) == {"category": "meeting"}

    def test_explicit_invalidation_of_one_file_spares_others(self, tmp_path):
        a = write(tmp_path / "a.md", category="todo")
        b = write(tmp_path / "b.md", category="idea")
        read_frontmatter_from_file(a)
        read_frontmatter_from_file(b)
        invalidate_frontmatter_cache(a)
        assert frontmatter_cache_stats()["entries"] == 1
        assert read_frontmatter_from_file(b) == {"category": "idea"}


class TestIsolation:
    def test_callers_cannot_corrupt_the_cache(self, tmp_path):
        """Processors mutate what they read — that must not leak between them."""
        note = write(tmp_path / "a.md", category="todo")
        first = read_frontmatter_from_file(note)
        first["category"] = "vandalised"
        first["injected"] = True
        assert read_frontmatter_from_file(note) == {"category": "todo"}

    def test_nested_values_are_copied_too(self, tmp_path):
        note = tmp_path / "a.md"
        note.write_text("---\ntags:\n- transcription\nprocessing_stages:\n- transcribed\n---\nbody\n")
        first = read_frontmatter_from_file(note)
        first["tags"].append("todo")            # exactly what the classifier does
        first["processing_stages"].append("classified")
        second = read_frontmatter_from_file(note)
        assert second["tags"] == ["transcription"]
        assert second["processing_stages"] == ["transcribed"]


class TestBehaviourPreserved:
    def test_no_frontmatter_returns_empty(self, tmp_path):
        note = tmp_path / "plain.md"
        note.write_text("just text, no frontmatter\n")
        assert read_frontmatter_from_file(note) == {}

    def test_missing_file_still_raises(self, tmp_path):
        with pytest.raises(OSError):
            read_frontmatter_from_file(tmp_path / "nope.md")

    def test_broken_yaml_still_raises_every_time(self, tmp_path):
        note = tmp_path / "bad.md"
        note.write_text("---\n: : not: valid: yaml:\n---\nbody\n")
        for _ in range(2):      # never cached, so it keeps surfacing
            with pytest.raises(Exception):
                read_frontmatter_from_file(note)
