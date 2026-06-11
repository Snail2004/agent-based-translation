import os
import sys
import tempfile
import unittest
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


class BackendApiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["THESIS_TOOL_PROJECTS_ROOT"] = cls.tmp.name
        backend_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(backend_root))

        from app import create_app

        cls.client = create_app().test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        os.environ.pop("THESIS_TOOL_PROJECTS_ROOT", None)

    def test_health(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def _split_txt_direct(self, source: str) -> tuple[list[dict], dict]:
        from services.extraction import split_txt

        report = {"skipped": []}
        chapters = split_txt(source, report)
        return chapters, report

    def _make_txt_project(self, doc_id: str, source: bytes | None = None) -> dict:
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": doc_id,
                "author": "Tester",
                "source_format": "txt",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (BytesIO(source or b"Chapter 1\n\nAlice arrived.\n\nBob waited."), "source.txt")},
            content_type="multipart/form-data",
        )
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False, "user": "tester"})
        self.assertEqual(extract.status_code, 201)
        return self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]

    def _project_root(self, doc_id: str) -> Path:
        return Path(self.tmp.name) / doc_id

    def _assert_project_valid(self, doc_id: str) -> None:
        validate = self.client.post(f"/api/projects/{doc_id}/validate")
        self.assertEqual(validate.status_code, 200)
        payload = validate.get_json()
        self.assertTrue(payload["data"]["ok"], payload["data"].get("errors"))

    def _extraction_report(self, doc_id: str) -> dict:
        return json.loads((self._project_root(doc_id) / "working" / "extraction_report.json").read_text(encoding="utf-8"))

    def _jsonl_rows(self, doc_id: str, filename: str) -> list[dict]:
        path = self._project_root(doc_id) / "canonical" / filename
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _assert_toc_report(self, report: dict) -> None:
        self.assertIn("toc", report)
        for key in ("toc_source", "toc_items", "chapters_matched", "match_rate", "fallback_used", "low_confidence"):
            self.assertIn(key, report["toc"])

    def _assert_chapter_properties(self, dataset: dict) -> None:
        previous_order = 0
        boilerplate = ("INCIDENTAL DAMAGES", "PROJECT GUTENBERG", "FULL PROJECT GUTENBERG")
        blocks_by_chapter: dict[str, list[dict]] = {}
        for block in dataset["blocks"]:
            blocks_by_chapter.setdefault(block["chapter_id"], []).append(block)
        for chapter in dataset["chapters"]:
            self.assertTrue(chapter["title"].strip())
            self.assertGreaterEqual(len(blocks_by_chapter.get(chapter["chapter_id"], [])), 1)
            self.assertGreater(chapter["order_index"], previous_order)
            previous_order = chapter["order_index"]
            self.assertFalse(any(phrase in chapter["title"].upper() for phrase in boilerplate))

    def _make_unextracted_txt_project(self, doc_id: str, source: bytes) -> None:
        create = self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": doc_id,
                "author": "Tester",
                "source_format": "txt",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        self.assertEqual(create.status_code, 201)
        upload = self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (BytesIO(source), "source.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201)

    def _minimal_structure_plan(self, doc_id: str, fingerprint: str) -> dict:
        return {
            "doc_id": doc_id,
            "source_fingerprint": fingerprint,
            "drop_parts": [{"part_index": 0, "reason": "title_page"}],
            "chapter_headings": [
                {"part_index": 1, "title": "I"},
                {"part_index": 3, "title": "II"},
            ],
            "merge_parts": [],
            "epub_section_roles": [],
            "flags": [],
            "confidence": 0.95,
            "notes": "Synthetic API fixture.",
        }

    def _annotation_fixture(self, doc_id: str) -> tuple[dict, dict, dict, dict]:
        dataset = self._make_txt_project(
            doc_id,
            b"Chapter 1\n\nAlice saw Bob.\n\nAlice smiled at Bob.\n\nAlice saw Alice.",
        )
        chapter = dataset["chapters"][0]
        blocks = [block for block in dataset["blocks"] if block["block_type"] != "heading"]
        by_text = {block["clean_text"]: block for block in blocks}
        first = by_text["Alice saw Bob."]
        second = by_text["Alice smiled at Bob."]
        third = by_text["Alice saw Alice."]
        return dataset, chapter, first, second, third

    def _good_annotation_candidate(self, doc_id: str, chapter_id: str, first: dict, second: dict) -> dict:
        return {
            "doc_id": doc_id,
            "chapter_id": chapter_id,
            "entity_candidates": [
                {
                    "existing_entity_id": None,
                    "entity_key": "alice",
                    "canonical_source": "Alice",
                    "suggested_canonical_target": "Alice",
                    "entity_type": "person",
                    "gender": "female",
                    "aliases_source": [],
                    "aliases_target": [],
                    "pronoun_policy": "",
                    "mentions": [
                        {
                            "block_id": first["block_id"],
                            "surface": "Alice",
                            "left_context": "",
                            "right_context": " saw",
                        },
                        {
                            "block_id": second["block_id"],
                            "surface": "Alice",
                            "left_context": "",
                            "right_context": " smiled",
                        },
                    ],
                    "confidence": 0.95,
                },
                {
                    "existing_entity_id": None,
                    "entity_key": "bob",
                    "canonical_source": "Bob",
                    "suggested_canonical_target": "Bob",
                    "entity_type": "person",
                    "gender": "male",
                    "aliases_source": [],
                    "aliases_target": [],
                    "pronoun_policy": "",
                    "mentions": [
                        {
                            "block_id": first["block_id"],
                            "surface": "Bob",
                            "left_context": "saw ",
                            "right_context": ".",
                        },
                        {
                            "block_id": second["block_id"],
                            "surface": "Bob",
                            "left_context": "at ",
                            "right_context": ".",
                        },
                    ],
                    "confidence": 0.95,
                },
            ],
            "glossary_candidates": [
                {
                    "existing_term_id": None,
                    "term_key": "smiled",
                    "source_term": "smiled",
                    "suggested_expected_target": "mim cuoi",
                    "suggested_allowed_variants": ["smiled"],
                    "suggested_forbidden_variants": [],
                    "occurrences": [
                        {
                            "block_id": second["block_id"],
                            "surface": "smiled",
                            "left_context": "Alice ",
                            "right_context": " at",
                        }
                    ],
                    "confidence": 0.9,
                }
            ],
            "discourse_candidates": [
                {
                    "block_id": first["block_id"],
                    "speaker_ref": "alice",
                    "addressee_ref": "bob",
                    "confidence": 0.8,
                }
            ],
            "relation_candidates": [
                {
                    "existing_relation_id": None,
                    "relation_key": "alice_bob_friend",
                    "source_ref": "alice",
                    "target_ref": "bob",
                    "relation_type": "friend",
                    "suggested_address_policy": {
                        "source_to_target": {"self_term": "toi", "address_term": "ban"},
                        "target_to_source": {"self_term": "toi", "address_term": "ban"},
                    },
                    "evidence": [
                        {
                            "block_id": first["block_id"],
                            "surface": "Alice saw Bob.",
                        }
                    ],
                    "reason": "Synthetic fixture relation for address-policy apply.",
                    "confidence": 0.87,
                }
            ],
            "summary_candidate": {
                "summary_source": "Alice sees Bob and smiles at him.",
                "characters_present_refs": ["alice", "bob"],
                "key_events": ["Alice sees Bob", "Alice smiles at Bob"],
                "setting": "A simple test scene",
                "emotional_tone": "plain",
                "motifs": ["meeting"],
                "open_threads": ["Whether Alice and Bob will keep interacting"],
                "confidence": 0.85,
            },
            "confidence": 0.9,
        }

    def _good_translation_preview(self, doc_id: str, chapter_id: str, block_id: str) -> dict:
        return {
            "doc_id": doc_id,
            "chapter_id": chapter_id,
            "model": "test-model",
            "skill_version": "dataset-translation-preview@1",
            "prompt_version": "test-prompt",
            "blocks": [
                {
                    "block_id": block_id,
                    "target_text": "Alice saw Bob.",
                    "mentions": [
                        {"entity_id": "e_001", "source_surface": "Alice", "target_surface": "Alice"},
                        {"entity_id": "e_002", "source_surface": "Bob", "target_surface": "Bob"},
                    ],
                    "address_applied": None,
                    "used_context": [chapter_id],
                    "notes": "Synthetic translation preview fixture.",
                }
            ],
        }

    def _assert_no_preview_input_span_keys(self, value):
        if isinstance(value, dict):
            for key, child in value.items():
                self.assertNotIn(key, {"span", "start", "end", "mentions", "occurrences"})
                self._assert_no_preview_input_span_keys(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_no_preview_input_span_keys(child)

    def _minimal_epub(self, opf: str, files: dict[str, str]) -> BytesIO:
        epub = BytesIO()
        with ZipFile(epub, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""")
            zf.writestr("OPS/content.opf", opf)
            for name, content in files.items():
                zf.writestr(f"OPS/{name}", content)
        epub.seek(0)
        return epub

    def test_seed_project_and_dataset(self):
        projects = self.client.get("/api/projects").get_json()
        self.assertTrue(projects["ok"])
        self.assertTrue(any(project["doc_id"] == "gold_demo_01" for project in projects["data"]))

        dataset = self.client.get("/api/projects/gold_demo_01/dataset").get_json()
        self.assertTrue(dataset["ok"])
        self.assertEqual(len(dataset["data"]["blocks"]), 14)
        self.assertEqual(len(dataset["data"]["chapters"]), 2)
        self.assertIn("reference_drafts", dataset["data"])
        self.assertIn("jobs", dataset["data"])
        self.assertIn("entity_relations", dataset["data"])
        self.assertEqual(dataset["data"]["entity_relations"][0]["relation_id"], "rel_001")

    def test_project_settings_note_only(self):
        doc_id = "project_settings"
        create = self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {"title": "Original Title", "license": "public-domain", "contamination_risk": "low"},
        })
        self.assertEqual(create.status_code, 201)

        update = self.client.patch(f"/api/projects/{doc_id}", json={
            "note": "Assigned to member A. Source looks clean.",
            "user": "tester",
        })
        self.assertEqual(update.status_code, 200)
        state = update.get_json()["data"]
        self.assertEqual(state["title"], "Original Title")
        self.assertEqual(state["note"], "Assigned to member A. Source looks clean.")

        projects = self.client.get("/api/projects").get_json()["data"]
        project = next(item for item in projects if item["doc_id"] == doc_id)
        self.assertEqual(project["title"], "Original Title")
        self.assertEqual(project["note"], "Assigned to member A. Source looks clean.")

        title_patch = self.client.patch(f"/api/projects/{doc_id}", json={"title": "Wrong route"})
        self.assertEqual(title_patch.status_code, 400)

    def test_normalize_candidate_plan_preview_and_apply(self):
        doc_id = "normalize_two_chapters"
        source = (
            b"Title.\n\nI\n\nFirst chapter body has enough text for coverage.\n\n"
            b"II\n\nSecond chapter body has enough text for coverage."
        )
        self._make_unextracted_txt_project(doc_id, source)

        candidate_response = self.client.post(f"/api/projects/{doc_id}/normalize/candidate-parts")
        self.assertEqual(candidate_response.status_code, 201)
        candidate = candidate_response.get_json()["data"]
        self.assertEqual(candidate["source_format"], "txt")
        self.assertEqual(len(candidate["parts"]), 5)
        self.assertEqual(candidate["parts"][1]["text"], "I")
        self.assertTrue(self._project_root(doc_id).joinpath("working", "normalized", "candidate_parts.json").exists())
        self.assertEqual(
            candidate["paths"]["candidate_parts"],
            str(self._project_root(doc_id).joinpath("working", "normalized", "candidate_parts.json").resolve()),
        )
        self.assertEqual(
            candidate["paths"]["agent_structure_plan"],
            str(self._project_root(doc_id).joinpath("working", "normalized", "agent_structure_plan.json").resolve()),
        )
        self.assertEqual(candidate["paths"]["project_root"], str(self._project_root(doc_id).resolve()))
        self.assertIn("normalization_history", candidate["paths"])
        self.assertTrue(candidate["normalizer"]["candidate_built"])
        self.assertEqual(candidate["normalizer"]["last_event"], "candidate_built")
        self.assertTrue(
            self._project_root(doc_id)
            .joinpath("working", "normalized", "normalization_history.jsonl")
            .exists()
        )

        plan = self._minimal_structure_plan(doc_id, candidate["source_fingerprint"])
        agent_plan_path = self._project_root(doc_id).joinpath("working", "normalized", "agent_structure_plan.json")
        agent_plan_path.write_text(json.dumps(plan), encoding="utf-8")
        agent_plan_response = self.client.get(f"/api/projects/{doc_id}/normalize/agent-plan")
        self.assertEqual(agent_plan_response.status_code, 200)
        agent_plan_payload = agent_plan_response.get_json()["data"]
        self.assertEqual(agent_plan_payload["plan"]["source_fingerprint"], candidate["source_fingerprint"])
        self.assertEqual(agent_plan_payload["path"], str(agent_plan_path.resolve()))

        preview_response = self.client.post(f"/api/projects/{doc_id}/normalize/plan", json={"plan": plan})
        self.assertEqual(preview_response.status_code, 201)
        preview = preview_response.get_json()["data"]["preview"]
        self.assertEqual([chapter["title"] for chapter in preview["chapters"]], ["I", "II"])
        self.assertEqual(preview["body_coverage"], "2/2")
        self.assertTrue(preview["content_invariance_ok"])
        self.assertFalse(preview["low_confidence"])
        self.assertFalse(self._project_root(doc_id).joinpath("canonical", "document.json").exists())
        project_state = self.client.get(f"/api/projects/{doc_id}").get_json()["data"]
        self.assertTrue(project_state["normalizer"]["plan_imported"])
        self.assertTrue(project_state["normalizer"]["normalized_preview_available"])
        self.assertEqual(project_state["normalizer"]["last_event"], "plan_imported")
        self.assertEqual(project_state["normalizer"]["chapters"], 2)

        apply_response = self.client.post(f"/api/projects/{doc_id}/normalize/apply", json={
            "approved": True,
            "user": "tester",
        })
        self.assertEqual(apply_response.status_code, 201)
        job = apply_response.get_json()["data"]
        self.assertEqual(job["type"], "normalize_extract")

        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertEqual([chapter["title"] for chapter in dataset["chapters"]], ["I", "II"])
        self.assertEqual(len(dataset["blocks"]), 4)
        self._assert_project_valid(doc_id)

        report = json.loads(
            self._project_root(doc_id)
            .joinpath("working", "normalized", "normalization_report.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(report["ai_provenance"]["prompt_id"], "source-structure-normalizer-v1")
        self.assertFalse(report["ai_provenance"]["tool_called_llm"])
        project_state = self.client.get(f"/api/projects/{doc_id}").get_json()["data"]
        self.assertTrue(project_state["normalizer"]["applied"])
        self.assertEqual(project_state["normalizer"]["last_event"], "applied")
        self.assertEqual(project_state["normalizer"]["blocks"], 4)
        self.assertEqual(
            [event["event"] for event in project_state["normalizer"]["history"]],
            ["candidate_built", "plan_imported", "applied"],
        )

    def test_normalize_invalid_plan_does_not_write_document(self):
        doc_id = "normalize_bad_plan"
        self._make_unextracted_txt_project(doc_id, b"Front.\n\nI\n\nBody.")
        candidate = self.client.post(f"/api/projects/{doc_id}/normalize/candidate-parts").get_json()["data"]
        plan = self._minimal_structure_plan(doc_id, candidate["source_fingerprint"])
        plan["source_fingerprint"] = "wrong:fingerprint"

        invalid = self.client.post(f"/api/projects/{doc_id}/normalize/plan", json={"plan": plan})
        self.assertEqual(invalid.status_code, 400)
        payload = invalid.get_json()
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["errors"])
        self.assertFalse(self._project_root(doc_id).joinpath("canonical", "document.json").exists())

    def test_normalize_apply_requires_approval_and_preview(self):
        doc_id = "normalize_requires_approval"
        self._make_unextracted_txt_project(doc_id, b"I\n\nBody.")

        no_preview = self.client.post(f"/api/projects/{doc_id}/normalize/apply", json={
            "approved": True,
            "user": "tester",
        })
        self.assertEqual(no_preview.status_code, 400)

        candidate = self.client.post(f"/api/projects/{doc_id}/normalize/candidate-parts").get_json()["data"]
        plan = {
            "doc_id": doc_id,
            "source_fingerprint": candidate["source_fingerprint"],
            "drop_parts": [],
            "chapter_headings": [{"part_index": 0, "title": "I"}],
            "merge_parts": [],
            "epub_section_roles": [],
            "flags": [],
            "confidence": 0.95,
            "notes": "Approval guard fixture.",
        }
        preview = self.client.post(f"/api/projects/{doc_id}/normalize/plan", json={"plan": plan})
        self.assertEqual(preview.status_code, 201)

        unapproved = self.client.post(f"/api/projects/{doc_id}/normalize/apply", json={
            "approved": False,
            "user": "tester",
        })
        self.assertEqual(unapproved.status_code, 400)
        self.assertFalse(self._project_root(doc_id).joinpath("canonical", "document.json").exists())

    def test_normalize_default_extract_path_is_unchanged(self):
        doc_id = "normalize_default_extract"
        self._make_unextracted_txt_project(doc_id, b"Chapter 1\n\nDefault extract body.")
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False, "user": "tester"})
        self.assertEqual(extract.status_code, 201)
        report = self._extraction_report(doc_id)
        self.assertNotIn("normalized_structure", report)
        self.assertFalse(self._project_root(doc_id).joinpath("working", "normalized", "candidate_parts.json").exists())

    def test_translation_preview_import_list_and_load(self):
        doc_id = "translation_preview_happy"
        dataset = self._make_txt_project(doc_id)
        chapter_id = dataset["chapters"][0]["chapter_id"]
        block = next(item for item in dataset["blocks"] if item["block_type"] != "heading")
        preview = self._good_translation_preview(doc_id, chapter_id, block["block_id"])

        response = self.client.post(f"/api/projects/{doc_id}/translation-preview/runs", json={"preview": preview})
        self.assertEqual(response.status_code, 201, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["warnings"], [])
        run = payload["data"]["run"]
        run_id = run["run_id"]
        self.assertEqual(run["kind"], "translation_preview")
        self.assertEqual(run["doc_id"], doc_id)
        self.assertEqual(run["chapter_id"], chapter_id)
        self.assertEqual(run["blocks"][0]["target_text"], "Alice saw Bob.")
        self.assertEqual(run["blocks"][0]["used_context"], [chapter_id])
        self.assertTrue(
            self._project_root(doc_id)
            .joinpath("working", "translation_preview", "runs", f"{run_id}.json")
            .exists()
        )
        self.assertTrue(
            self._project_root(doc_id)
            .joinpath("working", "translation_preview", "index.json")
            .exists()
        )
        self.assertFalse(self._project_root(doc_id).joinpath("canonical", "translation_preview").exists())

        listed = self.client.get(f"/api/projects/{doc_id}/translation-preview/runs")
        self.assertEqual(listed.status_code, 200)
        listed_runs = listed.get_json()["data"]["runs"]
        self.assertEqual(len(listed_runs), 1)
        self.assertEqual(listed_runs[0]["run_id"], run_id)
        self.assertEqual(listed_runs[0]["block_count"], 1)

        loaded = self.client.get(f"/api/projects/{doc_id}/translation-preview/runs/{run_id}")
        self.assertEqual(loaded.status_code, 200)
        loaded_run = loaded.get_json()["data"]["run"]
        self.assertEqual(loaded_run["run_id"], run_id)
        self.assertEqual(loaded_run["blocks"][0]["block_id"], block["block_id"])

    def test_export_package_includes_project_state_and_qc_without_previews(self):
        doc_id = "export_dataset_package"
        dataset = self._make_txt_project(doc_id)
        chapter_id = dataset["chapters"][0]["chapter_id"]
        block = next(item for item in dataset["blocks"] if item["block_type"] != "heading")
        preview = self._good_translation_preview(doc_id, chapter_id, block["block_id"])

        input_response = self.client.get(f"/api/projects/{doc_id}/translation-preview/input?chapter_id={chapter_id}")
        self.assertEqual(input_response.status_code, 200, input_response.get_json())
        imported = self.client.post(f"/api/projects/{doc_id}/translation-preview/runs", json={"preview": preview})
        self.assertEqual(imported.status_code, 201, imported.get_json())

        exported = self.client.post(f"/api/projects/{doc_id}/export", json={"user": "tester"})
        self.assertEqual(exported.status_code, 201, exported.get_json())
        data = exported.get_json()["data"]
        self.assertEqual(data["manifest_data"]["kind"], "dataset_package")
        self.assertTrue(data["manifest_data"]["qc_report"]["included"])
        self.assertFalse(data["manifest_data"]["translation_preview"]["included"])
        zip_path = Path(data["zip"])
        self.assertTrue(zip_path.exists())

        with ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            self.assertIn("raw/source.txt", names)
            self.assertIn("canonical/document.json", names)
            self.assertIn("working/review_state.json", names)
            self.assertIn("working/drafts.json", names)
            self.assertIn("QC_REPORT.json", names)
            self.assertIn("README_EXPORT.txt", names)
            self.assertFalse(any(name.startswith("working/translation_preview/") for name in names))
            qc = json.loads(zf.read("QC_REPORT.json").decode("utf-8"))
            self.assertEqual(qc["doc_id"], doc_id)

        downloaded = self.client.get(f"/api/projects/{doc_id}/exports/{data['filename']}")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.mimetype, "application/zip")
        downloaded.close()

    def test_export_with_translation_previews_includes_preview_files_and_downloads(self):
        doc_id = "export_with_previews"
        dataset = self._make_txt_project(doc_id)
        chapter_id = dataset["chapters"][0]["chapter_id"]
        block = next(item for item in dataset["blocks"] if item["block_type"] != "heading")
        preview = self._good_translation_preview(doc_id, chapter_id, block["block_id"])

        input_response = self.client.get(f"/api/projects/{doc_id}/translation-preview/input?chapter_id={chapter_id}")
        self.assertEqual(input_response.status_code, 200, input_response.get_json())

        imported = self.client.post(f"/api/projects/{doc_id}/translation-preview/runs", json={"preview": preview})
        self.assertEqual(imported.status_code, 201, imported.get_json())
        run_id = imported.get_json()["data"]["run"]["run_id"]

        exported = self.client.post(f"/api/projects/{doc_id}/export-with-previews", json={"user": "tester"})
        self.assertEqual(exported.status_code, 201, exported.get_json())
        data = exported.get_json()["data"]
        self.assertEqual(data["manifest_data"]["kind"], "dataset_plus_translation_previews")
        self.assertTrue(data["manifest_data"]["translation_preview"]["preview_only_not_gold"])
        self.assertEqual(data["manifest_data"]["translation_preview"]["counts"]["inputs"], 1)
        self.assertEqual(data["manifest_data"]["translation_preview"]["counts"]["runs"], 1)
        zip_path = Path(data["zip"])
        self.assertTrue(zip_path.exists())

        with ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            self.assertIn("raw/source.txt", names)
            self.assertIn("canonical/document.json", names)
            self.assertIn("working/review_state.json", names)
            self.assertIn("QC_REPORT.json", names)
            self.assertIn(f"working/translation_preview/input/{chapter_id}_input.json", names)
            self.assertIn(f"working/translation_preview/runs/{run_id}.json", names)
            self.assertIn("README_EXPORT.txt", names)

        downloaded = self.client.get(f"/api/projects/{doc_id}/exports/{data['filename']}")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.mimetype, "application/zip")
        self.assertGreater(len(downloaded.data), 100)
        downloaded.close()

    def test_translation_preview_bad_block_id_rejects(self):
        doc_id = "translation_preview_bad_block"
        dataset = self._make_txt_project(doc_id)
        chapter_id = dataset["chapters"][0]["chapter_id"]
        preview = self._good_translation_preview(doc_id, chapter_id, "missing_block")

        response = self.client.post(f"/api/projects/{doc_id}/translation-preview/runs", json={"preview": preview})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["errors"][0]["code"], "unknown_block_id")
        self.assertFalse(
            self._project_root(doc_id)
            .joinpath("working", "translation_preview", "runs")
            .exists()
        )

    def test_translation_preview_unknown_used_context_warns(self):
        doc_id = "translation_preview_unknown_context"
        dataset = self._make_txt_project(doc_id)
        chapter_id = dataset["chapters"][0]["chapter_id"]
        block = next(item for item in dataset["blocks"] if item["block_type"] != "heading")
        preview = self._good_translation_preview(doc_id, chapter_id, block["block_id"])
        preview["blocks"][0]["used_context"] = [chapter_id, "made_up_context"]

        response = self.client.post(f"/api/projects/{doc_id}/translation-preview/runs", json={"preview": preview})
        self.assertEqual(response.status_code, 201, response.get_json())
        warnings = response.get_json()["warnings"]
        self.assertTrue(any(item["code"] == "unknown_used_context" for item in warnings))
        run = response.get_json()["data"]["run"]
        self.assertEqual(run["blocks"][0]["used_context"], [chapter_id, "made_up_context"])
        self.assertTrue(any(item["code"] == "unknown_used_context" for item in run["warnings"]))

    def test_translation_preview_input_bundle(self):
        response = self.client.get("/api/projects/gold_demo_01/translation-preview/input?chapter_id=gold_demo_01_ch01")
        self.assertEqual(response.status_code, 200, response.get_json())
        bundle = response.get_json()["data"]
        self.assertEqual(bundle["doc_id"], "gold_demo_01")
        self.assertEqual(bundle["chapter_id"], "gold_demo_01_ch01")
        input_path = self._project_root("gold_demo_01").joinpath(
            "working", "translation_preview", "input", "gold_demo_01_ch01_input.json"
        )
        self.assertTrue(input_path.exists())
        self.assertEqual(bundle["paths"]["input"], str(input_path.resolve()))
        self.assertIn("preview_output_suggested", bundle["paths"])
        self.assertEqual(len(bundle["blocks"]), 7)
        self.assertTrue(all(block["block_id"].startswith("gold_demo_01_ch01_") for block in bundle["blocks"]))

        b004 = next(block for block in bundle["blocks"] if block["block_id"] == "gold_demo_01_ch01_b004")
        self.assertEqual(b004["discourse"]["speaker_entity_id"], "e_001")
        self.assertEqual(b004["discourse"]["addressee_entity_id"], "e_002")

        entity = next(row for row in bundle["known_entities"] if row["entity_id"] == "e_002")
        self.assertEqual(entity["canonical_target"], "Người Giữ Đồng Hồ")
        self.assertNotIn("mentions", entity)

        term = next(row for row in bundle["known_terms"] if row["term_id"] == "g_001")
        self.assertEqual(term["forbidden_variants"], ["sự xoay", "vòng quay"])
        self.assertNotIn("occurrences", term)

        relation = next(row for row in bundle["known_relations"] if row["relation_id"] == "rel_001")
        self.assertEqual(relation["address_policy"]["target_to_source"]["address_term"], "ông")
        self.assertNotIn("evidence", relation)

        self.assertIn("emotional_tone", bundle["chapter_summary"])
        self.assertIsInstance(bundle["chapter_summary"]["motifs"], list)
        self.assertIsInstance(bundle["chapter_summary"]["key_events"], list)
        self.assertIsInstance(bundle["chapter_summary"]["open_threads"], list)
        self._assert_no_preview_input_span_keys(bundle)

        saved = self.client.get("/api/projects/gold_demo_01/translation-preview/input/saved?chapter_id=gold_demo_01_ch01")
        self.assertEqual(saved.status_code, 200, saved.get_json())
        self.assertEqual(saved.get_json()["data"]["paths"]["input"], str(input_path.resolve()))
        self.assertEqual(len(saved.get_json()["data"]["blocks"]), 7)

    def test_translation_preview_import_agent_output_file(self):
        doc_id = "translation_preview_agent_output"
        dataset = self._make_txt_project(doc_id)
        chapter_id = dataset["chapters"][0]["chapter_id"]
        block = next(item for item in dataset["blocks"] if item["block_type"] != "heading")

        bundle_response = self.client.get(f"/api/projects/{doc_id}/translation-preview/input?chapter_id={chapter_id}")
        self.assertEqual(bundle_response.status_code, 200, bundle_response.get_json())
        output_path = Path(bundle_response.get_json()["data"]["paths"]["preview_output_suggested"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        preview = self._good_translation_preview(doc_id, chapter_id, block["block_id"])
        output_path.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")

        response = self.client.post(f"/api/projects/{doc_id}/translation-preview/runs/agent-output", json={"chapter_id": chapter_id})
        self.assertEqual(response.status_code, 201, response.get_json())
        data = response.get_json()["data"]
        self.assertEqual(data["run"]["chapter_id"], chapter_id)
        self.assertEqual(data["paths"]["agent_output"], str(output_path.resolve()))
        self.assertTrue(
            self._project_root(doc_id)
            .joinpath("working", "translation_preview", "runs", f"{data['run']['run_id']}.json")
            .exists()
        )

    def test_translation_preview_input_missing_chapter(self):
        response = self.client.get("/api/projects/gold_demo_01/translation-preview/input?chapter_id=missing_chapter")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["errors"][0]["code"], "missing_chapter")

    def test_annotation_input_resolve_and_apply(self):
        doc_id = "annotation_apply"
        _, chapter, first, second, _third = self._annotation_fixture(doc_id)
        chapter_id = chapter["chapter_id"]

        annotation_input = self.client.post(f"/api/projects/{doc_id}/annotate/input", json={
            "chapter_id": chapter_id,
            "user": "tester",
        })
        self.assertEqual(annotation_input.status_code, 201)
        input_payload = annotation_input.get_json()["data"]
        self.assertEqual(input_payload["chapter_id"], chapter_id)
        self.assertEqual(len(input_payload["blocks"]), 4)
        self.assertIn("known_relations", input_payload)
        self.assertEqual(input_payload["known_relations"], [])
        self.assertTrue(self._project_root(doc_id).joinpath("working", "annotation", f"{chapter_id}_input.json").exists())

        missing_candidate = self.client.get(f"/api/projects/{doc_id}/annotate/candidate?chapter_id={chapter_id}")
        self.assertEqual(missing_candidate.status_code, 404)

        candidate = self._good_annotation_candidate(doc_id, chapter_id, first, second)
        resolved = self.client.post(f"/api/projects/{doc_id}/annotate/resolve", json={
            "candidate": candidate,
            "user": "tester",
        })
        self.assertEqual(resolved.status_code, 201)
        preview = resolved.get_json()["data"]
        self.assertEqual([item["status"] for item in preview["entities"]], ["ok", "ok"])
        self.assertEqual(preview["glossary"][0]["status"], "ok")
        self.assertEqual(preview["discourse"][0]["speaker_entity_id"], "e_001")
        self.assertEqual(preview["relations"][0]["status"], "ok")
        self.assertEqual(preview["relations"][0]["source_entity_id"], "e_001")
        self.assertEqual(preview["relations"][0]["target_entity_id"], "e_002")
        self.assertTrue(self._project_root(doc_id).joinpath("working", "annotation", f"{chapter_id}_resolved.json").exists())

        loaded_candidate = self.client.get(f"/api/projects/{doc_id}/annotate/candidate?chapter_id={chapter_id}")
        self.assertEqual(loaded_candidate.status_code, 200)
        self.assertEqual(loaded_candidate.get_json()["data"]["candidate"]["doc_id"], doc_id)
        self.assertEqual(loaded_candidate.get_json()["data"]["candidate"]["chapter_id"], chapter_id)

        applied = self.client.post(f"/api/projects/{doc_id}/annotate/apply", json={
            "chapter_id": chapter_id,
            "approved": True,
            "accept_all_resolved": True,
            "user": "tester",
        })
        self.assertEqual(applied.status_code, 201, applied.get_json())
        counts = applied.get_json()["data"]["counts"]
        self.assertEqual(counts["entities"], 2)
        self.assertEqual(counts["entity_mentions"], 4)
        self.assertEqual(counts["glossary"], 1)
        self.assertEqual(counts["occurrences"], 1)
        self.assertEqual(counts["discourse"], 1)
        self.assertEqual(counts["relations"], 1)
        self.assertEqual(counts["summary"], 1)

        entities = self._jsonl_rows(doc_id, "entities.jsonl")
        glossary = self._jsonl_rows(doc_id, "glossary.jsonl")
        relations = self._jsonl_rows(doc_id, "entity_relations.jsonl")
        summaries = self._jsonl_rows(doc_id, "chapter_summaries.jsonl")
        self.assertEqual(len(entities), 2)
        self.assertEqual(len(glossary), 1)
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["source_entity_id"], "e_001")
        self.assertEqual(relations[0]["target_entity_id"], "e_002")
        self.assertEqual(relations[0]["relation_type"], "friend")
        self.assertEqual(relations[0]["address_policy"]["source_to_target"]["address_term"], "ban")
        self.assertEqual(len(glossary[0]["occurrences"]), 1)
        self.assertEqual(glossary[0]["status"], "candidate")
        self.assertEqual(summaries[0]["characters_present"], ["e_001", "e_002"])
        self.assertEqual(summaries[0]["key_events"], ["Alice sees Bob", "Alice smiles at Bob"])
        self.assertEqual(summaries[0]["open_threads"], ["Whether Alice and Bob will keep interacting"])

        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        block = next(item for item in dataset["blocks"] if item["block_id"] == first["block_id"])
        self.assertEqual(block["discourse"]["speaker_entity_id"], "e_001")
        self.assertEqual(block["discourse"]["addressee_entity_id"], "e_002")
        self.assertIn("e_001", block["annotations"]["entity_mentions"])
        self.assertIn("g_001", next(item for item in dataset["blocks"] if item["block_id"] == second["block_id"])["annotations"]["term_occurrences"])
        self._assert_project_valid(doc_id)

    def test_annotation_entity_type_aliases_normalize_before_apply(self):
        doc_id = "annotation_entity_type_alias"
        _dataset, chapter, first, second, _third = self._annotation_fixture(doc_id)
        candidate = self._good_annotation_candidate(doc_id, chapter["chapter_id"], first, second)
        candidate["entity_candidates"][1]["entity_type"] = "named_object"
        resolved = self.client.post(f"/api/projects/{doc_id}/annotate/resolve", json={
            "candidate": candidate,
            "user": "tester",
        })
        self.assertEqual(resolved.status_code, 201, resolved.get_json())
        self.assertEqual(resolved.get_json()["data"]["entities"][1]["entity_type"], "concept")

        applied = self.client.post(f"/api/projects/{doc_id}/annotate/apply", json={
            "chapter_id": chapter["chapter_id"],
            "approved": True,
            "accept_all_resolved": True,
            "user": "tester",
        })
        self.assertEqual(applied.status_code, 201, applied.get_json())
        entities = self._jsonl_rows(doc_id, "entities.jsonl")
        self.assertEqual(entities[1]["entity_type"], "concept")
        self._assert_project_valid(doc_id)

    def test_entity_relation_crud_routes(self):
        doc_id = "relation_crud"
        _dataset, chapter, first, second, _third = self._annotation_fixture(doc_id)
        candidate = self._good_annotation_candidate(doc_id, chapter["chapter_id"], first, second)
        resolved = self.client.post(f"/api/projects/{doc_id}/annotate/resolve", json={
            "candidate": candidate,
            "user": "tester",
        })
        self.assertEqual(resolved.status_code, 201)
        applied = self.client.post(f"/api/projects/{doc_id}/annotate/apply", json={
            "chapter_id": chapter["chapter_id"],
            "approved": True,
            "accept_all_resolved": True,
            "user": "tester",
        })
        self.assertEqual(applied.status_code, 201, applied.get_json())

        create = self.client.post(f"/api/projects/{doc_id}/relations", json={
            "source_entity_id": "e_002",
            "target_entity_id": "e_001",
            "relation_type": "friend",
            "address_policy": {
                "source_to_target": {"self_term": "toi", "address_term": "ban"},
                "target_to_source": {"self_term": "toi", "address_term": "ban"},
            },
            "evidence": [{"block_id": first["block_id"], "surface": "Alice saw Bob."}],
            "confidence": 0.74,
            "notes": "Manual relation fixture.",
            "user": "tester",
        })
        self.assertEqual(create.status_code, 201, create.get_json())
        relation = create.get_json()["data"]
        self.assertEqual(relation["relation_id"], "rel_002")
        self.assertEqual(relation["source_entity_id"], "e_002")

        patch = self.client.patch(f"/api/projects/{doc_id}/relations/{relation['relation_id']}", json={
            "relation_type": "rival",
            "address_policy": {
                "source_to_target": {"self_term": "tao", "address_term": "may"},
                "target_to_source": {"self_term": "toi", "address_term": "anh"},
            },
            "user": "tester",
        })
        self.assertEqual(patch.status_code, 200, patch.get_json())
        self.assertEqual(patch.get_json()["data"]["relation_type"], "rival")
        self.assertEqual(patch.get_json()["data"]["address_policy"]["source_to_target"]["address_term"], "may")

        bad = self.client.post(f"/api/projects/{doc_id}/relations", json={
            "source_entity_id": "e_missing",
            "target_entity_id": "e_001",
            "relation_type": "friend",
        })
        self.assertEqual(bad.status_code, 404)

        blocked_entity_delete = self.client.delete(f"/api/projects/{doc_id}/entities/e_001", json={"user": "tester"})
        self.assertEqual(blocked_entity_delete.status_code, 400)
        refs = blocked_entity_delete.get_json()["errors"][0]["references"]
        self.assertTrue(any(ref.get("kind") == "relation" for ref in refs))

        delete = self.client.delete(f"/api/projects/{doc_id}/relations/{relation['relation_id']}", json={"user": "tester"})
        self.assertEqual(delete.status_code, 200, delete.get_json())
        self.assertEqual([row["relation_id"] for row in self._jsonl_rows(doc_id, "entity_relations.jsonl")], ["rel_001"])

        undo = self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        self.assertEqual(undo.status_code, 200, undo.get_json())
        self.assertIn("rel_002", [row["relation_id"] for row in self._jsonl_rows(doc_id, "entity_relations.jsonl")])
        self._assert_project_valid(doc_id)

    def test_annotation_relation_unresolved_ref_is_not_auto_applied(self):
        doc_id = "annotation_relation_unresolved"
        _dataset, chapter, first, _second, _third = self._annotation_fixture(doc_id)
        candidate = {
            "doc_id": doc_id,
            "chapter_id": chapter["chapter_id"],
            "relation_candidates": [
                {
                    "relation_key": "dangling_relation",
                    "source_ref": "missing_source",
                    "target_ref": "missing_target",
                    "relation_type": "friend",
                    "suggested_address_policy": {
                        "source_to_target": {"self_term": "toi", "address_term": "ban"},
                        "target_to_source": {"self_term": "toi", "address_term": "ban"},
                    },
                    "evidence": [{"block_id": first["block_id"], "surface": "Alice saw Bob."}],
                }
            ],
        }
        resolved = self.client.post(f"/api/projects/{doc_id}/annotate/resolve", json={"candidate": candidate})
        self.assertEqual(resolved.status_code, 201)
        preview = resolved.get_json()["data"]
        self.assertEqual(preview["relations"][0]["status"], "needs_review")
        self.assertIn("Unresolved source_ref", " ".join(preview["relations"][0]["messages"]))

        applied = self.client.post(f"/api/projects/{doc_id}/annotate/apply", json={
            "chapter_id": chapter["chapter_id"],
            "approved": True,
            "accept_all_resolved": True,
        })
        self.assertEqual(applied.status_code, 201)
        self.assertEqual(applied.get_json()["data"]["counts"]["relations"], 0)
        self.assertEqual(self._jsonl_rows(doc_id, "entity_relations.jsonl"), [])

    def test_annotation_relation_overlapping_phases_warn_in_validate(self):
        doc_id = "annotation_relation_overlap"
        _dataset, chapter, first, second, _third = self._annotation_fixture(doc_id)
        candidate = self._good_annotation_candidate(doc_id, chapter["chapter_id"], first, second)
        candidate["relation_candidates"] = [
            {
                "relation_key": "alice_bob_before",
                "source_ref": "alice",
                "target_ref": "bob",
                "relation_type": "friend",
                "state_label": "before",
                "valid_from_block_id": first["block_id"],
                "valid_to_block_id": second["block_id"],
                "suggested_address_policy": {
                    "source_to_target": {"self_term": "toi", "address_term": "ban"},
                    "target_to_source": {"self_term": "toi", "address_term": "ban"},
                },
                "evidence": [{"block_id": first["block_id"], "surface": "Alice saw Bob."}],
            },
            {
                "relation_key": "alice_bob_after",
                "source_ref": "alice",
                "target_ref": "bob",
                "relation_type": "friend",
                "state_label": "after",
                "valid_from_block_id": first["block_id"],
                "valid_to_block_id": second["block_id"],
                "suggested_address_policy": {
                    "source_to_target": {"self_term": "toi", "address_term": "ban"},
                    "target_to_source": {"self_term": "toi", "address_term": "ban"},
                },
                "evidence": [{"block_id": second["block_id"], "surface": "Alice smiled at Bob."}],
            },
        ]

        resolved = self.client.post(f"/api/projects/{doc_id}/annotate/resolve", json={"candidate": candidate})
        self.assertEqual(resolved.status_code, 201)
        self.assertEqual([item["status"] for item in resolved.get_json()["data"]["relations"]], ["ok", "ok"])
        applied = self.client.post(f"/api/projects/{doc_id}/annotate/apply", json={
            "chapter_id": chapter["chapter_id"],
            "approved": True,
            "accept_all_resolved": True,
        })
        self.assertEqual(applied.status_code, 201, applied.get_json())
        self.assertEqual(applied.get_json()["data"]["counts"]["relations"], 2)

        validate = self.client.post(f"/api/projects/{doc_id}/validate")
        self.assertEqual(validate.status_code, 200)
        report = validate.get_json()["data"]
        self.assertTrue(report["ok"], report.get("errors"))
        self.assertTrue(any("overlapping phase ranges" in item.get("message", "") for item in report.get("warnings", [])))

    def test_annotation_ambiguous_surface_is_not_auto_applied(self):
        doc_id = "annotation_ambiguous"
        _dataset, chapter, _first, _second, third = self._annotation_fixture(doc_id)
        candidate = {
            "doc_id": doc_id,
            "chapter_id": chapter["chapter_id"],
            "entity_candidates": [
                {
                    "entity_key": "alice",
                    "canonical_source": "Alice",
                    "suggested_canonical_target": "Alice",
                    "entity_type": "person",
                    "mentions": [{"block_id": third["block_id"], "surface": "Alice"}],
                    "confidence": 0.6,
                }
            ],
        }
        resolved = self.client.post(f"/api/projects/{doc_id}/annotate/resolve", json={"candidate": candidate})
        self.assertEqual(resolved.status_code, 201)
        preview = resolved.get_json()["data"]
        self.assertEqual(preview["entities"][0]["status"], "needs_review")
        self.assertEqual(preview["entities"][0]["mentions"][0]["status"], "ambiguous")

        applied = self.client.post(f"/api/projects/{doc_id}/annotate/apply", json={
            "chapter_id": chapter["chapter_id"],
            "approved": True,
            "accept_all_resolved": True,
        })
        self.assertEqual(applied.status_code, 201)
        self.assertEqual(applied.get_json()["data"]["counts"]["entities"], 0)
        self.assertEqual(self._jsonl_rows(doc_id, "entities.jsonl"), [])

    def test_annotation_dual_tag_conflict_blocks_glossary_auto_apply(self):
        doc_id = "annotation_dual_tag"
        _dataset, chapter, first, _second, _third = self._annotation_fixture(doc_id)
        candidate = {
            "doc_id": doc_id,
            "chapter_id": chapter["chapter_id"],
            "entity_candidates": [
                {
                    "entity_key": "alice",
                    "canonical_source": "Alice",
                    "suggested_canonical_target": "Alice",
                    "entity_type": "person",
                    "mentions": [{
                        "block_id": first["block_id"],
                        "surface": "Alice",
                        "right_context": " saw",
                    }],
                }
            ],
            "glossary_candidates": [
                {
                    "term_key": "alice",
                    "source_term": "Alice",
                    "suggested_expected_target": "Alice",
                    "occurrences": [{
                        "block_id": first["block_id"],
                        "surface": "Alice",
                        "right_context": " saw",
                    }],
                }
            ],
        }
        resolved = self.client.post(f"/api/projects/{doc_id}/annotate/resolve", json={"candidate": candidate})
        self.assertEqual(resolved.status_code, 201)
        preview = resolved.get_json()["data"]
        self.assertEqual(preview["entities"][0]["status"], "ok")
        self.assertEqual(preview["glossary"][0]["status"], "needs_review")
        self.assertEqual(preview["glossary"][0]["occurrences"][0]["status"], "conflict_dual_tag")

        applied = self.client.post(f"/api/projects/{doc_id}/annotate/apply", json={
            "chapter_id": chapter["chapter_id"],
            "approved": True,
            "accept_all_resolved": True,
        })
        self.assertEqual(applied.status_code, 201)
        self.assertEqual(applied.get_json()["data"]["counts"]["entities"], 1)
        self.assertEqual(applied.get_json()["data"]["counts"]["glossary"], 0)

    def test_annotation_apply_requires_resolve_after_clean_text_drift(self):
        doc_id = "annotation_drift"
        _dataset, chapter, first, second, _third = self._annotation_fixture(doc_id)
        candidate = self._good_annotation_candidate(doc_id, chapter["chapter_id"], first, second)
        resolved = self.client.post(f"/api/projects/{doc_id}/annotate/resolve", json={"candidate": candidate})
        self.assertEqual(resolved.status_code, 201)
        patch = self.client.patch(f"/api/projects/{doc_id}/blocks/{second['block_id']}", json={
            "clean_text": "Alice smiled at Bob, then left.",
            "user": "tester",
        })
        self.assertEqual(patch.status_code, 200)

        applied = self.client.post(f"/api/projects/{doc_id}/annotate/apply", json={
            "chapter_id": chapter["chapter_id"],
            "approved": True,
            "accept_all_resolved": True,
        })
        self.assertEqual(applied.status_code, 409)
        self.assertEqual(applied.get_json()["errors"][0]["code"], "annotation_drift")

    def test_project_delete_requires_confirmation_and_protects_sample(self):
        doc_id = "project_delete"
        create = self.client.post("/api/projects", json={"doc_id": doc_id, "metadata": {"title": "Delete Me"}})
        self.assertEqual(create.status_code, 201)

        missing_confirm = self.client.delete(f"/api/projects/{doc_id}", json={"confirm_doc_id": ""})
        self.assertEqual(missing_confirm.status_code, 400)

        protected = self.client.delete("/api/projects/gold_demo_01", json={"confirm_doc_id": "gold_demo_01"})
        self.assertEqual(protected.status_code, 400)
        self.assertIn("protected", protected.get_json()["errors"][0]["message"])

        deleted = self.client.delete(f"/api/projects/{doc_id}", json={"confirm_doc_id": doc_id})
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.get_json()["data"]["deleted"])

        detail = self.client.get(f"/api/projects/{doc_id}")
        self.assertEqual(detail.status_code, 404)

    def test_validate_project(self):
        response = self.client.post("/api/projects/gold_demo_01/validate")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["ok"])
        self.assertEqual(payload["data"]["exit_code"], 0)

    def test_migrate_schema_1_4_to_1_5_creates_relation_sidecar(self):
        doc_id = "schema_migrate"
        self._make_txt_project(doc_id)
        canonical = self._project_root(doc_id) / "canonical"
        document_path = canonical / "document.json"
        relations_path = canonical / "entity_relations.jsonl"
        document = json.loads(document_path.read_text(encoding="utf-8"))
        document["schema_version"] = "1.4.0"
        document_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if relations_path.exists():
            relations_path.unlink()

        response = self.client.post(f"/api/projects/{doc_id}/migrate-schema", json={"user": "tester"})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["data"]
        self.assertEqual(payload["schema_version_before"], "1.4.0")
        self.assertEqual(payload["schema_version_after"], "1.5.0")
        self.assertTrue(payload["validation"]["ok"], payload["validation"].get("errors"))
        self.assertTrue(relations_path.exists())
        self.assertTrue(any(path.name.startswith("document.json.bak-") for path in canonical.iterdir()))
        migrated = json.loads(document_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["schema_version"], "1.5.0")

        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertTrue(dataset["history_state"]["can_undo"])
        undo = self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        self.assertEqual(undo.status_code, 200)
        restored = json.loads(document_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["schema_version"], "1.4.0")
        self.assertFalse(relations_path.exists())

    def test_undo_redo_clean_text(self):
        doc_id = "history_clean"
        dataset = self._make_txt_project(doc_id)
        block = next(block for block in dataset["blocks"] if block["block_type"] != "heading")
        old_text = block["clean_text"]
        new_text = "Alice arrived, then looked back."

        patch = self.client.patch(f"/api/projects/{doc_id}/blocks/{block['block_id']}", json={
            "clean_text": new_text,
            "user": "tester",
        })
        self.assertEqual(patch.status_code, 200)
        history = self.client.get(f"/api/projects/{doc_id}/history").get_json()["data"]
        self.assertTrue(history["can_undo"])
        self.assertFalse(history["can_redo"])

        undo = self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        self.assertEqual(undo.status_code, 200)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        restored = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertEqual(restored["clean_text"], old_text)
        self.assertTrue(dataset["history_state"]["can_redo"])

        redo = self.client.post(f"/api/projects/{doc_id}/redo", json={"user": "tester"})
        self.assertEqual(redo.status_code, 200)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        redone = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertEqual(redone["clean_text"], new_text)

    def test_multi_step_undo_redo_clean_text_uses_changed_file_snapshots(self):
        doc_id = "history_multi_step"
        dataset = self._make_txt_project(doc_id)
        block = next(block for block in dataset["blocks"] if block["block_type"] != "heading")
        old_text = block["clean_text"]
        first_text = "First persistent edit."
        second_text = "Second persistent edit."

        first = self.client.patch(f"/api/projects/{doc_id}/blocks/{block['block_id']}", json={
            "clean_text": first_text,
            "user": "tester",
        })
        self.assertEqual(first.status_code, 200)
        second = self.client.patch(f"/api/projects/{doc_id}/blocks/{block['block_id']}", json={
            "clean_text": second_text,
            "user": "tester",
        })
        self.assertEqual(second.status_code, 200)

        history_path = Path(self.tmp.name) / doc_id / "working" / "history.json"
        history_data = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertEqual(len(history_data["undo"]), 2)
        for event in history_data["undo"]:
            self.assertEqual(list(event["before_files"].keys()), ["canonical/document.json"])
            self.assertEqual(list(event["after_files"].keys()), ["canonical/document.json"])

        undo_second = self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        self.assertEqual(undo_second.status_code, 200)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        current = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertEqual(current["clean_text"], first_text)
        self.assertTrue(dataset["history_state"]["can_undo"])
        self.assertTrue(dataset["history_state"]["can_redo"])

        undo_first = self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        self.assertEqual(undo_first.status_code, 200)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        current = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertEqual(current["clean_text"], old_text)
        self.assertFalse(dataset["history_state"]["can_undo"])
        self.assertTrue(dataset["history_state"]["can_redo"])

        redo_first = self.client.post(f"/api/projects/{doc_id}/redo", json={"user": "tester"})
        self.assertEqual(redo_first.status_code, 200)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        current = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertEqual(current["clean_text"], first_text)

        redo_second = self.client.post(f"/api/projects/{doc_id}/redo", json={"user": "tester"})
        self.assertEqual(redo_second.status_code, 200)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        current = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertEqual(current["clean_text"], second_text)

    def test_history_compresses_large_changed_files_and_keeps_multiple_steps(self):
        from services.history import record_history, undo

        doc_id = "history_large_snapshots"
        project_path = Path(self.tmp.name) / doc_id
        canonical = project_path / "canonical"
        working = project_path / "working"
        canonical.mkdir(parents=True)
        working.mkdir(parents=True)
        document_path = canonical / "document.json"
        document_path.write_text("initial", encoding="utf-8")

        def write_large(label: str):
            def operation():
                document_path.write_text(" ".join(f"{label}_{i:05d}" for i in range(1500)), encoding="utf-8")
            return operation

        record_history(project_path, action="test", label="first large write", target={}, user="tester", operation=write_large("first"))
        record_history(project_path, action="test", label="second large write", target={}, user="tester", operation=write_large("second"))

        history_path = working / "history.json"
        history_data = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertEqual(len(history_data["undo"]), 2)
        self.assertEqual(history_data["undo"][0]["after_files"]["canonical/document.json"]["__history_encoding"], "gzip+base64")
        self.assertLess(history_path.stat().st_size, 100_000)

        undo(project_path, "tester")
        self.assertIn("first_00000", document_path.read_text(encoding="utf-8"))
        undo(project_path, "tester")
        self.assertEqual(document_path.read_text(encoding="utf-8"), "initial")

    def test_undo_redo_glossary_add(self):
        doc_id = "history_glossary"
        dataset = self._make_txt_project(doc_id)
        block = next(block for block in dataset["blocks"] if "Alice" in block["clean_text"])
        start = block["clean_text"].index("Alice")
        end = start + len("Alice")

        created = self.client.post(f"/api/projects/{doc_id}/glossary/from-selection", json={
            "block_id": block["block_id"],
            "start": start,
            "end": end,
            "source_term": "Alice",
            "expected_target": "Alice",
            "user": "tester",
        })
        self.assertEqual(created.status_code, 201)
        term_id = created.get_json()["data"]["term_id"]
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertTrue(any(term["term_id"] == term_id for term in dataset["glossary"]))

        self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertFalse(any(term["term_id"] == term_id for term in dataset["glossary"]))
        restored_block = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertNotIn(term_id, (restored_block.get("annotations") or {}).get("term_occurrences", []))

        self.client.post(f"/api/projects/{doc_id}/redo", json={"user": "tester"})
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertTrue(any(term["term_id"] == term_id for term in dataset["glossary"]))

    def test_undo_redo_review_state(self):
        doc_id = "history_review"
        dataset = self._make_txt_project(doc_id)
        block_id = dataset["blocks"][0]["block_id"]

        review = self.client.patch(f"/api/projects/{doc_id}/review/blocks/{block_id}", json={
            "reviewed": True,
            "reviewed_by": "tester",
            "user": "tester",
        })
        self.assertEqual(review.status_code, 200)
        self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertFalse(dataset["review_state"]["blocks"][block_id]["reviewed"])

        self.client.post(f"/api/projects/{doc_id}/redo", json={"user": "tester"})
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertTrue(dataset["review_state"]["blocks"][block_id]["reviewed"])

    def test_history_redo_clears_after_new_mutation(self):
        doc_id = "history_clear"
        dataset = self._make_txt_project(doc_id)
        block = next(block for block in dataset["blocks"] if block["block_type"] != "heading")
        self.client.patch(f"/api/projects/{doc_id}/blocks/{block['block_id']}", json={
            "clean_text": "First edit.",
            "user": "tester",
        })
        self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        history = self.client.get(f"/api/projects/{doc_id}/history").get_json()["data"]
        self.assertTrue(history["can_redo"])

        self.client.patch(f"/api/projects/{doc_id}/blocks/{block['block_id']}", json={
            "quality_flags": ["needs_review"],
            "user": "tester",
        })
        history = self.client.get(f"/api/projects/{doc_id}/history").get_json()["data"]
        self.assertFalse(history["can_redo"])

    def test_validate_does_not_create_history_and_empty_undo_fails(self):
        doc_id = "history_noop"
        self._make_txt_project(doc_id)
        history = self.client.get(f"/api/projects/{doc_id}/history").get_json()["data"]
        self.assertFalse(history["can_undo"])

        validate = self.client.post(f"/api/projects/{doc_id}/validate", json={"user": "tester"})
        self.assertEqual(validate.status_code, 200)
        history = self.client.get(f"/api/projects/{doc_id}/history").get_json()["data"]
        self.assertFalse(history["can_undo"])

        undo = self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        self.assertEqual(undo.status_code, 409)
        self.assertEqual(undo.get_json()["errors"][0]["code"], "empty_undo_stack")

    def test_reference_draft_undo_redo_keeps_csv_in_sync(self):
        doc_id = "history_reference_csv"
        dataset = self._make_txt_project(doc_id)
        block = next(block for block in dataset["blocks"] if block["block_type"] != "heading")
        working = Path(self.tmp.name) / doc_id / "working"
        drafts_path = working / "drafts.json"
        csv_path = working / "translation_review_log.csv"

        draft_a = self.client.post(f"/api/projects/{doc_id}/references/draft", json={
            "block_id": block["block_id"],
            "reference_vi": "BAN_DICH_A_UNIQUE",
            "source": "human",
            "translated_by": "tester",
            "user": "tester",
        })
        self.assertEqual(draft_a.status_code, 201)
        reference_id = draft_a.get_json()["data"]["reference_id"]
        self.assertIn("BAN_DICH_A_UNIQUE", csv_path.read_text(encoding="utf-8"))

        draft_b = self.client.post(f"/api/projects/{doc_id}/references/draft", json={
            "reference_id": reference_id,
            "block_id": block["block_id"],
            "reference_vi": "BAN_DICH_B_UNIQUE",
            "source": "human",
            "translated_by": "tester",
            "user": "tester",
        })
        self.assertEqual(draft_b.status_code, 201)
        self.assertIn("BAN_DICH_B_UNIQUE", csv_path.read_text(encoding="utf-8"))

        undo = self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        self.assertEqual(undo.status_code, 200)
        drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
        self.assertEqual(drafts["references"][reference_id]["reference_vi"], "BAN_DICH_A_UNIQUE")
        csv_text = csv_path.read_text(encoding="utf-8")
        self.assertIn("BAN_DICH_A_UNIQUE", csv_text)
        self.assertNotIn("BAN_DICH_B_UNIQUE", csv_text)

        redo = self.client.post(f"/api/projects/{doc_id}/redo", json={"user": "tester"})
        self.assertEqual(redo.status_code, 200)
        drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
        self.assertEqual(drafts["references"][reference_id]["reference_vi"], "BAN_DICH_B_UNIQUE")
        csv_text = csv_path.read_text(encoding="utf-8")
        self.assertIn("BAN_DICH_B_UNIQUE", csv_text)
        self.assertNotIn("BAN_DICH_A_UNIQUE", csv_text)

    def test_patch_metadata_persists(self):
        response = self.client.patch("/api/projects/gold_demo_01/metadata", json={
            "title": "[SYNTHETIC DEMO] The Turning - Edited",
            "user": "tester",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["title"], "[SYNTHETIC DEMO] The Turning - Edited")

        dataset = self.client.get("/api/projects/gold_demo_01/dataset").get_json()
        self.assertEqual(dataset["data"]["document"]["metadata"]["title"], "[SYNTHETIC DEMO] The Turning - Edited")

    def test_patch_block_and_review_state_persist(self):
        block_id = "gold_demo_01_ch01_b007"
        response = self.client.patch(f"/api/projects/gold_demo_01/blocks/{block_id}", json={
            "clean_text": "An old inscription on the dial marked the passage of time.",
            "quality_flags": ["ok", "reviewed_edit"],
            "user": "tester",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("block", response.get_json()["data"])

        review = self.client.patch(f"/api/projects/gold_demo_01/review/blocks/{block_id}", json={
            "reviewed": True,
            "reviewed_by": "tester",
            "user": "tester",
        })
        self.assertEqual(review.status_code, 200)
        self.assertTrue(review.get_json()["data"]["reviewed"])

        dataset = self.client.get("/api/projects/gold_demo_01/dataset").get_json()
        self.assertTrue(dataset["data"]["review_state"]["blocks"][block_id]["reviewed"])

    def test_patch_block_reanchors_shifted_spans_and_undo_restores(self):
        doc_id = "span_reanchor_shift"
        dataset = self._make_txt_project(
            doc_id,
            b"Chapter 1\n\nFilby became pensive. Clearly, the Time Traveller proceeded.",
        )
        block = next(item for item in dataset["blocks"] if "Filby" in item["clean_text"])
        old_text = block["clean_text"]
        filby_start = old_text.index("Filby")
        time_start = old_text.index("Time Traveller")

        entity = self.client.post(f"/api/projects/{doc_id}/entities/from-selection", json={
            "block_id": block["block_id"],
            "start": filby_start,
            "end": filby_start + len("Filby"),
            "surface": "Filby",
            "user": "tester",
        })
        self.assertEqual(entity.status_code, 201)
        glossary = self.client.post(f"/api/projects/{doc_id}/glossary/from-selection", json={
            "block_id": block["block_id"],
            "start": time_start,
            "end": time_start + len("Time Traveller"),
            "source_term": "Time Traveller",
            "expected_target": "Time Traveller",
            "user": "tester",
        })
        self.assertEqual(glossary.status_code, 201)
        term_id = glossary.get_json()["data"]["term_id"]

        new_text = old_text.replace("Filby", "Filb", 1)
        patch = self.client.patch(f"/api/projects/{doc_id}/blocks/{block['block_id']}", json={
            "clean_text": new_text,
            "user": "tester",
        })
        self.assertEqual(patch.status_code, 200)
        payload = patch.get_json()["data"]
        self.assertEqual(payload["relocated_count"], 1)
        self.assertEqual(len(payload["stale_spans"]), 1)
        self.assertEqual(payload["stale_spans"][0]["kind"], "entity")

        rows = self._jsonl_rows(doc_id, "glossary.jsonl")
        term = next(row for row in rows if row["term_id"] == term_id)
        span = term["occurrences"][0]["span"]
        self.assertEqual(span, [time_start - 1, time_start - 1 + len("Time Traveller")])
        self.assertEqual(new_text[span[0]:span[1]], "Time Traveller")

        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertTrue(dataset["review_state"]["blocks"][block["block_id"]]["needs_retag"])

        undo = self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        self.assertEqual(undo.status_code, 200)
        rows = self._jsonl_rows(doc_id, "glossary.jsonl")
        term = next(row for row in rows if row["term_id"] == term_id)
        self.assertEqual(term["occurrences"][0]["span"], [time_start, time_start + len("Time Traveller")])
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        restored = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertEqual(restored["clean_text"], old_text)

    def test_patch_block_reanchors_repeated_surfaces(self):
        doc_id = "span_reanchor_repeated"
        source = b"Chapter 1\n\nTime Traveller met the Time Traveller while the Time Traveller waited."
        dataset = self._make_txt_project(doc_id, source)
        block = next(item for item in dataset["blocks"] if "Time Traveller" in item["clean_text"])
        old_text = block["clean_text"]
        positions = []
        cursor = 0
        while True:
            found = old_text.find("Time Traveller", cursor)
            if found == -1:
                break
            positions.append(found)
            cursor = found + len("Time Traveller")
        self.assertEqual(len(positions), 3)

        term_ids = []
        for pos in positions:
            created = self.client.post(f"/api/projects/{doc_id}/glossary/from-selection", json={
                "block_id": block["block_id"],
                "start": pos,
                "end": pos + len("Time Traveller"),
                "source_term": "Time Traveller",
                "expected_target": "Time Traveller",
                "user": "tester",
            })
            self.assertEqual(created.status_code, 201)
            term_ids.append(created.get_json()["data"]["term_id"])

        prefix = "Note: "
        patch = self.client.patch(f"/api/projects/{doc_id}/blocks/{block['block_id']}", json={
            "clean_text": prefix + old_text,
            "user": "tester",
        })
        self.assertEqual(patch.status_code, 200)
        payload = patch.get_json()["data"]
        self.assertEqual(payload["relocated_count"], 3)
        self.assertEqual(payload["stale_spans"], [])

        rows = self._jsonl_rows(doc_id, "glossary.jsonl")
        by_id = {row["term_id"]: row for row in rows}
        for term_id, old_start in zip(term_ids, positions):
            span = by_id[term_id]["occurrences"][0]["span"]
            self.assertEqual(span, [old_start + len(prefix), old_start + len(prefix) + len("Time Traveller")])
            self.assertEqual((prefix + old_text)[span[0]:span[1]], "Time Traveller")
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertFalse(dataset["review_state"]["blocks"][block["block_id"]]["needs_retag"])

    def test_patch_block_marks_span_stale_when_surface_is_edited(self):
        doc_id = "span_reanchor_surface_changed"
        dataset = self._make_txt_project(doc_id, b"Chapter 1\n\nAlice arrived.")
        block = next(item for item in dataset["blocks"] if "Alice" in item["clean_text"])
        old_text = block["clean_text"]
        start = old_text.index("Alice")
        created = self.client.post(f"/api/projects/{doc_id}/glossary/from-selection", json={
            "block_id": block["block_id"],
            "start": start,
            "end": start + len("Alice"),
            "source_term": "Alice",
            "expected_target": "Alice",
            "user": "tester",
        })
        self.assertEqual(created.status_code, 201)

        patch = self.client.patch(f"/api/projects/{doc_id}/blocks/{block['block_id']}", json={
            "clean_text": old_text.replace("Alice", "Alicia", 1),
            "user": "tester",
        })
        self.assertEqual(patch.status_code, 200)
        payload = patch.get_json()["data"]
        self.assertEqual(payload["relocated_count"], 0)
        self.assertEqual(len(payload["stale_spans"]), 1)
        self.assertEqual(payload["stale_spans"][0]["expected_surface"], "Alice")
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertTrue(dataset["review_state"]["blocks"][block["block_id"]]["needs_retag"])

    def test_add_glossary_from_selection_and_patch(self):
        block_id = "gold_demo_01_ch01_b007"
        text = "An old inscription on the dial marked the passage of time."
        start = text.index("dial")
        end = start + len("dial")
        response = self.client.post("/api/projects/gold_demo_01/glossary/from-selection", json={
            "block_id": block_id,
            "start": start,
            "end": end,
            "source_term": "dial",
            "expected_target": "mặt đồng hồ",
            "user": "tester",
        })
        self.assertEqual(response.status_code, 201)
        term = response.get_json()["data"]
        self.assertEqual(term["source_term"], "dial")

        patch = self.client.patch(f"/api/projects/gold_demo_01/glossary/{term['term_id']}", json={
            "status": "verified",
            "allowed_variants": ["mặt đồng hồ"],
            "user": "tester",
        })
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(patch.get_json()["data"]["status"], "verified")

    def test_delete_locked_term_is_blocked(self):
        response = self.client.delete("/api/projects/gold_demo_01/glossary/g_001", json={"user": "tester"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["errors"][0]["code"], "locked_term")

    def test_delete_glossary_cascades_own_occurrence_and_undo_restores(self):
        doc_id = "glossary_delete_cascade"
        dataset = self._make_txt_project(doc_id)
        block = next(item for item in dataset["blocks"] if "Alice" in item["clean_text"])
        start = block["clean_text"].index("Alice")
        created = self.client.post(f"/api/projects/{doc_id}/glossary/from-selection", json={
            "block_id": block["block_id"],
            "start": start,
            "end": start + len("Alice"),
            "source_term": "Alice",
            "expected_target": "Alice",
            "user": "tester",
        })
        self.assertEqual(created.status_code, 201)
        term_id = created.get_json()["data"]["term_id"]

        deleted = self.client.delete(f"/api/projects/{doc_id}/glossary/{term_id}", json={"user": "tester"})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["data"]["removed_occurrences"], 1)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertFalse(any(row["term_id"] == term_id for row in dataset["glossary"]))
        clean_block = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertNotIn(term_id, (clean_block.get("annotations") or {}).get("term_occurrences", []))
        self._assert_project_valid(doc_id)

        undo = self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        self.assertEqual(undo.status_code, 200)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertTrue(any(row["term_id"] == term_id for row in dataset["glossary"]))
        restored = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertIn(term_id, (restored.get("annotations") or {}).get("term_occurrences", []))
        self._assert_project_valid(doc_id)

    def test_delete_glossary_cascades_multiple_block_refs(self):
        doc_id = "glossary_delete_multi"
        dataset = self._make_txt_project(doc_id, b"Chapter 1\n\nAlice arrived.\n\nAlice waited.")
        blocks = [item for item in dataset["blocks"] if "Alice" in item["clean_text"]]
        self.assertGreaterEqual(len(blocks), 2)
        term_id = "g_multi"
        occurrences = []
        document_path = self._project_root(doc_id) / "canonical" / "document.json"
        document = json.loads(document_path.read_text(encoding="utf-8"))
        for block in blocks[:2]:
            span = [block["clean_text"].index("Alice"), block["clean_text"].index("Alice") + len("Alice")]
            occurrences.append({"block_id": block["block_id"], "span": span})
            for chapter in document["chapters"]:
                for doc_block in chapter["blocks"]:
                    if doc_block["block_id"] == block["block_id"]:
                        doc_block.setdefault("annotations", {}).setdefault("term_occurrences", []).append(term_id)
        document_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        glossary_path = self._project_root(doc_id) / "canonical" / "glossary.jsonl"
        glossary_path.write_text(json.dumps({
            "term_id": term_id,
            "doc_id": doc_id,
            "source_term": "Alice",
            "expected_target": "Alice",
            "status": "candidate",
            "occurrences": occurrences,
        }) + "\n", encoding="utf-8")

        deleted = self.client.delete(f"/api/projects/{doc_id}/glossary/{term_id}", json={"user": "tester"})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["data"]["removed_occurrences"], 2)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertFalse(dataset["glossary"])
        for block in dataset["blocks"]:
            self.assertNotIn(term_id, (block.get("annotations") or {}).get("term_occurrences", []))
        self._assert_project_valid(doc_id)

    def test_add_entity_from_selection_and_patch(self):
        block_id = "gold_demo_01_ch01_b007"
        text = "An old inscription on the dial marked the passage of time."
        start = text.index("inscription")
        end = start + len("inscription")
        response = self.client.post("/api/projects/gold_demo_01/entities/from-selection", json={
            "block_id": block_id,
            "start": start,
            "end": end,
            "surface": "inscription",
            "canonical_target": "dòng khắc",
            "entity_type": "concept",
            "user": "tester",
        })
        self.assertEqual(response.status_code, 201)
        entity = response.get_json()["data"]
        self.assertEqual(entity["canonical_source"], "inscription")

        patch = self.client.patch(f"/api/projects/gold_demo_01/entities/{entity['entity_id']}", json={
            "aliases_target": ["dòng chữ khắc"],
            "pronoun_policy": "",
            "user": "tester",
        })
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(patch.get_json()["data"]["aliases_target"], ["dòng chữ khắc"])

    def test_delete_unreferenced_entity(self):
        doc_id = "entity_delete_free"
        self._make_txt_project(doc_id)
        entity_path = self._project_root(doc_id) / "canonical" / "entities.jsonl"
        entity_path.write_text(json.dumps({
            "entity_id": "e_free",
            "doc_id": doc_id,
            "canonical_source": "Free Entity",
            "canonical_target": "Free Entity",
            "entity_type": "concept",
            "aliases_source": [],
            "aliases_target": [],
            "pronoun_policy": "",
            "mentions": [],
            "annotated_by": "tester",
            "confidence": 1.0,
        }) + "\n", encoding="utf-8")

        response = self.client.delete(f"/api/projects/{doc_id}/entities/e_free", json={"user": "tester"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["data"]["deleted"])
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertFalse(any(row["entity_id"] == "e_free" for row in dataset["entities"]))

    def test_delete_entity_cascades_own_mentions_and_undo_restores(self):
        doc_id = "entity_delete_mentioned"
        dataset = self._make_txt_project(doc_id)
        block = next(item for item in dataset["blocks"] if "Alice" in item["clean_text"])
        start = block["clean_text"].index("Alice")
        response = self.client.post(f"/api/projects/{doc_id}/entities/from-selection", json={
            "block_id": block["block_id"],
            "start": start,
            "end": start + len("Alice"),
            "surface": "Alice",
            "user": "tester",
        })
        self.assertEqual(response.status_code, 201)
        entity_id = response.get_json()["data"]["entity_id"]

        delete = self.client.delete(f"/api/projects/{doc_id}/entities/{entity_id}", json={"user": "tester"})
        self.assertEqual(delete.status_code, 200)
        self.assertEqual(delete.get_json()["data"]["removed_mentions"], 1)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertFalse(any(row["entity_id"] == entity_id for row in dataset["entities"]))
        clean_block = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertNotIn(entity_id, (clean_block.get("annotations") or {}).get("entity_mentions", []))
        self._assert_project_valid(doc_id)

        undo = self.client.post(f"/api/projects/{doc_id}/undo", json={"user": "tester"})
        self.assertEqual(undo.status_code, 200)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertTrue(any(row["entity_id"] == entity_id for row in dataset["entities"]))
        restored = next(item for item in dataset["blocks"] if item["block_id"] == block["block_id"])
        self.assertIn(entity_id, (restored.get("annotations") or {}).get("entity_mentions", []))
        self._assert_project_valid(doc_id)

    def test_delete_entity_in_discourse_is_blocked(self):
        doc_id = "entity_delete_discourse_ref"
        dataset = self._make_txt_project(doc_id)
        block = next(item for item in dataset["blocks"] if "Alice" in item["clean_text"])
        start = block["clean_text"].index("Alice")
        created = self.client.post(f"/api/projects/{doc_id}/entities/from-selection", json={
            "block_id": block["block_id"],
            "start": start,
            "end": start + len("Alice"),
            "surface": "Alice",
            "entity_type": "person",
            "user": "tester",
        })
        self.assertEqual(created.status_code, 201)
        entity_id = created.get_json()["data"]["entity_id"]
        document_path = self._project_root(doc_id) / "canonical" / "document.json"
        document = json.loads(document_path.read_text(encoding="utf-8"))
        for chapter in document["chapters"]:
            for doc_block in chapter["blocks"]:
                if doc_block["block_id"] == block["block_id"]:
                    doc_block["discourse"] = {"speaker_entity_id": entity_id}
        document_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        response = self.client.delete(f"/api/projects/{doc_id}/entities/{entity_id}", json={"user": "tester"})
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["errors"][0]
        self.assertEqual(error["code"], "entity_still_referenced")
        self.assertEqual(error["references"][0]["block_id"], block["block_id"])
        self._assert_project_valid(doc_id)

    def test_delete_entity_in_summary_is_blocked(self):
        doc_id = "entity_delete_summary_ref"
        dataset = self._make_txt_project(doc_id)
        chapter_id = dataset["chapters"][0]["chapter_id"]
        block = next(item for item in dataset["blocks"] if "Alice" in item["clean_text"])
        start = block["clean_text"].index("Alice")
        created = self.client.post(f"/api/projects/{doc_id}/entities/from-selection", json={
            "block_id": block["block_id"],
            "start": start,
            "end": start + len("Alice"),
            "surface": "Alice",
            "entity_type": "person",
            "user": "tester",
        })
        self.assertEqual(created.status_code, 201)
        entity_id = created.get_json()["data"]["entity_id"]
        summary_path = self._project_root(doc_id) / "canonical" / "chapter_summaries.jsonl"
        summary_path.write_text(json.dumps({
            "doc_id": doc_id,
            "chapter_id": chapter_id,
            "summary_source": "Alice appears.",
            "source": "human",
            "characters_present": [entity_id],
        }) + "\n", encoding="utf-8")

        response = self.client.delete(f"/api/projects/{doc_id}/entities/{entity_id}", json={"user": "tester"})
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["errors"][0]
        self.assertEqual(error["code"], "entity_still_referenced")
        self.assertEqual(error["references"][0]["chapter_id"], chapter_id)
        self._assert_project_valid(doc_id)

    def test_patch_summary(self):
        response = self.client.patch("/api/projects/gold_demo_01/summary/gold_demo_01_ch01", json={
            "summary_source": "Mira wakes before the Turning and asks about the slow dawn.",
            "source": "human",
            "key_events": ["Mira wakes before the Turning."],
            "motifs": ["slow dawn"],
            "summary_target": "Mira thuc day truoc buoi Chuyen Minh.",
            "open_threads": ["Why the dawn is slow."],
            "translation_notes": "Keep Turning consistent.",
            "confidence": 0.81,
            "user": "tester",
        })
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["source"], "human")
        self.assertEqual(data["key_events"], ["Mira wakes before the Turning."])
        self.assertEqual(data["motifs"], ["slow dawn"])
        self.assertEqual(data["summary_target"], "Mira thuc day truoc buoi Chuyen Minh.")
        self.assertEqual(data["open_threads"], ["Why the dawn is slow."])
        self.assertEqual(data["translation_notes"], "Keep Turning consistent.")
        self.assertEqual(data["confidence"], 0.81)

    def test_patch_block_notes_only_updates_soft_annotations(self):
        doc_id = "block_notes"
        dataset = self._make_txt_project(doc_id)
        block = next(item for item in dataset["blocks"] if item["block_type"] != "heading")

        response = self.client.patch(f"/api/projects/{doc_id}/blocks/{block['block_id']}/notes", json={
            "motifs": ["arrival"],
            "tone": "quiet",
            "implicit_meaning": "Alice entering changes the scene.",
            "narrative_note": "Use as a lightweight interpretive note.",
            "user": "tester",
        })
        self.assertEqual(response.status_code, 200)
        annotations = response.get_json()["data"]["block"]["annotations"]
        self.assertEqual(annotations["motifs"], ["arrival"])
        self.assertEqual(annotations["tone"], "quiet")
        self.assertEqual(annotations["implicit_meaning"], "Alice entering changes the scene.")
        self.assertEqual(annotations["narrative_note"], "Use as a lightweight interpretive note.")
        self.assertEqual(annotations.get("term_occurrences", []), [])
        self.assertEqual(annotations.get("entity_mentions", []), [])
        self._assert_project_valid(doc_id)

    def test_patch_block_notes_rejects_hard_annotation_pointers(self):
        doc_id = "block_notes_guard"
        dataset = self._make_txt_project(doc_id)
        block = next(item for item in dataset["blocks"] if item["block_type"] != "heading")

        response = self.client.patch(f"/api/projects/{doc_id}/blocks/{block['block_id']}/notes", json={
            "entity_mentions": ["e_999"],
            "user": "tester",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["errors"][0]["code"], "read_only_or_unknown_field")

    def test_project_upload_extract_job_and_validate(self):
        doc_id = "phase3_txt"
        create = self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Phase 3 TXT",
                "author": "Tester",
                "source_format": "txt",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        self.assertEqual(create.status_code, 201)

        upload = self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (BytesIO(b"Chapter 1\n\n\"Hello,\" Alice said.\n\nShe waited.\n\nChapter 2\n\nThe room was quiet."), "source.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201)

        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False, "user": "tester"})
        self.assertEqual(extract.status_code, 201)
        job = extract.get_json()["data"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["document"]["chapters"], 2)

        job_status = self.client.get(f"/api/projects/{doc_id}/jobs/{job['job_id']}").get_json()
        self.assertEqual(job_status["data"]["status"], "done")

        validate = self.client.post(f"/api/projects/{doc_id}/validate").get_json()
        self.assertTrue(validate["data"]["ok"])

        blocked = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False}).get_json()
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["errors"][0]["code"], "confirm_overwrite_required")

    def test_txt_gutenberg_markers_are_stripped_and_reported(self):
        doc_id = "extract_gutenberg_txt"
        source = b"""Project Gutenberg header should not survive.

*** START OF THE PROJECT GUTENBERG EBOOK TEST BOOK ***

Chapter 1

Alice arrived.

*** END OF THE PROJECT GUTENBERG EBOOK TEST BOOK ***

Project Gutenberg footer should not survive.
"""
        dataset = self._make_txt_project(doc_id, source=source)
        all_text = "\n".join(block["clean_text"] for block in dataset["blocks"])
        self.assertIn("Alice arrived.", all_text)
        self.assertNotIn("Project Gutenberg header should not survive", all_text)
        self.assertNotIn("Project Gutenberg footer should not survive", all_text)
        report = self._extraction_report(doc_id)
        self.assertTrue(report["gutenberg"]["start_marker_found"])
        self.assertTrue(report["gutenberg"]["end_marker_found"])
        self.assertEqual(report["source_format"], "txt")
        self.assertEqual(report["blocks"], len(dataset["blocks"]))

    def test_source_format_uses_actual_extension_and_reports_mismatch(self):
        doc_id = "extract_format_mismatch"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Mismatch",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (BytesIO(b"Chapter 1\n\nAlice arrived."), "source.txt")},
            content_type="multipart/form-data",
        )
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False, "user": "tester"})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertEqual(dataset["document"]["metadata"]["source_format"], "txt")
        from config import PIPELINE_VERSION

        self.assertEqual(dataset["document"]["metadata"]["pipeline_version"], PIPELINE_VERSION)
        report = self._extraction_report(doc_id)
        self.assertEqual(report["source_format_mismatch"], {"declared": "epub", "actual": "txt"})

    def test_txt_toc_driven_split_ignores_fake_legal_caps(self):
        doc_id = "extract_txt_toc"
        source = """Project Gutenberg header.

*** START OF THE PROJECT GUTENBERG EBOOK TEST BOOK ***

Contents

STORY OF THE DOOR
SEARCH FOR MR. HYDE

STORY OF THE DOOR

Alice arrived.

INCIDENTAL DAMAGES EVEN IF YOU GIVE NOTICE OF THE POSSIBILITY OF SUCH DAMAGE.

SEARCH FOR MR. HYDE

Bob waited.

*** END OF THE PROJECT GUTENBERG EBOOK TEST BOOK ***
""".encode("utf-8")
        dataset = self._make_txt_project(doc_id, source=source)
        self.assertEqual([chapter["title"] for chapter in dataset["chapters"]], ["STORY OF THE DOOR", "SEARCH FOR MR. HYDE"])
        self._assert_chapter_properties(dataset)
        report = self._extraction_report(doc_id)
        self._assert_toc_report(report)
        self.assertEqual(report["toc"]["toc_source"], "text")
        self.assertEqual(report["toc"]["toc_items"], 2)
        self.assertEqual(report["toc"]["chapters_matched"], 2)
        self.assertEqual(report["toc"]["match_rate"], 1.0)
        self.assertFalse(report["toc"]["fallback_used"])

    def test_txt_no_toc_falls_back_without_crashing(self):
        doc_id = "extract_txt_no_toc"
        dataset = self._make_txt_project(doc_id, source=b"Opening page\n\nAlice arrived.\n\nBob waited.")
        self.assertEqual(len(dataset["chapters"]), 1)
        self._assert_chapter_properties(dataset)
        report = self._extraction_report(doc_id)
        self._assert_toc_report(report)
        self.assertEqual(report["toc"]["toc_source"], "none")
        self.assertTrue(report["toc"]["fallback_used"])
        self.assertFalse(report["toc"]["low_confidence"])

    def test_txt_low_toc_match_uses_legacy_fallback(self):
        doc_id = "extract_txt_low_toc"
        source = b"""Contents

STORY ONE
STORY TWO

STORY ONE

Alice arrived.
"""
        dataset = self._make_txt_project(doc_id, source=source)
        self.assertEqual(len(dataset["chapters"]), 1)
        report = self._extraction_report(doc_id)
        self._assert_toc_report(report)
        self.assertEqual(report["toc"]["toc_source"], "text")
        self.assertEqual(report["toc"]["toc_items"], 2)
        self.assertEqual(report["toc"]["chapters_matched"], 1)
        self.assertTrue(report["toc"]["fallback_used"])
        self.assertTrue(report["toc"]["low_confidence"])

    def test_txt_toc_duplicate_standalone_match_is_low_confidence(self):
        source = """Contents

THE DOOR
THE WINDOW

THE DOOR

Door chapter starts normally.

THE WINDOW

FALSE: this line is a prose mention or running header inside door chapter.

More door-chapter prose continues here.

THE WINDOW

REAL window chapter prose.
"""
        chapters, report = self._split_txt_direct(source)
        self.assertEqual([chapter["title"] for chapter in chapters], ["THE DOOR", "THE WINDOW"])
        self.assertEqual(report["toc"]["match_rate"], 1.0)
        self.assertFalse(report["toc"]["fallback_used"])
        self.assertTrue(report["toc"]["low_confidence"])
        self.assertEqual(report["toc"]["ambiguous_titles"], ["THE WINDOW"])
        self.assertEqual(report["toc"]["ambiguous_count"], 1)

    def test_txt_running_header_repeat_is_low_confidence(self):
        source = """Contents

CHAPTER I
CHAPTER II

CHAPTER I

Alice arrived.

CHAPTER I

More page text after a repeated running header.

CHAPTER II

Bob waited.
"""
        _, report = self._split_txt_direct(source)
        self.assertTrue(report["toc"]["low_confidence"])
        self.assertEqual(report["toc"]["ambiguous_titles"], ["CHAPTER I"])

    def test_txt_clean_toc_is_not_overflagged(self):
        source = """Contents

CHAPTER I
CHAPTER II

CHAPTER I

Alice arrived.

CHAPTER II

Bob waited.
"""
        chapters, report = self._split_txt_direct(source)
        self.assertEqual([chapter["title"] for chapter in chapters], ["CHAPTER I", "CHAPTER II"])
        self.assertEqual(report["toc"]["match_rate"], 1.0)
        self.assertFalse(report["toc"]["fallback_used"])
        self.assertFalse(report["toc"]["low_confidence"])
        self.assertEqual(report["toc"]["ambiguous_titles"], [])
        self.assertEqual(report["toc"]["ambiguous_count"], 0)

    def test_dialogue_classifier_is_conservative_for_narrative_quotes(self):
        from services.extraction import block_type_for

        self.assertEqual(block_type_for('"Hello," Bob said.'), "dialogue")
        self.assertEqual(
            block_type_for('Alice called it "strange" and later "curious" while she kept walking.'),
            "paragraph",
        )

    def test_epub_ncx_toc_names_chapters_and_skips_non_toc_spine(self):
        doc_id = "extract_epub_ncx_toc"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "NCX EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <manifest>
    <item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="ad" href="ad.xhtml" media-type="application/xhtml+xml"/>
    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="toc"><itemref idref="ad"/><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""
        epub = self._minimal_epub(opf, {
            "toc.ncx": """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
<navPoint id="n1" playOrder="1"><navLabel><text>Story One</text></navLabel><content src="c1.xhtml"/></navPoint>
<navPoint id="n2" playOrder="2"><navLabel><text>Story Two</text></navLabel><content src="c2.xhtml"/></navPoint>
</navMap></ncx>""",
            "ad.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Advertisement</h1><p>Skip me.</p></body></html>""",
            "c1.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Alice arrived.</p></body></html>""",
            "c2.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Bob waited.</p></body></html>""",
        })
        self.client.post(f"/api/projects/{doc_id}/source", data={"file": (epub, "source.epub")}, content_type="multipart/form-data")
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertEqual([chapter["title"] for chapter in dataset["chapters"]], ["Story One", "Story Two"])
        self._assert_chapter_properties(dataset)
        all_text = "\n".join(block["clean_text"] for block in dataset["blocks"])
        self.assertNotIn("Skip me.", all_text)
        report = self._extraction_report(doc_id)
        self._assert_toc_report(report)
        self.assertEqual(report["toc"]["toc_source"], "ncx")
        self.assertFalse(report["toc"]["fallback_used"])
        self.assertIn({"file": "OPS/ad.xhtml", "reason": "not-in-toc"}, report["skipped"])

    def test_epub_nav_toc_names_chapters(self):
        doc_id = "extract_epub_nav_toc"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Nav EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""
        epub = self._minimal_epub(opf, {
            "nav.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol><li><a href="c1.xhtml">Chapter Alpha</a></li><li><a href="c2.xhtml">Chapter Beta</a></li></ol></nav>
</body></html>""",
            "c1.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Alice arrived.</p></body></html>""",
            "c2.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Bob waited.</p></body></html>""",
        })
        self.client.post(f"/api/projects/{doc_id}/source", data={"file": (epub, "source.epub")}, content_type="multipart/form-data")
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertEqual([chapter["title"] for chapter in dataset["chapters"]], ["Chapter Alpha", "Chapter Beta"])
        self._assert_chapter_properties(dataset)
        report = self._extraction_report(doc_id)
        self._assert_toc_report(report)
        self.assertEqual(report["toc"]["toc_source"], "nav")
        self.assertFalse(report["toc"]["fallback_used"])

    def test_epub_toc_anchors_split_multiple_chapters_in_one_file(self):
        doc_id = "extract_epub_anchor_split"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Anchor EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="body"/></spine>
</package>"""
        epub = self._minimal_epub(opf, {
            "nav.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol><li><a href="body.xhtml#ch1">I</a></li><li><a href="body.xhtml#ch2">II</a></li></ol></nav>
</body></html>""",
            "body.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h2 id="ch1">I</h2><p>Alice arrived.</p>
<h2 id="ch2">II</h2><p>Bob waited.</p>
</body></html>""",
        })
        self.client.post(f"/api/projects/{doc_id}/source", data={"file": (epub, "source.epub")}, content_type="multipart/form-data")
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertEqual([chapter["title"] for chapter in dataset["chapters"]], ["I", "II"])
        self.assertEqual(len(dataset["chapters"]), 2)
        chapter_text = {
            chapter["title"]: " ".join(block["clean_text"] for block in dataset["blocks"] if block["chapter_id"] == chapter["chapter_id"])
            for chapter in dataset["chapters"]
        }
        self.assertIn("Alice arrived.", chapter_text["I"])
        self.assertNotIn("Bob waited.", chapter_text["I"])
        self.assertIn("Bob waited.", chapter_text["II"])
        report = self._extraction_report(doc_id)
        self.assertEqual(report["toc"]["toc_source"], "nav")
        self.assertFalse(report["toc"]["low_confidence"])
        self.assertEqual(report["toc"]["duplicate_targets"], [])

    def test_epub_toc_marked_file_can_still_contain_body_anchors(self):
        doc_id = "extract_epub_toc_file_body_anchors"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "TOC File With Body EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="mixed" href="mixed.xhtml" media-type="application/xhtml+xml"/>
    <item id="body2" href="body2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="toc"><itemref idref="mixed"/><itemref idref="body2"/></spine>
  <guide><reference type="toc" href="mixed.xhtml"/></guide>
</package>"""
        epub = self._minimal_epub(opf, {
            "toc.ncx": """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
<navPoint id="n1" playOrder="1"><navLabel><text>I</text></navLabel><content src="mixed.xhtml#ch1"/></navPoint>
<navPoint id="n2" playOrder="2"><navLabel><text>II</text></navLabel><content src="mixed.xhtml#ch2"/></navPoint>
<navPoint id="n3" playOrder="3"><navLabel><text>III</text></navLabel><content src="body2.xhtml"/></navPoint>
</navMap></ncx>""",
            "mixed.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Table of Contents</h1><p>I</p><p>II</p>
<h2 id="ch1">I</h2><p>Alice arrived.</p>
<h2 id="ch2">II</h2><p>Bob waited.</p>
</body></html>""",
            "body2.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><h2>III</h2><p>Carol stayed.</p></body></html>""",
        })
        self.client.post(f"/api/projects/{doc_id}/source", data={"file": (epub, "source.epub")}, content_type="multipart/form-data")
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertEqual([chapter["title"] for chapter in dataset["chapters"]], ["I", "II", "III"])
        all_text = "\n".join(block["clean_text"] for block in dataset["blocks"])
        self.assertNotIn("Table of Contents", all_text)
        report = self._extraction_report(doc_id)
        self.assertFalse(report["toc"]["low_confidence"])

    def test_epub_toc_missing_anchor_is_low_confidence(self):
        doc_id = "extract_epub_missing_anchor"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Missing Anchor EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="body"/></spine>
</package>"""
        epub = self._minimal_epub(opf, {
            "nav.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol><li><a href="body.xhtml#ch1">I</a></li><li><a href="body.xhtml#missing">II</a></li></ol></nav>
</body></html>""",
            "body.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h2 id="ch1">I</h2><p>Alice arrived.</p>
<h2 id="ch2">II</h2><p>Bob waited.</p>
</body></html>""",
        })
        self.client.post(f"/api/projects/{doc_id}/source", data={"file": (epub, "source.epub")}, content_type="multipart/form-data")
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        report = self._extraction_report(doc_id)
        self.assertTrue(report["toc"]["low_confidence"])
        self.assertEqual(report["toc"]["anchor_split_failed"][0]["reason"], "missing-anchor")

    def test_epub_first_same_file_toc_entry_can_start_without_anchor(self):
        doc_id = "extract_epub_first_entry_no_anchor"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "First Entry No Anchor EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="body"/></spine>
</package>"""
        epub = self._minimal_epub(opf, {
            "nav.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol><li><a href="body.xhtml">Chapter XXIV</a></li><li><a href="body.xhtml#continuation">Walton, in Continuation</a></li></ol></nav>
</body></html>""",
            "body.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h2>Chapter XXIV</h2><p>Victor speaks.</p>
<h2 id="continuation">Walton, in Continuation</h2><p>Walton writes.</p>
</body></html>""",
        })
        self.client.post(f"/api/projects/{doc_id}/source", data={"file": (epub, "source.epub")}, content_type="multipart/form-data")
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertEqual([chapter["title"] for chapter in dataset["chapters"]], ["Chapter XXIV", "Walton, in Continuation"])
        report = self._extraction_report(doc_id)
        self.assertFalse(report["toc"]["low_confidence"])

    def test_epub_nav_duplicate_title_is_low_confidence(self):
        doc_id = "extract_epub_nav_duplicate"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Duplicate Nav EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""
        epub = self._minimal_epub(opf, {
            "nav.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol><li><a href="c1.xhtml">Same Title</a></li><li><a href="c2.xhtml">Same Title</a></li></ol></nav>
</body></html>""",
            "c1.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Alice arrived.</p></body></html>""",
            "c2.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Bob waited.</p></body></html>""",
        })
        self.client.post(f"/api/projects/{doc_id}/source", data={"file": (epub, "source.epub")}, content_type="multipart/form-data")
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        report = self._extraction_report(doc_id)
        self.assertEqual(report["toc"]["toc_source"], "nav")
        self.assertTrue(report["toc"]["low_confidence"])
        self.assertEqual(report["toc"]["ambiguous_titles"], ["Same Title"])

    def test_epub_front_matter_guide_entries_are_skipped(self):
        doc_id = "extract_epub_front_matter"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Front Matter EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <manifest>
    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="toc" href="toc.xhtml" media-type="application/xhtml+xml"/>
    <item id="body" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="cover"/><itemref idref="toc"/><itemref idref="body"/></spine>
  <guide>
    <reference type="cover" href="cover.xhtml"/>
    <reference type="toc" href="toc.xhtml"/>
  </guide>
</package>"""
        epub = self._minimal_epub(opf, {
            "cover.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Cover</h1><p>Skip me.</p></body></html>""",
            "toc.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Contents</h1><p>Skip me too.</p></body></html>""",
            "chapter1.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter One</h1><p>Alice arrived.</p></body></html>""",
        })
        self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (epub, "source.epub")},
            content_type="multipart/form-data",
        )
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertEqual(len(dataset["chapters"]), 1)
        self.assertEqual(dataset["chapters"][0]["title"], "Chapter One")
        all_text = "\n".join(block["clean_text"] for block in dataset["blocks"])
        self.assertIn("Alice arrived.", all_text)
        self.assertNotIn("Skip me.", all_text)
        report = self._extraction_report(doc_id)
        self.assertTrue(report["front_matter_metadata"])
        self.assertEqual({item["file"] for item in report["skipped"]}, {"OPS/cover.xhtml", "OPS/toc.xhtml"})

    def test_epub_nav_landmarks_entries_are_skipped(self):
        doc_id = "extract_epub_nav_landmarks"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Landmarks EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>
    <item id="body" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="title"/><itemref idref="body"/></spine>
