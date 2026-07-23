# Task: D2L Translator Quality Auditor V1

Status: draft rev2 for architecture review; 0-API; no runtime implementation authorized by this file

## 0. Purpose

Add a bounded, evidence-addressed quality gate between D2L translation and
publication. The gate must catch both:

1. deterministic defects that code can prove, such as damaged protected spans,
   output-only foreign scripts, missing blocks, and untranslated headings; and
2. semantic defects that require language judgment, such as omission, unsupported
   addition, reversed negation, wrong comparison, or terminology used with the
   wrong meaning.

The target is a usable autonomous pipeline, not a claim that every translation is
perfect. A chapter run may finish with held blocks, but a held block is never
publication-ready.

This task is book-neutral. Historical D2L defects are regression evidence only.
No production rule may contain a known answer, a source-specific phrase, or a
special case for one block.

## 1. Verified motivation

### 1.1 Structural success is not semantic success

The current protected-span and line-skeleton contracts can prove exact LaTeX,
markup, block coverage, and structural preservation. They cannot prove that the
Vietnamese text preserves the source meaning.

The scale-18 canary preserved all protected mathematics and structure, but two
English headings escaped the existing publication audit. Those defects are
deterministic and should never consume an LLM audit call.

### 1.2 Current foreign-script hygiene is incomplete

The historical S1 output for
`d2l_linear_networks_linear_regression_concise_b038` contains the Persian token
`هنوز`. The S0 row for the same block does not contain it. This exact evidence does
not establish that `d2l_preface_index_b011` or both arms contain the same defect;
any Console claim to that effect must be checked as a separate projection/join
issue.

`pipeline/translate/hygiene.py` currently flags only a closed subset of scripts:
Cyrillic, CJK, Hangul, and Thai. Persian uses Arabic-script letters, so the token
is valid Unicode and bypasses both the forbidden-control gate and the current
foreign-script gate.

The repair must generalize by target-language/script policy. It must not add
`هنوز` to a blacklist.

### 1.3 Current hygiene retry is not a publication authority

The current runner appends a diagnostic re-ask and retranslates the full window.
After a second foreign-script failure, it can return a translated result together
with a major QA issue. Therefore the existing path is useful evidence collection,
but it is not sufficient as the final publication eligibility decision.

## 2. Architectural invariants

1. **Code proves mechanics; the LLM judges meaning.** Code does not infer literary
   or technical meaning. The Quality Auditor does not validate hashes, JSON shape,
   placeholder cardinality, or Unicode byte identity.
2. **Translator and Auditor are separate authorities.** The Translator proposes
   Vietnamese text. The Auditor reports typed defects. Neither publishes output.
3. **Code owns the final state transition.** Only code maps validated evidence to
   `PASS_FIRST`, `PASS_AFTER_RETRY`, or `HOLD`.
4. **No answer injection.** A retry may receive block IDs, issue types, and exact
   defective spans from its own prior output. It must never receive a reference
   translation, a suggested replacement, gold, an oracle answer, or an Auditor
   rewrite.
5. **One translation retry maximum per window.** The initial translation plus at
   most one semantic repair call is the entire translation-attempt budget. A
   mechanical failure and a semantic failure do not each receive a separate retry.
6. **Every publication candidate is audited.** A block cannot become
   publication-ready without deterministic validation and a valid Quality Auditor
   result for the final translation bytes.
7. **The same quality tier applies to S0 and S1.** Gate versions, Auditor prompt,
   model, thresholds, retry cap, and HOLD policy must be identical. Per-arm retry
   rates remain separately observable.
8. **Run configuration remains immutable.** A retry uses the same Translator
   model and generation profile as the initial attempt. Changing model or
   generation settings starts a new run/arm. Changing only an API distribution
   source is allowed solely under the separately sealed source-resolution policy
   and only when that source serves the already sealed model/profile.
9. **SF-BT and reference scoring remain Evaluation-only.** Back-translation,
   reference translations, community glossary gold, and evaluation scores cannot
   trigger runtime retry or appear in the Auditor packet.
10. **HOLD is fail-forward, not silent acceptance.** A chapter can complete with
    held blocks. Those blocks remain visible and traceable but are excluded from a
    complete publication overlay.
11. **Historical evidence is immutable.** Existing reports, SQLite rows, prompts,
    and run IDs are never relabelled as if this gate had reviewed them.
12. **Prompt-byte changes require a new version.** The prompt ID, canonical schema,
    local validator, deterministic policy, and retry-note policy are independently
    versioned and sealed.

## 3. Roles and recommended model split

