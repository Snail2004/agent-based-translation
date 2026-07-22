# TASK_EVAL_LIVE_PILOT_CAPABILITY_PROBE_V1

Status: IMPLEMENTED (official-native profile v5; 0-API re-gate passed)

## Objective

Qualify the exact provider/model/native-Structured-Output contract required by
each remote Evaluation role before the four-unit D2L MLP pilot may call a
normal scorer.

## Scope

- Reuse `SharedLlmCapabilityProbe`; do not create a second transport.
- One sealed role, source row, model, schema and local validator per probe.
- Exactly one physical attempt. No retry, fallback, row rotation, cache,
  checkpoint, score publication or normal pipeline authority.
- Reuse the existing response schema and semantic validator for:
  `evaluation.sf_bt.back_translator`, `evaluation.sf_bt.semantic_judge` and
  `evaluation.pj.judge`.
- Use only synthetic capability text. No source package, translation, gold,
  oracle, human reference, score result or runtime callback enters a probe.
- Provider source and requested/accepted model identities are caller-supplied;
  production code contains no credential, physical row or model default.
- Native Structured Output qualification is restricted to direct official
  Google or OpenAI endpoints. Proxies, gateways, resellers, routers and local
  callbacks must use a separately versioned non-native JSON path.
- Official Google probes use `generationConfig.responseJsonSchema`, matching
  the normal Evaluation runtime envelope. Legacy `responseSchema` is rejected
  before transport; the canonical schema hash and local validator are unchanged.
- Probe temperature, sampling and reasoning settings follow the Evaluation role
  budget. In particular, `reasoning_effort=none` is sent to official Google as
  `generationConfig.thinkingConfig.thinkingBudget=0`. The synthetic probe keeps
  its separate bounded 512-token completion cap; it qualifies transport/schema
  capability rather than exercising the role's maximum runtime output length.
- Persist only the shared terminal probe receipt and capability evidence.

## Live gate after 0-API tests

1. Select one externally registered Gemini Free physical source revision.
2. Discover and seal exact requested and accepted observed model IDs.
3. Build the clean Evaluation implementation binding.
4. Execute at most one probe per role/schema, maximum three calls total.
5. Stop on any failed evidence. Never rotate row/model or retry silently.
6. Only three qualified evidence rows may enter `EvaluationLivePilotProfileV1`.

## Non-goals

No scorer quality claim, real score, API key loader, provider selection,
capability invention, shared-core edit, model fallback, App/UI work, DB write,
source/translation mutation or COMET checkpoint acquisition.

## Verification

Official-native profile v5 closes the V4 synthetic-prompt contract defect. V4
disabled provider thinking successfully and all three responses finished, but
the PJ synthetic prompt omitted the real local validator's 25-word note limit.
The model returned an otherwise valid 198-character note that exceeded 25
words. V4 is retained as failed evidence and cannot qualify V5. V5 must pass
fresh 0-API gates before another row/model/schema capability run.

Profile v5 integration verification:

- focused capability, runner, profile, adapter and shared-backend gate:
  `99 passed`;
- complete Evaluation-selected regression: `364 passed`, `1,030 deselected`;
- API/network calls during integration: `0`;
- V1 through V4 failure evidence remained immutable.

Profile v5 live qualification:

- fixed source/bucket: official Google Gemini Free row 1, revision V1;
- models: `gemini-2.5-flash` for back translation and `gemini-3.5-flash`
  for the semantic and PJ judges;
- three sequential calls: all `qualified`, all `finish_reason=stop`;
- provider usage: 175 prompt, 148 completion, 323 total tokens;
- reasoning usage: absent on all three calls (`thinkingBudget=0`);
- result root:
  `data/reports/evaluation_v1/live_capability_probe_20260719T235652Z_row1_v5`;
- authority remains capability-only; scorer quality is not established by this
  gate.

Official-native profile v4 closes the V3 runtime-equivalence defect. The V3
canary omitted Google's `thinkingConfig`, allowing provider thinking to consume
455 of the PJ role's 512 output tokens before the visible JSON was complete.
V3 is retained as failed evidence and cannot qualify V4. V4 must pass fresh
0-API gates before one new row/model/schema capability run.

Profile v4 integration verification:

- focused capability, runner, profile, adapter and shared-backend gate:
  `99 passed`;
- complete Evaluation-selected regression: `364 passed`, `1,030 deselected`;
- API/network calls during integration: `0`;
- V1, V2 and V3 failure evidence remained immutable.

Official-native profile v3 binds shared-core commit
`8645fa258209f4fc5a2a587dfaaa36fd7b8e831d`, which normalizes official Google
completion usage as candidate plus thought tokens while retaining thought
tokens as the reasoning subset. The incomplete row1 v2 attempt remains
immutable evidence and cannot qualify or seed a v3 attempt. Profile v3 must
pass the 0-API gates below before using a fresh run prefix, seal, and output
root.

Profile v3 integration verification:

- shared/Evaluation capability, runner, profile, adapter, and executor gate:
  `111 passed`;
- complete Evaluation-selected regression: `364 passed`, `1,030 deselected`;
- API/network calls during integration: `0`;
- failed row1 v1 and incomplete row1 v2 evidence remained immutable.

Official-native profile v2 re-gate after shared-core commit
`8945db52a9f05b832c30f4e20ce41012b8e5fe9f`:

- Evaluation/shared capability gate: 41 passed.
- Runner/probe/profile/adapter/Phase2B gate: 98 passed.
- Evaluation regression: 364 passed; 1,029 unrelated tests deselected.
- Failed row1 v1 evidence remained byte-identical to commit `41f1296`.
- API/network/credential calls during the re-gate: 0.

Original profile v1 verification:

- Dedicated Evaluation capability probes: 18 passed.
- Adjacent Evaluation profile/adapter/preflight and shared capability gate:
  74 passed.
- Complete `pipeline/tests` regression: 1,384 passed in 298.59 seconds.
- The complete gate used a temporary read-only mirror of frozen DB SHA-256
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
  source and mirror remained byte-identical and the mirror was removed.
- Google, OpenAI Chat Completions and OpenAI Responses request shapes are
  deterministic and preserve the exact existing role response schema.
- Adversarial probes cover foreign model/source/core identity, response-schema
  tampering, semantically invalid JSON, duplicate attempts and forbidden
  Evaluation authority identifiers.
- API/network calls, credential reads, provider fallback, DB writes,
  cache/checkpoint publication and source/translation mutation: 0.