</package>"""
        epub = self._minimal_epub(opf, {
            "nav.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="landmarks"><ol><li><a epub:type="titlepage" href="title.xhtml">Title</a></li></ol></nav>
</body></html>""",
            "title.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Title Page</h1><p>Skip this title page.</p></body></html>""",
            "chapter1.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter One</h1><p>Alice arrived.</p></body></html>""",
        })
        self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (epub, "source.epub")},
            content_type="multipart/form-data",
        )
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        all_text = "\n".join(block["clean_text"] for block in dataset["blocks"])
        self.assertIn("Alice arrived.", all_text)
        self.assertNotIn("Skip this title page.", all_text)
        report = self._extraction_report(doc_id)
        self.assertTrue(report["front_matter_metadata"])
        self.assertEqual(report["skipped"][0]["file"], "OPS/title.xhtml")

    def test_epub_type_front_back_bodymatter_are_skipped(self):
        doc_id = "extract_epub_type_matter"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Matter EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="front" href="front.xhtml" media-type="application/xhtml+xml"/>
    <item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="back" href="back.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="front"/><itemref idref="c1"/><itemref idref="back"/></spine>
</package>"""
        epub = self._minimal_epub(opf, {
            "nav.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol>
  <li><a href="front.xhtml">Titlepage</a></li>
  <li><a href="chapter1.xhtml">Chapter One</a></li>
  <li><a href="back.xhtml">Colophon</a></li>
</ol></nav>
</body></html>""",
            "front.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body epub:type="frontmatter"><h1>Titlepage</h1><p>Skip front.</p></body></html>""",
            "chapter1.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body epub:type="bodymatter z3998:fiction"><h1>Chapter One</h1><p>Alice arrived.</p></body></html>""",
            "back.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body epub:type="backmatter"><h1>Colophon</h1><p>Skip back.</p></body></html>""",
        })
        self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (epub, "source.epub")},
            content_type="multipart/form-data",
        )
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertEqual([chapter["title"] for chapter in dataset["chapters"]], ["Chapter One"])
        all_text = "\n".join(block["clean_text"] for block in dataset["blocks"])
        self.assertIn("Alice arrived.", all_text)
        self.assertNotIn("Skip front.", all_text)
        self.assertNotIn("Skip back.", all_text)
        report = self._extraction_report(doc_id)
        self.assertTrue(report["front_matter_metadata"])
        skipped = {item["file"]: item["reason"] for item in report["skipped"]}
        self.assertIn("OPS/front.xhtml", skipped)
        self.assertIn("OPS/back.xhtml", skipped)
        self.assertIn("epub-type:front-matter:frontmatter", skipped["OPS/front.xhtml"])
        self.assertIn("epub-type:front-matter:backmatter", skipped["OPS/back.xhtml"])
        self.assertEqual(report["toc"]["toc_items"], 1)
        self.assertFalse(report["toc"]["low_confidence"])

    def test_epub_gutenberg_body_license_is_trimmed(self):
        doc_id = "extract_epub_gutenberg_trim"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Gutenberg Body EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest><item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""
        epub = self._minimal_epub(opf, {
            "chapter1.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Chapter One</h1>
<p>Alice arrived before dawn.</p>
<h1>THE FULL PROJECT GUTENBERG LICENSE</h1>
<p>Redistribution license text should not become a story block.</p>
</body></html>""",
        })
        self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (epub, "source.epub")},
            content_type="multipart/form-data",
        )
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        all_text = "\n".join(block["clean_text"] for block in dataset["blocks"])
        self.assertIn("Alice arrived before dawn.", all_text)
        self.assertNotIn("Redistribution license text", all_text)
        self.assertNotIn("THE FULL PROJECT GUTENBERG LICENSE", all_text)
        report = self._extraction_report(doc_id)
        trims = report["gutenberg"]["epub_body_trimmed"]
        self.assertEqual(trims[0]["file"], "OPS/chapter1.xhtml")
        self.assertEqual(trims[0]["blocks_removed"], 2)

    def test_epub_project_gutenberg_prose_is_not_trimmed(self):
        doc_id = "extract_epub_gutenberg_prose"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Gutenberg Prose EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest><item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""
        epub = self._minimal_epub(opf, {
            "chapter1.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Chapter One</h1>
<p>The librarian mentioned Project Gutenberg during ordinary dialogue.</p>
<p>Alice kept reading.</p>
</body></html>""",
        })
        self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (epub, "source.epub")},
            content_type="multipart/form-data",
        )
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        all_text = "\n".join(block["clean_text"] for block in dataset["blocks"])
        self.assertIn("Project Gutenberg during ordinary dialogue.", all_text)
        self.assertIn("Alice kept reading.", all_text)
        report = self._extraction_report(doc_id)
        self.assertNotIn("epub_body_trimmed", report.get("gutenberg", {}))

    def test_reextract_is_blocked_when_annotations_exist(self):
        doc_id = "extract_guard_annotations"
        dataset = self._make_txt_project(doc_id)
        block = next(block for block in dataset["blocks"] if "Alice" in block["clean_text"])
        start = block["clean_text"].index("Alice")
        response = self.client.post(f"/api/projects/{doc_id}/glossary/from-selection", json={
            "block_id": block["block_id"],
            "start": start,
            "end": start + len("Alice"),
            "source_term": "Alice",
            "expected_target": "Alice",
            "user": "tester",
        })
        self.assertEqual(response.status_code, 201)

        blocked = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": True, "user": "tester"})
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(blocked.get_json()["errors"][0]["code"], "annotations_present")

    def test_epub_upload_extract_and_validate(self):
        doc_id = "phase3_epub"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Phase 3 EPUB",
                "author": "Tester",
                "source_format": "epub",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        epub = BytesIO()
        with ZipFile(epub, "w") as zf:
            zf.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""")
            zf.writestr("OPS/content.opf", """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>""")
            zf.writestr("OPS/c1.xhtml", """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Chapter One</h1><p>Alice arrived.</p><p>"Hello," Bob said.</p>
