# TASK_EVAL_SCORER_PROMPTS_V3

Status: OFFLINE STAGE CONTRACT AND RENDERER IMPLEMENTED - SEMANTIC PROBE PENDING

Owner: Evaluation workstream

Supersedes the externally reviewed, uncommitted V2 draft. Any later change to
runtime prompt bytes requires a new candidate ID and a new task version.

## Purpose

Define book-neutral prompt candidates, closed semantic outputs, presentation
policy, and the missing SF-BT stage boundary for the three common quality
methods. This draft is not wired to the runner and does not authorize a
provider call.

The scorer receives only `EvaluationScorerInputPacketV1`. It never receives an
arm ID, producer/model identity, memory, glossary, gold, human reference,
score, expected winner, or prior verdict.

Prompt hashes below cover the exact text inside each `text` fence, excluding
the fence and its surrounding newline, encoded as UTF-8 with LF line endings.

## SF-QE

SF-QE has no LLM prompt. It consumes the active English source and one active
Vietnamese candidate as a segment pair. The model/checkpoint and raw-score
contract remain separately pinned; raw values must not be clamped or presented
as an absolute percentage.

## SF-BT stage 1: back-translation candidate

Candidate ID: `sf_bt_reverse_v3_1_candidate`

Canonical prompt SHA-256:
`5965e61a247256c93395c915cd91bd5e089b5e3f1d7b29b8d330ca3d01144ffd`

Revision `v3_1` clarifies one semantic boundary without changing the response
shape: a referring expression already present in ACTIVE may be rendered with
its context-resolved antecedent, while an entity not referred to by ACTIVE may
not be imported.

```text
You are an independent Vietnamese-to-English back-translator used for translation evaluation.

Translate only the block marked ACTIVE into English.
Use PRECEDING and FOLLOWING blocks only to resolve references, terminology, and continuity.

Requirements:
- Preserve every fact, claim, number, negation, entity, and logical relation that the ACTIVE block itself expresses.
- Use PRECEDING and FOLLOWING only to choose correct English wording for references and terminology that the ACTIVE block already contains. Never import a fact, claim, number, or entity that appears only in PRECEDING or FOLLOWING. If the ACTIVE block omits something, your English must omit it too.
- A pronoun or other referring expression in ACTIVE may be rendered with its context-resolved antecedent when needed for unambiguous English. This resolves an ACTIVE reference; it does not authorize adding an entity that ACTIVE never refers to.
- Preserve Markdown, inline code, URLs, and LaTeX math exactly where possible.
- Do not summarize, shorten, expand, explain, criticize, or improve the content.
- If the ACTIVE block is unclear or appears incorrect, translate it as faithfully as possible; do not guess a better version.
- Do not translate the context blocks as additional output.
- Return only the JSON object described below.

Return JSON exactly:
{"back_translation":"English translation of the ACTIVE block"}

VIETNAMESE BLOCK SEQUENCE
{target_block_sequence}
```

The renderer must label each row `PRECEDING`, `ACTIVE`, or `FOLLOWING` and
include its canonical block ID. The packet contract already guarantees that no
English source object is present in this stage.

Proposed closed response:

```json
{"back_translation":"..."}
```

An empty answer, extra key, prose outside JSON, truncation, or malformed JSON is
a response-contract failure. It may consume only the sealed transport retry
budget; a low-quality but valid answer is not retried.

Stage 1 is unary. This version does not reuse one back-translation merely because the
two arms have byte-equal ACTIVE text: their visible context and provenance may
differ. A future execution dedupe is legal only when the complete model-facing
packet is content-identical and the reused result remains bound to every
logical job.

## SF-BT sealed stage boundary

The current `EvaluationScorerInputPacketV1` protects only the stage-1 input.
No live SF-BT run is allowed until the following internal, content-addressed
boundary exists:

1. `SFBackTranslationResultV1` binds the validated stage-1 packet hash, plan,
   config, input set, job, unit, attempt, prompt version/hash, model profile,
   finish reason, and exact `back_translation` bytes.
2. `SFBTSemanticJudgeInputPacketV1` binds that stage-1 result hash and obtains
   the canonical active English source from the same validated common input by
   exact block identity.
3. The stage-2 packet exposes two opaque English passage slots in a sealed
   presentation order. It exposes no arm ID, producer model, memory, glossary,
   gold, reference, score, expected winner, or prior verdict.
4. The validator rejects a foreign source, foreign stage-1 result, mismatched
   block/job/unit, duplicate identity, hash drift, unknown key, or non-finite
   value. Validation and canonicalization do not mutate input.

This is an internal Evaluation contract. It does not alter
`D2LEvaluationInputV1`, `TranslationArtifactV1`, or `FullRunReportV1`.

