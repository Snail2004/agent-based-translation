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

## Data-source policy

- `GET /projects/<doc_id>/source-package` is authoritative for mode,
  lifecycle, permissions, source identity, and frozen state.
- `GET /projects/<doc_id>/source-package/review` is authoritative for units,
  issues, outline/navigation/candidate evidence, supported actions, and the
  expected state/tree/report/hierarchy identities.
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
- A `409` is never retried. The UI refreshes status/review, displays the server
  error code and message, and asks the user to inspect the new revision.
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

- Left: ordered backend report units, classification, block count, and review
  flags.
- Center: backend block IDs, issue evidence, global candidate evidence, and
  TOC/navigation evidence when present.
- Right: update title/classification and set/clear an earlier parent unit.
- Split is limited to an existing block boundary supplied by the selected
  unit. Merge is limited to the immediately following report unit.
- Full hashes and candidate/run identities stay collapsed under technical
  details.

The review contract currently does not relay block text or a revision history
list. V1 shows block IDs and the evidence that exists instead of reading a
parallel preview source or fabricating history.

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
2. Use `source_package_dev.html` for unmanaged, draft, stale, finalized,
   frozen, legacy, and publication fixture states.
3. Exercise update, split, merge, hierarchy, finalize, prepare, and publication
   actions. Confirm stale refreshes without retry.
4. Capture 1440x900, 1024x768 (plus 900px compatibility), and 390x844 views.
5. Check keyboard focus, modal confirmation, console errors, overlap, and
   horizontal overflow.
6. Run `git diff --check`, secret scan, and exact owned-path scan.
