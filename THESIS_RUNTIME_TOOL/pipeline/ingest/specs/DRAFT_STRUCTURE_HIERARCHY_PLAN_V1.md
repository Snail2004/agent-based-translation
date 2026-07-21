# Draft Structure Hierarchy Plan V1

Status: Phase A2, experimental, proposal-only, 0-API implementation contract.

## 1. Purpose

Phase A1 may change unit boundaries, titles, and admission classifications. After
an approved A1 plan has been applied and the resulting canonical source package
has been materialized, Phase A2 may propose a hierarchy among the resulting
existing units.

Phase A2 never changes the canonical source package. It creates one sealed,
non-load-bearing evidence payload:

`draft_structure_hierarchy_overlay_v1`

The overlay is not consumed by D2L, Literary, Evaluation, the App, or any live
pipeline in this phase.

## 2. Required sequencing

1. Normalize a source into the accepted canonical package.
2. Apply and human-approve any A1 boundary/update plan.
3. Materialize and validate the complete post-boundary canonical package.
4. Rebuild the A1 report and global skeleton from that authoritative package.
5. Build an A2 hierarchy proposal against those exact identities.
6. Require explicit human approval of the A2 action set.
7. Validate the complete graph and emit the hierarchy overlay.

An A1 correction plan and an A2 hierarchy plan cannot be mixed or applied in
one transaction. A2 cannot reference an ID that does not exist in the current
post-boundary package.

## 3. Canonical immutability

The following inputs remain byte-identical before and after A2:

- `document.json`
- `structure_manifest.json`
- `asset_manifest.json`
- `admitted_projection_v1.json`
- source bytes and assets
- all chapter IDs, block IDs, unit IDs, block order, and exact block cover
- source text, clean text, admission channels, and policy identities

In particular, A2 does not write `parent_unit_id` back into the canonical
`structure_manifest`. That would invalidate the asset-manifest and admission
projection identities. The hierarchy overlay is a separate evidence sidecar.

## 4. Hierarchy plan

The plan schema version is `draft_structure_hierarchy_plan_v1`. It binds:

- authoritative source, document, structure, asset-manifest, admission, and
  draft-project-state identities;
- the complete post-boundary report identity;
- the complete global-skeleton identity;
- the global-structure policy identity.

The only actions are:

- `set_parent(child_unit_id, parent_unit_id)`
- `clear_parent(child_unit_id)`

Every action records the prior parent assignment and a content-addressed prior
assignment hash. Actions never mint IDs. Duplicate actions for one child,
unknown IDs, no-op actions, or invalid graph changes are sealed as
`review_required`, not silently applied.

Model and synthetic proposers are always `review_required`. A human must create
or reseal the approved action set with `proposer.kind = human` before apply.

## 5. Graph invariants

The effective assignment map exact-covers all existing units once. Code enforces:

- each unit has at most one parent;
- parent is null or an existing unit ID;
- no self-parent;
- no cycle;
- parent occurs before child in source order;
- every subtree occupies one contiguous source-order interval;
- therefore sibling/subtree intervals cannot cross.

Invalid or ambiguous proposals remain review-required. There is no first-match,
auto-reparent, forward reference, or best-effort repair.

## 6. Hierarchy overlay

The overlay schema version is `draft_structure_hierarchy_overlay_v1`. It is
sealed, deterministic, and marked `experimental_non_load_bearing`. Its ordered
rows exact-cover every existing unit as:

`child_unit_id -> parent_unit_id | null`

The overlay binds the same authoritative identities as the plan plus the plan
hash. Revalidation rederives the report and skeleton from the complete source
package, rebuilds the approved plan, recomputes the effective graph, and checks
the overlay hash. No consumer may treat this overlay as canonical in Phase A2.

## 7. LLM proposal boundary

The A2 context pack contains the complete ordered unit outline, current parent
assignments, and complete navigation evidence. It is bound to the authoritative
package, report, skeleton, policy, and draft lifecycle. The response exact-covers
every unit with one hierarchy action or one abstention.

Prompt rendering, response validation, and assistant execution all require the
complete authoritative package. If the complete hierarchy context exceeds the
configured prompt budget, the operation fails closed; A2 does not truncate or
independently shard a hierarchy graph.

The assistant output is proposal-only and cannot directly emit an applicable
overlay. Phase A2 tests use a fake executor only; no live API is called.

## 8. Lifecycle

Context generation and apply require:

- `project_state.lifecycle == draft`
- `project_state.pipeline_run_count == 0`

Active or completed projects fail closed before prompt generation or mutation.
Post-run migration and revision handling are out of scope.

## 9. Acceptance probes

The 0-API gate covers:

- valid nesting and valid root clearing;
- unknown, future, and removed IDs;
- self-parent, cycle, child-before-parent, crossing, and non-contiguous subtree;
- stale post-boundary report/skeleton/package identity;
- mixed A1/A2 plan rejection;
- resealed foreign semantic lineage;
- active/completed lifecycle rejection;
- overlay identity and payload-hash tamper;
- complete canonical-package byte identity;
- deterministic plan, prompt, response, overlay, and rerun hashes.