| Role | Default model | Authority |
|---|---|---|
| Translator | Gemini 3.5 Flash | Produce Vietnamese translations only |
| Deterministic quality gates | Code | Prove mechanical defects and receipts |
| Translation Quality Auditor | GPT-5.5 through the sealed gateway profile | Report semantic findings only |
| Quality decision reducer | Code | Compute pass, retry, or hold |
| Publication Exporter | Input Normalization | Render only publication-eligible overlay rows |

The gateway is a third-party route. The Auditor profile must therefore use
`prompt_validated` JSON-object mode when honestly supported, or prompt-generated
JSON with local parsing. It must not send a native JSON-Schema parameter or claim
native Structured Output authority. The canonical local validator remains final.

Proposed identifiers:

```text
role_id: d2l.translator.quality_auditor
preset_id: d2l.translator.quality_auditor.gpt55_gateway_v1
prompt_id: d2l_translation_quality_audit_v1
response_contract: d2l_translation_quality_audit_response_v1
deterministic_policy: d2l_translation_deterministic_quality_v2
retry_policy: d2l_translation_targeted_repair_v1
publication_policy: d2l_translation_publication_eligibility_v1
```

Provider source, source revision, route, physical quota bucket, capability record,
and model revision are resolved and sealed outside the semantic prompt. They must
not be hard-coded into the prompt text.

## 4. End-to-end state machine

```text
Translator initial attempt
  -> parse and canonical local validation
  -> protected-span restore and exact receipt
  -> deterministic quality detector
       -> candidate is not auditable
            -> retry unused: one repair call, then rebuild candidate
            -> retry already used: HOLD
       -> candidate is auditable
            -> Quality Auditor
                 -> no major finding: PASS_FIRST or PASS_AFTER_RETRY
                 -> major finding and retry unused:
                        one targeted repair call
                        -> all deterministic gates again
                        -> Quality Auditor again on final bytes
                             -> no major: PASS_AFTER_RETRY
                             -> major/invalid: HOLD
                 -> major finding and retry already used: HOLD
```

An Auditor advisory never consumes the retry budget. It is persisted for analysis
but cannot independently block publication.

### 4.1 Auditable versus unauditable output

An output is **unauditable** when no trustworthy source-to-target candidate can be
formed, including:

- invalid or unparseable response;
- missing, duplicate, foreign, or reordered block IDs;
- missing, duplicate, reordered, malformed, or model-authored protected refs;
- restored protected bytes differing from source;
- unresolved forbidden control characters;
- failed block/slot exact cover.

An output is **auditable with deterministic findings** when block alignment remains
trustworthy, including:

- output-only foreign script;
- target text equal to source text for a translatable block;
- untranslated heading or substantial source-language residue;
- suspicious empty or gross length-ratio anomaly;
- list/directive/line-skeleton mismatch for which source and target blocks remain
  alignable.

The latter findings are supplied to the Auditor so one repair can address both
mechanical and semantic defects.

## 5. Deterministic quality detector V2

### 5.1 One pure detector, separate consumers

Runtime and Evaluation should call the same pure detection library so the rule
bytes do not drift. They retain separate authority:

- runtime maps findings to retry/HOLD through this task's policy;
- Evaluation records the same findings as measurements only.

Evaluation data, thresholds, scores, or decisions never flow back into runtime.

### 5.2 General output-only script rule

For the English-to-Vietnamese profile:

1. Latin letters, Vietnamese combining marks, digits, punctuation, and ordinary
   whitespace are permitted.
2. Protected formulas, code, URLs, markup payloads, and preserve-only content are
   compared/restored by their own contracts and excluded from language judgment.
3. A non-Latin alphabetic span is permitted only when the exact normalized span is
   present in the corresponding source block or in an explicitly preserved source
   span.
4. A non-Latin alphabetic span created only in the target is reported as
   `unexpected_output_script`.
5. Detection is based on Unicode properties and the sealed target-language script
   policy. It must not use a book-specific word blacklist.

This catches Arabic/Persian, Cyrillic, Hebrew, CJK, Hangul, Thai, and other
output-only scripts while allowing a genuine source token to survive unchanged.
English residue is a separate rule because English and Vietnamese both use Latin.

### 5.3 Required deterministic finding types

```text
invalid_response_contract
block_exact_cover_failure
protected_ref_failure
protected_restore_mismatch
forbidden_control_character
unexpected_output_script
target_equals_source
untranslated_heading
source_language_residue_candidate
empty_translation
gross_length_anomaly
line_skeleton_mismatch
```

Only `source_language_residue_candidate` and `gross_length_anomaly` are heuristic
findings. They cannot alone force HOLD; the Auditor must confirm a major semantic
defect. All other mechanically proven major findings can trigger repair directly.

### 5.4 Sealed heuristic thresholds

The two heuristic rules are deterministic only when their normalization and
numeric thresholds are part of the versioned policy bytes. V2 uses these initial,
book-neutral, deliberately conservative values:

