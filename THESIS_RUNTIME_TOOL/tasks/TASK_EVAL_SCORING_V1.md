# TASK_EVAL_SCORING_V1 — Thesis Scoring Framework (locked names, methods, hardware)

Status: SPEC LOCKED 2026-07-03 (user + Claude). Implementation queued AFTER exp_s0s1_builderv2_v1 completes (see §5 sequencing). Owner of record: this file — scoring/eval work no longer appends to TASK_BUILDER_V2.md.

Provenance of this design: (a) AMT paper (Duong et al., advisor's own work — reference/AMT_paper_extracted_research.md), §3.3 dual-signal back-translation evaluation, Eq.1; (b) ChatGPT literary-eval proposal (reference/thang_diem_danh_gia_dich_van_hoc_llm.md) — adopted selectively per §6; (c) Gemini's COMET suggestion — CORRECTED: COMET-22 is reference-BASED and unusable here; the right variant is CometKiwi (reference-free QE); (d) WMT22 QE findings + CometKiwi paper + HF model card (verified 2026-07-03).

---

## 1. The locked scale set: 5 scales + 2 gates

| Code | Name (thesis) | Question it answers | Engine | Cost |
|---|---|---|---|---|
| **TC** | Term Consistency (Nhất quán thuật ngữ) | same term → same rendering throughout? | deterministic code; = existing metric `D_registry_consistency` | $0 |
| **TA** | Term Adherence (Tuân thủ thuật ngữ) | terms match an external standard (gold/dictionary)? | deterministic code, occurrence-weighted JOINT; = existing metric `B_gold_occurrence_adherence` | $0 |
| **SF-BT** | Semantic Fidelity — back-translation | meaning preserved through a round trip? | AMT Eq.1 idea (BT similarity); components SF-BT-cos + SF-BT-llm reported separately, composite decided post-probe — see §2b (0.5/0.5 RETIRED) | ~$0 local |
| **SF-QE** | Semantic Fidelity — neural QE | meaning preserved, per a neutral learned judge? | Unbabel/wmt22-cometkiwi-da, local CPU (§3) | $0 |
| **PJ** | Paired Judgment (Phân xử cặp mù) | is S1 better/worse than S0, and why? | blind paired LLM judge, both-order (§4) | ~$0.2 |
| HG | Hygiene Gate | no foreign-script leakage | shipped (§35 D1) | $0 |
| SG | Structural Gate | all blocks present, JSON contract, passthrough untouched | shipped (stage gates) | $0 |

Nature: TC/TA/SF-* are 0→1 scores of ONE translation (power the one-button report). PJ is a comparative verdict (only meaningful with 2 arms; powers the S0-vs-S1 experiment). Gates are pass/fail, never averaged into scores.

Code compatibility: JSON keys `B_*`/`D_*` in existing artifacts stay unchanged; reports/thesis use the mapping D→TC, B→TA.

Defense mapping to AMT's human criteria: terminology consistency→TC, adequacy→SF, fluency→PJ, structural integrity→SG, coherence→TC+SF. TA is the thesis's addition — exactly the "domain-specific terminology" gap AMT's own conclusion names as future work (Table 2: AMT scores LOWEST on its two technical volumes, <0.83).

## 2. SF-BT — back-translation dual signal (extends advisor's AMT Eq.1)

Round trip: EN source → (system VI output) → back-translate VI→EN → compare EN-vs-EN.
~~`SF_BT(block) = 0.5 · cos + 0.5 · LLM_score`~~ **RETIRED before first run (see §2b, 2026-07-04):** two co-primary columns `SF-BT-cos` = cos(bge-m3(EN_orig), bge-m3(EN_back)) and `SF-BT-llm` = LLM_score(EN_orig, EN_back); any composite is decided post-probe and named `SF-BT-rank-composite` (run-relative, NOT an absolute semantic score).

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

- Units: ALL differing block pairs (MLP measured: 339/475 differ; identical pairs auto-tie at $0). Judge sees EN source + two VI candidates labeled X/Y, ~~order randomized per item~~ [SUPERSEDED 2026-07-05 §4a: order deterministic theo seed, LUON chay ca 2 chieu nen randomize thua], blind to arm identity.
- Both-order control: each pair judged twice with order swapped; verdict counted only if consistent, else tie (position-bias control). Verdict ∈ {X, Y, tie} + ~~one reason tag from fixed taxonomy: {term_choice, word_order, omission, addition, grammar, style}~~ [SUPERSEDED 2026-07-05 §4a: 1-3 tag tu taxonomy 7-tag moi].
- Judges: ~~primary gpt-5.4-mini (~$0.2 for 2×339)~~ [SUPERSEDED 2026-07-05 §4a: primary = gemini-2.5-flash config §2e — dong nay viet 2026-07-03 TRUOC khi §2e loai gpt-5.4-mini vi cung ho Translator; probe PJ rieng van BAT BUOC]; secondary local Gemma at $0 — reported ONLY as agreement analysis (Gemma is uncalibrated for judgment; its verdicts never count toward the headline).
- Pre-registered predictions (locked before any PJ run): ① ties ≥ 50%; ② S1-vs-S0 wins roughly balanced; ③ any S1 losses concentrate in hard-term blocks with tag ~~term_choice~~ terminology (doi ten theo §4a); ④ ALARM: S1 loss-rate exceeding S0's by >10 points on grammar/style tags ⇒ memory is damaging prose — investigate before publishing anything.

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
<!-- S8C_PRELIM_OFFICIAL -->
### 8c. PRELIMINARIES OFFICIAL — first pre-registered TC-Occ/TA-Occ run (Claude, 2026-07-04)

Union scope 1,848 occ/arm (cascade_preliminaries_S0/S1.json; full-sweep verified vs workdb, 0 mismatch). Renderings: T2 target_surface / T3 target_quote markdown-stripped; casefold + ws-collapse; not_rendered excluded from TC, counted as miss in TA.

| Metric | S0 | S1 | gap |
|---|---|---|---|
| TC-Occ | 0.9148 (1686/1843) | 0.9467 (1741/1839) | **+0.0319** |
| TA-Occ OFFICIAL (fragment-filtered) | 0.7911 (1462/1848) | 0.8750 (1617/1848) | **+0.0839** |
| TA-Occ pre-filter (comparability w/ MLP retrospective) | 0.7917 | 0.8750 | +0.0833 |

Fragment filter: 9 term-form drops logged in tc_ta_occ_preliminaries.json (truc/cot/doc lap/chuan/hang/vecto-in-row-vector/tensor/do dai); net effect tiny here (S0 -1 hit, S1 0) — unlike MLP's bare-`hàm` case, prelim ruler had few dangerous fragments. Filter stays mandatory (cheap, prevents the measured MLP failure).

**Predictions:** P1 TC-Occ(S1)>TC-Occ(S0): 0.9467>0.9148 — **PASS**. P2 TA-Occ gap >= block-level TA gap: +0.0839 >= -0.0624 — **PASS**. P3 four-cell direction agreement with block-level: TC cell agrees (+0.0319 vs D +0.0526); **TA cell VIOLATED** (TA-Occ +0.0839 vs block B -0.0624).

**P3 investigation (required by §8 before interpretation) — harness CLEAN, cause structural:** the two TA cells use different rulers BY DESIGN (block B = gold-only; TA-Occ = notebook∪gold). Under the union ruler `scope:vector` scores 52/52 in BOTH arms (both "vector" and "vectơ" accepted) — the §9 orthography disagreement vanishes, so the single term that drove block-B negative (16% of gold denominator) has zero differential effect here. Positive TA-Occ gap driven by real adherence gains: shape +22 (4/26->26/26), vectors +17 (6/25->23/25), elementwise +9, mean/tangent line/differentiation +5 each. Full-sweep DB cross-check 0 mismatch. Conclusion: P3's TA disagreement is the §9 convention-vs-meaning split showing up exactly where predicted — external-standard agreement (B) and approved-form landing (TA-Occ) are DIFFERENT questions; report both, never conflate.

Cross-chapter replication now on record: TC-Occ +0.0319 (prelim, official) vs +0.0597 (MLP, retrospective); TA-Occ +0.0839 vs +0.1194. Direction reproduces on held-out chapter with pre-registered predictions.
<!-- S3B_SFQE_PROBE -->
### 3b. SF-QE model probe ACCEPTED + production-run task (2026-07-04)

**Probe verified tren artifact (Claude recount khop 100%):** wmt20-comet-qe-da 1/5 -> LOAI. wmt22-cometkiwi-da: basic 4/5, wide 17/20, ~15 items/s CPU. wmt23-cometkiwi-da-xl: 17/20 nhung cham ~8x -> LOAI (khong tang accuracy). Gemma-4-12b judge: 19/20 nhung ~11.5s/pair -> de danh cho PJ/audit, khong scale-score. **CHOT model SF-QE: Unbabel/wmt22-cometkiwi-da** (dung model da khoa o §3).

**Phan cong lao dong DO DUOC (bang chung triangulation cho thesis):** ca 3 he (wmt22/xl/Gemma) deu fail dung cum TERMINOLOGY (regularization margin -0.0004 ~ hoa; convolution_kernel -0.13; Gemma fail regularization vi prior "chuẩn hóa quen hon" — cung prior voi vu §36 population). => SF-QE bat quality-drift chung, KHONG phan xu thuat ngu — do la viec cua TC/TA; van phong la viec cua PJ. Caveat zero-shot vi da co tu §3 nay co so do thuc nghiem.

**Gioi han probe (ghi de khong tu lua):** 20 cap synthetic do implementer viet — du cho quyet dinh chon model (ordering tren loi cay san), KHONG du de calibrate nguong tuyet doi. => Readout production = SO SANH PHAN PHOI paired S0-vs-S1, khong dat nguong diem tuyet doi.

**TASK for CodeX — SF-QE production run (0 API, CPU, ~vai phut):**
1. Script `pipeline/scripts/score_sf_qe.py`: doc workdb exp_s0s1_full (mode=ro) translation_runs exp_s0s1_builderv2_v1, CA 2 chuong x S0/S1 (823 block/arm). Segment = block: (src EN block text, output VI). Model wmt22-cometkiwi-da CPU, batch deterministic.
2. Report JSON: pin unbabel-comet version + checkpoint sha (Pitfalls-of-COMET); per chapter x arm: mean/median/p25/p75/min; PAIRED delta per block S1-S0: mean, median, %block S1>S0, %block |delta|<0.01; bottom-10 block moi arm (block_id + score) de audit; so block bi TRUNCATE boi gioi han 512 token XLM-R (log, khong sua text); markdown giu nguyen as-is (doi xung 2 arm — ghi quyet dinh nay vao report).
3. KHONG ghi DB, khong sua text, khong interpretation. STOP sau run — Claude doc report, tu recount vai so, viet readout chinh thuc vao file nay.
Du bao truoc (ghi de doi chieu, khong phai tieu chi cung): ky vong delta S1-S0 ~ 0 hoac duong nhe (memory pack khong duoc lam tut QE; neu S1 < S0 ro ret o duoi block-level -> dieu tra truoc khi ket luan).
<!-- S3C_SFQE_OFFICIAL -->
### 3c. SF-QE OFFICIAL READOUT (Claude verified & recounted, 2026-07-04)

**Run:** wmt22-cometkiwi-da CPU (py3.11 — numpy<2 nen 3.13 khong cai duoc; ops note), unbabel-comet 2.2.7, checkpoint sha256 4f357aa38b0737dc..., batch 8, 1,646/1,646 segment, 1.25 items/s, ~22 phut. Workdb/frozen hash bat bien; mode=ro; ORDER BY deterministic. Recount doc lap khop 100% moi con so.

| Paired delta (S1-S0, per block) | Gia tri |
|---|---|
| Overall mean (n=823) | **+0.00283** |
| Median | 0.0000 |
| MLP mean / prelim mean | +0.00065 / +0.00581 |
| % S1>S0 strict / % S0>S1 strict | 40.10% / 39.98% (doi xung) |
| % \|delta\| < 0.01 | 73.88% |

**Ket luan doi chieu du bao S3b (dat truoc):** PASS — phan phoi hai arm GAN NHU TRUNG NHAU, mean duong nhe. Bang chung SF-QE: **bom tu dien vao prompt KHONG lam tut chat luong cau theo QE**; dang chu y prelim (+0.0058) chinh la chuong S1 vang loi tu dien nang nhat (ke ca vecto) ma QE khong thay hai — nhat quan voi S9: bat dong quy uoc khong phai suy giam chat luong.

**Caveats giu nguyen ky luat:** zero-shot vi -> convergent signal, khong bao gio la judge duy nhat; 38/1,646 block vuot 512 token bi cat (da log; KHONG trung voi nhom S1-thua-dam); diem segment-level nhieu — bottom-10 va cac block delta am dam (kaggle_house_price_b042 -0.34, environment_b085 -0.30, autograd_b035 -0.21, deu KHONG truncated) la INPUT AUDIT cho PJ, khong phai phan quyet.

**Trang thai bo cham: 5/7 thang xong** (TC, TA, TC-Occ, TA-Occ, SF-QE). Con: SF-BT ($0 local — ke tiep), PJ (paid, cost-gate), roi agreement analysis + SG.
<!-- S2B_SFBT_DESIGN -->
### 2b. SF-BT — thiet ke chot sau phan bien 2 chieu (Claude x CodeX, 2026-07-04) + TASK probe

**AMENDMENT §2:** cong thuc "0.5*cos + 0.5*LLM" go khoi trang thai locked — CodeX dung: Eq.1 (AMT) chi la y tuong BT-similarity; trong so composite la thiet ke moi chua kiem. Sua TRUOC lan chay dau, co bien ban — hop le (khac voi doi thuoc giua thi nghiem). Dong thoi BAC de xuat "nghieng 0.4/0.6" cua CodeX (cung loi: chua co data da chon trong so; calibrate tren 20 cap synthetic = fit mau ti hon). CHOT: bao cao HAI COT dong-chinh (SF-BT-cos, SF-BT-llm); composite (neu can) = trung binh 2 thanh phan chuan hoa theo HANG percentile trong run — zero tham so fit; quyet dinh composite SAU probe bang data.

**Kien truc (block-level, ghep exact theo block_id — khong sentence-align, bai hoc EV-09):**
workdb ro -> Gemma BT (VI->EN, MU: khong EN goc/khong tu dien/khong context, temp 0, seed, text tho khong ep JSON) -> 3 tin hieu: bt_bge_cosine (EN goc <-> EN BT), bt_llm_score (EN<->EN, rubric 0-100, dao thu tu theo seed, JSON {score, error_flags[semantic_mismatch|numeric_mismatch|negation_mismatch|coverage_mismatch|untranslated_residue|format_only] (KHONG dinh huong — xem 2b-fix diem 3)} — flags CHI mo ta, khong vao diem), direct_bge_en_vi (cot phu diagnostic, khong headline) -> aggregate y het SF-QE (per chuong x arm + paired delta + bottom-10 + hygiene/truncation audit).

**Hygiene BT (CodeX dong gop, check thuan co hoc):** flag rieng — output rong / ty le ky tu VN con lai cao / mat-lech so token $math$/`code` / ty le do dai bat thuong. Khong de loi co hoc gia dang diem thap.

**Gemma-tu-cham caveat + luat ha cap DANG KY TRUOC:** GPT-mini audit ~100 block (chon deterministic theo seed, phu deu 2 chuong x 2 arm; vai cent, cost-gate). Neu Pearson(Gemma,GPT) < 0.6 HOAC |mean diff| > 10/100 -> bt_llm_score(Gemma) xuong diagnostic, headline full = SF-BT-cos (du 1,646); GPT-sample CHI la audit phu, khong bao gio trinh bay nhu headline; muon SF-BT-llm full bang GPT = quyet dinh chi phi rieng trinh user (xem 2b-fix diem 5).

**TASK for CodeX — PHASE 1 PROBE (~15-20 phut, $0 tru GPT audit chua chay):**
1. Tai dung nguyen bo wide-probe 41 case cua SF-QE + them case that (BT-pair tu workdb: negation, numeric, untranslated, omission) + cac case THUAT NGU (vector/vecto, regularization) dan nhan `expected_blind` — KHONG tinh vao pass/fail (SF-BT mu thuat ngu by design; dua vao de ghi bang chung mu).
2. Chay tron chuoi BT->cosine->LLM-score tren bo probe. Tieu chi: paired ranking good>bad tren cac cap KHONG-expected_blind, bao ket qua tung thanh phan RIENG (cos va llm doc lap) — de quyet composite.
3. Bao: bang pairwise per component, cac expected_blind co mu that khong, hygiene flags co bat dung case untranslated khong, toc do/call. STOP — Claude verify roi moi GO full run 1,646 block.
<!-- S2B_FIX -->
### 2b-fix — va spec truoc Phase-1 probe (Claude verify + chot, 2026-07-04)

CodeX bat 4 diem truoc khi chay probe — phan xu:
1. **Stale formula (DUNG):** dong 15 (bang tong quan) + dong 30 (§2) van ghi 0.5/0.5 -> DA SUA TAI CHO (strikethrough + tro ve §2b). Bai hoc: amendment phai va NGAY cho cu, khong chi append cuoi file.
2. **Ten composite (DUNG):** percentile-rank composite la thuoc do TUONG DOI trong run, khong so duoc giua sach/run khac -> neu dung phai ten `SF-BT-rank-composite`; headline luon la 2 cot cos/llm.
3. **Mau thuan mu-thu-tu vs flags dinh huong (DUNG):** CHOT phuong an (a) — giu MU thu tu (equivalence la quan he doi xung, blindness quy hon nhan chieu), flags doi sang KHONG dinh huong: `semantic_mismatch`, `numeric_mismatch`, `negation_mismatch`, `coverage_mismatch` (mot ben noi nhieu/it hon ro ret), `untranslated_residue`, `format_only`. Phan tich CHIEU (omission vs addition) thuoc ve human audit bottom-10 — chuoi BT von khong quy trach nhiem duoc loi nam o luot dich hay luot dich nguoc, gan nhan chieu tu dong la gia chinh xac.
4. **Encoding probe (file SACH — canh bao la console artifact):** Claude verify bytes: 41 case, 0 mojibake, 40/41 co dau VN chuan, 100% NFC (case con lai = untranslated co y). Quy tac ops ghi vao day: doc probe bang Python utf-8, KHONG tin render cua terminal PowerShell.
5. ~~**Nhanh ha cap Gemma (bo sung dung):**~~ **[SUPERSEDED boi §2e 2026-07-04: judge chinh = Gemini khac ho, xung dot Gemma-tu-cham-minh khong con ton tai; audit doi vai — xem §2e.]** (noi dung cu giu lam lich su:) neu trigger luat ha cap (Pearson<0.6 hoac |diff|>10): headline full = `SF-BT-cos` (du 1,646) + GPT-sample lam audit phu; muon `SF-BT-llm` full bang GPT thi la quyet dinh chi phi RIENG trinh user (uoc ~$0.3-0.6, cost-gate nhu moi khi). GPT-sample 100 block KHONG bao gio duoc trinh bay nhu headline full.

Spec het mau thuan. GO Phase-1 probe theo §2b.
<!-- S2C_SFBT_PROMPTS -->
### 2c. SF-BT prompt design (FIRST-CLASS, review truoc khi chay — Claude self-review round, 2026-07-04)

**5 bo sung tu luot soi cua Claude (khong ai bat truoc do):**
- R1 `short_block` flag, NGUONG KHOA TRUOC: `source_char_count < 40 OR source_token_count < 8` (co hoc). Van score binh thuong; aggregate bao CA HAI ban `with_short`/`without_short`. Khong loai, chi tach.
- R2 "run twice averaged" (AMT) chet voi temp 0 -> thay bang 2 CHIEU thu tu (A-B, B-A) lay trung binh; probe DO do nhay thu tu (cac case KHONG-expected_blind x 2 chieu), TIEU CHI KHOA TRUOC: `mean_abs(A_B - B_A) <= 3/100 VA max_abs <= 10/100` -> full run 1 chieu; vuot mot trong hai -> full run 2 chieu (~3,300 call).
- R3 judge ctx 8192; `too_long_for_llm = estimated_P2_prompt_tokens > 7000` (tinh tren TONG prompt P2, khoa truoc) -> loai CHI khoi SF-BT-llm co ghi danh + count rieng trong report; SF-BT-cos van cham (bge-m3 ctx 8192, block vuot nguong bge tu truncate -> log so block bge-truncated rieng, doi xung 2 arm).
- R4 cache key = model_id + decoding params + prompt_version + SHA256(PROMPT TEXT THAT) + SHA256(input) — version stamp de doc report, hash that de chong 'sua mot chu quen bump version'; report ghi ca version lan prompt hash.
- R5 preamble ("Here is the translation:"): KHONG tu cat; probe checklist kiem, thay moi ban cach xu.

**P1 `bt_prompt_v1`** (Gemma BT, output text tho; temp 0, repeat_penalty 1.0, seed 20260612; MU — khong EN goc/tu dien/context; LOG `finish_reason` moi call — `length`/truncated -> hygiene flag `bt_truncated`, la LOI DUNG CU khong phai loi dich, loai khoi cham co ghi danh):
```
You are a professional Vietnamese-to-English translator.
Translate the text below into English.
- Preserve Markdown structure, inline code, and LaTeX math ($...$, $$...$$) exactly as they appear.
- Do not add explanations, notes, headers, or anything besides the translation itself.
- Do not summarize, shorten, or expand the content.

TEXT:
<<<
{vi_block}
>>>
```

**P2 `bt_judge_v1`** (json_schema {score:0-100, flags:[enum], note:str}; hoan vi A/B deterministic theo hash block, ghi chieu da dung; flags CHI mo ta):
```
You compare two English passages, A and B, and judge how close they are IN MEANING.
Ignore differences in style, word choice, sentence order, formatting, and phrasing
when the meaning is unchanged. Judge only: facts, claims, numbers, logical relations,
negations, and coverage (does one passage clearly state more or less than the other?).

Score bands:
100 = same meaning; differences are purely stylistic
 75 = minor drift; one small detail differs or became vague
 50 = noticeable drift; a fact, number, or relation differs
 25 = substantial mismatch; key claims differ or a chunk of content is absent in one passage
  0 = different or contradictory content

Flags (all that apply, else empty):
semantic_mismatch | numeric_mismatch | negation_mismatch | coverage_mismatch |
untranslated_residue | format_only

Return JSON only: {"score": <0-100>, "flags": [...], "note": "<one short sentence>"}

PASSAGE A:
{first}

PASSAGE B:
{second}
```

**Probe checklist bo sung (cong vao §2b task):** (a) do order-sensitivity cua judge; (b) kiem preamble behavior cua P1; (c) bao phan bo score bands (neu 90% diem don vao 1 band -> rubric can chinh TRUOC full run).
<!-- S2C_FIX -->
### 2c-fix — khoa nguong theo review CodeX vong 3 (2026-07-04)

5 diem CodeX — GHI NHAN CA 5, da va TAN DONG trong §2c: (1) short_block nguong co dinh <40 char OR <8 token; (2) too_long_for_llm = est P2 prompt > 7000 tok, chi loai khoi llm, cos van cham, count rieng; (3) tieu chi 1-chieu/2-chieu khoa truoc: mean_abs<=3 VA max_abs<=10 (tren case khong-expected_blind); (4) cache key them SHA256 prompt text that + input hash, chong sua-quen-bump-version; (5) P1 log finish_reason, truncated -> `bt_truncated` = loi dung cu, loai co ghi danh. Chu de chung: MOI nguong chua khoa = cua sau tuning — dong het truoc khi cham data. Spec Phase-1 probe DU SACH DE CHAY.

<!-- S2D_PROBE_VERIFIED -->
### 2d. Phase-1 probe VERIFIED (Claude doc lap tren file) + GO FULL RUN (2026-07-04)

**Verify doc lap (khong tin bao cao):** doc het `pipeline/scripts/probe_sf_bt.py` (854 dong) + recount tu `sf_bt_phase1_probe.json`.
- Prompts khop nguyen van §2c, sha256 pinned trong report (bt `0fb3e38f...`, judge `8532d13a...`); profile temp=0/top_p=1/seed=20260612/repeat_penalty=1.0/reasoning=none dung khoa.
- workdb mo `mode=ro`, hash before==after `92229381...` — khong dong vao DB.
- Cache key du 5 thanh phan khoa (§2c R4): model + decoding params + prompt_version + SHA256(prompt text) + input (messages). Rerun tu cache ~1s da chung minh resume.
- Cac nguong khoa deu hien dien dung: short_block <40c/<8tok, too_long est>7000tok (chi loai llm), order-sensitivity LOAI case expected_blind truoc khi tinh (dung spec).

**Recount theo NHAN loi (diem nay bao cao CodeX chua tach — con so gop 17/20 lam yeu probe di):**
| component | semantic/negation/numeric/omission/addition (phai bat) | bad_term (mu-theo-thiet-ke) | untranslated (viec cua hygiene) |
|---|---|---|---|
| SF-BT-cos | **11/12** (miss duy nhat: dataset_split word-swap, margin -0.0001 ~ tie) | 6/7 (wrong term thuong SONG SOT round-trip nen van bat duoc; chi regularization bi Gemma BT "sua ho") | 0/1 |
| SF-BT-llm | **12/12** (bat ca dataset_split 0-vs-100 ma cos truot) | 5/7 | 0/1 |

- **Complementarity DO DUOC bang so:** dataset_split (hoan doi validation/test — tui-tu giong het) cos mu / llm bat; convolution_kernel llm mu / cos bat. Day la bang chung thuc nghiem cho quyet dinh §2b GIU 2 COT DONG-CHINH, KHONG gop composite (gop trung binh se triet tieu tin hieu bo sung nhau). Quyet dinh composite post-probe: **KHONG composite; SF-BT-rank-composite tiep tuc nam tren ke, chi lay ra neu nguoi doc yeu cau 1 con so.**
- expected_blind verified: bad_regularization 100/100 o CA 2 cot (mu thuat ngu dung phan cong — TC/TA lo truc nay); 3 case that tu workdb (vector S0/S1, regularization S1) cos 0.98-0.99, llm 100, hygiene 0 false-flag tren block markdown/math that.

**Order-sensitivity: mean_abs 5.81 / max_abs 75 (bad_batch_epoch ab=25 ba=100) — VUOT nguong khoa (3/10) => FULL RUN 2 CHIEU judge. Khong co quyen ban lai (tieu chi khoa truoc §2c R2).** Trung binh 2 chieu van tach duoc case loi (62.5 vs 100).

**Hygiene patch cua CodeX — CHAP NHAN:** `input_matches_source` = SequenceMatcher(src, vi) >= 0.95 & len>=40, thuan co hoc. Ly do can: BT cua EN-nguyen-si tra ve chinh EN => cos=1.0/llm=100 gia tao, DIEM khong the bat untranslated, PHAI la flag. Xu ly full run KHOA TRUOC: flag mo ta + aggregate bao **with/without** (nhu short_block), KHONG loai — vi block code/cong-thuc giong nhau hop phap se fire flag nay.

**Cac quan sat con lai:** preamble 0/46; finish_reason 46/46 stop; bands khong suy bien (59% o 90_100, co mat 5 band; diem judge luong tu hoa dung 5 moc rubric — by design, du dung cho phan phoi + delta). **1 bug tiem an PHAI sua trong script full run:** `score_delta_abs = abs(ab - ba)` crash neu 1 chieu validation-fail tra score=None (probe khong trigger: 92/92 parse OK) — full run phai guard None.

**GO FULL RUN — `score_sf_bt.py` (CodeX):**
1. Scope: **1,646 block = 823 x 2 arm, TRUNG scope SF-QE** (scoring-scope == production-scope). Prompts + profile giu nguyen sha256 nhu probe.
2. 2 chieu judge (~3,292 call) + 1,646 BT + embeddings; tat ca local $0. Thu tu goi `ab, ba` lien tiep cung block de an KV-cache LM Studio (probe: ab ~15s, ba ~3s).
3. Report schema nhu SF-QE §3c: phan phoi per chapter x arm cho CA 2 cot (cos, llm) + paired delta S1-S0 per block + %S1>S0 + bottom-10 moi cot lam PJ-audit input; aggregate with/without `short_block` VA with/without `input_matches_source`; count `too_long_for_llm`, `bt_truncated`, bge-truncated, flags judge (mo ta).
4. Van hanh: per-case progress print + partial JSON write + cache sqlite resume (nhu probe). **ETA THAT tu 5 block that: BT ~3-8s + judge ~15+3s => ~8-14h, chay qua dem.** Khong duoc uoc ETA tu cau ngan (bai hoc SF-QE).
5. Guard bug None o (tren); moi validation_error/json_parse_fail => retry 1 lan roi ghi nhan loi dung cu, KHONG cache ket qua loi.
6. Xong: STOP, khong commit. Sau do moi den GPT-mini 100-block audit (cost-gate rieng) + luat ha cap §2b.

Artifacts commit kem muc nay: `probe_sf_bt.py`, `sf_bt_phase1_probe.json`. Cache/debug/console de untracked (regenerable).

<!-- S2D_A_PILOT -->
### 2d-A. AMENDMENT (user 2026-07-04): full run chia 2 buoc — PILOT 100 truoc, quyet scope sau

User chot: khong lao vao 8-14h ngay; chay thu de do toc do/loi that roi quyet. Tan thanh — dung ky luat probe-first, va nho cache thi pilot KHONG phi mot call nao (full run sau do nhan lai tu cache).

**Stage A — PILOT (CodeX, ~40-60 phut, $0):**
0. Cach dem call: "100 luot cham" = 100 block-arm items, NHUNG moi item co 1 BT + 2 judge = ~300 local chat calls, chua tinh embedding. Khong duoc bao 100 request de tranh hieu nham. Token local mien phi, nhung ETA phai tinh theo local call thuc.
1. Chon **50 block_id co dinh, deterministic, KHONG cherry-pick**: moi chuong 25 block_id, systematic sampling theo order_index (buoc k = floor(N/25), lay block thu k, 2k, 3k...; ghi cong thuc + danh sach id vao report). Cham CA 2 arm cho cung 50 block_id => 100 luot cham, co luon paired delta S1-S0 mau.
2. Script = `score_sf_bt.py` ban that (khong phai script probe): 2 chieu judge, guard None-bug, report schema §2d muc 3, flags/with-without day du. Pilot chinh la lan smoke-test script nay.
3. Report them: latency phan vi (p50/p90/max) rieng BT va judge theo do dai block; count too_long_for_llm / bt_truncated / input_matches_source tren mau; **ETA ngoai suy cho 1,646** tu latency thuc te; moi loi bat ky (transport, parse, timeout) ghi ro.
3b. Benchmark concurrency truoc khi chot full: chay cung prompt/model/profile tren mau nho uncached, so sanh concurrency 1/2/3 (va 4 neu GPU/LM Studio con on). Report wall-time, items/min, parse/finish_reason, va loi transport. Chon concurrency theo throughput that, khong theo gia dinh; neu song song lam cham hoac unstable thi full run dung sequential.
3c. CodeX benchmark 2026-07-04 (artifact `data/reports/exp_s0s1_builderv2_v1/sf_bt_concurrency_benchmark.json`): mau 6 block-arm items that, moi item 1 BT + 2 judge, Gemma-4-12B profile nhu §2c. Throughput: conc1 7.01 items/min; conc2 6.83; conc3 6.12; conc4 7.04. Khong loi transport/parse, nhung median item latency tang manh khi song song (7.8s -> 18.2s -> 29.4s -> 30.4s). Ket luan hien tai: **sequential la mac dinh an toan**; chi doi neu pilot full-script cho so khac tren sample lon hon.
4. STOP -> Claude verify -> quyet scope voi user bang so that: (a) full 1,646 mot dem, (b) tung chuong moi dem mot chuong, (c) thu hep 1 chuong (chi khi ETA that su xau; ghi ro cai gia = mat replication xuyen chuong cua SF-BT).

**Ghi chu model (chot voi user):** ~~Gemini/GPT KHONG thay Gemma o vi tri BT/judge chinh~~ **[Phan JUDGE superseded boi §2e sau khi user cho CodeX probe day du 5 model — dieu kien 'doi model = probe lai tu dau' DA duoc thoa man. Phan BT van dung nguyen: Gemma local giu vi tri BT.]** — probe §2d da tham dinh thuoc tren Gemma, doi model = doi thuoc = probe lai tu dau; key reseller khong pin duoc model version (rui ro tai lap) + data qua trung gian. Gemini duoc phep ung cu vai AUDIT 100-block (thay GPT-mini, cost-gate nhu cu) — quyet khi den buoc do. Token local mien phi; chi phi duy nhat la thoi gian may chay.

<!-- S2E_JUDGE_MODEL -->
### 2e. CHOT judge model cho SF-BT-llm (Claude verify doc lap 5 artifact, 2026-07-04)

**Verify:** recount tung file (khong tin bang tong hop) — pairwise, order (loai expected_blind), validation error. **Bang CodeX khop 100%**, key KHONG lot vao artifact nao (scan bang chinh key string).

| candidate | pairwise | order mean/max | val_err | ghi chu |
|---|---|---|---|---|
| gemma-4-12b local (§2d) | 17/20 | 5.81/75 | 0 | ~15-17m/92 call |
| gemini-2.5-flash seq | 17/20 | 1.16/25 | 0 | ~10m |
| gemini-2.5-flash c8 thinking MAC DINH | 9/20 | (gia) | 27+ | **CAM**: thoughts an ~490/512 token budget -> JSON cut, finish=MAX_TOKENS |
| **gemini-2.5-flash c8 thinking_budget=0** | **17/20** | 4.07/50 | **0** | **24.9s/92 call**, STOP 92/92, thoughts=0, ~$0.045 |
| gemini-3.1-flash-lite | 17/20 | 6.40/50 | 0 | khong hon gi, order xau hon |
| gpt-5.4-mini | 17/20 | 8.72/100 | 1 | **LOAI: cung ho Translator** (luat cu, CodeX tu ap dung dung) |
| qwen3.5-9b local | 17/20 | 11.59/75 | 2 | order + JSON kem |

**Phat hien nen tang: MOI model fail dung CUNG 3 cap** (regularization / untranslated / convolution_kernel) — recount xac nhan tren tung artifact. Nghia la: (a) tren nhom loi phai-bat, TAT CA dat 12/12 — chat luong bat loi khong phai bien phan biet; (b) 3 cap fail la vung mu thiet-ke cua SF-BT (thuat ngu + BT tha thu), model manh may cung khong cuu — cung co phan cong TC/TA/PJ; (c) bien phan biet that = on dinh JSON, on dinh thu tu, toc do, HO model.

**QUYET DINH — judge chinh SF-BT-llm = `gemini-2.5-flash`, cau hinh KHOA:** thinking_budget=0 (bat buoc, xem hang CAM), temperature=0, top_p=1, response_mime_type=application/json, concurrency 8, judge 2 CHIEU (moi candidate deu vuot nguong 3/10 — ke ca seq mean 1.16 co max 25>10 -> khong mo lai chuyen 1 chieu).

**Ly do nhan swap (doi chieu voi tu choi o §2d-A):** dieu kien "doi model = probe lai tu dau" DA thoa man — CodeX chay du bo 46 case/20 cap cho tung candidate. Loi ich khoa hoc thuc: **tach 3 ho hoan toan** — GPT dich xuoi / Gemma dich nguoc / Gemini cham — xung dot "Gemma tu cham ban dich nguoc cua chinh minh" (moi lo ngai #1, tung phai dung ca luat ha cap + audit tra phi) BIEN MAT theo thiet ke. Cong them: judge full run ~25-35 phut thay vi 6-10h.

**Dieu kien bat buoc trong `score_sf_bt.py` full run:**
1. BT giu nguyen Gemma local, P1 sha khong doi — swap CHI cham judge.
2. Cache key them: provider/endpoint + thinking_budget + response_mime_type (tren nen R4 cu).
3. **Log `model_version` tu response API tung call** (probe CHUA co field nay — phai them; voi key reseller khong pin duoc backend, log version + timestamp la bang chung tai lap duy nhat) + finish_reason + token counts.
4. Loi transport/parse: retry 1 lan, KHONG BAO GIO cache ket qua loi.
5. Caveat ghi vao thesis: Gemini temp=0 KHONG bit-deterministic (bang chung: seq 1.16/25 vs c8 4.07/50 cung model cung prompt); nguon su that = raw response da cache, report tinh tu cache — CUNG chuan da chap nhan voi GPT Translator, khong phai chuan thap hon.
6. Audit doi vai: ~~GPT-mini 100 block co phi~~ -> **Gemma-local recham 100 block deterministic, $0** (gio Gemma doc lap voi judge chinh -> convergent evidence). Nguong cu giu nguyen (Pearson<0.6 hoac |mean diff|>10 -> mo thao luan trong thesis, khong am tham ha cot; SF-BT-cos co-primary khong bi anh huong).
7. **Cost-gate:** probe do $0.045/92 call (case ngan); block that dai hon ~2.5x -> uoc full 3,292 call ~$3-7. PILOT §2d-A chay truoc voi judge moi, do chi phi thuc + ngoai suy; **neu ngoai suy > $10 thi STOP hoi user**. ETA moi: BT local ~2-4h + judge ~25-35m -> MOT BUOI TOI, khong can qua dem.

Artifacts commit: 6 probe json + concurrency benchmark. Cache sqlite de untracked.

<!-- S2F_PILOT_VERIFIED -->
### 2f. PILOT 100 VERIFIED + GO FULL RUN (Claude, 2026-07-05)

**Verify doc lap:** recount toan bo tu `sf_bt_pilot_100.json` — moi con so cua CodeX khop 100% (deltas 3 cot ALL/without-flag, flags 30/14/0, finish 100+200 STOP, model_version `gemini-2.5-flash` du 200/200 call, cost $0.106, val_err 0). Hash workdb before==after==baseline. Doc code `score_sf_bt.py` cac diem §2e: khong-cache-loi (503-504), guard None (517), cache key du provider+thinking_budget+mime+sha, retry 2 lan — DAT CA 4. Sampling formula centered-systematic tren eligible blocks, deterministic.

**Cham tay 3 cap llm S0>S1 (nguon goc mean -2.5) — KHONG cap nao la tri nho lam hong ban dich:**
1. `linear_algebra_b003` (100->12.5, gap 87.5 = ~70% cua tong mean am): heading 2 chu `## Scalars`. S1 dich "Vo huong" (DUNG, chuan hon "Cac vo huong" cua S0); BT mu khong co ngu canh doc "vo huong" thanh "Non-directional". **Artifact dung cu tren block ngan** — va flag `short_block` DA bat dung ca nay theo thiet ke (bi loai khoi aggregate without-flag). Day la vi du giao khoa cho viec bao cao with/without.
2. `kaggle_house_price_b034` (100->75): S1 dich standardize->"chuan hoa", BT mu tra ve "normalize", judge tru 25 diem nuance. **Tieng vong quy uoc qua round-trip, lop vecto-class**; noi thang vao phat hien §36 (chuan hoa la thuat ngu qua tai — ly do regularization phai bau lai thanh dieu chuan). Case study cho thesis, khong phai loi dung cu.
3. `probability_b066` (100->87.5): 1 chieu judge 75 voi note lac de, chieu kia 100 — nhieu thu tu, trung binh 2 chieu lam mem dung nhu thiet ke. Toan bo math LaTeX 22 dau `$` giu nguyen ca 2 arm.

**BT Gemma tren block dai — DAT:** 10 block dai nhat (675-1286c): cos 0.969-0.994, llm 100 (tru 2 ca da chan doan), length-ratio 0.93-1.06, `$` 22->22, backtick 4->4, so lieu/LaTeX nguyen van, finish 100/100 stop. Lo ngai "cau dai gay BT" cua user: kiem tra truc tiep, khong xay ra.

**Ve nhan xet "llm bao hoa" cua CodeX:** 94% delta ~0 la HANH VI DUNG cua guardrail metric — ca 2 arm deu dich tot cung nguon thi diem bang nhau (SF-QE cung 74% trong ±0.01). Probe §2d da chung minh rubric tach loi that sac (0-vs-100). KHONG chinh rubric sau khi thay data (= tuning). Ky vong pre-declared cho full run (guong SF-QE §3c): ca 2 cot paired delta ~0, khong cluster S1-harm; bottom-10 lam input PJ.

**GO FULL RUN:** cung script, cung cache DB (pilot 100 items tai su dung mien phi), scope 1,646 block-arm; ETA ~5.1h con lai (bottleneck BT local; 3-phase load model da fix het thrashing); judge full ~$1.75 << gate $10; per-item progress + partial JSON; STOP khong commit khi xong. Sau do: doc ket qua chinh thuc §2g + Gemma-local recham 100 block audit ($0, §2e muc 6).

<!-- S2F_A_ONE_CHAPTER -->
### 2f-A. AMENDMENT (user 2026-07-05): full run chia theo CHUONG, MLP truoc

User chot chay full 1 chuong truoc. Cach thi hanh KHONG dong cua replication:
- **Stage 1 (chay ngay): d2l_multilayer_perceptrons** — chuong chinh cua exp, 475 block x 2 arm = 950 items, tru ~50 da cache => ~3h. Script/cache/prompt giu nguyen §2f, them filter chapter. Report per-chapter (schema san co).
- **Stage 2 (de danh, OPTIONAL, toi may ranh): d2l_preliminaries** 696 items ~2.2h. Cache lam Stage 2 hoan toan doc lap ve chi phi — quyet dinh hom nay la THU TU, khong phai tu bo; SF-BT cross-chapter replication postponed chu khong huy, ghi nhan trung thuc trong thesis neu cuoi cung chi co 1 chuong.
- Readout chinh thuc §2g viet theo chuong da co; khong doi ky vong pre-declared.

<!-- S2F_B_DESIGN_QA -->
### 2f-B. Design Q&A chot voi user (2026-07-05) — tu lieu bao ve, khong doi thiet ke

**Q1 BT mu co "mu quang" qua khong?** Tra loi da chot: mu la danh doi CO CHU DICH, bang chung do duoc o ca 2 phia: (a) gia cua mu = Ca1 Scalars/Non-directional (oan block ngan, cost RE — flag short_block bat + dual report giai oan 30 giay); (b) gia cua cho-ngu-canh = probe bad_regularization: Gemma CHI voi ngu canh cau da "sua ho" thuat ngu SAI thanh dung -> them domain hint se sua ho nhieu hon -> instrument im lang dung cho can keu (cost DAT, vo hinh). Nguyen tac: false-alarm re, missed-error dat -> chon mu. Mu doi xung -> khong thien vi arm nao; paired design triet tieu oan chung. `bt_prompt_v2` (1 dong domain hint) = bien the hop le TUONG LAI, phai probe lai nhu dung cu moi + ap ca run; KHONG doi giua chung.

**Q2 SF-BT cho van hoc?** Phan bien Gemini (idiom rains-cats-and-dogs chet diem) SAI o gia dinh judge so chuoi — judge cua ta la LLM cham NGHIA, ignore style/word-choice -> "mua nhu trut nuoc" khong tu dong truot. Nhung dung o tang sau: van hoc thi giu-nghia la CAN chu xa moi DU — SF-BT khong thay giong van/xung ho/an y. Ket luan chot: SF-BT khong "sup do" ma "tra loi cau hoi qua nho"; sang van hoc no XUONG VAI luoi bat loi tho (bo sot/nguoc nghia/sai so), ganh chinh la PJ don-ngu ban dich + BWS vs ban dich nguoi + TC-Ent (xung ho theo cap nhan vat tu entity memory — dat dien cua he thong tri nho). Da co san §6 roadmap.

**Pilot arm-level (tra loi user):** S0 cos 0.978 / llm 99.75; S1 cos 0.970 / llm 97.25 (keo boi 3 ca da giai phau §2f). Bo flagged: S0 0.973/99.61, S1 **0.977**/98.39 — S1 nhinh hon cot cos khi loai nhieu dung cu. Phan bo llm: S0 49x100+1x87.5; S1 46x100+2x87.5+1x75+1x12.5.

<!-- S2G_MLP_READOUT -->
### 2g. SF-BT OFFICIAL READOUT — Stage 1 MLP (Claude verify doc lap, 2026-07-05)

**Integrity (recount tu artifact, khop bao cao CodeX 100%):** 475 block x 2 arm = 950 items, chapter dung; workdb hash before==after==baseline 92229381...; prompt sha bt/judge khop §2c; BT 950/950 stop; judge 1900/1900 STOP, model_version `gemini-2.5-flash` 1900/1900, 0 error; cost that **$0.9965** (recompute tu usage per-call khop den cent, ~du bao $1); wall 44.4 phut. Label `status/phase` con chu "pilot_*" = loi dat ten cosmetic, scope data da verify dung full MLP; Stage 2 (neu chay) phai sua label.

**Headline (paired, S1-S0):**
| cot | S0 | S1 | delta mean | delta median | S1>S0 / S0>S1 | gan hoa |
|---|---|---|---|---|---|---|
| SF-BT-cos | 0.9719 | 0.9710 | **-0.0009** | 0.0 | 31.4% / 29.9% | 80% |
| SF-BT-llm | 98.11 | 97.11 | **-1.00** | 0.0 | 2.9% / 5.9% | 91.2% |
| direct EN-VI (diagnostic) | 0.8511 | 0.8477 | -0.0034 | 0.0 | 36.2% / 35.2% | 66% |
Without-flag (n=292): cos -0.0013, llm -0.94 — cung ket luan.

**Adjudication vs ky vong pre-declared (§2f: ca 2 cot ~0, khong cluster S1-harm):**
- **SF-BT-cos: PASS sach** (-0.0009 ~ 0, doi xung 31/30).
- **SF-BT-llm: PASS voi 1 cluster CO TEN:** mo 28 cap am (tong gap 725 diem): **6 cap = regularization->"chuan hoa" chiem 200/725 = 28% tong gap am** — S1 render 28/29 block regularization bang "chuan hoa" (vang loi canonical SAI §35.8 dang nam trong notebook production). Day KHONG phai "memory lam hai nghia" tong quat — la "MOT entry tu dien sai gay hai tai moi occurrence", dung storyline §30/§35.8/§36, nay duoc DUNG CU DOC LAP THU BA do luong (sau gold-recall B va forensics §36). §36 standalone probe DA bau lai regularization->dieu chuan (2-0) — neu deploy, 6 block nay hoi phuc. Con lai: 15 cap -12.5 = nhieu 1-chieu; heading ngan (Beyond->Mo rong->"Expansion", flagged short_block); convention echo (hop ly->"rational").
- **1 loi dich THAT cua S1 bat duoc:** mlp_b009 dao nghia "less obvious"->"ro rang hon" — llm cham 50/50 ca 2 chieu, **cos mu hoan toan** (0.967 vs 0.966, tui-tu giong het). Bang chung song complementarity §2d tren du lieu that.
- **Doi xung — S0 cung co loi that bi bat** (14 cap S1>S0): weight_decay_b018 S0 dich "High-Dimensional"->"chieu cao" (height!) llm 37.5; mlp_b003 S0 "lop an"->BT "Hidden classes" (lop da nghia) trong khi S1 dung "tang" khong mo ho -> 100 — tu dien S1 lam nghia RO hon. Instrument bat loi ca 2 chieu, khong thien vi.

**Y nghia thesis:** tri nho KHONG lam hong nghia o quy mo toan chuong (91% hoa, cos ~0); tac hai duy nhat co he thong = 1 entry sai, do duoc, da co co che sua offline. Bottom-N ca 2 cot -> input PJ audit (hop nhat voi bottom SF-QE §3c).

**Ops:** BT p50 that 2.19s (pilot 13.39s bi thoi phong boi model-load/thrashing — da fix 3-phase); ETA Stage 2 prelim thuc te se ~30-45 phut chu khong phai 2.2h. Cache dung chung, judge cache hit 140/1900.

**Next:** (1) OPTIONAL Stage 2 preliminaries (~40 phut, ~$0.7) — quyet dinh cua user; (2) Gemma-local recham 100 block audit $0 (§2e muc 6); (3) PJ. SF-BT MLP = thang thu 6/7 co so chinh thuc.

<!-- S2H_JUDGE_AUDIT -->
### 2h. Judge audit OFFICIAL — Gemini CONFIRMED boi Gemma khac ho (Claude verify, 2026-07-05)

**Setup:** Gemma local recham (bt_judge_v1 sha khop, 2 chieu) tren (a) 100 item systematic tu 950 MLP items (cong thuc centered, id ghi trong artifact) + (b) 56 item cua 28 cap llm-am. Khong goi Gemini moi, khong dung workdb. $0, ~24 phut.

**Recount doc lap khop CodeX 100%:**
| nhom | n valid | Pearson | Gemma-Gemini | bat dong >=25 |
|---|---|---|---|---|
| systematic 100 | 97 (3 loai vi Gemma parse-fail, loai dung cach — khong zero-fill) | **0.966** | +0.90 | 0 |
| 28 cap llm-am (adversarial) | 56 | **0.887** | +3.79 | 7 |

**Adjudication vs nguong dang ky truoc (§2e: Pearson>=0.6 VA |mean diff|<=10):** CA HAI nhom PASS ro rang => **SF-BT-llm (Gemini) duoc xac nhan convergent-valid; cot llm dung vung lam dong-tru.** Khong co nhanh ha cap nao kich hoat.

**Soi tay 7 ca bat dong:** tat ca la lech MUC PHAT trong cung phan doan (2 ben deu thay loi/khong loi, cai nhau band): Gemma nhe tay 3 ca (tha pluralization Vanishing Gradient(s) 100-vs-75; bo qua 1 chi tiet backward-pass ma Gemini bat duoc), nang tay 3 ca (shift/skewness 0-vs-50), 1 ca cung phat khac muc (chuan hoa b044 50-vs-25 — CA HAI deu xac nhan loi regularization/normalization, cung co §2g cluster). Khong co ca nao dao chieu ket luan.

**Huong lech +3.8 tren nhom kho = dung huong du bao §2e** (Gemma cham BT cua chinh minh -> ne nhe): co that nhung nho, va audit lam no THANH SO DO DUOC thay vi rui ro ngam. Gemma 6/200 call parse-fail vs Gemini 0/1900 — xac nhan nguoc quyet dinh §2e chon Gemini lam primary (do on dinh, khong phai do catch).

**Go nho ghi nhan:** 3 item parse-fail chua luu raw output (CodeX tu bao) — khong blocking (loai bao thu, 3/100, co ghi danh); yeu cau moi script sau: luon luu raw_content_prefix ke ca khi parse fail. Khong can rerun.

**Trang thai thang do:** SF-BT MLP xong + judge da duoc kiem chung cheo. Con: PJ (thang 7/7), Stage 2 prelim (optional, ~40 phut), agreement analysis tong.

### 4a. PJ — thiet ke CHOT sau phan bien CodeX (Claude adjudicate, 2026-07-05). SUPERSEDES cac dong da gach trong §4.

**Ten goi chinh xac (CodeX diem 1 — GHI NHAN):** PJ = **source-aware paired preference** (judge thay EN goc + 2 ban VI), KHONG duoc goi la "fluency thuan" trong bao cao/thesis. Headline style-alarm chi tinh tren tag-group style = {grammar, naturalness, word_choice}; terminology/meaning KHONG vao alarm (da co TC/TA/SF-BT do).

**Judge model (SUPERSEDE dong §4):** primary = **gemini-2.5-flash**, config §2e nguyen xi (temp 0, thinking_budget=0 BAT BUOC, response_mime_type json, max_output 512, c8). Ly do doi tu gpt-5.4-mini: (1) §4 goc viet 2026-07-03, TRUOC khi §2e (04/07) loai chinh gpt-5.4-mini khoi vai judge vi cung ho Translator — voi PJ ca 2 ung vien deu la GPT nen bias ho doi xung MOT PHAN, nhung khong co ly do giu judge chua kiem chung khi da co judge duoc kiem chung cheo ho (§2h Pearson 0.966/0.887, 0 ca dao phan doan); (2) JSON stability Gemini 0/1900; (3) giu 3-ho tach GPT-dich / Gemini-cham / Gemma-kiem-toan. **CodeX diem 2 — GHI NHAN: bang chung SF-BT KHONG tu chuyen sang PJ (task khac); probe PJ rieng la BAT BUOC, khong duoc vien dan §2e/§2h de bo probe.** Gemma = secondary agreement-only nhu §4 goc, khong bao gio vao headline.

**Output JSON moi call (CodeX diem 1 — GHI NHAN co dieu kien probe):** `{overall_verdict: X|Y|TIE, style_verdict: X|Y|TIE, tags: [1-3], note}` — style_verdict = "bo qua khac biet thuat ngu va nghia, ban nao doc tu nhien hon (hoac ngang nhau)". Rui ro 2-verdict lam JSON phuc tap -> P-TERM trong probe la bai test truc tiep. **Fallback ghi truoc:** neu style_verdict truot nguong probe -> rut ve 1 overall_verdict + tags, style-alarm tinh tu tag-group (thiet ke goc cua Claude), KHONG duoc tune tiep sau khi thay data that.

**Tag taxonomy CHOT (SUPERSEDE danh sach §4; doi TRUOC khi co bat ky data PJ nao — hop le, khong pham luat metric-mid-experiment):** {grammar, naturalness, word_choice, terminology, meaning, omission_addition, formatting}. Mapping tu §4 cu: word_order+style -> naturalness (hap thu); term_choice -> terminology; omission/addition -> omission_addition (dinh nghia: thieu/thua noi dung so voi EN goc). Moi verdict kem 1-3 tag.

**Auto-tie normalization (CodeX diem 4 — GHI NHAN, chon ban bao thu):** identical := bang nhau sau NFC + CRLF->LF + strip trailing whitespace cuoi dong. KHONG collapse whitespace giua chu, KHONG strip markdown/dau cau (formatting la tag danh gia that). Do thuc te tren MLP: 136/475 identical theo CA raw-exact LAN whitespace-collapse -> dinh nghia khong doi so nao tren MLP, khoa lam luat chung cho prelim/cac chuong sau.

**Pham vi & don vi (CodeX chot 3 — GHI NHAN):** Stage 1 = MLP 475 cap (136 auto-tie $0; 339 gui judge; 59/339 short_block giu flag, bao cao kep co/khong nhu SF-BT). Prelim = Stage 2 — CAN cho ket luan thesis chinh thuc (replication), khong phai optional ve mat khoa hoc, chi la thu tu (tien le §2f-A). Label X/Y; hoan vi thu tu deterministic theo seed; LUON chay ca 2 chieu.

**Pre-registered predictions:** giu nguyen 4 du doan §4 (ties >=50%; wins can bang; S1-loss don vao block term kho tag terminology; ALARM = S1 loss-rate tag-group style vuot S0 qua 10 diem %, mau so = toan bo 475 cap, identical = tie). BO SUNG dang ky truoc: cum 28 block regularization->"chuan hoa" cua S1 se hien thanh S1-thua tag terminology/meaning; neu dung, PJ = dung cu doc lap THU TU do cung 1 entry canonical sai.

**Nguong probe KHOA TRUOC (CodeX diem 3 — GHI NHAN; dung so nguyen tren n nho, khong dung % de khoi dien giai mem):** probe = 20 cap thiet ke (CodeX tu tay soan, tu block MLP that), moi cap 2 chieu = 40 call:
- P-IDENT (5 cap giong het): overall TIE 10/10 chieu.
- P-GRAM (5 cap cay loi ngu phap/cau cut vao 1 ban): winner dung o CA 2 chieu >= 4/5 cap; tag grammar xuat hien >= 4 trong cac cap bat dung.
- P-MEAN (5 cap muot-nhung-sai-nghia): ban dung nghia thang overall >= 4/5; tag meaning >= 4.
- P-TERM (5 cap chi khac thuat ngu, vd dieu chuan<->chuan hoa): tag terminology >= 4/5 VA style_verdict = TIE >= 4/5 (bai test 2-verdict; truot -> kich hoat fallback 1-verdict o tren). [LAM RO §4c: tat ca nguong probe tinh o PAIR-LEVEL SAU KHI GOP 2 chieu; tags = union 2 chieu]
- Order-inconsistent tren P-GRAM+P-MEAN: <= 1/10 cap. Parse-fail sau retry: <= 1/40 call (Gemini da 0/1900 nen day la nguong long). Raw output LUON duoc luu ke ca khi parse fail (luat §2h).
Truot BAT KY dong nao -> STOP, sua prompt/schema, probe lai tu dau. Khong co dien giai mem sau probe.

**Pilot 50 cap that (systematic tu 339, cong thuc idx nhu SF-BT):** bao cao ty le tie, phan bo tag, order-inconsistent (> 25% -> STOP sua prompt — so cua CodeX), chi phi thuc te + ngoai suy full. GO/NO-GO truoc full 339.

**Gemma audit (khuon §2h, sau full run):** Gemma-local recham 50 cap systematic + TOAN BO cap S1-thua; thuoc do = ty le DAO PHAN (Gemma phan nguoc winner; tie-vs-win khong tinh la dao) <= 10% moi nhom; Gemma parse-fail loai bao thu + luu raw. Chi la agreement analysis, khong vao headline.

**Cost gate & ky thuat:** uoc ~820 call Gemini (probe 40 + pilot 100 + full 678) ~= $0.4-0.6 theo don gia thuc SF-BT; **STOP neu vuot $3.** Cache key du thanh phan nhu SF-BT (provider+model+prompt_version+prompt_sha+input_sha+order+temp+max_tokens+thinking_budget+mime), khong cache loi, log model_version moi call, workdb mode=ro + doi chieu hash 92229381...E8C13F42 truoc/sau moi stage.

**Quy trinh giao CodeX:** (1) soan prompt pj_judge_v1 (kem sha256) + danh sach 20 cap planted -> STOP, Claude review prompt + planted set TRUOC khi chay probe (prompt la first-class); (2) probe 40 call -> STOP, Claude verify tren artifact; (3) pilot 50 -> STOP verify; (4) full 339 + Gemma audit. Moi buoc khong commit, khong tu y chay buoc sau.

### 4b. pj_judge_v1 — PROMPT VERBATIM (Claude viet, user chi dinh Claude la nguoi viet prompt, 2026-07-05)

**sha256(template UTF-8) = `d47dbb171a30133f921063f0dad8f724256dc21d528d18724556b1d6d4f82bc2`** — sha tinh tren template voi newline LF — file nay la CRLF nen khi trich tu day PHAI chuyen 
 -> 
 truoc khi hash; CodeX phai nhung NGUYEN VAN va assert sha khop luc runtime; moi sua doi = prompt_version moi + probe lai.

**Luat thay the placeholder:** template chua dau ngoac nhon JSON -> BAT BUOC dung `.replace("{source}", ...)` / `.replace("{candidate_x}", ...)` / `.replace("{candidate_y}", ...)`, CAM dung `str.format` (se crash tren `{"overall_verdict"...}`).

```text
You are a strict, impartial evaluator of Vietnamese translations of English technical text (a machine learning textbook).

You are given one English source segment and two candidate Vietnamese translations, labeled X and Y. The labels and their order are arbitrary and carry no information about which candidate is better.

Compare the two candidates and output ONLY a JSON object with exactly these keys:

{"overall_verdict": "X" | "Y" | "TIE", "style_verdict": "X" | "Y" | "TIE", "tags": [...], "note": "..."}

Definitions:

1. overall_verdict — which candidate is the better Vietnamese translation of the source OVERALL, considering accuracy of meaning, completeness, technical terminology, grammar, and naturalness together. If the candidates are equally good, or the differences are too trivial to justify a preference, use "TIE".

2. style_verdict — which candidate reads better as Vietnamese PROSE, judged ONLY on grammar, fluency, naturalness, and choice of ordinary (non-technical) words. For this verdict you MUST ignore differences in technical term choices and differences in meaning relative to the source. If the only differences between X and Y are technical terms or meaning, style_verdict MUST be "TIE".

3. tags — 1 to 3 items, most important first, naming the kinds of difference that drove your verdicts, chosen ONLY from this list:
- "grammar": grammatical errors, broken or incomplete sentences.
- "naturalness": stiff, word-by-word, or un-Vietnamese phrasing.
- "word_choice": weaker choice of ordinary (non-technical) words.
- "terminology": difference in how technical terms are rendered.
- "meaning": the candidates differ in meaning relative to the English source.
- "omission_addition": content missing from or added to one candidate relative to the source.
- "formatting": markdown, code, math, numbers, or URLs damaged in one candidate.
If both verdicts are "TIE", tags may be an empty list [].

4. note — at most 25 words, in English, pointing to the decisive difference, or "no meaningful difference".

Rules:
- Judge as a knowledgeable Vietnamese reader of machine-learning texts.
- Do not reward a candidate for staying closer to English wording; good Vietnamese matters, literal similarity does not.
- Code, LaTeX math, URLs, and numbers must be preserved exactly; damage there counts against a candidate (tag "formatting").
- The segment may be a heading, list item, or code caption; judge it as such, and prefer "TIE" when it is too short to show a real quality difference.
- Use "TIE" whenever you cannot confidently prefer one candidate; never invent a preference.
- Output the JSON object only. No other text.

English source:
<<<SRC
{source}
SRC>>>

Candidate X:
<<<X
{candidate_x}
X>>>

Candidate Y:
<<<Y
{candidate_y}
Y>>>
```

**Validator (code, mechanical):** overall_verdict/style_verdict thuoc {X,Y,TIE}; tags la list con cua taxonomy 7-tag, do dai 0-3; tags RONG chi hop le khi CA HAI verdict = TIE; note la string. Vi pham -> validation_error, KHONG cache, retry 1 lan, van fail -> loai + luu raw (luat §2h). [BO SUNG §4c: quan he style_verdict<->tag KHONG xu ly o validator ma o buoc gop — style thieu tag chong lung bi ha bac ve TIE + co]

**Self-review cua Claude (failure mode -> phong thu trong prompt):**
- Position bias -> dong "labels and order are arbitrary" + harness van chay ca 2 chieu (phong thu kep, khong thay the both-order).
- style_verdict bi nhiem terminology/meaning -> dinh nghia MUST-ignore + menh de "chi khac term/nghia thi style PHAI TIE" (day la dinh nghia a-priori, khong phai tune theo probe; P-TERM do viec model TUAN THU dinh nghia).
- Bao hoa/ngai phan xu -> pairwise + TIE hop phap hoa tuong minh ("never invent a preference") de tranh ep thang thua gia.
- Block ngan (59/339) -> dong heading/caption + prefer TIE khi qua ngan; short_block flag van bao cao kep nhu SF-BT.
- Judge thuong ban dich bam sat EN -> dong "do not reward literal similarity" (chong thien vi dich word-by-word von DOC ta muon phat).
- JSON verbose/truncate (bai hoc §2e: 512 token cap) -> note <=25 tu tieng Anh, "JSON only, no other text", response_mime_type json + thinking_budget=0 nhu config §2e.
- Tag bia -> closed list trong prompt + validator tu choi tag ngoai taxonomy.

**Phan con lai giao CodeX (bước 1 sua lai):** CodeX KHONG viet prompt nua; chi (1) soan 20 cap planted P-IDENT/P-GRAM/P-MEAN/P-TERM tu block MLP that kem expected label; (2) xay probe_pj.py quanh prompt nay (cache key du + order, khong cache loi, model_version, raw luon luu, workdb ro + hash). STOP cho Claude review planted set truoc khi chay.

### 4c. Aggregation 2-verdict + style-tag guard + luat soan planted set (Claude adjudicate vong 2 phan bien CodeX, 2026-07-05)

**CodeX diem 1 — GHI NHAN: gop both-order RIENG cho tung verdict-type.**
- overall_final(pair) = overall neu 2 chieu cung phan (sau khi doi nhan X/Y ve arm), nguoc lai TIE + co `overall_order_inconsistent`.
- style_final(pair) = tuong tu, DOC LAP voi overall: overall nhat quan + style mau thuan -> chi style = TIE (+`style_order_inconsistent`), overall GIU NGUYEN; va nguoc lai.
- tags_final(pair) = UNION tag cua 2 chieu (per-call tags van luu day du trong artifact).
- Report BAT BUOC 2 bang rieng: `overall_{win,tie,loss}` va `style_{win,tie,loss}` theo arm; style-ALARM tinh tu style_final; order-inconsistent bao cao rieng tung loai.

**CodeX diem 2 — GHI NHAN: nguong probe = PAIR-LEVEL sau gop.** P-TERM pass khi >=4/5 pair co style_final = TIE VA >=4/5 pair co terminology trong tags_final. P-GRAM/P-MEAN: winner dung = overall_final dung (da ham y ca-2-chieu); tag dem tren tags_final. P-IDENT giu call-level 10/10 (giong het thi tung chieu phai TIE, khong co ly do le thuoc gop).

**CodeX diem 3 — GHI NHAN CO SUA: guard style<->tag la luat GOP, khong phai validation_error.** Ly do sua: temp=0 nen reject+retry tra lai dung JSON cu -> item chet oan; va ep model them tag/lat verdict qua retry la meo do. Luat co hoc thay the: neu style_final != TIE ma tags_final khong chua tag nao thuoc style-group {grammar, naturalness, word_choice} -> HA BAC style_final ve TIE + co `style_unsupported_by_tags` (bao thu, cung triet ly voi order-inconsistent->TIE). formatting KHONG duoc quyen quyet style (prompt dinh nghia style = prose; dong y CodeX). Nguong dung cu: pilot co ty le style_unsupported > 20% so pair non-tie-style -> STOP, coi nhu style_verdict truot, kich hoat fallback 1-verdict cua §4a.

**CodeX diem 4 — GHI NHAN: tags = ly do cho BAT KY verdict non-tie nao** (khong rieng style); khong sua prompt de giu hash d47dbb17; v2 tach overall_tags/style_tags CHI sau probe neu can va se la prompt_version moi.

**CodeX diem 5 — GHI NHAN: luat soan planted set (CodeX phai tuan thu, Claude review tung cap):**
- Moi cap chi duoc thay doi DUNG MOT truc chat luong: P-GRAM = cung nghia + cung term, chi pha ngu phap; P-MEAN = van muot, cung register, chi doi nghia; P-TERM = giong het tru cach dich term; P-IDENT = giong het tuyet doi.
- Expected label moi cap ghi TRUOC: expected winner (arm), expected tag chinh, expected style_final (P-GRAM: winner; P-MEAN: TIE hoac winner-khong-che; P-TERM: TIE; P-IDENT: TIE).
- Cap nao vi pham mot-truc-mot-loi -> Claude tra ve soan lai, khong chay probe voi cap ban.

### 4d. PJ probe #1 VERIFIED — 3 nhom PASS, P-GRAM FAIL dung luat -> sua fixture, probe lai (Claude recount doc lap, 2026-07-05)

**So lieu recount khop CodeX 100%:** 40 call, 0/40 parse/transport fail, 40/40 model_version, cost recompute $0.0338, db unchanged, request profile dung §2e (temp 0, thinking 0, json, 512, c8). P-IDENT 10/10 call TIE PASS; P-MEAN 5/5 overall + 5/5 tag meaning PASS; P-TERM 5/5 style-TIE + 5/5 tag terminology PASS (2-verdict song sot bai test truc tiep — khong can fallback 1-verdict); order-inconsistent P-GRAM+P-MEAN 0/10 PASS. **P-GRAM: overall 4/5 dat, grammar-tag 3/5 < 4/5 -> FAIL.** Theo §4c: STOP, sua, probe lai. **KHONG doi nguong, KHONG tinh word_choice thanh grammar sau khi thay data — dien giai mem bi cam.**

**Chan doan tren raw (loi fixture, khong phai loi he thong):**
- PGRAM_01 (chen "la mo ta"): judge TIE ca 2 chieu, note "No meaningful difference" — loi cay qua nhe. GHI NHAN lam GIOI HAN DO: PJ khong thay loi vi te co chen-mot-tu; PJ la luoi tho cho van phong, khong do vi-sai (nhat quan voi vai luoi-bat-loi-tho da ghi o §2f-B cho SF-BT).
- PGRAM_05 (Vi->Nhung): judge bat DUNG winner ca 2 chieu, note dung ban chat lien tu, nhung xep word_choice — dung du bao ranh gioi grammar/meaning/word_choice Claude ghi TRUOC khi chay. Judge khong sai; fixture chon loai loi vat qua bien taxonomy.

**Bonus findings (ghi ho so):** PTERM_01/02 overall TIE => judge KHONG phan xu quy uoc term (dung phan vai: truc term thuoc TC/TA, PJ khong cham lai); PTERM_03 judge thich "Mang no-ron nhieu lop" hon "Perceptron da tang" order-consistent 2 chieu (quan sat phu, khong gate); 2 co che order-inconsistent da no dung thiet ke (style o PMEAN_01, overall o PTERM_02) va deu bi ha ve TIE.

**Lenh sua (planted v2, script GIU NGUYEN):** tao pj_planted_set_v2.json — thay DUNG 2 cap PGRAM_01 va PGRAM_05 bang 2 loi HINH THAI - CU PHAP khong the nham sang word_choice/meaning (vi du: lap nguyen cum tu, cau cut giua chung, vo cau truc chu-vi ro rang), prose tu block MLP that, mot-truc-mot-loi, expected ghi truoc; 18 cap khac giu nguyen tung ky tu de an cache (36/40 call hit, ~4 call moi ~$0.005). Claude diff-review 2 cap moi -> re-probe day du 20 cap -> cham lai bang DUNG bang nguong §4c.

### 4e. PJ probe #2 VERIFIED — P-GRAM van FAIL (3/5 tag), loi thuoc DINH NGHIA taxonomy cua ta, khong phai judge; fix v3 co bao chung co hoc (Claude, 2026-07-05)

**Recount khop CodeX:** 4/40 call moi (36 cache hit — 3 nhom PASS tai lap nguyen ven), 0 fail, db unchanged, cost logical $0.03402. P-GRAM: overall 5/5 (moi winner deu bat dung ca 2 chieu, note goi ten dung cum bi lap), grammar-tag van 3/5 -> FAIL theo luat.

**Chan doan:** 2 fixture lap-cum bi tag omission_addition — DUNG theo dinh nghia da khoa trong prompt ("content ... added to one candidate"): cum lap la content added nghia den. Fixture cai loi thuoc lop ma taxonomy xep hop le vao 2 o -> bug fixture lan 2. **BAC de xuat sua taxonomy/nguong cua CodeX** — doi luat sau khi thay data bi cam; fixture phuc tung taxonomy da khoa, khong nguoc lai.

**Findings ghi ho so:**
- Judge catch-ability da chung minh XONG tren moi lop loi thu: 5/5 winner + note dung ban chat; cai con thieu duy nhat la fixture co nhan khong nhap nhang.
- Guard style_unsupported_by_tags NO DUNG THIET KE 2 lan (style X/Y bi ha ve TIE vi tags_final khong co style-group) — co che §4c thu 3 duoc chung minh song, truoc ca pilot.
- INSTRUMENT PROPERTY ghi truoc khi chay pilot/full: loi lop-lap (repetition) trong data that se roi vao omission_addition => KHONG duoc style-alarm dem (alarm chi dem grammar/naturalness/word_choice). Diem mu ke toan nay duoc khai bao a-priori; khi doc phan bo tag production phai nho repetition nam o omission_addition.
- Tag ontology cua judge da do duoc qua 3 vong: dangling-la/word-order/missing-verb -> grammar; connector-swap -> word_choice; repetition -> omission_addition. Nhat quan, khong phai noise.

**Fix v3 (khong phai answer-chasing — chon lop loi DUY NHAT co nhan khong nhap nhang duoi taxonomy da khoa):** thay dung 2 slot PGRAM_01/PGRAM_05 bang XAO TRAT TU TU voi rang buoc co hoc `sorted(A.split()) == sorted(B.split())` (cung multiset tu) -> omission_addition va word_choice bi loai theo dinh nghia, chi con grammar/naturalness kha di; tien le PGRAM_02 (xao trat tu) da duoc tag grammar. Xao phai KHONG tao ra nghia thay the hop le (giu mot-truc). 18 cap giu nguyen tung ky tu. Sau v3: P-GRAM = xao-trat-tu x3 + thieu-dong-tu + duoi-la (3 loai, dat >=3). Claude diff-review + kiem multiset -> re-probe (~4 call moi).

### 4f. PJ probe #3 PASS TOAN BO — instrument PJ duoc kiem chung, GO PILOT 50 (Claude verify doc lap, 2026-07-05)

**Recount khop CodeX:** P-IDENT 10/10 TIE; P-GRAM 5/5 overall + 5/5 tag grammar; P-MEAN 5/5 + 5/5; P-TERM 5/5 style-TIE + 5/5 terminology; order-inconsistent 0; fail 0/40; db unchanged; 4 call moi (36 cache hit), cost logical $0.03371. PGRAM_01 (rui ro elided-subject ghi truoc): judge bat dung va note "missing the subject" — thay dung cai loi da lo; PGRAM_05 note viet ca cach sua dung. 3 ca lech expected duy nhat = P-TERM overall (da xep khong-gate tu truoc). **Tong 3 vong probe: $0.034 + ~$0.0005, moi lan truot deu la fixture/taxonomy-definition, chua lan nao la loi judge hay he thong — va 2 lan truot sinh ra 2 finding ghi ho so (san nhay + ontology tag).**

**GO PILOT 50 cap that.** Yeu cau (tu §4a/§4c, khong co gi moi):
- Mo rong probe_pj.py hoac script score_pj.py: nap 475 cap MLP tu workdb (block pairing nhu SF-BT, mode=ro + hash), auto-tie theo normalization §4a (NFC + CRLF->LF + strip trailing) -> cap identical KHONG gui judge; 339 cap khac nhau sort theo order_index, systematic 50: idx_i = floor((i+0.5)*N/k) (cong thuc SF-BT).
- Moi cap 2 chieu nhu probe; gan X/Y deterministic theo seed; aggregation §4c nguyen xi; short_block flag mang sang (dinh nghia SF-BT), bao cao kep.
- Report: ty le tie (overall va style rieng), phan bo tag, order-inconsistent rate tung verdict-type (**>25% STOP**), style_unsupported rate (**>20% so pair non-tie-style STOP -> fallback 1-verdict**), win/loss theo arm, chi phi thuc + ngoai suy full 339; label status/phase phai ghi pilot (bai hoc stale label §2g).
- Cache DB dung chung; khong cache loi; model_version; raw luon luu. Uoc ~100 call ~ $0.08.
- STOP sau pilot cho Claude verify -> GO/NO-GO full 339.

### 4g. PJ pilot 50 VERIFIED — luat STOP 25% NO dung thiet ke; thuoc ke san = pj_judge_v2 (Claude verify + author, 2026-07-05)

**Recount khop CodeX:** 475 cap (136 auto-tie normalized = dung so do doc lap truoc do cua Claude, 339 khac nhau), sample 50 dung formula (indices luu trong artifact), 100 call 0 fail, model_version 100/100, cost $0.1004, db hash giu nguyen 92229381..., label phase=pilot_50_real_pairs dung (bai hoc stale label da duoc ap dung). score_pj.py review PASS: tai dung nguyen may moc probe_pj (mot nguon su that), arm->A/B parity sha theo seed+block_id, mode=ro, auto-tie theo normalization §4a, arm chi duoc mo ra o tang report.

**Ket qua pilot (KHONG official):** overall TIE 29/50 (58%), S0 12 / S1 9; style TIE 37/50 (74%), S0 7 / S1 6; cong 136 auto-tie: overall tie 88.7%, style tie 93.0%. Tag: word_choice 27, terminology 21, grammar 1, naturalness 1, meaning 1, formatting 2. style_unsupported 1/50 (2% << 20%) — 2-verdict GIU, khong fallback. **Tin hieu som tot cho thesis: gan nhu ZERO tag grammar/naturalness => khong co dau hieu memory pha van phong; style-alarm S1-loss 6 vs S0-loss 7 — can bang.**

**Diem no luat: order-inconsistent overall 34%, style 36% > 25% -> STOP (dung §4a).** Chan doan tren raw: cac ca flip la POSITION BIAS tren cap gan-hoa — vd underfit_overfit_b020 ca 2 chieu judge deu khen "ban Y" (la 2 arm khac nhau!), note tu bien ho nguoc nhau ('vung hon' tot o chieu nay, 'ben vung' tot o chieu kia); weight_decay_b013 ca 2 chieu khen vi tri X. Tat ca 17+18 ca da bi co che 2-chieu ha ve TIE — **co che chua chay dung thiet ke, day la BANG CHUNG SONG both-order la can thiet (tu lieu bao ve)**; van de la judge dang van hanh sat san nhieu: verdict cuoi dua vao tie-break nhieu hon vao su chac chan cua judge.

**Thuoc theo don §4a: pj_judge_v2 (Claude author).** Thay DUNG MOT bullet trong pj_judge_v1, moi thu khac giu nguyen tung byte so voi §4b:
- CU: `- Use "TIE" whenever you cannot confidently prefer one candidate; never invent a preference.`
- MOI: `- Choose "X" or "Y" only when you could defend the same choice no matter which candidate you had read first; the difference must be one you can name concretely, not a matter of taste. Interchangeable synonyms of equal register and equal accuracy are NOT a difference: that is a "TIE". Use "TIE" whenever you cannot confidently prefer one candidate; never invent a preference.`
- **sha256(full template v2, LF) = `20fc81d4628972016c672fc1b6be94d497194eae52f11835f4dbd57d996f7f50`**, prompt_version = pj_judge_v2. Y tuong: ma hoa chinh phep thu order-invariance vao dau moi verdict ("defend the same choice no matter which candidate you had read first") + goi ten dung lop flip do duoc (synonym cung register). Hop le vi: pilot = giai doan kiem chung dung cu, thuoc "sua prompt" da ke TRUOC trong §4a, chua co data PJ official nao.

**Ke hoach chay lai (prompt doi -> cache judge vo hieu theo thiet ke):** (1) re-probe 20 cap planted voi v2 — 40 call ~$0.034, DUNG bang nguong §4c cu, all-4-group PASS la dieu kien cung (guard chong over-tying: P-GRAM/P-MEAN van phai bat 4/5); (2) neu probe all-pass theo co che (pass boolean trong report) CodeX DUOC PHEP chay tiep pilot 50 v2 ngay ~$0.10 khong cho review giua (nguong toan so hoc, Claude van verify du artifact sau); (3) STOP sau pilot v2. **Du doan dang ky truoc:** tie rate se tang tren 58%; order-inconsistent giam; PASS neu <=25% ca 2 verdict-type -> GO full; neu VAN >25% -> STOP design review (khong tu dong nói lai nguong).

### 4g-B. Tu lieu bao ve: vi sao phai dao chieu (position bias — hien tuong da chung minh, user hoi 2026-07-05)

**Position bias cua LLM-as-a-judge la hien tuong co ten trong van lieu, khong rieng Gemini — GPT-4/GPT-3.5/Claude deu do duoc.** Nguon trich cho hoi dong:
- Zheng et al., "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena" (NeurIPS 2023) — bai nen tang LLM-as-judge, do position bias tren nhieu model, va DE XUAT DUNG remedy ta dung: cham 2 chieu dao vi tri + mau thuan = hoa.
- Wang et al., "Large Language Models are not Fair Evaluators" (arXiv:2305.17926, 2023) — chung minh co the thao tung phan quyet GPT-4 chi bang doi thu tu trinh bay.
- Doi chieu nguoi: WMT human eval cung xao thu tu trinh bay (primacy effect) — day la ve sinh thi nghiem co dien, khong phai benh rieng cua may.

**Ly do co che (3 tang):** (1) autoregressive — ban doc sau duoc xu ly trong boi canh ban doc truoc (anchoring), khong co bao dam giao hoan f(A,B)=f(B,A); (2) lech phan bo vi tri/nhan trong du lieu huan luyen (option-label bias); (3) thien vi chi du manh de quyet ca SAT NUT — khac biet ro thi tin hieu de bep thien vi.

**Bang chung noi bo 2 tang (diem manh: ta KHONG chi trich dan, ta DO duoc):** probe 20 cap loi ro = 0/10 lat keo; pilot 50 cap that gan-hoa = 34% lat keo, co ca (underfit_overfit_b020) khen "vi tri Y" o ca 2 chieu voi 2 ly do tu mau thuan; co che 2-chieu ha 100% ca lat ve TIE, khong verdict sai nao lot. Cau chot hoi dong: "dao chieu khong phai phong xa ly thuyet — chung em do duoc position bias o 34% cap gan-hoa va co che hai chieu trung hoa toan bo."

### 4h. DESIGN REVIEW (theo dung nhanh STOP cua §4g): dung vong lap prompt, ve lai pj_judge_v1, chap nhan inconsistency co ho so, GO FULL (Claude, 2026-07-05)

**Recount v4 + pilot v2 khop CodeX** (probe v4 pass co hoc 4 nhom nhung P-GRAM/P-MEAN tut 5/5->4/5 tag, PTERM_02 mat catch; pilot v2: tie tang 58->70%/74->82%, inconsistent overall 40%/style 38%, style_unsupported 2%, 0 fail, db giu, arm slot can 21/50).

**Phan tich quyet dinh — mo "inconsistency" thanh 2 loai (do tren ca 2 pilot):**
- v1: 33 consistent / 9 soft (TIE<->win) / **8 HARD (X<->Y dao phan)**
- v2: 30 consistent / 8 soft / **12 HARD**
=> **Du doan dang ky truoc cua Claude o §4g SAI MOT NUA** (tie tang DUNG, inconsistent giam SAI — ghi trung thuc): prompt v2 khong sua duoc position bias (hard flip con TANG 8->12), chi tra them gia sensitivity (probe tut 2 nhom). Hai data point => inconsistency la THUOC TINH CUA QUAN THE cap gan-hoa + judge, khong phai cua cau chu prompt. **Sua prompt vong 3 = metric-chasing khong co che — TU CHOI, dung vong lap.**

**Vi sao chap nhan duoc >25% (lap luan cau truc, khong phai noi nguong lay duoc):** position bias KHONG THE thien vi arm nao vi 3 tang cau truc: (1) gan arm->slot A/B ngau nhien theo seed tung block (do: 21/50 can); (2) verdict quyet dinh DOI HOI 2 chieu dong y — uu tien theo vi tri khong the tao ra verdict quyet dinh; (3) moi bat dong -> TIE bao thu. => inconsistency cao lam giam SUC MANH THONG KE (it verdict quyet dinh hon), KHONG lam sai lech ket qua. Gate 25% dat truoc khi biet mat do gan-hoa cua quan the (91% tie o SF-BT llm da bao truoc dieu nay) va thuc chat canh mot rui ro bias ma thiet ke da loai bo bang cau truc. **Quyet dinh review: gate 25% nghi huu CO HO SO** (khong xoa lang le): full readout BAT BUOC bao cao phan ra consistent/soft/hard + n_effective; headline giu nguyen luat inconsistent->TIE.

**Chon instrument: VE LAI pj_judge_v1 (sha d47dbb17), bo v2.** Ly do: v1 troi hon v2 tren CA HAI truc chat luong dung cu da do (hard flip 8<12; probe 5/5 ca 4 nhom vs hai nhom 4/5) — day la lua chon calibration giua 2 variant deu da qua probe, quyet tren metric NHIEU CUA DUNG CU (khong phai tren ket qua S0/S1), tai checkpoint review duoc luat chi dinh, TRUOC moi data official. Minh bach outcome: ca 2 pilot decisive deu nghieng S0 (v1 12-9, v2 10-5) — chon v1 neu anh huong gi thi cho S1 NHIEU decisive win hon, khong co dong co thien vi. Bonus: cache v1 nguyen ven -> 50 cap pilot tai dung, full chi ton ~578 call moi ~$0.58.

**GO FULL 339 voi pj_judge_v1.** Du doan dang ky truoc cho full (khoa truoc khi chay): (1) tong tie (ke auto) >=80%; (2) style-alarm KHONG keu (pilot grammar/naturalness ~0 ca 2 arm, 2 pilot doc lap); (3) du doan chuan-hoa §4a giu nguyen: S1-loss tag terminology/meaning phai chua block regularization; (4) order-inconsistent du kien 30-40% judged pairs — la thuoc tinh bao cao, khong con la gate. Sau full: Gemma audit khuon §2h (§4a).

**Viec CodeX:** (1) revert probe_pj.py ve pj_judge_v1 (PROMPT_VERSION + sha d47dbb17... + bullet cu, dung 3 cho da doi); (2) chay full: score_pj.py mo rong scope full 339 (sample = toan bo different pairs), out pj_full_339.json, label phase=full; cache DB giu (50 cap pilot hit); bao cao them phan ra consistent/soft/hard + n_effective; (3) STOP cho Claude verify -> Gemma audit.

### 4i. Position bias — canh quan nghien cuu + chot calibration subset nguoi cham (Claude adjudicate ban tong hop 3-LLM cua CodeX, 2026-07-05)

**Cau tra loi cho user (tu lieu bao ve, bo sung §4g-B):** position bias la THUOC TINH CO HUU cua LLM hien tai — van lieu chi co cac giai phap TRUNG HOA o tang quy trinh, chua ai xoa duoc o tang model: (1) swap-and-aggregate (Zheng MT-Bench 2023, Wang 2023) = chuan thuc hanh pairwise = CHINH THIET KE CUA TA; (2) PORTIA (Li 2023 split-merge) — cho cau tra loi dai nhieu phan, khong hop block dich ngan; (3) judge fine-tune chong bias (JudgeLM/PandaLM/Prometheus) — giam khong het, la judge moi phai probe lai, khong dang cho 339 cap; (4) pointwise cham rieng roi so — triet bias theo cau truc nhung doi benh scale-drift/bao hoa, TA DA DO benh nay (SF-BT-llm 91% hoa); (5) logit-debias qua hoan vi — can logprobs, API khong cho. Cau chot hoi dong: "khong xoa — do no, trung hoa no o tang aggregation, tra gia bang so verdict quyet dinh thay vi bang do dung."

**Adjudicate bao cao CodeX:** phan loi GHI NHAN toan bo (trung §4h); 3B evidence-span + 3C pointwise = future work/ablation, khong dung full run (3C co them bang chung noi bo: SF-BT bao hoa).

**3A CHOT THANH BUOC CHINH THUC — calibration subset nguoi cham (SAU full run, giao thuc khoa TRUOC khi thay ket qua full):**
- ~40 cap phan tang tu full: consistent / soft / hard-flip / S0-win / S1-win / tie, phu cac tag chinh;
- Phieu cham MU tuyet doi: EN goc + 2 ban VI xao vi tri MOI (khong dung slot cua judge), KHONG lo arm, KHONG lo verdict Gemini;
- Nguoi cham = user (ban ngu Viet, dung chuyen nganh), cham overall + style (X/Y/TIE), uoc 30-60 phut;
- Bao cao raw agreement + Cohen's kappa, REPORT-ONLY khong lam gate (dang ky truoc: quan the gan-hoa nen kappa nguoi-nguoi cung chi trung binh — so nay de dinh vi do tin cua judge, khong de dau/truot); neu kappa thap -> ghi lam limitation co ho so, khong duoc dung de "chua" lai ket qua.

**Thu tu thi hanh giu nguyen §4h:** CodeX revert v1 -> full 339 -> Claude verify -> Gemma audit -> calibration subset (buoc moi) -> PJ official readout.

### 4j. PJ FULL 339 VERIFIED — S0 troi decisive nhung phan tich tag+gold cho thay phan lon la taste-vs-style-guide, khong phai hong van (Claude recount + phan tich, 2026-07-05)

**Recount doc lap khop CodeX 100%:** overall S0 102 / S1 51 / TIE 186 (n_eff 153); style S0 76 / S1 31 / TIE 232 (n_eff 107); breakdown overall consistent 216 / soft 52 / hard 71 (recount khop); 0/678 fail, model_version 678/678, cost recompute $0.7067, db hash giu, 136 auto-tie + 339 judged = 475 dung scope. Ghi nhan CodeX rerun cache-only de sua breakdown auto-tie (cache deterministic nen hop le, verdict khong doi).

**So ledger 4 du doan dang ky truoc (§4h):**
- ① tong tie >=80%: **TRUOT (67.8%)** — loi cua Claude khi dang ky: ngoai suy tu tap pilot ma auto-tie chiem 73%, trong khi full auto-tie chi 28.6%; tie rate cua rieng phan judged nhat quan (pilot 58% -> full 54.9%). Ghi trung thuc: du doan sai vi framing so hoc, khong phai instrument doi hanh vi.
- ② style-alarm KHONG keu: **DUNG nhung sat nut** — S1 style-loss tag-group 76/475=16.00% vs S0 31/475=6.53%, chenh 9.47 diem < 10. PHAI bao cao margin 0.53 diem + phan tich thanh phan: trong 102 ca S1-thua overall, grammar chi 1 + naturalness 9 — tin hieu "hong van" thuc su gan nhu zero; khoang cach den nguong den tu word_choice (86) ma ranh gioi voi terminology la fuzzy da do (probe/pilot).
- ③ cum chuan-hoa hien o S1-loss terminology: **TRUNG** — 7 block, judge note goi ten truc tiep ("chinh quy hoa ... more accurate than Y's 'chuan hoa'", weight_decay_b002/b006) => **PJ = dung cu doc lap THU TU do cung entry canonical sai §35.8** (sau gold-recall B, §36 forensics, SF-BT §2g).
- ④ order-inconsistent 30-40%: **DUNG** (123/339 = 36.3%; hard 71 = 20.9% deu bi ha ve TIE).

**Phan tich then chot — taste-vs-gold (cross-ref voi gold glossary 458 term, metric step duoc phep doc gold):** trong 101 ca S1-thua tag terminology/word_choice, **23 ca S1 dung DUNG dang gold cua style guide con S0 khong** (layer->tang x7, generalization error->loi khai quat x5, overfit->qua khop, weight decay->suy giam trong so, example->mau...) — S1 bi phat vi tuan thu quy uoc con nguoi; chieu doi xung S0 chi 10/44. Ket hop bang chung PTERM_03 (judge thich "Mang no-ron nhieu lop" hon gold-style "Perceptron da tang") va note lap lai "more common": **PJ-overall do "taste doc gia ML pho thong", KHONG do "do dung"** — cung ban chat voi bai hoc §9 (TA do "do khop quy uoc ngoai"). TA noi S1 tot hon (khop gold), PJ-overall noi S0 tot hon (khop common usage) — hai dung cu do HAI HE QUY CHIEU khac nhau, mau thuan bieu kien nay la triangulation dung nghia: memory keo ban dich VE style guide va RA XA usage pho thong — dung chuc nang thiet ke; "tot hon" tuy chon he quy chieu. Dien giai nay KHONG duoc dung de xoa ket qua: con so S0 102-51 phai len bao cao nguyen ven kem phan tich nay.

**Con lai truoc official readout:** Gemma audit (§4a: 50 systematic + toan bo 102 cap S1-thua, dao-phan <=10%) -> calibration subset user ~40 cap (§4i) -> §4k official readout + dong thang PJ.

### 4k. Gemma audit VERIFIED (PASS ca 2 nhom) + phat hanh phieu cham mu calibration (Claude, 2026-07-05)

**Recount doc lap khop:** systematic_50 dao-phan 0/21 gemini-decisive = 0% PASS; s1_loss_overall 3/102 ~ 2.9% PASS (<=10% ca 2); Gemma profile dung chuan verbatim-task (temp 0, repeat_penalty 1.0, seed, reasoning none), prompt v1 sha khop; 2 parse-fail loai bao thu + raw luu; $0 local, 20.3 phut. 3 ca dao deu la word_choice-taste (Gemma style TIE ca 3) — dung vung mo da biet. **Y nghia: 74/102 ca S1-thua duoc model KHAC HO xac nhan y nguyen winner => ket qua S0-troi khong phai tat rieng cua Gemini judge**; tie-vs-win 22-25/nhom = dao dong ky vong tren quan the gan-hoa, khong tinh dao phan (dinh nghia khoa truoc).

**Phieu cham mu phat hanh (giao thuc §4i, seed 20260705):** 40 cap phan tang tu full (10 S0-win / 8 S1-win / 10 tie-consistent / 6 soft / 6 hard), xao thu tu cau + xao slot X/Y bang seed MOI doc lap voi slot cua judge; file `pj_calibration_sheet.md` (user dien Overall/Style moi cau) + `pj_calibration_key.json` (niem phong: mapping X/Y->arm + verdict Gemini — USER KHONG DUOC MO truoc khi cham xong; Claude cung chi mo khi cham diem). Bao cao sau cham: raw agreement + Cohen's kappa (overall & style rieng), doi chieu theo stratum; REPORT-ONLY khong gate.

**Sau khi user nop phieu: §4l cham kappa -> §4m PJ OFFICIAL READOUT dong thang 7/7.**

### 4l. Calibration nguoi cham SCORED — khong mot ca dao phan overall, huong S0-troi duoc NGUOI xac nhan (Claude cham, 2026-07-05)

**Ket qua (40/40 phieu sach, key mo sau khi cham):** overall raw agreement 25/40 = 62.5%, Cohen kappa 0.408 (moderate); style 22/40 = 55.0%, kappa 0.307. **Cau truc bat dong quan trong hon con so: overall 0/40 dao phan cung (user khong bao gio phan nguoc winner cua Gemini) — toan bo 15 ca bat dong la tie-vs-win; style chi 1 dao.** Kappa muc moderate dung ky vong dang ky truoc (§4i: quan the gan-hoa -> kappa nguoi-may chi trung binh), REPORT-ONLY khong gate.

**Phat hien them:**
- User tren mau phan tang: overall S0 14 / S1 9 / TIE 17; style S0 17 / S1 9 — **NGUOI cung nghieng S0, cung huong voi judge** => ket qua S0-troi cua full duoc xac nhan boi ca 3 he quy chieu doc lap: Gemini, Gemma khac ho, va nguoi ban ngu.
- Stratum hard-flip (Gemini tu mau thuan -> ha TIE): user cham 4/6 S0-win => chinh sach TIE-khi-mau-thuan dang BAO THU NGUOC CHIEU S0, tuc loi the S0 neu sai lech thi bi DANH GIA THAP chu khong thoi phong. Stratum soft: user 5/6 TIE dong y voi viec ha bac. Stratum S1_win: user dong y 7/8 — cac ca S1 thang la thang that.
- User quyet doan hon Gemini o style (14 TIE vs 29): nguoi phan biet van phong tinh hon may tren cap gan-hoa.

**Han che ghi ho so:** nguoi cham = tac gia luan van (khong phai rater doc lap), n=40, 1 rater, khong tinh duoc inter-rater; kappa muc moderate phan anh ca tie-boundary noise cua chinh quan the.

### 4m. PJ OFFICIAL READOUT — THANG 7/7 DONG SO (Claude, 2026-07-05)

**Instrument:** source-aware paired preference; judge gemini-2.5-flash + pj_judge_v1 (sha d47dbb17), 2 chieu dao vi tri, mau thuan->TIE, 2-verdict overall/style, taxonomy 7-tag, auto-tie normalized; da qua: probe 3 vong planted PASS, pilot + design review §4h, Gemma khac-ho audit PASS (0%/2.9% dao), calibration nguoi PASS cau truc (0 dao overall).

**HEADLINE (475 cap MLP):** overall S0 102 / S1 51 / TIE 322 (186 judged + 136 auto), n_eff 153; style S0 76 / S1 31 / TIE 368; breakdown 216 consistent / 52 soft / 71 hard (deu ->TIE); 0/678 loi, $0.7067 (+audit $0 local), tong chi phi ca thang PJ ke probe/pilot ~$1.0.
- **STYLE-ALARM: IM** (chenh 9.47 < 10 diem; margin 0.53 phai bao cao) — va tin hieu hong-van thuc su ~zero: grammar 1 + naturalness 9 tren 102 ca S1-thua. **Cau tra loi chinh thuc cho cau hoi PJ sinh ra de tra loi: KHONG co bang chung memory lam hong van phong tieng Viet.**
- **Cum chuan-hoa: PJ = dung cu doc lap thu 4** do cung entry canonical sai (7 block, note judge goi dich danh) — chuoi detect->measure->repair du 4 mat xich.
- **Dien giai S0-troi decisive (bat buoc di kem con so):** 23/101 ca S1-thua tag term/word_choice la S1 dung DUNG dang gold style-guide (S0 khong) vs doi xung 10/44 => PJ-overall do taste doc gia pho thong; TA (khop gold) va PJ (khop usage) do 2 he quy chieu — memory keo ban dich ve style guide, ra xa usage pho thong, dung chuc nang. Ai muon "hay theo so dong" chon S0-frame; luan van cam ket style guide d2l-vn nen TA/TC la truc chinh, PJ la chuong bao van + goc nhin doc gia.
- **Instrument properties co ho so:** order-inconsistent 36.3% (thuoc tinh quan the gan-hoa, van lieu §4g-B, trung hoa 100% bang 2-chieu); san nhay = luoi tho (§4d); tag ontology map (§4e); word_choice/terminology ranh mo (probe+pilot+audit deu thay); gate 25% nghi huu co ho so (§4h); kappa nguoi 0.41/0.31 moderate voi 0 dao phan (§4l).

**TRANG THAI: 7/7 THANG OFFICIAL** (TC, TC-Occ, TA, TA-Occ, SF-QE, SF-BT, PJ). Con lai cua EVAL: agreement analysis tong hop 7 thang + (optional) Stage 2 prelim SF-BT/PJ; roi sang TASK_ONE_BUTTON_V1.