## SF-BT stage 2: semantic comparison candidate

Candidate ID: `sf_bt_semantic_judge_v3_candidate`

Canonical prompt SHA-256:
`64d4f9c9fb63b190afc4ff54f6fd1ab9ced26d47f7653d9227003ab63b689753`

```text
You compare two English passages.
Judge how closely they match IN MEANING. The passage labels and order are arbitrary.

Ignore differences in style, word choice, sentence order, formatting, and phrasing when meaning is unchanged.
Judge only facts, claims, numbers, logical relations, negations, and coverage.
Coverage counts in both directions: content present in only one passage is a mismatch.
Length by itself is not evidence of better or worse meaning preservation.

Score bands:
100 = same meaning; differences are purely stylistic
75 = minor drift; one small detail differs or became vague
50 = noticeable drift; a fact, number, or relation differs
25 = substantial mismatch; key claims differ or content is absent
0 = different or contradictory content

Choose exactly one band value: 0, 25, 50, 75, or 100. Do not output any other number.

Flags, all that apply or an empty list:
semantic_mismatch | numeric_mismatch | negation_mismatch | coverage_mismatch | untranslated_residue | format_only

Return JSON exactly:
{"score":0,"flags":[],"note":"one short English sentence"}

PASSAGE A
{passage_a}

PASSAGE B
{passage_b}
```

`score` must be exactly one of `{0, 25, 50, 75, 100}`; any other value is a
response-contract failure. It remains the SF-BT LLM component and is not
averaged with embedding cosine unless a later composite is separately
preregistered. `flags` are diagnostic and do not secretly change the score.

The canonical full run uses one sealed presentation per item. A deterministic,
preregistered sample of `min(50, eligible_items)` is also judged in reverse
presentation order, balanced across arms and source order. If more than 10% of
audited items change band between presentation orders, or any single item
changes by two or more bands, the run stops before a headline is published and
requires an explicit decision on full both-order scoring. It does not silently
add retries or average away the finding.

## PJ: blind paired judgment candidate

Candidate ID: `pj_common_v2_candidate`

Canonical prompt SHA-256:
`faaa90b02253a24ce1143b84dd6840533c7b4349c95c64486fd12ec6673511d8`

Presentation and aggregation policy ID: `pj_presentation_policy_v3`. The
prompt bytes are unchanged from the reviewed V2 candidate; only the external
policy version advances.

```text
You are a strict, impartial evaluator of Vietnamese translations of an English source.

You receive one English block sequence and two Vietnamese candidate sequences labeled Candidate 1 and Candidate 2. Their labels and order are arbitrary and reveal nothing about the systems that produced them.

Judge only the ACTIVE block. Use PRECEDING and FOLLOWING blocks to understand continuity, references, register, and tone; do not score those context blocks as additional items.
A context row may read [CONTEXT BLOCK NOT AVAILABLE]. Never count an unavailable context row for or against a candidate.

Return only JSON with exactly these keys:
{"overall_verdict":"candidate_1|candidate_2|tie","style_verdict":"candidate_1|candidate_2|tie","tags":[],"note":"one short English sentence"}

Definitions:
1. overall_verdict: the better translation overall, considering meaning, completeness, terminology, grammar, naturalness, tone/voice, and formatting. Use tie when differences are immaterial or evidence is insufficient.
2. style_verdict: the better Vietnamese prose, considering grammar, fluency, naturalness, register, tone/voice, and ordinary word choice. Ignore differences that are only technical terminology or source-faithfulness. If only meaning or terminology differs, style_verdict must be tie.
3. tags: zero to three decisive categories from this closed list, most important first:
grammar | naturalness | word_choice | terminology | meaning | omission_addition | formatting | tone_voice
4. note: at most 25 English words identifying the decisive difference, or "no meaningful difference".

Rules:
- Do not reward literal English-like wording when natural Vietnamese preserves the meaning.
- Longer output is not better output. Judge fidelity and natural Vietnamese, not length.
- Do not assume either candidate is a human, model, baseline, or memory-assisted output, and do not try to identify its producer.
- Prefer the candidate that preserves code, math, URLs, numbers, names, and document structure when the source requires them.
- The ACTIVE block may be a heading, caption, fragment, dialogue line, or prose paragraph. Judge it according to its actual function.
- Never invent a preference. Tie is a valid result.

ENGLISH SOURCE SEQUENCE
{source_block_sequence}

CANDIDATE 1 SEQUENCE
{candidate_1_block_sequence}

CANDIDATE 2 SEQUENCE
{candidate_2_block_sequence}
```

Proposed closed response:

```json
{
  "overall_verdict": "candidate_1|candidate_2|tie",
  "style_verdict": "candidate_1|candidate_2|tie",
  "tags": ["closed taxonomy, zero to three"],
  "note": "at most 25 English words"
}
```

### PJ context symmetry

Context is renderer-owned presentation data, not an extra item to score. For
each non-active block position:

- if both candidates have valid text, render both;
- if either candidate lacks valid text, mask that position for both candidates
  with the same `[CONTEXT BLOCK NOT AVAILABLE]` placeholder;
- never show one candidate's target context while hiding the corresponding row
  for the other candidate.

The source context may remain visible because it is common to both candidates.
The active candidate row must remain present and translated; otherwise the job
is blocked before rendering.

### PJ identical-pair and order policy

The renderer first applies the symmetric context policy above. It then compares
the complete displayed candidate sequences using only the declared mechanical
normalization: Unicode NFC, CRLF-to-LF, and removal of trailing whitespace at
line ends. It does not collapse internal whitespace or remove punctuation,
Markdown, code, or math.

- complete displayed sequences equal: deterministic tie at zero model calls,
  with both verdict origins set to `mechanical_equal`;
- sequences differ: judge both presentation orders;
- aggregate `overall_verdict` and `style_verdict` independently after
  opaque-slot unmapping;
- both orders select the same underlying candidate: win, origin
  `unanimous_win`;
- both orders return tie: tie, origin `both_orders_tie`;
- one order returns tie and the other a winner: tie, origin `mixed_tie`;
- the two orders select opposite candidates: tie, origin `order_conflict`;
- any leg missing or contract-invalid after the sealed retry budget: pair
  origin `evidence_missing`; exclude it from win/tie/loss counts and report it
  separately as a denominator fact;
- only valid non-win origins collapse to headline tie. `evidence_missing` never
  becomes a tie. Tags and notes remain per-call diagnostics and never break a
  tie.

The aggregate result carries `overall_decision_origin` and
`style_decision_origin` separately because the two verdict types may resolve
differently. `order_conflict` rates are reported separately for overall and
style, using valid non-mechanical both-order pairs as their denominators. A
mechanical `active_equal_context_diff` analysis flag may be recorded when the
active texts match but the complete displayed sequences do not; it never
changes a verdict.

The earlier measurement `136/475 identical` used an older active-text rule and
must not be reused as the new call count. Dry-render recomputes equality under
the complete-sequence rule. Both-order is intentionally more expensive than a
single order; it is selected for item-level position-bias control, not described
as a cost reduction.

## Fairness and aggregation locks

- SF-QE and SF-BT are unary: every selected arm is scored independently on the
  same eligible-unit universe.
- PJ uses only explicitly declared pairs. It never expands all baselines into
  an unapproved all-pairs graph.
- PJ slot mapping remains outside the prompt and result.
- A low or unfavorable score is never a retry reason.
- Missing, failed, or contract-invalid rows remain denominator facts.
- SF-QE raw score, SF-BT cosine, SF-BT LLM score, PJ overall counts, and PJ
  style counts remain separate. No weighted total is defined in v1.
- SF-QE and SF-BT values may be compared between arms only when scorer version,
  checkpoint/model profile, admitted universe, and aggregation policy match.
  They are not absolute quality percentages.
- For the current S0/S1 pilot, the known forward-translator family, SF-BT
  back-translator family, and SF-BT semantic-judge family must be pairwise
  distinct; the PJ judge must differ from the known forward-translator family.
  A conflict blocks pilot plan sealing.
- This pilot-specific lock does not define a universal policy for future
  Human/Google/multi-model baseline graphs. Those plans must carry known
  producer-family metadata outside model-visible prompts, preregister a
  pair-level conflict policy, and report any unavoidable overlap. They may not
  silently claim family independence or silently switch judges.

## Model decisions still open before implementation

1. SF-QE has historical evidence for `Unbabel/wmt22-cometkiwi-da`; the current
   package version and checkpoint bytes must be reverified and pinned before
   reuse.
2. The historical D2L run used local Gemma-4-12B as back-translator with
   deterministic settings. That is evidence, not automatic authority for the
   common D2L-plus-literary scorer; local availability and current probe
   behavior must be confirmed.
3. Semantic-judge model for SF-BT.
4. PJ judge model.

Historical model choices and results must be cited when a candidate is reused,
but a historical D2L-specific lock does not silently pin the common scorer.
Model choices and call counts are sealed only after current prompt probes,
availability verification, independence review, and token/cost preflight.

## Required probe before live scoring

- book-neutral planted cases covering identical outputs, meaning reversal,
  omission, number/negation drift, grammar, terminology-only difference,
  tone/register, formatting, and short fragments;
- a long escaped-output case containing quotes, backslashes, Markdown, code,
  URLs, and LaTeX, with output-cap and finish-reason assertions;