```text
normalization: Unicode NFKC + casefold + whitespace collapse
protected/preserve spans: removed before measurement

source_language_residue_candidate:
  minimum exact contiguous source tokens also present in target: 4
  minimum exact contiguous source characters also present in target: 20

gross_length_anomaly:
  minimum eligible source letters/digits: 80
  minimum target/source letters-and-digits ratio: 0.25
  maximum target/source letters-and-digits ratio: 4.00
```

The residue rule tokenizes Unicode letter/digit runs and reports only a contiguous
source-derived run that satisfies both minima. Explicitly preserved source spans
and protected refs are excluded. The length rule counts Unicode letters and digits
after the same exclusions. Boundary probes at exactly below/equal/above every
threshold are required in Phase A.

Changing any value or normalization rule creates a new deterministic policy
version. Phase A may reject these initial values using book-neutral offline
fixtures, but it must replace them with explicit numeric values and re-version the
policy before the phase can pass. No threshold may remain implicit, null, or
implementation-defined.

## 6. Auditor context packet

The Auditor receives one translation window, never a full chapter or full book.
Repeated glossary and receipt data are deduplicated once per window.

Canonical packet shape:

```json
{
  "contract_version": "d2l_translation_quality_audit_input_v1",
  "window_id": "w_example_001",
  "source_language": "en",
  "target_language": "vi",
  "blocks": [
    {
      "block_id": "example_b001",
      "block_type": "heading",
      "source_audit_text": "A source heading",
      "target_audit_text": "Một tiêu đề đích",
      "applicable_glossary_refs": ["glossary_gradient_v3"],
      "deterministic_findings": []
    }
  ],
  "glossary_cards": [
    {
      "glossary_ref": "glossary_gradient_v3",
      "source_term": "gradient",
      "allowed_target_variants": ["gradient", "đạo hàm gradient"],
      "policy": "context_sensitive"
    }
  ],
  "protected_receipt": {
    "policy_id": "d2l_latex_markup_line_protected_spans_v4",
    "exact_cover": true,
    "source_target_ref_order_equal": true,
    "raw_protected_payload_visible": false
  },
  "context_only_neighbors": []
}
```

### 6.1 Packet rules

- `source_audit_text` and `target_audit_text` use the same opaque protected refs.
  Raw LaTeX, code payload, and structural bytes are not needed for semantic audit.
- Each glossary card is sent once and linked from applicable blocks by ID.
- Only glossary rows actually retrieved/applied for this window are included.
- Neighbor blocks are optional, bounded, read-only, and explicitly marked
  context-only. Default: at most one preceding and one following block when they
  were already part of the Translator's sealed context.
- The packet does not contain a reference translation, human judgment, community
  gold, Evaluation score, back-translation, prior Auditor chain-of-thought, or a
  proposed correction.
- The semantic packet does not reveal S0/S1 arm identity, Translator model/provider,
  or whether the candidate is a first attempt or repair. Those facts remain in
  sealed provenance outside the model-visible packet so they cannot bias judgment.
- Prompt and packet hashes, run/attempt identity, provider usage, and source hashes
  are persisted as provenance but need not consume semantic prompt tokens unless
  required by the local validator.
- Phase B must seal both `max_glossary_cards_per_window` and
  `max_glossary_tokens_per_window` from the dry-render measurements. Exceeding
  either cap fails packet construction; it must not silently truncate or dump the
  full registry.

## 7. Auditor response contract

Canonical response:

```json
{
  "contract_version": "d2l_translation_quality_audit_response_v1",
  "window_id": "w_example_001",
  "audited_block_ids": ["example_b001"],
  "findings": [
    {
      "block_id": "example_b001",
      "issue_type": "meaning_omission",
      "severity": "major",
      "source_evidence": "source words copied exactly",
      "target_evidence": "",
      "reason": "A necessary condition from the source is absent."
    }
  ]
}
```

Closed `issue_type` enum:

```text
meaning_omission
unsupported_addition
polarity_or_negation_error
numeric_or_comparison_error
relation_or_logic_error
referent_or_scope_error
terminology_context_error
untranslated_source_content
local_coherence_error
style_or_fluency_advisory
semantic_other
```

Closed severity enum:

```text
major
advisory
```

`semantic_other` requires concrete source/target evidence and cannot be used as a
generic expression of uncertainty. `style_or_fluency_advisory` must always have
severity `advisory`.

Known limitation: a `local_coherence_error` is attached to one audited block, so
its evidence fields quote only that block's source/target text. The bounded
neighbor may be discussed concisely in `reason`, but V1 does not add cross-block
evidence fields or allow neighbor text to satisfy the exact-substring validator.

### 7.1 Local validator

The validator must reject:

