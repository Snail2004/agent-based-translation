# TASK_EVAL_SCORING_V1 — Thesis Scoring Framework (locked names, methods, hardware)

Status: SPEC LOCKED 2026-07-03 (user + Claude). Implementation queued AFTER exp_s0s1_builderv2_v1 completes (see §5 sequencing). Owner of record: this file — scoring/eval work no longer appends to TASK_BUILDER_V2.md.

Provenance of this design: (a) AMT paper (Duong et al., advisor's own work — reference/AMT_paper_extracted_research.md), §3.3 dual-signal back-translation evaluation, Eq.1; (b) ChatGPT literary-eval proposal (reference/thang_diem_danh_gia_dich_van_hoc_llm.md) — adopted selectively per §6; (c) Gemini's COMET suggestion — CORRECTED: COMET-22 is reference-BASED and unusable here; the right variant is CometKiwi (reference-free QE); (d) WMT22 QE findings + CometKiwi paper + HF model card (verified 2026-07-03).

---

## 1. The locked scale set: 5 scales + 2 gates

| Code | Name (thesis) | Question it answers | Engine | Cost |
|---|---|---|---|---|
| **TC** | Term Consistency (Nhất quán thuật ngữ) | same term → same rendering throughout? | deterministic code; = existing metric `D_registry_consistency` | $0 |
| **TA** | Term Adherence (Tuân thủ thuật ngữ) | terms match an external standard (gold/dictionary)? | deterministic code, occurrence-weighted JOINT; = existing metric `B_gold_occurrence_adherence` | $0 |
| **SF-BT** | Semantic Fidelity — back-translation | meaning preserved through a round trip? | AMT Eq.1: Sim = 0.5·cosine + 0.5·LLM-score, with 3 upgrades (§2) | ~$0 local |
| **SF-QE** | Semantic Fidelity — neural QE | meaning preserved, per a neutral learned judge? | Unbabel/wmt22-cometkiwi-da, local CPU (§3) | $0 |
| **PJ** | Paired Judgment (Phân xử cặp mù) | is S1 better/worse than S0, and why? | blind paired LLM judge, both-order (§4) | ~$0.2 |
| HG | Hygiene Gate | no foreign-script leakage | shipped (§35 D1) | $0 |
| SG | Structural Gate | all blocks present, JSON contract, passthrough untouched | shipped (stage gates) | $0 |

Nature: TC/TA/SF-* are 0→1 scores of ONE translation (power the one-button report). PJ is a comparative verdict (only meaningful with 2 arms; powers the S0-vs-S1 experiment). Gates are pass/fail, never averaged into scores.

Code compatibility: JSON keys `B_*`/`D_*` in existing artifacts stay unchanged; reports/thesis use the mapping D→TC, B→TA.

Defense mapping to AMT's human criteria: terminology consistency→TC, adequacy→SF, fluency→PJ, structural integrity→SG, coherence→TC+SF. TA is the thesis's addition — exactly the "domain-specific terminology" gap AMT's own conclusion names as future work (Table 2: AMT scores LOWEST on its two technical volumes, <0.83).

## 2. SF-BT — back-translation dual signal (extends advisor's AMT Eq.1)

Round trip: EN source → (system VI output) → back-translate VI→EN → compare EN-vs-EN.
`SF_BT(block) = 0.5 · cos(bge-m3(EN_orig), bge-m3(EN_back)) + 0.5 · LLM_score(EN_orig, EN_back)`; chapter score = mean over scope blocks. LLM_score: 0→1 semantic similarity, run twice, averaged (per AMT).

Three declared upgrades over AMT (each is a methods contribution, cite politely):
1. **Independent back-translator**: local Gemma-4-12B (different family from the GPT translator). AMT used the same TranslationAgent both ways → self-agreement bias. Same deterministic profile as §35.11b (temp 0, repeat_penalty 1.0).
2. **Block-exact alignment + longer encoder**: our JSON contract aligns EN↔VI by block_id by construction (AMT needed paragraph-alignment via Controller, MPNet 384-token limit). Encoder = bge-m3 (local LM Studio endpoint, 8192 ctx, already serving T1).
3. **Optional third signal** (report-only, not in the formula): direct cross-lingual cos(bge-m3(EN_orig), bge-m3(VI)) — impossible for AMT's English-only MPNet.

Honest limits (state in thesis): round-trip can mask fluent-but-wrong VI (the back-translator may repair errors); it under-detects terminology-convention misses → that's what TA/TC and PJ are for. No retranslation feedback loop in the EXPERIMENT (AMT iterates to convergence; we measure arms as-produced — measurement must not modify the subject).

## 3. SF-QE — CometKiwi, facts that prevent wasted work

- Model: `Unbabel/wmt22-cometkiwi-da` (InfoXLM-large backbone, ~565M). Born to score: trained to predict human Direct Assessment of translation quality from (source, MT) WITHOUT reference. WMT22 QE winner; community standard.
- **Vietnamese caveat (exact wording for the thesis):** vi is a *supported inference language* via the 94-language backbone; the human-score training data (WMT DA/MLQE-PE: en-de, en-mr, en-yo, en-zh, ro-en, ne-en, ...) contains NO en-vi → vi scoring is zero-shot transfer. Therefore SF-QE is reported as convergent evidence; cross-scale agreement (§5b) is itself a result, and no single scale is the supreme judge.
- Gated model, license CC-BY-NC-SA-4.0 (fine for a thesis; cite). USER action required once: accept conditions on the HF page + `huggingface-cli login`. CodeX cannot do this step.
- **Hardware truth (AMD RX 6800 + i5-14400F + Windows): run on CPU via PyTorch. Do NOT attempt CUDA (NVIDIA-only), ROCm (Linux / RDNA3+ only), or torch-directml (stale).** LM Studio/Vulkan serves the LLM/embedding parts; CometKiwi is an encoder and simply runs CPU: ~10–20 min for 950 blocks, hours for a full book (overnight, cached). Future optimization if ever needed: ONNX export + onnxruntime-directml (works on RX 6800), NOT a prerequisite.
- Reproducibility (per "Pitfalls of COMET"): pin `unbabel-comet` package version + checkpoint hash in every report; never compare scores across versions; guard empty strings before scoring.
- xCOMET-XL/XXL (error spans) and wmt23-cometkiwi-XL: optional future add-ons, not in v1. Unbabel Tower/TowerInstruct GGUF models seen in LM Studio are translation LLMs, NOT scorers — do not confuse.

## 4. PJ — blind paired judge (S0 vs S1)

- Units: ALL differing block pairs (MLP measured: 339/475 differ; identical pairs auto-tie at $0). Judge sees EN source + two VI candidates labeled X/Y, order randomized per item, blind to arm identity.
- Both-order control: each pair judged twice with order swapped; verdict counted only if consistent, else tie (position-bias control). Verdict ∈ {X, Y, tie} + one reason tag from fixed taxonomy: {term_choice, word_order, omission, addition, grammar, style}.
- Judges: primary gpt-5.4-mini (~$0.2 for 2×339); secondary local Gemma at $0 — reported ONLY as agreement analysis (Gemma is uncalibrated for judgment; its verdicts never count toward the headline).
- Pre-registered predictions (locked before any PJ run): ① ties ≥ 50%; ② S1-vs-S0 wins roughly balanced; ③ any S1 losses concentrate in hard-term blocks with tag term_choice; ④ ALARM: S1 loss-rate exceeding S0's by >10 points on grammar/style tags ⇒ memory is damaging prose — investigate before publishing anything.

## 5. Execution & sequencing

(a) Order: finish exp_s0s1_builderv2_v1 first (cascade STOP-B → overlay → prelim pair → prelim 0-API scoring). THEN one scoring pass computes SF-BT, SF-QE, PJ for both chapters × both arms in one config. Pre-registered predictions for SF: S1 ≥ S0 (memory must not hurt semantics); both arms inside AMT's technical-volume band (~0.78–0.85) — if wildly outside, suspect the harness before the translation.
(b) Agreement analysis (thesis section, not an afterthought): correlations/rank-agreement among SF-BT, SF-QE, PJ, and TA/TC deltas. Triangulation IS the answer to the advisor's challenge ("how can code score a sentence?"): no proxy is trusted alone; each has a named blind spot covered by another.
(c) One-button report layout (per advisor's requirement): scores TC/TA/SF-BT/SF-QE + gates + auto-watchlist (§36) with EN–VI evidence pairs from cascade marks; human review remains an OFF-default toggle.

## 6. Literary roadmap (appendix — NOT current scope)

When the pipeline moves to novels (original project root: Treasure Island infra — entities, speaker_turns already in DB), extend rather than replace:
- Faithfulness → SF-BT + SF-QE unchanged.
- Narrative consistency → TC family: TC-Term (story-world terms) + **TC-Ent** (names, nicknames, and per-character-pair xưng hô consistency from entity memory — measurable by code; no standard metric offers this; a distinctive contribution).
- Fluency → PJ target-only variant (monolingual VI reading, how real readers consume novels).
- Literary style → BWS (best-worst scaling over 3–4 anonymized versions incl. public human translation, e.g. Treasure Island VI).
- Cultural/terminology → TA against locked entity/glossary memory; Format → SG.
- Critical gates adopted from the ChatGPT NTQS proposal (reverse-meaning / wrong-character / lost-passage caps the ceiling). NTQS aggregate /100 with fixed weights: report the per-dimension PROFILE as the science; any aggregate is labeled a presentation convention (weights are attackable).
- Sources to cite for this phase: LITEVAL (MQM insufficient for literary), DITING (web-novel dimensions incl. zero-pronoun/xưng hô), TransProQA, TransAgents (monolingual preference), BWS methodology; GEMBA-MQM for LLM-judge error spans.

## 7. Governance note

TASK_BUILDER_V2.md CLOSES when: cascade STOP-B verified + overlay smoke on real marks + prelim S0/S1 run & scored (§35.9) + §36 re-election implemented and validated on the MLP dictionary. A final closing section will summarize outcomes and point here. All scoring/eval work from now on lives in THIS file; literary-phase work will get its own file when it starts.
