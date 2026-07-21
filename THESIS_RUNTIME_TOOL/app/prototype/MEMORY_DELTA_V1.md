# Memory Delta V1

`memory_delta_v1` is the read-only UI projection used by the Agent Console to
show durable Builder and Auditor memory changes over time.

This document describes a **prototype candidate**, not an accepted runtime
emitter contract. The current development harness supplies synthetic events so
the Console can be tested before the terminology and literary pipelines expose
an authoritative outbox. Production emitters require a separate integration
decision and may refine field names without changing the UI principles below.

## Boundary

- The Console consumes persisted events and never mutates memory.
- The Console does not diff complete registry snapshots.
- The Console does not interpret raw LLM output.
- V1 shows only changes whose memory write has already committed durably.
- A producer derives the delta from a durable commit receipt and delivers it
  through a retry-safe outbox; a proposal or review decision is not a delta.
- A delta is a small display projection, not a complete stored record.
- Unknown enum values or incomplete events are ignored by the panel.
- Canonical source, translation artifacts, evaluation reports, and memory
  deltas remain separate read-only inputs.
- Evaluation never emits memory deltas.
- Historical runs without persisted deltas are not reconstructed by inference.

## Event

The outer event uses `event = "memory_delta"`. Its payload is:

```json
{
  "contract": "memory_delta_v1",
  "domain": "terminology",
  "collection": "term",
  "operation": "revised",
  "lifecycle": "committed",
  "delta_id": "mdv1_6e5bfbce31bd559fd9fdad5a",
  "record_id": "term_regularization",
  "label": "regularization",
  "revision_before": 1,
  "revision_after": 2,
  "record_hash_before": "78e52a38a1342a862c5a303392c9b83310272e0b17253ab622b9da4e7cd996c0",
  "record_hash_after": "44c1824d59c44d466ce2d0aa72fb533b22b4362b50d20642ee1cd1fd1b34a56e",
  "before": {
    "target": "chuẩn hóa",
    "status": "context_sensitive"
  },
  "after": {
    "target": "điều chuẩn",
    "status": "mandatory"
  },
  "evidence_delta": 2,
  "source_refs": [
    {
      "chapter_id": "chapter_01",
      "block_id": "chapter_01_b004"
    }
  ],
  "reason_code": "evidence_gated_revision",
  "commit_receipt": {
    "receipt_id": "receipt_term_demo_g02",
    "state_generation": 2,
    "committed_at": "2026-07-18T03:05:00Z"
  }
}
```

## Closed Values

`domain`

- `terminology`
- `literary`

`domain` and `collection`

- `terminology` -> `term`
- `literary` -> `entity` or `term`

`relation` and `summary` are intentionally excluded until they have
authoritative stores.

`operation`

- `added`
- `reinforced`
- `revised`

`lifecycle`

- `committed`: persisted working-memory state

## Required Fields

- `contract`
- `domain`
- `collection`
- `operation`
- `lifecycle`
- `delta_id`
- `record_id`
- `label`
- `revision_before` and `record_hash_before` for a non-add operation
- `revision_after`
- `record_hash_after`
- at least one exact `source_refs` entry with `chapter_id` and `block_id`
- `commit_receipt.receipt_id`
- `commit_receipt.state_generation`

`revision_before` and `record_hash_before` are `null` for `added`. `delta_id`
must be deterministic so outbox retry cannot create a duplicate row. `before`
and `after` remain compact display projections. Full records belong in the
Memory workspace, not the Console event stream.

## Replay

The panel derives its state from the same event prefix as the Event Stream.
Seeking backward hides later deltas; seeking forward reveals them in event
order. Replay never calls an API and never changes persisted run data.

## Console Ledger Views

The compact bottom ledger keeps three meanings separate:

- **Thay đổi** lists only committed `memory_delta_v1` events in replay order.
- **Hiện có** folds the latest committed revision per `record_id` from the
  visible run prefix. It is a run-local projection, not the full registry.
- **Chờ xử lý** renders the existing watchlist/held input. These rows are not
  committed deltas and never imply a memory write.

The ledger header may summarize the latest context-pack size, rendered
terminology adherence, and held count. These summaries replace the former
detailed Memory Content, Consistency, and Watchlist sections in the narrow
right rail; the authoritative details remain available in their dedicated
artifacts/workspaces.

## Synthetic Fixture

`fixtures/one_button_preface_golden/memory_delta_fixture.json` is the shared UI
fixture. It is explicitly marked `prototype_only` and contains:

- terminology `term` commits;
- literary `entity` and `term` commits;
- no relation, summary, evaluation, candidate, or reviewed events.

It is loaded only by `console_dev.html`; it is never merged into the historical
golden event log or a real run.