</body></html>""")
        epub.seek(0)
        upload = self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (epub, "source.epub")},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201)
        extract = self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        self.assertEqual(extract.status_code, 201)
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        self.assertEqual(len(dataset["chapters"]), 1)
        self.assertTrue(any(block["block_type"] == "dialogue" for block in dataset["blocks"]))
        report = self._extraction_report(doc_id)
        self._assert_toc_report(report)
        self.assertEqual(report["toc"]["toc_source"], "none")
        self.assertTrue(report["toc"]["fallback_used"])
        validate = self.client.post(f"/api/projects/{doc_id}/validate").get_json()
        self.assertTrue(validate["data"]["ok"])

    def test_pdf_upload_is_rejected(self):
        doc_id = "phase3_pdf"
        self.client.post("/api/projects", json={"doc_id": doc_id, "metadata": {"license": "public-domain", "contamination_risk": "low"}})
        upload = self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (BytesIO(b"%PDF-1.4"), "source.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 400)
        self.assertIn("PDF logical extraction", upload.get_json()["errors"][0]["message"])

    def test_reference_lifecycle_and_freeze(self):
        doc_id = "phase4_freeze"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Phase 4 Freeze",
                "author": "Tester",
                "source_format": "txt",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (BytesIO(b"Chapter 1\n\nAlice arrived.\n\n\"Good morning,\" Bob said."), "source.txt")},
            content_type="multipart/form-data",
        )
        self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False, "user": "tester"})

        blocked = self.client.post(f"/api/projects/{doc_id}/freeze", json={"user": "tester"})
        self.assertEqual(blocked.status_code, 409)
        reasons = blocked.get_json()["errors"][0]["reasons"]
        self.assertTrue(any("unreviewed" in reason for reason in reasons))

        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        for block in dataset["blocks"]:
            self.client.patch(f"/api/projects/{doc_id}/review/blocks/{block['block_id']}", json={
                "reviewed": True,
                "reviewed_by": "tester",
            })
        chapter_id = dataset["chapters"][0]["chapter_id"]
        self.client.patch(f"/api/projects/{doc_id}/summary/{chapter_id}", json={
            "summary_source": "Alice arrives and Bob greets her.",
            "source": "human",
        })
        block_id = next(block["block_id"] for block in dataset["blocks"] if block["block_type"] != "heading")
        draft = self.client.post(f"/api/projects/{doc_id}/references/draft", json={
            "block_id": block_id,
            "draft_vi": "Alice đến.",
            "source": "human",
            "translated_by": "translator_01",
        })
        self.assertEqual(draft.status_code, 201)
        reference_id = draft.get_json()["data"]["reference_id"]

        missing_source = self.client.post(f"/api/projects/{doc_id}/references/{reference_id}/review", json={
            "source": "",
            "reviewed_by": "reviewer_01",
        })
        self.assertEqual(missing_source.status_code, 400)

        reviewed = self.client.post(f"/api/projects/{doc_id}/references/{reference_id}/review", json={
            "source": "human",
            "reference_vi": "Alice đến.",
            "reviewed_by": "reviewer_01",
        })
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.get_json()["data"]["status"], "reviewed")

        locked = self.client.post(f"/api/projects/{doc_id}/references/{reference_id}/lock")
        self.assertEqual(locked.status_code, 200)
        self.assertEqual(locked.get_json()["data"]["status"], "locked")

        export = self.client.post(f"/api/projects/{doc_id}/export")
        self.assertEqual(export.status_code, 201)
        self.assertIn("zip", export.get_json()["data"])

        freeze = self.client.post(f"/api/projects/{doc_id}/freeze")
        self.assertEqual(freeze.status_code, 201)
        self.assertTrue(freeze.get_json()["data"]["frozen"])

    def test_freeze_is_blocked_by_draft_reference(self):
        doc_id = "phase4_draft"
        self.client.post("/api/projects", json={
            "doc_id": doc_id,
            "metadata": {
                "title": "Phase 4 Draft",
                "source_format": "txt",
                "license": "public-domain",
                "contamination_risk": "low",
            },
        })
        self.client.post(
            f"/api/projects/{doc_id}/source",
            data={"file": (BytesIO(b"Chapter 1\n\nAlice arrived."), "source.txt")},
            content_type="multipart/form-data",
        )
        self.client.post(f"/api/projects/{doc_id}/extract", json={"overwrite": False})
        dataset = self.client.get(f"/api/projects/{doc_id}/dataset").get_json()["data"]
        for block in dataset["blocks"]:
            self.client.patch(f"/api/projects/{doc_id}/review/blocks/{block['block_id']}", json={
                "reviewed": True,
                "reviewed_by": "tester",
            })
        self.client.patch(f"/api/projects/{doc_id}/summary/{dataset['chapters'][0]['chapter_id']}", json={
            "summary_source": "Alice arrives.",
            "source": "human",
        })
        block_id = next(block["block_id"] for block in dataset["blocks"] if block["block_type"] != "heading")
        self.client.post(f"/api/projects/{doc_id}/references/draft", json={
            "block_id": block_id,
            "draft_vi": "Alice đến.",
            "source": "human",
        })
        blocked = self.client.post(f"/api/projects/{doc_id}/freeze")
        self.assertEqual(blocked.status_code, 409)
        reasons = blocked.get_json()["errors"][0]["reasons"]
        self.assertTrue(any("draft reference" in reason for reason in reasons))


if __name__ == "__main__":
    unittest.main()
