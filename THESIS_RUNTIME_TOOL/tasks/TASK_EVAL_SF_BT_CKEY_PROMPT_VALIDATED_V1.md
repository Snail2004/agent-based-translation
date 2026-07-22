# TASK: Evaluation SF-BT CKEY Prompt-Validated Calibration V1

Status: LIVE_CALIBRATION_COMPLETE_MEASUREMENT_ONLY

## Objective

Run the independently reviewed SF-BT band calibration through the user-assigned
CKEY third-party route without claiming provider-enforced JSON Schema support.
The semantic judge, fixture, score bands, prompt, canonical response schema and
local validator remain unchanged.

## Versioned source variants

- endpoint class: third-party proxy;
- OpenAI-compatible variant: `openai_chat_completions`,
  `https://api.xah.io/v1`, `openai_compatible_chat_v1`, `chat_completions`;
- Google-compatible variant: `google_genai_generate_content`,
  `https://api.xah.io/v1beta`, `google_genai_rest_v1`,
  `models_generate_content`;
- each live run seals exactly one variant and exact requested model;
- credential: external `CKEY.txt`, one non-empty row, referenced and committed
  only through a credential commitment;
- structured-output mode: `prompt_validated`;
- transport retry, semantic retry, fallback and provider rotation: disabled.

## Capability gate

Before calibration, run one separately sealed canary per Evaluation LLM role.
OpenAI-compatible requests may use only
`response_format={"type":"json_object"}` as a syntax aid. Google-compatible
requests use only `responseMimeType="application/json"`. Neither variant may
contain `json_schema`, `responseJsonSchema`, strict-schema payloads or other
native-schema parameters. Each response is parsed and checked by the unchanged
role-local validator. A failed role halts later role probes.

Qualified evidence is bound to the exact source revision, route, requested and
observed model IDs, canonical schema hash, local validator hash, implementation
hash and physical quota bucket.

## Calibration run boundary

The failed official-Google logical run is immutable and cannot be resumed with
CKEY because source route, requested model identity and output mode differ. CKEY
therefore starts a new logical run and output root.

The new run retains:

- the approved 15-case fixture and fixture hash;
- two canonical repeats plus five reversed orientation screens, 35 calls total;
- temperature `0`, cache bypass and no hidden retry;
- per-call input/output caps and invocation-level call/token caps;
- one checkpoint after every locally accepted response;
- exact band values `0|25|50|75|100`; pseudo-precision remains invalid.

Third-party profiles use an `8192` prompt-usage certification cap because the
measured proxy usage includes provider-side tokens not present in the literal
prompt. This changes only the accounting envelope; prompt bytes, fixture,
rubric, temperature and output cap remain unchanged. Official-source profiles
retain the original `4096` prompt cap.

Third-party profiles also certify up to `2048` provider-reported completion
tokens while the generation request remains capped at `512`. The split records
unlabelled proxy-side usage without granting the model a larger response. The
largest measured probe response contained 80 candidate tokens while the proxy
reported 1073 completion tokens after truthful total reconciliation. The
finite `2048` envelope provides headroom without making proxy usage unbounded.

Prompt-validated profiles append one versioned output-envelope instruction to
the otherwise unchanged semantic prompt: emit one raw JSON object with no
Markdown fence or surrounding prose. Native official profiles do not receive
this instruction. Canonical parsing and local validation remain unchanged.

## Completed live measurement

The sealed CKEY capability run qualified all three Evaluation roles. The
subsequent SF-BT band calibration completed `35/35` calls through three bounded
checkpointed invocations with zero ledger errors. Both fifteen-case primary
repeats achieved exact-band accuracy `1.0`; repeat exact agreement was `1.0`.
The orientation screen was exact in `4/5` cases and within one band in `5/5`.
The result remains measurement-only and does not authorize a production
quality claim without a bounded real-block pilot.

## 0-API acceptance

1. CKEY source contains no plaintext credential.
2. Native and third-party capability authorities cannot be cross-labelled.
3. Wire request contains JSON-object mode and no native schema.
4. Invalid JSON, unknown fields, non-finite values and score `58` fail closed.
5. Wrong source, credential ref, model, schema, validator or capability cannot
   authorize calibration.
6. Existing official-Google required-mode behavior remains unchanged.
7. Focused Evaluation and full pipeline tests pass without API or DB mutation.

## Live policy

Run a bounded capability canary first. Only qualified evidence can authorize a
new CKEY calibration run. Historical Google failures remain immutable. Any CKEY
error halts without provider/model/output-mode fallback; a later retry requires a
new sealed attempt while preserving the same CKEY semantic contract.

## Recorded live finding

The sealed OpenAI-compatible canary requested
`tranhieu13102003/gemini-3.5-flash` and halted on its first role with HTTP 403
`permission_error`; no later role or calibration call ran. This evidence is
immutable. The next canary is a new Google-compatible run using the current
pipeline-configured CKEY model `vuduythanh2023/gemini-3.5-flash`.
