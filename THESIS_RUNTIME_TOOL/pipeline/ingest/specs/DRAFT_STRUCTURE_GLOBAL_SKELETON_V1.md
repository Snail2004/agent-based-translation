# Draft Structure Global Skeleton V1

Status: implementation contract for DEC-031 Phase A1. This document does not
authorize Phase A2 hierarchy mutation or a live model call.

## 0. Objective

Prevent a structurally incomplete document from being reported as clean merely
because a parser demoted a heading to prose, failed to resolve an EPUB TOC
target, flattened repeated numbering, or did not pre-flag the affected unit.

The system has two separate responsibilities:

1. **Inventory** is deterministic and whole-book. It records every structural
   signal before any prompt focus or token budget is applied.
2. **Decision context** is bounded. It shards the inventory and adds local prose
   only around the candidate currently being reviewed.

The model may propose a bounded correction. Code validates IDs, exact cover,
source immutability, scope, and lifecycle. A human approval gate remains
load-bearing. No proposal changes the canonical package by itself.

## 1. Scope and non-scope

### 1.1 Phase A1 writes

- `pipeline/ingest/draft_structure.py`
- `pipeline/ingest/draft_structure_llm.py`
- ingest-owned tests and fixtures
- this specification

### 1.2 Explicitly out of scope

- changes to EPUB, HTML, Markdown, TXT, or PDF normalizers;
- changes to `document.json`, the locked schema, block IDs, chapter IDs,
  canonical text, clean text, assets, or admission rows;
- App/backend, D2L, Literary, Evaluation, SQLite, checkpoint, or consumer wiring;
- live API calls;
- hierarchy mutation (`set_parent`), which is Phase A2;
- named-book exceptions, fuzzy first-match ownership, OCR, or invented text.

Frozen producer and run artifacts remain immutable. Only a new project in the
pre-run Draft Structure lifecycle is eligible for a later approved correction.

## 2. Data flow

```text
canonical package + structure sidecars
                    |
                    v
      GlobalStructureSkeletonV1 (whole book, sealed)
                    |
          +---------+----------+
          |                    |
          v                    v
  Draft Structure report   bounded context packs
                               |
                               v
                 candidate-scoped advisory response
                               |
                               v
                 existing correction-plan validator
                               |
                               v
                     explicit human approval
```

Inventory generation is never conditional on `include_all_units`, issue focus,
prompt budget, or a default-off feature flag.

Self-hashes are necessary but not authoritative. Before code may construct or
render any model context, it rebuilds the expected skeleton from the current
`document.json`, structure manifest, asset manifest, admission projection, and
serialized policy, then requires exact equality. A correctly re-sealed payload
with a foreign or re-owned block reference therefore remains invalid.

## 3. GlobalStructurePolicyV1

The policy is a closed, serialized object and part of the skeleton identity.
Defaults are book-neutral and testable:

| Field | Meaning |
|---|---|
| `mechanical_line_max_chars` | Maximum normalized line length considered by the mechanical detector. |
| `high_navigation_mismatch_ratio` | Unresolved TOC ratio that creates a document issue after the minimum entry count. |
| `high_navigation_mismatch_min_entries` | Minimum TOC size before the mismatch ratio gate applies. |
| `candidate_overflow_threshold` | Candidate count that creates an overflow issue; it never truncates inventory. |
| `signal_starvation_min_blocks` | One-unit document size at which absence of usable structural signals becomes review-required. |
| `text_like_block_types` | Closed runtime block types eligible for mechanical candidate detection. |

Policy changes mint a new skeleton hash. No threshold may contain a title,
author, corpus name, or expected answer.

## 4. Candidate inventory

Every row has one deterministic `candidate_id` bound to the complete input
identity. Every row uses existing block/unit/navigation IDs only.

Closed candidate kinds for A1:

1. `existing_unit_boundary`: every current boundary after the first unit. This
   permits a later proposal to merge adjacent units without hiding boundaries
   that the parser already created.
2. `internal_heading`: a runtime heading block that is not the first block of
   its owning unit.
3. `mechanical_text_boundary`: a short text-like block with a strong mechanical
   chapter signal (chapter marker, Roman/Arabic ordinal, or uppercase title).
4. `navigation_entry`: every TOC/navigation entry, including already mapped,
   uniquely title-mapped, zero-match, and multi-match entries.
5. `duplicate_title_group`: every normalized title/number that occurs in more
   than one unit or navigation entry.
6. `numbering_restart`: every non-increasing restart in a comparable ordered
   chapter/book/part numbering family.
7. `signal_starvation`: one document-level candidate when a sufficiently large,
   single-unit document has no boundary signal.

Candidates contain IDs and compact structural evidence, not copied canonical
prose. Local text previews are rendered later from `document.json`.

## 5. Navigation resolution

Resolution order is closed:

1. Accept an existing numeric sidecar `mapped_block` only when it addresses one
   current block exactly.
