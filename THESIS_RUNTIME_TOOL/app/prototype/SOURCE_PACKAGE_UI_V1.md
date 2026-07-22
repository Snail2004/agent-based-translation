# Managed Source Package UI V1

## Scope

This surface is a consumer of the frozen Source Package lifecycle. It owns no
normalizer, package, hash, identifier, overlay, publication path, or runtime
manifest logic. Production code calls the existing backend only through
`app/prototype/api.js`.

Supported source formats shown by the UI are TXT, EPUB, Markdown, HTML, and
PDF. The managed workflow is:

`source uploaded -> managed draft -> review/revision -> finalized_pre_run -> run_started_frozen`

`legacy_only` remains on the legacy path and is never normalized implicitly.

## D2L pre-segmented import

The quick-import modal exposes **D2L pre-segmented bundle** as a distinct
managed-source path. It is wired to backend candidate `1f6e2c6f` and must not
fall back to the generic Markdown upload/normalization path.

- The UI first creates a new local project, then sends one synchronous
  `multipart/form-data` request to
  `POST /projects/<doc_id>/source-package/import-d2l-presegmented`.
- The request contains exactly one file for each field: `source` named
  `d2l_full_book_en_marked_v1.md`, `block_map` named `block_map.json`, and
  `manifest` named `manifest.json`. It sends no query parameters, options,
  aliases, ZIP, duplicate, or extra form fields.
- Filename, completeness, and duplicate-file checks run before submission.
  The submit action stays disabled until the three-file bundle and new project
  metadata are valid. While the synchronous request is active, the modal is
  locked and shows an indeterminate busy state; it never fabricates percentage
  progress.
- Backend error `code`, `message`, and HTTP status are shown without rewriting
  the request or attempting generic import. On success, the App reloads source
  status, opens **Structure**, and loads the authoritative review payload.
- Production never reads the D2L bundle from a fixture or client filesystem
  path after upload. Unit content continues to come only from the bound review
  unit-block endpoint described below.

The backend candidate is not copied into this UI branch. Promotion and
integration of backend commit `1f6e2c6f` are separate coordination work.

## Data-source policy

- `GET /projects/<doc_id>/source-package` is authoritative for mode,
  lifecycle, permissions, source identity, and frozen state.
- `GET /projects/<doc_id>/source-package/review` is authoritative for units,
  outline/navigation/candidate evidence, supported actions, the six expected
  identities, and the ordered `source_package_issue_queue_v1`. Issue
  navigation comes only from the queue's explicit nullable targets and stable
  `navigation` object; the UI does not reconstruct targets from issue text.
- Block text/type is rendered only from
  `GET /projects/<doc_id>/source-package/review/units/<unit_id>/blocks`. Every
  page sends the current `state_sha256`, `candidate_tree_sha256`,
  `document_sha256`, `structure_sha256`, and `report_sha256` exactly once. The
  UI follows backend pagination up to 200 rows per request and accepts the
  result only when `source_package_unit_blocks_v1` exactly covers the ordered
  block IDs of the selected review unit. Missing, partial, malformed, duplicate,
  or stale pages fail closed.
- The UI renders only fields present in those payloads. It does not infer a
  lifecycle from chapter or block counts and does not calculate quality
  metrics.
- Production does not load `fixtures/source_package_ui_v1/scenarios.js`.
  `source_package_dev.html` is the isolated browser harness.

## Mutation contract

- Normalize always sends exactly `{}`.
- Correction and hierarchy requests use the latest review snapshot, set
  `approved: true`, use the current UI user, and contain only backend-supported
  action shapes.
- Finalize sends the latest expected state, candidate tree, report, and
  hierarchy hashes. No client-generated hash or path is sent.
- Every successful mutation invalidates the local review snapshot and triggers
  a fresh status plus review load.
- A `409` is never retried with stale identities. This applies to mutations and
  unit-block reads. The UI refreshes status/review, clears boundary/merge/issue
  selection, and asks the user to inspect the new revision.
- `managed_finalized_pre_run` exposes prepare/run only. The stricter App UI
  product gate deliberately does not offer post-finalize correction even
  though the backend can create a new draft revision.
- `managed_run_started_frozen` is absolutely read-only.

## Publication guard

`publishSourcePackage(docId, overlay)` sends one exact
`canonical_translation_overlay_v1` object unchanged. The UI never constructs
an overlay from block previews, translation rows, reports, SQLite, run artifact
paths, or client filesystem paths.

The production App currently has no authoritative translation-overlay
producer/relay in its state or API. Therefore **Xuất tài liệu** is disabled and
explains the missing contract. The dev harness may inject a fixture overlay to
exercise the enabled response rendering, including `publication_id` and the
artifact fields returned by the fake endpoint.

The backend publication response has artifact metadata/paths but no dedicated
download route in the frozen contract. V1 displays those returned paths and
does not synthesize a URL.

## Review model and controls

- Left: ordered backend report units, classification, block count, review
  flags, and explicit merge-pair selectors.
- Center: backend block IDs plus up to three lines of source text and block type
  loaded from the five-bound unit-block endpoint. The issue counter starts a
  previous/next sequence, selects the queue's navigation unit, and highlights
  its target block when present.
- Right: update title/classification and set/clear an earlier parent unit. At
  920px and below this surface is a dismissible detail drawer instead of a
  third stacked column.
- Split stays disabled until the reviewer selects an existing boundary with
  its grip control. Merge stays disabled until exactly two adjacent report
  units are selected.
- Full hashes, candidate/mechanical signals, and TOC/navigation evidence stay
  collapsed under technical details.

The review contract does not expose a revision-history list. V1 therefore does
not fabricate history. Block content and issue navigation are wired to the
backend contract implemented at `720ffe08`; the dev fixture mirrors those two
schemas without being loaded by production.

## Guard

- Managed review with missing expected identities or `report.units` locks all
  mutations.
- While one action is in flight, all mutation actions are disabled.
- Frozen/finalized modes disable edit controls even if stale local form state
  remains.
- Parent options are existing earlier units only; self-parent and forward
  parent choices are not offered. Backend remains the final validator.
- Legacy and malformed/tampered responses fail closed.

## Known gap

Production export remains blocked until an authoritative producer/relay makes
the exact canonical translation overlay available to App state/API. A backend
download route is also absent, so publication artifacts are identifiers/paths,
not clickable downloads. Neither gap is bypassed in frontend code.

## Test plan

1. Compile modified JSX with esbuild.
2. Against backend candidate `1f6e2c6f`, import the real three-file D2L bundle
   and confirm the request is sent once with the exact endpoint and fields,
   then confirm the App reloads source status/review in Structure.
3. Use `source_package_dev.html` for unmanaged, draft, stale, finalized,
   frozen, legacy, and publication fixture states.
4. Exercise update, split, merge, hierarchy, finalize, prepare, and publication
   actions. Confirm stale correction and stale unit-block reads refresh without
   retry. Exercise valid, unavailable, partial, and stale unit-block responses.
5. Capture 1440x900, 1024x768 (plus 900px compatibility), and 390x844 views.
6. Check issue previous/next focus, explicit split/merge selection, mobile
   drawer close/Escape focus return, modal confirmation, console errors,
   overlap, and horizontal overflow.
7. Run `git diff --check`, secret scan, and exact owned-path scan.
