# Workflow replay Resume UI fixture

`workflow_manifest.json`, `events.jsonl`, and `artifact_index.json` are copied
unchanged from a package emitted and revalidated by the authoritative
`WorkflowRelayV1`. The generation follows the same relay path as
`test_resume_advances_attempt_without_relaying_prefix_twice`:

- attempt 1 emits component sequence 1 through 3 and pauses;
- attempt 2 starts with `component_resumed` and emits sequence 4 through 7;
- the final parent manifest records attempt 2 and `last_component_seq=7`.

The parent manifest hash is
`98a116b05e95182b5ab43852c3b6ad7969fad8ababf6b42b7b7318fe415ea853`.

`workflow_replay_dev.html` derives the sequence-gap and attempt-regression
negative cases in memory, reseals only the parent event hash chain, and never
writes those mutations back to this relay-generated fixture.