- non-JSON, duplicate keys, extra top-level fields, or schema mismatch;
- a different contract version or window ID;
- `audited_block_ids` that are missing, duplicated, foreign, or reordered;
- findings for blocks outside the packet;
- issue types or severities outside the enums;
- duplicate identical findings;
- `source_evidence` not found byte-for-byte in the block source, except that
  `unsupported_addition` may use an empty source evidence;
- `target_evidence` not found byte-for-byte in the candidate target, except that
  `meaning_omission` may use an empty target evidence;
- `style_or_fluency_advisory` marked major;
- replacement translations, suggested wording, or fields not defined here.

Code mints stable finding IDs after validation from the sealed packet hash,
block ID, issue type, severity, and evidence. The model does not mint IDs.

## 8. Decision reducer and publication states

### 8.1 Reducer

```text
valid final audit with zero major findings
  + retry_used=false -> PASS_FIRST
  + retry_used=true  -> PASS_AFTER_RETRY

one or more major findings
  + retry_used=false -> RETRY_TARGETED
  + retry_used=true  -> HOLD

invalid audit response
  + semantic audit retry policy unused -> one audit re-ask, not a translation retry
  + audit re-ask still invalid -> HOLD_AUDIT_CONTRACT

unauditable translation candidate
  + translation retry unused -> RETRY_TARGETED or full-window repair as required
  + translation retry used -> HOLD_TRANSLATION_CONTRACT
```

Transport retry and Auditor JSON re-ask are not translation retries, but both must
have their own sealed hard caps. They cannot change provider/model/profile or hide
usage.

### 8.2 HOLD semantics

HOLD is a durable machine-readable status, not a demand for immediate human input.

- Other windows and the chapter run continue.
- The last candidate, all findings, attempts, and provenance remain append-only.
- The block is excluded from the publication-ready exact-cover set.
- A draft exporter may represent it only as `review_required`/held, never as passed.
- A final publication claim is blocked while any required block remains held.
- A later end-of-chapter batch audit or optional human/editor review may resolve the
  hold through a separately versioned decision path. Silence never resolves it.

## 9. Targeted repair contract

The repair request receives:

- the unchanged original active window for context;
- accepted block translations as read-only context;
- only the failed block IDs as writable output slots;
- validated deterministic and Auditor issue summaries;
- the same relevant glossary and protected-ref contract;
- no replacement wording.

All failed blocks in one window are repaired in one call. Unaffected blocks are
not regenerated.

If block/placeholder exact cover is too damaged to isolate trustworthy block
targets, the one repair call may retranslate the full window. This is a mechanical
fallback inside the same retry budget, not an additional attempt.

Exact repair-note template appended to the existing Translator contract:

```text
QUALITY REPAIR REQUEST — d2l_translation_targeted_repair_v1

Your previous candidate was parsed and checked. Retranslate only the writable
block IDs listed below. Use the unchanged source window, glossary, and protected
references. Preserve the source meaning completely and return no commentary.

For each writable block, address only the supplied issue evidence. The issue
descriptions identify defects; they do not supply replacement translations.
Do not copy a defective target span merely because it is quoted below.

WRITABLE BLOCK IDS:
{{writable_block_ids_json}}

VALIDATED ISSUES:
{{validated_issue_summaries_json}}

Return exactly the canonical translation response shape for the writable block
IDs. Do not return accepted read-only blocks or extra keys.
```

## 10. Quality Auditor prompt

### 10.1 System prompt: `d2l_translation_quality_audit_v1`