2. Otherwise normalize the navigation title with Unicode NFKC, case-folding,
   whitespace collapse, and punctuation-to-space normalization.
3. If the navigation entry declares a target file, compare only blocks whose
   source-map file is exactly that value. An absent source-map scope remains a
   zero-match; it never falls back to a same-titled block in another file. If
   the entry declares no target file, compare the whole book.
4. Bind the title only when exactly one block has the same normalized text.
5. Zero matches remain `unresolved_zero_match` with an empty candidate set.
6. Multiple matches remain `unresolved_multiple_match` with the complete ordered
   candidate block set. Never pick the first match.

Duplicate titles remain explicit evidence even if another entry has a valid
anchor mapping.

## 6. Issue taxonomy

The skeleton emits sealed issues with deterministic IDs:

- `global_structure_high_navigation_mismatch`
- `global_structure_unresolved_navigation`
- `global_structure_duplicate_title_group`
- `global_structure_numbering_restart`
- `global_structure_internal_heading`
- `global_structure_signal_starvation`
- `global_structure_candidate_overflow`

Unit-scoped issue codes are projected onto the Draft Structure report unit so a
legacy issue-focused view cannot call the affected unit clean. Document issues
remain in the skeleton and report issue list.

## 7. Bounded context V2

`GlobalStructureContextV1` is separate from the legacy unit-focused response
contract. Across one document, packs must exact-cover every `candidate_id`
exactly once. Outline and navigation rows are supporting context: a bounded
neighbor row may appear in more than one pack when different candidates need
it. The complete, non-truncated outline/navigation inventories remain sealed in
the global skeleton.

Each pack contains:

- identities and policy hash;
- global statistics;
- one outline shard;
- one navigation shard;
- one candidate shard;
- local previews only for uniquely anchored candidates in that shard;
- candidate-specific allowed actions.

The builder increases shard count until each prompt fits the explicit character
budget. It never removes persisted candidates to make a prompt fit. If even one
bounded row cannot fit, the operation fails closed for manual review.

Pack construction and public prompt rendering require the complete
authoritative source-package inputs. A caller cannot authorize a prompt from a
standalone self-consistent report or skeleton.

## 8. Candidate-scoped response

The response is a strict object containing `actions` and `abstentions`.

An action wrapper has exactly:

```json
{
  "candidate_id": "cand_...",
  "proposal": {"action_type": "..."}
}
```

An abstention has exactly:

```json
{
  "candidate_id": "cand_...",
  "reason": "insufficient_context"
}
```

Every assigned candidate is covered exactly once. An action is accepted only if
it is in that candidate's closed scope:

- split only at the candidate's existing anchored block;
- merge only for the candidate's existing adjacent unit pair;
- update only an existing unit referenced by the candidate;
- unresolved zero/multi-match and group-only candidates are abstention-only.

The validator strips `candidate_id` before handing proposals to the existing
correction-plan builder. Non-human proposals remain review-required.

## 9. Phase A2 sequencing (not implemented in A1)

Hierarchy is not a fourth action in the boundary correction plan.

1. Apply approved A1 split/merge/update actions first. Code mints deterministic
   new unit/chapter IDs.
2. Rebuild and seal the report/skeleton from the resulting package.
3. Build a separately versioned parent plan that references only IDs present in
   that rebuilt report.
4. Validate one parent at most, no self-parent, no cycle, parent-before-child,
   and non-crossing contiguous subtrees.
5. Apply hierarchy only to additive structure sidecars. IDs, ordered block
   cover, source text, assets, and admission rows remain unchanged.

No plan may contain both a boundary action and a parent action.

## 10. Acceptance probes

A1 is complete only when all probes are green:

1. A demoted heading is discovered with every default flag unchanged.
2. A title-case TOC entry maps only to one exact block.
3. Duplicate chapter numbering produces explicit group/restart issues and never
   first-match ownership.
4. A signal-starved large TXT document becomes review-required.
5. Candidate overflow preserves every candidate and shards context without
   truncation.
6. Context packs exact-cover candidate IDs; the sealed skeleton retains the
   complete outline and navigation inventories.
7. Zero/multi-match navigation remains unresolved.
8. Tampered input, skeleton hash, candidate ID, scope, or pack coverage fails
   closed. A foreign navigation/candidate block reference still fails after an
   attacker recomputes every affected ID and hash.
9. Source/clean text, assets, block order, and exact cover are unchanged.
10. Two deterministic runs produce byte-identical skeletons and context packs.
11. A bounded offline Gulliver audit turns the previously silent 46/48 TOC
    mismatch and repeated numbering into explicit review evidence.

## 11. Stop conditions

Stop and return to architecture review if:

- required evidence is absent from existing sidecars and would require a
  normalizer edit;
- a candidate cannot be tied to existing IDs or an explicit unresolved set;
- exact-cover packs cannot remain bounded without dropping evidence;
- the implementation would require source mutation, model-minted IDs,
  consumer wiring, or hierarchy in the A1 commit.
