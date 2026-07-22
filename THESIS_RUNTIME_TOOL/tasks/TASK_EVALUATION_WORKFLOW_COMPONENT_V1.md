# Evaluation Workflow Component V1

Status: Phase A, fixture-only and 0-API.

This task implements the Evaluation-side component boundary from DEC-057. It
does not implement the neutral parent workflow relay and does not start a live
benchmark. The long run remains blocked until the D2L component, the neutral
relay, and this component pass their independent gates.

## Ownership

Evaluation owns only:

- `component_manifest.json` semantics for `component_id=evaluation`;
- the Evaluation-local `events.jsonl` stream and its `component_seq`;
- the Evaluation artifact index;
- `scoring_receipt_v1`, including the exact accepted handoff echo;
- local validation and checkpoint identity rules.

Evaluation does not write `workflow_manifest.json`, the parent `events.jsonl`,
the parent `artifact_index.json`, or the parent global sequence. A neutral
relay is the only authority for those objects.

## Handoff boundary

`ScoringHandoffV1` is accepted only as a typed, already sealed object. The
validator never scans a directory and never discovers a missing artifact by
path search. It requires:

- the exact ordered arm list `s0`, `s1`, `community`, `google_nmt`, `llm_lc`;
- a typed binding for every translation artifact;
- a producer component/run for every arm; Evaluation and the relay cannot
  author a translation input;
- a six-part source package binding: document, structure manifest, asset
  manifest, admitted projection, normalization receipt and package seal;
- optional glossary/context/projection bindings represented as either `null`
  or a complete typed binding;
- exact coverage accounting over one shared admitted block universe;
- a relay-computed `input_set_sha256` over the ordered input rows;
- a content hash on the handoff itself.

`sha256_kind` is explicit. The contract accepts `physical` or a named
`canonical:<contract>@<semver>` authority; a bare or ambiguous hash is
rejected. No aggregate `source_package_sha256` is invented by Evaluation.

## Receipt

`ScoringReceiptV1` binds the workflow and Evaluation component run, then
echoes the five validated `translation_inputs` and the exact
`input_set_sha256`. The echo is compared to the validated handoff, not merely
to a recomputed summary. A receipt is either `accepted` or `rejected`; a
rejected receipt carries a reason code and still preserves the exact input
lineage that was considered.

The receipt's `evaluation_component_attempt_id` is the current component
attempt. It is not a scorer logical request ID and not a provider physical
attempt index.

## Component identity and Resume

- `workflow_run_id` is the parent identity and is never minted by Evaluation.
- `component_run_id` remains stable across a real Resume.
- `component_attempt_id` is `evalcomp_attempt_NNNN` and increments exactly by
  one after a `component_halted` event.
- `logical_request_id` and `physical_attempt_index` appear only in the typed
  retry payload and cannot substitute for component identity.
- Manifest revisions are content-addressed by their own hash. Events bind the
  manifest revision active when they were written.

The local event stream has a contiguous `component_seq` beginning at one and
an append-only hash chain. The first event starts attempt one. A Resume must
be the next event after a halt, reference the immediately preceding attempt,
and use the next component attempt ID. `component_done` and `component_failed`
are terminal; later append is rejected.

## Event payload policy

The event envelope and every event payload are closed schemas. The supported
events cover lifecycle, stage start/progress, validation, retry,
checkpoint, stage completion, halt, and terminal completion/failure. Payloads
contain IDs, counters, status and typed artifact bindings only. Raw prompts,
raw model responses, source prose, secrets, gold/reference content, scores or
recommendations do not enter the component event stream.

## Verification

`pipeline/tests/test_evaluation_workflow_component_v1.py` is fixture-only and
does not call a provider, read credentials, mount SQLite, mutate translation
memory, or write a report root. It probes:

- five-arm exact ordering and producer authority;
- rejection of a D2L `scoring_handoff_fragment_v1` (S0/S1 only) as a final
  handoff; only the neutral relay may compose the five-arm object and its
  `input_set_sha256`;
- source/coverage/input-set hash drift;
- receipt echo tampering after a valid reseal;
- contiguous component sequence and hash-chain bypass;
- Resume lineage and attempt identity separation;
- terminal append rejection;
- artifact-parent and path containment rules.

This module is a contract/validator milestone only. Wiring it into the live
benchmark runner is a later step after the neutral relay contract is accepted.