```text
You are the Technical Translation Quality Auditor for English-to-Vietnamese
technical book translation.

Your only task is to compare each supplied English source block with its candidate
Vietnamese translation and report concrete semantic defects. You are an auditor,
not a translator, editor, scorer, or publication authority.

AUTHORITY BOUNDARY
- Report findings only. Do not rewrite any translation.
- Do not provide replacement Vietnamese wording or a corrected sentence.
- Do not decide PASS, RETRY, HOLD, publication, or scoring. Deterministic code does
  that after validating your response.
- Use only the supplied packet. Do not use outside knowledge to invent missing
  requirements.
- The packet intentionally withholds model, arm, provider, and retry state. Judge
  only the supplied source, target, glossary, receipts, and evidence.
- Do not compare against an imagined reference translation.

WHAT TO REVIEW
For every block, compare source meaning with target meaning. Report a finding only
when the supplied text provides direct evidence of one of these problems:
1. a meaningful clause, condition, restriction, or argument is omitted;
2. the target adds a factual or logical claim unsupported by the source;
3. polarity, negation, modality, certainty, or emphasis is reversed materially;
4. a number, quantity, ordering, inequality, comparison, or mathematical relation
   is described incorrectly in prose;
5. an actor, referent, scope, dependency, cause, or logical relation is changed;
6. a technical term is rendered with the wrong meaning in this local context;
7. meaningful source-language content remains untranslated;
8. the target is locally incoherent or says something incompatible with the
   neighboring supplied blocks.

WHAT NOT TO REVIEW
- Do not audit JSON syntax, block ordering, hashes, placeholders, LaTeX bytes,
  markup bytes, code bytes, or line skeletons. Deterministic receipts are
  authoritative for those properties.
- Treat matching opaque refs such as MATH_REF and STRUCT_REF as equal protected
  source material. Do not request their expansion and do not claim their content
  is missing when the receipt says exact_cover=true.
- Do not require one fixed glossary rendering when a glossary card is marked
  context_sensitive or provides multiple allowed variants. Judge whether the
  chosen wording is correct in the supplied sentence.
- Do not report mere stylistic preference as a major defect. Awkward but accurate
  Vietnamese may receive only style_or_fluency_advisory.
- Do not reward literal word-for-word translation or punish natural Vietnamese
  restructuring when meaning is preserved.

SEVERITY
- major: the defect can change, remove, add, or materially obscure source meaning,
  or leaves meaningful source content untranslated.
- advisory: the meaning is preserved, but fluency or style could be improved.
- When evidence is insufficient, do not guess a major defect. Omit the finding or
  use an advisory only when there is a concrete stylistic problem.

EVIDENCE
- source_evidence must be an exact substring of the corresponding source block.
- target_evidence must be an exact substring of the corresponding target block.
- For meaning_omission, target_evidence may be an empty string.
- For unsupported_addition, source_evidence may be an empty string.
- Keep reason concise and diagnostic. Do not include a proposed correction.

OUTPUT
Return one JSON object and no prose, Markdown, or code fence.
Use exactly this shape:
{
  "contract_version": "d2l_translation_quality_audit_response_v1",
  "window_id": "<copy input window_id>",
  "audited_block_ids": ["<every input block_id exactly once in input order>"],
  "findings": [
    {
      "block_id": "<one input block_id>",
      "issue_type": "meaning_omission|unsupported_addition|polarity_or_negation_error|numeric_or_comparison_error|relation_or_logic_error|referent_or_scope_error|terminology_context_error|untranslated_source_content|local_coherence_error|style_or_fluency_advisory|semantic_other",
      "severity": "major|advisory",
      "source_evidence": "<exact source substring or allowed empty string>",
      "target_evidence": "<exact target substring or allowed empty string>",
      "reason": "<concise diagnosis without replacement wording>"
    }
  ]
}

An empty findings list is correct when no supported defect is present. Do not add
a finding merely to appear thorough.

PROMPT VERSION: d2l_translation_quality_audit_v1
```

### 10.2 User message template

```text
Audit the following sealed translation-quality packet.

Important:
- Review every block ID exactly once.
- Deterministic findings are evidence, not an instruction to invent additional
  semantic problems.
- Return JSON only under d2l_translation_quality_audit_response_v1.

PACKET:
{{canonical_packet_json}}
```

The packet is serialized canonically and hashed. No prose is appended after the
JSON packet.

## 11. Auditor semantic retry

An invalid Auditor response may receive one semantic re-ask using the same sealed
packet, model, and generation profile. This is not a translation retry because it
does not generate target text.

The re-ask contains only local-validator errors, for example:

```text
Your previous audit response failed the canonical local validator:
{{closed_validation_errors_json}}

Return the same audit again as one valid JSON object under
d2l_translation_quality_audit_response_v1. Do not add prose, replacement
translations, new block IDs, or extra fields.
```

One invalid response followed by one invalid re-ask produces
`HOLD_AUDIT_CONTRACT`. It must not silently become PASS.

## 12. Artifacts and provenance

Each window records immutable, content-addressed artifacts:

```text
input_packet.json
input_packet.sha256
request.json
raw_response.json
validation.json
validated_findings.json
decision.json
usage.json
```

If a repair occurs, add an attempt-scoped subdirectory containing:

```text
repair_request.json
repair_raw_response.json
repair_validation.json
final_audit/
```

`decision.json` binds:

- source package and admission projection hashes;
- translation run/experiment/arm/window identity;
- Translator prompt/policy/model/profile hashes;
- final target bytes and protected receipt;
- deterministic gate policy/hash;
- Auditor packet/prompt/schema/validator/model/profile hashes;
- retry usage and final publication state.

No artifact may contain plaintext credentials, chain-of-thought, gold, reference
translations, evaluation scores, or hidden provider reasoning.

## 13. Adversarial Phase A, 0 API

### 13.1 Deterministic probes

Required pass/fail coverage:

