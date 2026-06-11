import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
HANDOFF_ROOT = Path(__file__).resolve().parents[3]
RAW_ROOT = HANDOFF_ROOT.parent / "AILAB_SOURCES_RAW" / "canterville_ghost"
sys.path.insert(0, str(BACKEND_ROOT))


def synthetic_candidate(parts: list[dict], source_format: str = "txt") -> dict:
    from services.structure_normalizer import _part_hash, _source_fingerprint

    normalized = []
    for index, part in enumerate(parts):
        text = part["text"]
        item = {
            "index": index,
            "source_ref": part.get("source_ref", {}),
            "text": text,
            "hash": _part_hash(text),
            "n_lines": part.get("n_lines", 1),
            "is_heading_candidate": part.get("is_heading_candidate", False),
        }
        if "spine_index" in part:
            item["spine_index"] = part["spine_index"]
        normalized.append(item)
    return {
        "doc_id": "synthetic",
        "source_format": source_format,
        "pipeline_version": "test",
        "source_path": "",
        "parts": normalized,
        "source_fingerprint": _source_fingerprint(normalized),
        "parser_report": {},
    }


class StructureNormalizerTest(unittest.TestCase):
    def canterville_txt_plan(self, candidate: dict) -> dict:
        return {
            "doc_id": "canterville_ghost",
            "source_fingerprint": candidate["source_fingerprint"],
            "drop_parts": [
                *[{"part_index": index, "reason": "title_page"} for index in range(1, 6)],
                {"part_index": 0, "reason": "front_matter"},
                {"part_index": 6, "reason": "imprint"},
                {"part_index": 7, "reason": "imprint"},
                *[{"part_index": index, "reason": "front_matter"} for index in range(8, 26)],
            ],
            "chapter_headings": [
                {"part_index": 26, "title": "I"},
                {"part_index": 49, "title": "II"},
                {"part_index": 56, "title": "III"},
                {"part_index": 69, "title": "IV"},
                {"part_index": 80, "title": "V"},
                {"part_index": 117, "title": "VI"},
                {"part_index": 132, "title": "VII"},
            ],
            "merge_parts": [],
            "epub_section_roles": [],
            "flags": [],
            "confidence": 0.9,
            "notes": "Manual fixture for Phase 0.",
        }

    def test_canterville_txt_candidate_parts_and_baseline(self):
        from services.extraction import split_txt
        from services.structure_normalizer import build_candidate_parts

        source = RAW_ROOT / "source.txt"
        candidate = build_candidate_parts(source, doc_id="canterville_ghost")
        self.assertEqual(candidate["source_format"], "txt")
        self.assertEqual(len(candidate["parts"]), 151)
        self.assertEqual(candidate["parts"][26]["text"], "I")
        self.assertEqual(candidate["parts"][49]["text"], "II")
        self.assertEqual(candidate["parts"][132]["text"], "VII")

        report = {"skipped": []}
        chapters = split_txt(source.read_text(encoding="utf-8"), report)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0]["title"], "Chapter 1")
        self.assertEqual(report["toc"]["toc_source"], "none")
        self.assertTrue(report["toc"]["fallback_used"])

    def test_canterville_txt_manual_plan_normalizes_to_seven_chapters(self):
        from services.structure_normalizer import apply_plan, build_candidate_parts, normalized_to_document

        source = RAW_ROOT / "source.txt"
        candidate = build_candidate_parts(source, doc_id="canterville_ghost")
        plan = self.canterville_txt_plan(candidate)
        with tempfile.TemporaryDirectory() as tmp:
            result = apply_plan(candidate, plan, tmp)

        normalized = result["normalized_document"]
        self.assertEqual([chapter["title"] for chapter in normalized["chapters"]], ["I", "II", "III", "IV", "V", "VI", "VII"])
        expected_ranges = [(27, 48), (50, 55), (57, 68), (70, 79), (81, 116), (118, 131), (133, 150)]
        for chapter, (start, end) in zip(normalized["chapters"], expected_ranges):
            refs = [block["source_part_refs"][0] for block in chapter["blocks"]]
            self.assertEqual(refs[0], start)
            self.assertEqual(refs[-1], end)
            for block in chapter["blocks"]:
                part_text = candidate["parts"][block["source_part_refs"][0]]["text"]
                self.assertEqual(block["text"], part_text)

        report = result["normalization_report"]
        self.assertFalse(report["low_confidence"])
        self.assertFalse(report["needs_human_check"])
        self.assertLess(report["drop_fraction"], 0.30)
        self.assertEqual(len(report["dropped_parts"]), 26)

        document = normalized_to_document("canterville_ghost", {"title": "The Canterville Ghost"}, normalized)
        self.assertEqual(len(document["chapters"]), 7)
        self.assertEqual(document["chapters"][0]["blocks"][0]["block_type"], "heading")

    def test_canterville_epub_manual_plan_drops_front_and_back_matter(self):
        from services.extraction import split_epub
        from services.structure_normalizer import apply_plan, build_candidate_parts

        source = RAW_ROOT / "source.epub"
        baseline_report = {"skipped": []}
        baseline_chapters = split_epub(source, baseline_report)
        self.assertEqual([chapter["title"] for chapter in baseline_chapters], ["WILDE", "VI", "VII"])
        self.assertTrue(baseline_report["toc"]["low_confidence"])

        candidate = build_candidate_parts(source, doc_id="canterville_ghost")
        self.assertEqual(candidate["source_format"], "epub")
        self.assertEqual(len(candidate["parts"]), 166)
        plan = {
            "doc_id": "canterville_ghost",
            "source_fingerprint": candidate["source_fingerprint"],
            "drop_parts": [{"part_index": index, "reason": "front_matter"} for index in range(0, 31)],
            "chapter_headings": [
                {"part_index": 31, "title": "I"},
                {"part_index": 54, "title": "II"},
                {"part_index": 61, "title": "III"},
                {"part_index": 73, "title": "IV"},
                {"part_index": 84, "title": "V"},
                {"part_index": 126, "title": "VI"},
                {"part_index": 141, "title": "VII"},
            ],
            "merge_parts": [],
            "epub_section_roles": [{"spine_index": 3, "role": "back_matter", "reason": "gutenberg_license"}],
            "flags": [],
            "confidence": 0.85,
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = apply_plan(candidate, plan, tmp)

        normalized = result["normalized_document"]
        self.assertEqual([chapter["title"] for chapter in normalized["chapters"]], ["I", "II", "III", "IV", "V", "VI", "VII"])
        body_text = "\n".join(block["text"] for chapter in normalized["chapters"] for block in chapter["blocks"])
        self.assertNotIn("THE FULL PROJECT GUTENBERG", body_text)
        self.assertNotIn("LIST OF ILLUSTRATIONS", body_text)
        self.assertFalse(result["normalization_report"]["low_confidence"])
        self.assertFalse(result["normalization_report"]["needs_human_check"])

    def test_rejects_invalid_fingerprint(self):
        from services.structure_normalizer import validate_plan

        candidate = synthetic_candidate([{"text": "I"}, {"text": "Body"}])
        plan = {"doc_id": "synthetic", "source_fingerprint": "wrong", "chapter_headings": [], "drop_parts": [], "merge_parts": [], "confidence": 0.9}
        result = validate_plan(candidate, plan)
        self.assertFalse(result["ok"])
        self.assertIn("fingerprint", result["errors"][0]["location"])

    def test_rejects_drop_heading_conflict(self):
        from services.structure_normalizer import validate_plan

        candidate = synthetic_candidate([{"text": "I"}, {"text": "Body"}])
        plan = {
            "doc_id": "synthetic",
            "source_fingerprint": candidate["source_fingerprint"],
            "drop_parts": [{"part_index": 0, "reason": "front_matter"}],
            "chapter_headings": [{"part_index": 0, "title": "I"}],
            "merge_parts": [],
            "confidence": 0.9,
        }
        result = validate_plan(candidate, plan)
        self.assertFalse(result["ok"])

    def test_rejects_invalid_separator(self):
        from services.structure_normalizer import validate_plan

        candidate = synthetic_candidate([{"text": "I"}, {"text": "Body A"}, {"text": "Body B"}])
        plan = {
            "doc_id": "synthetic",
            "source_fingerprint": candidate["source_fingerprint"],
            "drop_parts": [],
            "chapter_headings": [{"part_index": 0, "title": "I"}],
            "merge_parts": [{"part_indices": [1, 2], "separator": "\n"}],
            "confidence": 0.9,
        }
        result = validate_plan(candidate, plan)
        self.assertFalse(result["ok"])

    def test_drop_fraction_guard_flags_without_silent_pass(self):
        from services.structure_normalizer import apply_plan

        candidate = synthetic_candidate([
            {"text": "Very long front matter " * 20},
            {"text": "I"},
            {"text": "Body"},
        ])
        plan = {
            "doc_id": "synthetic",
            "source_fingerprint": candidate["source_fingerprint"],
            "drop_parts": [{"part_index": 0, "reason": "front_matter"}],
            "chapter_headings": [{"part_index": 1, "title": "I"}],
            "merge_parts": [],
            "confidence": 0.9,
        }
        with tempfile.TemporaryDirectory() as tmp:
            report = apply_plan(candidate, plan, tmp)["normalization_report"]
        self.assertTrue(report["low_confidence"])
        self.assertTrue(report["needs_human_check"])
        self.assertEqual(report["dropped_parts"][0]["part_index"], 0)

    def test_part_before_first_heading_is_preserved_and_flagged(self):
        from services.structure_normalizer import apply_plan

        candidate = synthetic_candidate([
            {"text": "Preface that should not disappear."},
            {"text": "I"},
            {"text": "Body"},
        ])
        plan = {
            "doc_id": "synthetic",
            "source_fingerprint": candidate["source_fingerprint"],
            "drop_parts": [],
            "chapter_headings": [{"part_index": 1, "title": "I"}],
            "merge_parts": [],
            "confidence": 0.9,
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = apply_plan(candidate, plan, tmp)
        self.assertTrue(result["normalization_report"]["needs_human_check"])
        self.assertEqual(result["normalized_document"]["chapters"][0]["blocks"][0]["source_part_refs"], [0])

    def test_empty_headings_fallback_preserves_content(self):
        from services.structure_normalizer import apply_plan

        candidate = synthetic_candidate([{"text": "One"}, {"text": "Two"}])
        plan = {
            "doc_id": "synthetic",
            "source_fingerprint": candidate["source_fingerprint"],
            "drop_parts": [],
            "chapter_headings": [],
            "merge_parts": [],
            "confidence": 0.9,
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = apply_plan(candidate, plan, tmp)
        self.assertTrue(result["normalization_report"]["needs_human_check"])
        self.assertEqual(len(result["normalized_document"]["chapters"]), 1)
        refs = [block["source_part_refs"][0] for block in result["normalized_document"]["chapters"][0]["blocks"]]
        self.assertEqual(refs, [0, 1])

    def test_epub_role_precedence_drops_spine_before_part_rules(self):
        from services.structure_normalizer import apply_plan

        candidate = synthetic_candidate([
            {"text": "Title page", "spine_index": 0},
            {"text": "I", "spine_index": 1},
            {"text": "Body", "spine_index": 1},
        ], source_format="epub")
        plan = {
            "doc_id": "synthetic",
            "source_fingerprint": candidate["source_fingerprint"],
            "drop_parts": [],
            "chapter_headings": [{"part_index": 1, "title": "I"}],
            "merge_parts": [],
            "epub_section_roles": [{"spine_index": 0, "role": "front_matter", "reason": "title_page"}],
            "confidence": 0.9,
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = apply_plan(candidate, plan, tmp)
        self.assertEqual(result["normalization_report"]["dropped_parts"][0]["via"], "epub_section_roles")
        self.assertEqual(result["normalized_document"]["chapters"][0]["blocks"][0]["source_part_refs"], [2])


if __name__ == "__main__":
    unittest.main()
