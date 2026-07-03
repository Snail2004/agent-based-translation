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

### 5d. Report modes (gold-dependence of the table)

- **Benchmark mode** (gold available, e.g. D2L; 2 arms optional): full table — TC-Occ/TA-Occ lead (post-prelim validation), block-level TC/TA as the $0 deterministic audit twin (labeled "measured conservative lower bound"), SF-BT/SF-QE, PJ if 2 arms, gates + watchlist.
- **Production mode** (arbitrary book, one-button, NO gold): TA-vs-gold columns DO NOT EXIST. Table = TC-Occ/TC (no external standard needed) + TA-Occ against the book's OWN notebook (label honestly: "tuân thủ từ điển tự xây" — discipline, not external correctness) + SF-BT/SF-QE (both reference-free — the reason CometKiwi was chosen over COMET-22) + HG/SG + §36 watchlist (the only net for consistent-but-wrong when no gold exists).
- Optional plug-in: user supplies an external glossary/gold file → TA-vs-gold columns light up. Design the report schema with this slot from day one.

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


## 8. TC-Occ / TA-Occ — per-occurrence pair (PRE-REGISTERED 2026-07-03, before preliminaries runs)

The cascade localization layer (T1 bge-m3 + T2 code + T3 local Gemma, §35.10-§35.13 of TASK_BUILDER_V2) enables per-occurrence versions of the TC/TA pair. Locked BEFORE the preliminaries chapter cascade runs; for MLP these are RETROSPECTIVE illustrations (computed after seeing data) and are labeled as such wherever cited — never headline.

Definitions (occurrence-weighted, both arms scored on the identical frozen ruler — verified 537 terms, 0 accepted_forms differences between arms):
- **TC-Occ** (consistency): per term, share of localized occurrences using the term's majority rendering; renderings taken from cascade marks (T2 target_surface / T3 target_quote_clean, casefolded); not_rendered occurrences excluded. Answers "did the system keep ONE form", ignoring the dictionary.
- **TA-Occ** (adherence): share of occurrences whose localized rendering hits ANY accepted form. Answers "did each occurrence land on an approved form", ignoring switching. Ruler = notebook ∪ gold variants.
- The pair is orthogonal by construction (the 4-quadrant diagnosis: consistent+adherent ideal / consistent+off = the regularization→chuẩn hóa failure / switching+adherent = variant churn / both low = chaos).

Ruler hygiene (required before TA-Occ becomes official): **fragment filter** — drop any accepted form that is (a) a strict substring of another accepted form of the same term AND (b) shared as an accepted form with ≥1 other in-scope term (measured case: bare `hàm` credited sigmoid/loss/tanh function). Dropped forms logged + listed in the report for audit. TC-Occ needs no filter (dictionary-free).

MLP retrospective values (union scope 2,487 occ/arm):

| Lens | TC S0 | TC S1 | TA S0 | TA S1 |
|---|---|---|---|---|
| Block-level (LOCKED headline) | 0.7590 | 0.8253 | 0.7580 | 0.7657 (gold-only ruler, 1,182 occ) |
| Per-occurrence (retrospective, pre-filter) | 0.8646 | 0.9243 | 0.7543 | 0.8737 (union ruler) |

Note the two TA columns use DIFFERENT rulers (external gold-only vs pack∪gold) — never present them as the same scale at two resolutions; TA block-level vs TA-Occ answer "correct vs external standard" vs "landed on an approved form".

Pre-registered predictions for preliminaries (locked now): P1 TC-Occ(S1) > TC-Occ(S0); P2 TA-Occ gap (S1−S0) ≥ block-level TA gap; P3 all four cells agree in direction with block-level. Any violation = investigate harness before interpretation.


### 8b. Measured failure modes of the block-level pair (2026-07-03, evidence for the TC-Occ/TA-Occ upgrade)

Audited D's fail buckets for S1-MLP against cascade localization (which sees every rendering):

