# Source Structure Normalizer Phase 0/1

## Summary

Phase 0/1 implements a deterministic core for a source-structure normalizer. It does not call an LLM and does not change schema `1.4.0` or the default extractor path. The normalizer is opt-in: code first builds candidate parts from immutable TXT/EPUB source, then a validated `StructurePlan` chooses drops, chapter headings, optional merges, and EPUB section roles. Code applies that plan and writes working artifacts:

- `working/normalized/structure_plan.json`
- `working/normalized/normalized_document.json`
- `working/normalized/normalization_report.json`

The implementation lives in `app/backend/services/structure_normalizer.py`.

## Canterville Baseline

Source fixture:

```text
../AILAB_SOURCES_RAW/canterville_ghost/source.txt
../AILAB_SOURCES_RAW/canterville_ghost/source.epub
```

Direct extractor results before normalizer:

| Format | Direct extractor result | TOC report | Verdict |
|---|---:|---|---|
| TXT | 1 chapter: `Chapter 1` | `toc_source=none`, `fallback_used=True`, `low_confidence=False` | Misses bare Roman-numeral chapters |
| EPUB | 3 chapters: `WILDE`, `VI`, `VII` | `toc_source=ncx`, `toc_items=11`, `anchor_split_failed`, `low_confidence=True` | Front matter leaks and chapters are incomplete |

## Normalizer Result

Manual StructurePlans were applied after building candidate parts.

| Format | Candidate parts | Normalized chapters | Block counts | Dropped parts | Drop fraction | Flags |
|---|---:|---|---|---:|---:|---|
| TXT | 151 | `I`, `II`, `III`, `IV`, `V`, `VI`, `VII` | 22, 6, 12, 10, 36, 14, 18 | 26 | 0.0199 | none |
| EPUB | 166 | `I`, `II`, `III`, `IV`, `V`, `VI`, `VII` | 22, 6, 11, 10, 41, 14, 18 | 37 | 0.0499 | none |

The TXT plan drops Project Gutenberg/title/illustration front matter at parts `0-25` and uses Roman numeral headings at parts `26,49,56,69,80,117,132`.

The EPUB plan drops front matter at parts `0-30`, uses Roman numeral headings at parts `31,54,61,73,84,126,141`, and marks spine `3` as `back_matter` to remove the Project Gutenberg license.

## What Improved

- Canterville TXT improves from `1` fallback chapter to `7` intended chapters.
- Canterville EPUB improves from noisy `WILDE/VI/VII` extraction to `7` intended chapters.
- Front/back matter removal is explicit and audited with dropped-part snippets.
- The bridge reuses existing `block_type_for()` and `chapters_to_document()`; no block-type logic was reimplemented.
- Default extraction is unchanged; normalizer output is only used when explicitly applied.

## Invariants Covered

Unit tests cover:

- deterministic Canterville TXT candidate parts (`151` parts)
- direct extractor baseline for TXT and EPUB
- manual TXT and EPUB StructurePlans
- content invariance for every normalized block
- fingerprint mismatch rejection
- part cannot be both dropped and heading
- invalid merge separator rejection
- drop fraction guard (`> 0.30` flags `low_confidence` and `needs_human_check`)
- pre-heading content is preserved and flagged, not silently dropped
- empty headings fallback to one chapter and flag
- EPUB role precedence drops a whole front/back-matter spine before part-level rules

## Deferred Work

- LLM/Agent generation of `StructurePlan` is intentionally out of scope for Phase 0/1.
- The current EPUB fixture is manual. A later phase should expose this through the web tool as an opt-in action and let users inspect candidate parts before approving a plan.
- If a source has one candidate part containing multiple real paragraphs, the normalizer still only flags `needs_human_check`; it does not split by offset.

## Verification Commands

```powershell
cd "C:\Users\nguye\OneDrive\Tài liệu\Baitap\DuAnCNTT\odl-pdf-demo\research\agent-based-translation\AILAB_HANDOFF"

python dataset_spec\tools\validate.py --dataset dataset_spec\sample\gold_demo_01 --schema dataset_spec\schema
python -m unittest discover app\backend\tests
```

Observed results:

```text
AILAB dataset validation: PASS
Ran 63 tests in 8.644s
OK
```
