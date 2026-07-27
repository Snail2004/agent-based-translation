# Literary OpenRouter Runbook

## Ownership

- OpenRouter is a standing Literary-only credential.
- Literary may self-authorize bounded OpenRouter runs without requesting a
  per-run Coordinator allocation.
- Every live run still requires its own immutable seal, isolated output root,
  explicit model/provider/bucket, token and retry caps, and an honest handoff
  update at a clean milestone.
- Never fall back or rotate into D2L, Evaluation, Input Normalization, or App UI
  credentials.

## Secret and profile

- Put the key in `OPENROUTER-KEY.txt` under the selected `--credential-root`.
- The file contains exactly one non-empty `sk-or-v1-...` line.
- Never commit, print, or copy the plaintext key into a report.
- Provider profile:
  `pipeline/configs/literary_provider_profile_b2_openrouter_gemini35_v1.json`
- Diagnostic profile:
  `pipeline/configs/literary_b2_openrouter_schema_probe_profile_v1.json`
- Quota bucket: `openrouter-literary-v1`
- API base: `https://openrouter.ai/api/v1`
- Model: `google/gemini-3.5-flash`

The current physical key has a user-controlled USD 5 key limit. Treat the
provider's current-key endpoint as the authority for remaining key allowance;
do not infer account-wide balance from this repository.

## Required routing policy

The checked-in diagnostic profile seals all of the following:

- provider only `google-vertex`;
- provider fallback disabled;
- every requested parameter must be supported;
- provider data collection denied;
- zero-data-retention endpoint required;
- reasoning effort `minimal`;
- zero retries for qualification probes;
- semantic and production publishing disabled.

Do not silently loosen any item after a transport failure. Change policy only
in a new versioned profile, test it offline, commit it, and create a new seal.

## Qualification status

Evidence roots:

- `data/reports/literary_b2_openrouter_schema_probe_20260717_184334/`
- `data/reports/literary_b2_openrouter_schema_probe_20260717_184643/`

Observed on Literary HEAD `c8a04d6`:

1. key authentication and Google Vertex generation work;
2. one generated canary used 142 prompt, 32 completion, and zero reasoning
   tokens at provider-recorded cost USD 0.000501;
3. JSON syntax and field-shape restriction worked, but a JSON Schema `const`
   did not override a deliberately conflicting prompt value.

Therefore this route is authenticated and transport-usable but is **not yet
qualified for Literary B2 strict structured output**. Do not run semantic B2
or publish from it until a separately sealed nested-schema/normalization gate
defines and passes the acceptable authority boundary.

## Sealed diagnostic commands

Run from `THESIS_RUNTIME_TOOL`. Substitute unique output and credential roots;
never reuse a closed output root.

```powershell
python -m pipeline.scripts.run_literary_b2_ckey_diagnostic_v1 prepare `
  --output-root <new-report-root> `
  --credential-root <directory-containing-OPENROUTER-KEY.txt> `
  --full-load-request <validated-B2-request.json> `
  --profile pipeline/configs/literary_b2_openrouter_schema_probe_profile_v1.json

python -m pipeline.scripts.run_literary_b2_ckey_diagnostic_v1 execute `
  --output-root <same-new-report-root> `
  --credential-root <directory-containing-OPENROUTER-KEY.txt> `
  --profile pipeline/configs/literary_b2_openrouter_schema_probe_profile_v1.json
```

`prepare` performs no model call. `execute` is bounded by the sealed profile.
After execution, query OpenRouter's current-key and generation endpoints only
for usage verification; those reads do not authorize a model retry.