1. **Undetected black hole, PROVEN mis-fail:** of S1's 15 undetected hard terms, cascade shows 7 rendered with PERFECT 1.00 consistency (bias term→hạng thiên lệch, missing values→giá trị khuyết, regularization term→số hạng chuẩn hóa, Gaussian distribution, deep learning framework, output layers, vanishing gradient problem) and 3 more at 0.67–0.83 — incl. multilayer perceptrons→perceptron đa tầng which MATCHES GOLD yet D fails it. Root cause: D's ruler = OLD 1,608-entry registry; S1 follows the NEW notebook; notebook forms ∉ old ruler ⇒ invisible. This bias runs AGAINST S1 (S1 undetected 15 vs S0 7) ⇒ the +0.066 headline gap is a conservative LOWER bound on the memory effect.
2. **Binary drift over-punishment:** 6 S1 hard terms failed with majority share ≥0.80 (training data 21:3:1=0.84, gradient 15:1:1=0.88, activation functions 8:2 …). Several "drifts" are Vietnamese plural morphology only (các hàm kích hoạt vs hàm kích hoạt) — not terminology inconsistency.
3. TA (B) is sounder (occurrence-weighted, JOINT, gold ruler both arms blind) — its residual blind spot (min() counting, masquerade) is measured small (~1.8% suspects).

Verdict recorded: instrument fidelity ranking TA-Occ/TC-Occ > TA > TC. exp_s0s1 headline stays B/D v3 per pre-registration (comparability; bias is anti-S1 so conservative); the per-occurrence pair (§8) is the designated successor — after prelim P1–P3 + fragment filter, one-button report leads with TC-Occ/TA-Occ and keeps B/D as the deterministic $0 audit twin. Do NOT patch D's ruler mid-experiment (no new metric versions inside exp_s0s1).
<!-- S9_CONVENTION_VS_MEANING -->
## 9. B/TA = do khop QUY UOC ngoai, khong phai do dung — chot thao luan vecto (user + Claude, 2026-07-04)

Boi canh: prelim §35.9 B S1 0.6036 < S0 0.6660, ~toan bo do `vector` (80 occ = 16.1% mau so): notebook chon "vectơ" (chinh ta SGK VN hop le), gold d2l-vn chon "vector" (co discussion URL — quy uoc cong dong). Ca hai DUNG tieng Viet; day la bat dong PHONG CACH (nhu radio/ra-đi-ô), khong phai loi dich.

CHOT 4 diem:
1. **Dinh danh lai B/TA trong moi bao cao:** "do khop voi quy uoc ben ngoai (gold style guide)", KHONG dien dat la "do dung/chat luong". Headline giu nguyen so xau (ky luat cu — khong doi thuoc giua thi nghiem).
2. **Miss cua B phai phan loai 2 ro khi bao cao:** (a) LOI NGHIA (vd gradient->đạo hàm riêng — chinh B da giup bat ca nay, khong duoc vut B) vs (b) LECH QUY UOC (vecto — nghia nguyen ven). Phan loai la viec cua human/judge annotate (tinh than §33 tiering), KHONG hardcode danh sach vao code.
3. **Style = INPUT khai bao TRUOC cua user, khong phai thu Builder phai doan:** san pham mot-nut them tham so a-priori (giu-thuat-ngu-Anh / Viet-hoa / nop style-guide file rieng = external glossary adapter §5d). Thi hanh qua prompt Builder. **CAM sua prompt vi diem benchmark da nhin** (tuning-on-test tra hinh — "uu tien giu tieng Anh" sau khi thay B thap chinh la hoc thuoc gold). Trong exp_s0s1_builderv2_v1: khong doi gi.
4. **Luan diem chuong ket qua (case study trung tam):** S0 duoc 53/80 diem vector NHO tron lan 74 vector/35 vectơ — voi nguoi doc, van ban tron 2 cach viet TE hon S1 thong nhat 107/109. Tuc B dang THUONG cho su thieu nhat quan cua S0 o term tan suat cao = artifact cua thuoc, khong phai uu diem baseline. He thong memory bien chat luong tu may-rui-tung-cau thanh MOT quyet dinh tu dien: chon dung -> dung ca chuong; chon "sai" quy uoc -> sua 1 dong la lanh ca chuong; S0 sai thi khong co cho nao de sua.

Lien ket: §36 re-election KHONG bat duoc loai (b) khi khong co gold (back-translation cua "vectơ" van ra "vector" — khong co tin hieu bat dong); luoi duy nhat cho loai (b) o production = style-guide adapter do user cung cap.