1. Persian/Arabic-script target-only token is flagged without matching its word.
2. Cyrillic, Hebrew, CJK, Hangul, and Thai target-only spans are flagged.
3. A non-Latin span present exactly in source is permitted.
4. Vietnamese NFC and NFD combining forms are not falsely flagged.
5. Protected Greek/math/code text restores exactly and is not language-audited.
6. Forbidden control characters remain rejected.
7. A translatable heading copied unchanged is flagged.
8. Missing/duplicate/reordered block IDs fail before audit.
9. Missing/duplicate/reordered protected refs fail before audit.
10. A historical Persian-defect replay is caught, while the corresponding clean S0
    text passes.

### 13.2 Prompt/schema probes

1. Empty findings for a clean exact-cover packet passes.
2. Foreign/reordered/missing audited block IDs fail.
3. Foreign block finding fails.
4. Unknown issue type or severity fails.
5. Non-substring evidence fails under the issue-specific exceptions.
6. A replacement-translation field or extra top-level key fails.
7. `style_or_fluency_advisory` marked major fails.
8. Duplicate findings fail or are deterministically rejected before persistence.
9. Invalid JSON fails closed and one bounded audit re-ask is representable.
10. Retry state cannot be changed by model output.

### 13.3 State-machine probes

1. Clean initial output -> `PASS_FIRST`.
2. Advisory-only initial output -> `PASS_FIRST` plus advisory record.
3. Major initial finding -> exactly one `RETRY_TARGETED`.
4. Clean repaired output -> `PASS_AFTER_RETRY`.
5. Major repaired output -> `HOLD`.
6. Mechanical retry consumes the same one-retry translation budget.
7. Invalid Auditor twice -> `HOLD_AUDIT_CONTRACT`.
8. HOLD does not stop later windows but cannot enter publication-ready overlay.
9. S0 and S1 resolve the same quality policy bytes and separate telemetry.
10. Resume rejects prompt/schema/validator/model/policy drift.

## 14. Bounded live calibration

Live calibration is a separate sealed run after Phase A passes.

### 14.1 Three clean calls

Audit the three existing six-block Translator windows without modifying their
translations. This measures false retry-triggering alarms.

Acceptance:

- three valid Auditor responses;
- every block exact-covered;
- every major finding on a nominally clean packet is adjudicated outside runtime by
  a human developer as `genuine_defect` or `false_alarm`, using only the sealed
  source/target packet and finding evidence;
- zero `false_alarm` major findings that would trigger retry;
- a `genuine_defect` is preserved as a successful discovery, and its window is
  removed from the clean denominator rather than counted as an Auditor failure;
- if removals leave fewer than three genuinely clean windows, add replacement
  clean windows under the same sealed selection procedure before calculating the
  false-alarm result;
- advisory findings may exist but must contain valid evidence;
- no publication or SQLite mutation.

This human adjudication exists only to label the development calibration set. It
does not enter runtime packets, resolve production HOLD rows, or become translation
authority.

### 14.2 Three sabotaged calls

Create immutable copies of the same three packets and plant semantic defects that
deterministic code cannot detect. Expected labels remain outside the Auditor
request.

Required defect families across the three copies:

1. reverse a negation or condition;
2. omit a meaningful clause;
3. alter a number, ordering, inequality, or comparison stated in prose;
4. use a technical term with a wrong contextual meaning;
5. change a referent, scope, cause, or dependency.

Acceptance:

- all planted major defects are reported with the correct block IDs and valid
  evidence;
- zero fabricated retry-triggering findings on untouched blocks;
- no replacement translation appears in output;
- no gold/reference/evaluation data appears in requests.

This is six GPT-5.5 audit calls total: three clean and three sabotaged. It does not
invoke Translator repair.

### 14.3 One integrated repair canary

After calibration passes, run one window through the complete state machine:

```text
candidate -> gates -> Auditor -> one targeted Translator repair -> gates -> Auditor
```

The canary must prove that accepted blocks are not regenerated, the final audit
binds the repaired bytes, and a second semantic failure becomes HOLD rather than a
third translation attempt.

## 15. Acceptance for implementation readiness

The architecture may be wired into a bounded chapter canary only when:

- all 0-API probes pass;
- the output-only script rule catches the historical class without book-specific
  lexical rules;
- clean calibration produces zero false retry-triggering major findings;
- all planted major defects are detected;
- the integrated repair canary obeys one translation retry maximum;
- S0/S1 quality-tier bytes are identical and retry telemetry is arm-separated;
- held blocks are excluded from publication-ready exact cover;
- frozen source/runtime DB hashes remain unchanged in canaries;
- no credential, gold, reference, score, or chain-of-thought leak is found;
- provider-reported usage/cost is recorded truthfully, with unknown cost remaining
  null.

## 16. Implementation phases and scope

### Phase A: spec and 0-API contract

- implement/generalize the pure deterministic detector;
- implement packet/schema/local validator/state reducer with fake transport;
- freeze prompt bytes and prompt hash;
- run all adversarial probes;
- no API and no publication.