- asymmetric missing-context cases proving that PJ masks the same context row
  for both candidates;
- rendered-prompt identifier scan: assert the final model-visible text contains
  no plan ID, job ID, arm ID, packet hash, or serialized producer/workstream
  value. Generic rubric vocabulary such as `producer` or `evaluation` is not
  itself an identifier leak;
- both presentation orders where applicable;
- exact JSON, taxonomy, note-length, truncation, and no-prose checks;
- no in-domain answer examples in runtime prompts;
- prompt byte hash and version pinning;
- model version, finish reason, token usage, retry, and raw-response logging;
- a STOP gate before chapter-scale execution.

### SF-BT context ablation

The stage-1 context profile remains a measured run-config decision, not a
prompt intuition. Compare profile A (`no_context`) with profile B
(`bounded_neighbors`) on book-neutral synthetic fixtures that never enter a
runtime prompt:

- P1: ACTIVE omits a fact repeated in PRECEDING, 10 items;
- P2: ACTIVE omits a fact absent from context, 10 items;
- P3: ACTIVE uses an anaphor whose referent appears only in context while the
  canonical English source uses a noun, 10 items;
- P4: an ambiguous term is disambiguated by context, 10 items;
- P5: context contains a number or claim absent from both canonical English and
  VI ACTIVE, 10 items.

Measure context-import rate directly from back-translation output and omission
detection through the sealed stage-2 judge. The exact marker matcher, score
criterion, direction balance, manual P4 audit, and PJ tag handling are pinned
in `TASK_EVAL_SCORER_PLANTED_PROBE_V1.md`. Select profile B only when all are
true:

- pooled profile-B import rate across P1 and P5 is at most 5%;
- P2 omission detection is at least 90% in both profiles and differs by no more
  than 10 percentage points;
- P1 omission detection under B is no more than 10 percentage points below P1
  under A;
- B reduces each of P3 and P4 false-alarm rates by at least 20 percentage
  points versus A.

Otherwise select profile A. The selected profile, fixture-set hash, sample
counts, and measurements are sealed in the run config/report. A low-quality
probe result is not retried into a preferred policy.

### SF-QE truncation accounting

After the SF-QE checkpoint is pinned, use its exact tokenizer and effective
input limit to preregister a truncation policy. Never allow silent truncation.
The report records total eligible rows, over-limit rows, truncated rows if the
declared policy permits truncation, and score coverage with and without those
rows. No threshold is guessed before the checkpoint is fixed.

## Explicitly deferred

- No separate `PJ-context` or chapter-coherence scorer is added in this D2L
  pilot. The common PJ already receives bounded neighboring context. A distinct
  literary coherence experiment requires its own preregistered task and is not
  a blocker here.
- No xCOMET span extraction, multi-judge ensemble, weighted composite, all-pair
  baseline graph, or cross-arm SF-BT execution dedupe is added in this version.

## Offline implementation handback

Implemented without provider calls, database access, App wiring, scorer
execution, or public producer-contract changes:

- `pipeline/eval/sf_bt_stage_contracts_v1.py`
  - closed, content-addressed `SFBackTranslationResultV1`;
  - closed, content-addressed `SFBTSemanticJudgeInputPacketV1`;
  - exact raw-response and rendered-prompt binding, explicit stage-1 context
    profile, model/transport provenance, source/result reconstruction, opaque
    passage slots, immutable validation, and NFC-before-hash handling.
- `pipeline/eval/scorer_prompts_v3.py`
  - byte-pinned renderers for all three frozen prompt candidates;
  - non-recursive placeholder substitution so source text cannot be re-read as
    a template field;
  - SF-BT context profiles `no_context` and `bounded_neighbors`;
  - PJ symmetric context masking, full-sequence mechanical equality, both-order
    render preparation, and `active_equal_context_diff`;
  - strict closed response parsers for SF-BT semantic bands and PJ verdicts.

Offline evidence:

- focused packet + stage + renderer suite: 69 passed;
- all Evaluation tests: 180 passed;
- repository pipeline suite from the worktree root: 1,064 passed, 2 skipped,
  6 failed only because four legacy tests assumed the runtime directory as cwd
  and two Literary probes require the untracked frozen D2L SQLite absent from
  this Evaluation worktree;
- the four cwd-sensitive failures passed when rerun from
  `THESIS_RUNTIME_TOOL`;
- the two frozen-DB probes were not bypassed, patched, or supplied with a copied
  database.

The semantic planted-set and P1-P5 model measurements remain pending review and
an explicit API allocation. These offline tests prove transport, blinding,
rendering, and response-contract behavior; they do not claim that any model
meets the semantic rubric.
