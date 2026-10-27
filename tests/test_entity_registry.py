"""
Tests for the entity registry and the confidence features built on it.
"""

from processors.notes import entity_confidence
from processors.notes.entity_registry import EntityRegistry, entity_key, normalize_alias


def make_registry(tmp_path, meetings=()):
    registry = EntityRegistry(tmp_path / "registry.json", "[[Maxime Fournes]]")
    for filename, date, resolved, attendees in meetings:
        registry.record_meeting(filename, date, resolved, attendees)
    return registry


def ent(name, link, entity_type="people"):
    return {"detected_name": name, "resolved_link": link, "entity_type": entity_type}


class TestNormalisation:
    def test_accents_case_and_punctuation(self):
        assert normalize_alias("Jérémy  Duval.") == "jeremy duval"

    def test_path_prefix_is_same_entity(self):
        assert entity_key("[[People/Maxime Fournes]]") == entity_key("[[Maxime Fournes]]")


class TestLearning:
    def test_counts_aliases_and_rejections(self, tmp_path):
        registry = make_registry(tmp_path, [
            ("a.md", "2026-01-01", [ent("Tom", "[[Tom Okafor]]"), ent("Blorf", "")], []),
            ("b.md", "2026-01-02", [ent("Tom", "[[Tom Okafor]]")], []),
            ("c.md", "2026-01-03", [ent("Tom", "[[Thomas Lindqvist]]")], []),
        ])
        ev = registry.evidence("Tom")
        assert ev.total == 3
        counts = {c.link: c.alias_count for c in ev.candidates}
        assert counts["[[Tom Okafor]]"] == 2
        assert counts["[[Thomas Lindqvist]]"] == 1
        assert registry.evidence("blorf").rejected == 1

    def test_recording_twice_is_ignored(self, tmp_path):
        registry = make_registry(tmp_path)
        assert registry.record_meeting("a.md", "2026-01-01", [ent("Tom", "[[Tom Okafor]]")])
        assert not registry.record_meeting("a.md", "2026-01-01", [ent("Tom", "[[Tom Okafor]]")])
        assert registry.evidence("Tom").total == 1

    def test_companions_exclude_the_user(self, tmp_path):
        registry = make_registry(tmp_path, [
            ("a.md", "2026-01-01", [ent("Tom", "[[Tom Okafor]]")], ["[[Maxime Fournes]]", "[[Nora Castell]]"]),
        ])
        companions = registry.lookup("[[Tom Okafor]]")["companions"]
        assert "nora castell" in companions
        assert "maxime fournes" not in companions

    def test_save_and_reload(self, tmp_path):
        registry = make_registry(tmp_path, [("a.md", "2026-01-01", [ent("Tom", "[[Tom Okafor]]")], [])])
        registry.save()
        reloaded = EntityRegistry(tmp_path / "registry.json", "[[Maxime Fournes]]")
        assert reloaded.evidence("Tom").candidates[0].link == "[[Tom Okafor]]"
        assert "a.md" in reloaded.processed


class TestCandidates:
    def test_fuzzy_match_finds_misspelling(self, tmp_path):
        registry = make_registry(tmp_path, [
            ("a.md", "2026-01-01", [ent("Mira Solberg", "[[Mira Solberg]]")], []),
        ])
        links = [c.link for c in registry.evidence("Myra Solberg").candidates]
        assert "[[Mira Solberg]]" in links

    def test_first_name_matches_full_name(self, tmp_path):
        registry = make_registry(tmp_path, [
            ("a.md", "2026-01-01", [ent("Thomas Lindqvist", "[[Thomas Lindqvist]]")], []),
        ])
        candidate = registry.evidence("Thomas").candidates[0]
        assert candidate.link == "[[Thomas Lindqvist]]"
        assert candidate.token_match

    def test_attendee_and_companions_are_flagged(self, tmp_path):
        registry = make_registry(tmp_path, [
            ("a.md", "2026-01-01", [ent("Tom", "[[Tom Okafor]]")], ["[[Nora Castell]]"]),
            ("b.md", "2026-01-02", [ent("Tom", "[[Tom Varga]]")], ["[[Lena Hart]]"]),
        ])
        ev = registry.evidence("Tom", ["[[Maxime Fournes]]", "[[Nora Castell]]"])
        by_link = {c.link: c for c in ev.candidates}
        assert by_link["[[Tom Okafor]]"].companion_score == 1.0
        assert by_link["[[Tom Varga]]"].companion_score == 0.0

    def test_profile_mentions_aliases_and_companions(self, tmp_path):
        registry = make_registry(tmp_path, [
            ("a.md", "2026-01-01", [ent("Tom", "[[Tom Okafor]]")], ["[[Nora Castell]]"]),
        ])
        profile = registry.profile("[[Tom Okafor]]")
        assert "tom x1" in profile and "[[Nora Castell]]" in profile
        assert registry.profile("[[Nobody]]") == "[[Nobody]] (new)"