### Phase B: dry render and seal

- render the three clean and three sabotaged packets;
- count tokens and seal call/token/cost/retry caps;
- seal exact gateway source/profile/model/capability;
- no API.

### Phase C: bounded Auditor calibration

- run 3 clean + 3 sabotaged GPT-5.5 Auditor calls;
- stop and review before any Translator retry call.

### Phase D: one integrated repair canary

- run exactly one complete retry path;
- verify state, artifacts, usage, and publication eligibility;
- no full chapter yet.

### Phase E: chapter canary

- apply identical quality tier to S0 and S1;
- report first-pass pass rate, retry rate, Auditor calls, Translator repair calls,
  token usage, and publication-ready coverage per arm;
- split HOLD telemetry into at least:
  `mechanical_repair_then_semantic_hold`, `semantic_retry_then_hold`,
  `hold_translation_contract`, and `hold_audit_contract`;
- report retry rate separately for S0 and S1 while keeping quality-policy bytes
  identical. This permits the measured hypothesis that memory may improve
  first-pass quality without confounding the ablation with different gates.

## 17. Explicit non-goals

- no full-chapter or whole-book run in this task;
- no back-translation or scoring in runtime;
- no human reference, community gold, or Evaluation callback;
- no change to B1/B2/glossary authority;
- no redesign of the Translator prompt beyond the versioned repair suffix;
- no Publication Exporter or Input Normalization change;
- no UI workflow for resolving HOLD;
- no production database migration;
- no automatic model/provider fallback;
- no claim that passing this gate equals professional human translation quality.

## 18. Expected future write set

The exact implementation write set must be reviewed before Phase A edits. Likely
D2L-owned files include a new quality module, prompt/contract module, tests, and a
canary script. Integrating the state machine will likely touch shared hotspots:

```text
THESIS_RUNTIME_TOOL/pipeline/translate/runner.py
THESIS_RUNTIME_TOOL/pipeline/scripts/run_translate.py
```

Those shared-hotspot edits require their normal exact-delta gate. This spec does
not authorize them. It also does not authorize changes under `pipeline/eval/**`,
Input Normalization, Literary, App UI, or the shared LLM backend core.

## 19. Review questions for Claude

1. Is the split between unauditable mechanical failure and auditable deterministic
   finding sufficient to preserve one total translation retry?
2. Is the issue enum broad enough without giving the Auditor an unbounded rewrite
   role?
3. Should any issue other than `style_or_fluency_advisory` be permitted as advisory,
   or should semantic issue types always be major when emitted?
4. Is exact-substring evidence too strict for omission/addition after the two
   explicit empty-evidence exceptions?
5. Does the clean/sabotaged calibration adequately distinguish Auditor value from
   deterministic gate value?
6. Are there any packet fields that create answer leakage, Evaluation leakage, or
   avoidable context growth?

## 20. Phase A implementation record

Status: **implemented and verified offline. Phase B is recorded in section 21;
all live/runtime wiring remains closed.**

Bounded implementation files:

```text
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_quality_gates_v2.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_translation_quality_auditor_v1.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_translation_quality_state_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_quality_gates_v2.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_quality_auditor_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_quality_state_v1.py
```

Frozen semantic identifiers and hashes:

```text
deterministic_policy_id=d2l_translation_deterministic_quality_v2
deterministic_policy_sha256=AB860AB69901702A65A1B8554DD69A9C94FC30C73FB32AC21E68336BF26F3891
audit_prompt_sha256=2A3E0AA7DF324D192A19057C3D4F89F2C892F0CD1B7B8FBCF9435077C46F7F14
response_schema_sha256=17B24A817119837BD5D6F3F08A71F7589CCA21F60B80D6A6BBCB2170F94990FF
audit_reask_policy_sha256=193FBC31758E3CD323637DA5DBC8C6CC27A0BACE54E041FC8B48887AE852049B
targeted_repair_prompt_sha256=88F906A0418BF671FCCE976162D6A52E587174F5D433A14AFC902F89715CFC84
state_policy_id=d2l_translation_publication_eligibility_v1
```

The audit prompt hash binds both the system prompt and the packet-bearing user
template. The semantic manifest also binds the response schema, exact local
validator ID, audit re-ask bytes, targeted-repair suffix bytes, deterministic
policy, state policy, glossary caps, and token-counter ID. Phase B must still seal
the actual glossary caps/tokenizer and the provider/model/source profile.

The new pure detector owns the seven output-level finding families represented by
`d2l_translation_deterministic_quality_v2`. Existing response-contract,
block/protected-ref, restore, and line-skeleton validators remain the source of
their already mechanical facts; this Phase A does not duplicate or reinterpret
them. The runner adapter must map those validated facts to the unauditable/repair
branches during later exact-delta integration. Raw fake response strings are
exercised through the production JSON parser and local validator; no parallel fake
network or transport implementation was introduced.

