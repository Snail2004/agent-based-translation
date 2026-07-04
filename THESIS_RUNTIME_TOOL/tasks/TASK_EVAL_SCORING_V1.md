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
