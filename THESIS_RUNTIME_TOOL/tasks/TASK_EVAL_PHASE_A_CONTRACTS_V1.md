# TASK EVAL Phase A - Versioned Evaluation Contracts V1

Status: READY_FOR_REVIEW
Type: 0-API contract scaffold
Decision: DEC-013 (`765bc9b`), DEC-015 (`36b2e87`)
Author/Implementer: CodeX

## 1. Objective

Establish the first one-way, versioned Evaluation boundary without moving or
executing any scorer. Phase A defines:

1. deterministic JSON canonicalization and self-hash rules;
2. a closed, gold-free `D2LEvaluationInputV1` runtime snapshot;
3. a closed persisted `FullRunReportV1` projection for App read-only relay;
4. fixture-only adversarial tests proving ordering, leakage, identity,
   reference, path, usage-null, and one/two-arm behavior.

This task creates contracts only. It does not claim that a D2L exporter,
Evaluation writer, scorer migration, or App integration already exists.

## 2. Exact Scope

### In scope

- `pipeline/eval/contracts_v1.py`
- `pipeline/eval/d2l_input_v1.py`
- `pipeline/eval/full_run_report_v1.py`
- the three matching test modules;
- four fixed JSON fixtures under `pipeline/tests/fixtures/evaluation_v1/`;
- this task record.

### Out of scope

- held scorers: `builder_gold.py`, `occurrence_adherence.py`,
  `term_policy.py`, `d2l_translate_score.py`;
- held CLI: `score_run.py`;
- `pipeline/eval/__init__.py`;
- D2L runtime/exporter, Literary, App backend/UI, ingest, memory, retrieval,
  windower, SQLite/schema, provider clients, and central coordination files;
- database access, scorer execution or migration, report writer wiring,
  callbacks, API, LLM, or provider usage.

## 3. Mechanical Contract

### 3.1 Closed schemas

Every object has a required/optional key table. Unknown keys are fatal at all
levels. Enum values are closed. IDs and references are validated mechanically.
JSON values must be finite and serializable.

### 3.2 Canonical ordering table

- Object keys are serialized deterministically.
- Every list path is declared exactly once as either set-like or a semantic
  sequence.
- Set-like lists sort by canonical element bytes and reject duplicates.
- Semantic sequences preserve input order.
- An unclassified list or a field in both classes is fatal.
- Strings are NFC-normalized in the returned copy.
- Validation and canonicalization never mutate caller input.

### 3.3 Self-hash

`D2LEvaluationInputV1.integrity.package_sha256` and
`FullRunReportV1.integrity.report_sha256` are computed after removing only the
respective self-hash field. Artifact-set hashes cover the canonical artifact
rows. Hashes use lowercase SHA-256.

### 3.4 D2LEvaluationInputV1

The package contains only immutable runtime facts:

- producer and project/run/document/profile identities;
- ordered selected chapters and source blocks;
- explicit translation arms and exact-covered translation rows;
- runtime profile, glossary rows, injection facts, and provenance artifacts;
- package/artifact hashes.

The recursive negative list rejects keys or authority roles representing gold,
oracle, human reference content, eval fixes/overrides, scores, thresholds,
recommendations, or result callbacks. Source prose is not scanned for those
words because a technical source may legitimately contain them.

Translation rows must reference their own arm artifact. Every non-excluded
block has one explicit row per arm, including missing/failed rows. Preserved
blocks are byte-equal passthrough. Runtime term support keeps source order.

### 3.5 FullRunReportV1

The persisted report contains explicit:

- report/producer/method/version identity;
- project/logical-run/attempt identity;
- arms and immutable translation hashes;
- metric method, per-arm values, persisted comparison, claim, usage, stages,
  artifacts, and caveats;
- explicit missing/failed/not-applicable artifact states.

One-arm reports never fabricate S0, delta, comparison, or BETTER/NOT_BETTER.
S0/S1 authority comes from explicit arm roles, never labels or array position.
Unknown usage remains null. Validators do not sum tokens, price usage,
recompute delta, or reconstruct a verdict.

## 4. Acceptance

1. Three focused test modules pass.
2. Existing offline test suite passes without held-file edits.
3. Unknown keys, recursive forbidden runtime data, unsafe paths, unknown
   references, mismatched self/artifact hashes, non-finite values, and bad
   ordering fail closed.
4. Reordering set-like fields preserves hashes; reordering semantic sequences
   changes hashes.
5. Input objects remain byte-equivalent after validation calls.
6. One-arm and S0/S1 fixtures validate under the same schema.
7. Usage nulls are preserved and stage totals are not used to rewrite report
   totals.
8. Exact write-set, whitespace, credential, and no-API/no-DB scans are clean.

## 5. Implementation Evidence

Implemented the exact DEC-013 write-set without editing held scorers, CLI,
runtime exporters, App files, `pipeline/eval/__init__.py`, database files, or
provider code.

Public integration surface:

- `pipeline.eval.full_run_report_v1.validate_full_run_report` is a stable,
  direct-import public function;
- it validates the closed schema, semantic references, self/artifact hashes,
  finite values, one-arm constraints, artifacts, claims, and usage facts;
- it does not mutate its input and returns a detached canonical copy;
- an App transport may validate and then relay its original parsed payload to
  preserve persisted semantic-sequence order.

Verification on the implementation worktree:

- focused contract suite: `50 passed in 0.49s`;
- Python compilation: passed for all three contract modules;
- full applicable offline suite on final bytes: `925 passed, 1 skipped,
  2 deselected in 124.62s`;
- initial raw full suite, before the final additional focused self-hash probe:
  `924 passed, 1 skipped, 2 failed in 139.96s`; both failures are pre-existing
  environment probes requiring the untracked, absent
  `data/jobs/d2l_p1/memory.sqlite3` frozen database;
- `git ls-files` confirms that frozen database is not part of this worktree's
  Git checkout; DEC-013 did not copy or create it to manufacture a green run;
- no API call, provider allocation, production DB read/write, callback, or
  scorer execution occurred.

The two deliberately deselected environment-only tests were:

- `test_probe_31_frozen_database_hash_is_unchanged`;
- `test_probe_22_existing_foundation_is_not_imported_or_mutated`.

Focused adversarial coverage includes unknown and non-string keys, recursive
gold/oracle/eval-authority leakage, non-finite numbers, unsafe paths, missing
and cross-arm references, semantic ordering, self/artifact hash tampering,
one-arm fabricated comparison/claim data, comparison-role authority,
artifact/status/hash consistency, null-preserving usage, usage provenance,
and input immutability.

## 6. Review Handoff

Independent review must inspect the committed bytes, rerun focused/full tests,
and verify that no runtime/gold callback or held-file dependency was added.