Offline verification:

```text
focused Phase A adversarial tests: 74 passed
Translator/protected-span/JSON-envelope regression: 197 passed
API calls: 0
runner or CLI wiring changes: 0
SQLite reads/writes: 0
publication writes: 0
```

An additional full `pipeline/tests` run reached `1451 passed, 2 skipped`. Its 30
failures separated into five cwd-relative failures that passed when rerun from the
runtime root (`23 passed`) and 25 pre-existing fixture-dependent failures. The
remaining tests require the absent frozen `data/jobs/d2l_p1/memory.sqlite3` or
historical report bytes whose physical hashes do not match this worktree. No DB or
historical artifact was copied, rewritten, or normalized to manufacture a green
result. None of the remaining failures imports or exercises the three new quality
modules.

Phase A deliberately does not edit `pipeline/translate/runner.py` or
`pipeline/scripts/run_translate.py`. Consequently this commit cannot call an
Auditor, request a translation retry, change an existing translation, or publish a
new overlay. Phase B is the next authorized step: dry-render the six calibration
packets, measure token growth, and seal caps without API use.

## 21. Phase B implementation record

Status: **dry render complete; Phase C blocked pending an exact generation
capability qualification for the sealed source/model revision.**

Bounded implementation and evidence paths:

```text
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_translation_quality_auditor_phase_b_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_quality_auditor_phase_b_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_translation_quality_auditor_phase_b_v1/calibration_spec.json
THESIS_RUNTIME_TOOL/data/reports/d2l_hardening_v1/translator_quality_auditor_phase_b_v1/**
```

The dry bundle contains three baseline packets and three sabotaged copies over the
same existing six-block Translator windows. Five planted defects cover negation,
omission, numeric fact, referent/scope, and contextual terminology. Expected
labels are stored only in development calibration files outside model-visible
requests. The generator rejects any mutation that changes protected-reference
order or introduces a new deterministic finding, so a mechanical corruption
cannot be mislabeled as an Auditor challenge.

Measured and sealed limits:

```text
token_estimator=utf8_bytes_div3_plus_chat_overhead_v1
measured_input_tokens_est_range=3864..6519
measured_max_glossary_cards=19
measured_max_glossary_tokens_est=970
max_glossary_cards_per_window=24
max_glossary_tokens_per_window=1216
max_input_tokens_per_attempt=8192
max_output_tokens_per_attempt=2048
nominal_call_cap=6
max_attempts_per_packet=2
absolute_attempt_cap=12
aggregate_token_halt=122880
cost_usd=null
cost_status=unknown
```

The estimator is deterministic and intentionally conservative; it does not claim
to be the exact GPT-5.5 tokenizer. The aggregate halt reserves the sealed
worst-case input and output limits for every possible attempt.

Sealed route and blocker:

```text
source_id=modelapi_shared_v1
source_revision=modelapi_profile_v1
physical_quota_bucket_id=modelapi-shared-v1
model=gpt-5.5
structured_output.mode=disabled
native_schema_parameter=false
generation_qualified=false
structured_output_qualified=false
live_phase_c_authorized=false
```

This is a third-party route, so the request uses prompt-generated JSON plus the
unchanged canonical local parser/validator and sends no native schema parameter.
Phase C cannot start from this seal until a separately sealed bounded canary
qualifies generation for exact source/revision/route/model bytes. The dry seal and
historical Translator artifacts must not be relabeled after that qualification;
Phase C requires a fresh run seal referencing the new capability evidence.

Evidence hashes:

```text
generator_code_sha256=DF8FE72D094008D81DA204CA794F1CFA3E3EB88B13353DB70409A4596F08EC9D
run_seal_sha256=36C4A84ED22D9891E03D551C5698787134876F8DEFC92562F0DDC6FF41D3EA9C
artifact_manifest_sha256=4F8FB42A1D2F775D2B6CF5387292EC3644713BFFE0E7A6F27727BE6386CE6F98
frozen_source_db_sha256_before=64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715
frozen_source_db_sha256_after=64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715
```

The existing probability baseline includes two genuine untranslated headings.
They remain unchanged and are explicitly annotated outside the model request. If
the Phase-C Auditor detects them, they count as genuine discoveries and that
window must be replaced before computing the clean false-alarm denominator.

Offline verification:

```text
focused Phase B tests: 7 passed
combined Phase A + Phase B tests: 81 passed
Translator and adjacent quality regression: 173 passed
second dry render: 33/33 files byte-identical
API calls: 0
runner or CLI wiring changes: 0
SQLite writes: 0
publication writes: 0
```