class TestConfidenceFeatures:
    def test_consistent_history_scores_higher_than_none(self, tmp_path):
        registry = make_registry(tmp_path, [
            (f"{i}.md", f"2026-01-0{i}", [ent("Nora", "[[Nora Castell]]")], []) for i in range(1, 6)
        ])
        ev = registry.evidence("Nora")
        agreeing = entity_confidence.features("[[Nora Castell]]", ev, 0.9)
        assert agreeing["hist_share"] == 1.0 and agreeing["hist_disagrees"] == 0.0
        contradicting = entity_confidence.features("[[Nora Smith]]", ev, 0.9)
        assert contradicting["is_new"] == 1.0 and contradicting["hist_disagrees"] == 1.0
        assert entity_confidence.probability(agreeing) > entity_confidence.probability(contradicting)

    def test_none_answer_uses_rejection_history(self, tmp_path):
        registry = make_registry(tmp_path, [("a.md", "2026-01-01", [ent("Blorf", "")], [])])
        feats = entity_confidence.features("", registry.evidence("Blorf"), 0.8)
        assert feats["is_none"] == 1.0 and feats["hist_share"] == 1.0


class TestCuratedDescriptions:
    def test_round_trip_and_profile_prefers_curated(self, tmp_path):
        from processors.notes.entity_registry import load_descriptions, read_description_rows, write_descriptions
        path = tmp_path / "Entity Descriptions.md"
        write_descriptions(path, {"tom okafor": ("[[Tom Okafor]]", "Runs the Lisbon chapter | events")})
        assert load_descriptions(path) == {"tom okafor": "Runs the Lisbon chapter / events"}
        assert read_description_rows(path)["tom okafor"][0] == "[[Tom Okafor]]"

        registry = make_registry(tmp_path, [("a.md", "2026-01-01", [ent("Tom", "[[Tom Okafor]]")], [])])
        registry.record_meeting("b.md", "2026-01-02", [ent("Tom", "[[Tom Okafor]]")], [], {"[[Tom Okafor]]": "learnt"})
        assert "learnt" in registry.profile("[[Tom Okafor]]")
        registry.curated = load_descriptions(path)
        profile = registry.profile("[[Tom Okafor]]")
        assert "Runs the Lisbon chapter" in profile and "learnt" not in profile

    def test_hand_edited_rows_are_read(self, tmp_path):
        from processors.notes.entity_registry import load_descriptions
        path = tmp_path / "Entity Descriptions.md"
        path.write_text("| Entity | Description |\n|---|---|\n| [[People/Nora Castell]] | Treasurer |\n| [[Empty]] |  |\n")
        assert load_descriptions(path) == {"nora castell": "Treasurer"}


class TestAttendeeCandidates:
    def _registry(self, tmp_path):
        return make_registry(tmp_path, [
            ("a.md", "2026-01-01", [ent("Google", "[[Google]]", "org")], ["[[Nora Cooke]]", "[[Mira Solberg]]"]),
        ])

    def test_unrelated_word_does_not_match_part_of_an_attendee_name(self, tmp_path):
        ev = self._registry(tmp_path).evidence("Google", ["[[Nora Cooke]]"])
        assert "[[Nora Cooke]]" not in [c.link for c in ev.candidates]

    def test_misheard_attendee_still_matches(self, tmp_path):
        ev = self._registry(tmp_path).evidence("Myra", ["[[Mira Solberg]]"])
        assert "[[Mira Solberg]]" in [c.link for c in ev.candidates]
