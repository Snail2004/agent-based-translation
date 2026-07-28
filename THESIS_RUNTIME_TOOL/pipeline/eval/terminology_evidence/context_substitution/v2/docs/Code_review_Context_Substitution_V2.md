# CODE REVIEW — CONTEXT SUBSTITUTION V2

## Phạm vi review

Artifact được review: `context_substitution.zip`

Đã thực hiện:

- đọc toàn bộ source Python và tài liệu trong archive;
- kiểm tra cấu trúc package;
- chạy `compileall`;
- đối chiếu với hợp đồng Context Substitution Test V2;
- chạy hai phép thử cô lập cho Context Selector và Application Contract.

Không thể xác minh từ archive hiện tại:

- tuyên bố `31/31 test đạt`, vì archive không chứa test suite;
- CLI `run_d2l_term_evidence_v1.py`, vì archive không chứa file này;
- synthetic end-to-end trong repo đầy đủ;
- live probe ShopAI Key, CKey và Gemini;
- import/runtime đầy đủ vì archive thiếu `pipeline.eval.contracts_v1`,
  `legacy_term_evidence` và các dependency cấp repo.

## Kết luận

Kiến trúc đã đi đúng hướng và tách scope tốt. Các điểm mạnh nhất là:

- selector không nhận candidate wording;
- LLM không sở hữu total/label/final decision;
- `final_glossary_decision` luôn null và được contract kiểm tra;
- trial retry bị giới hạn;
- output schema và run self-hash nghiêm ngặt;
- contrastive context không cộng vào C;
- support set chưa giả mạo embedding;
- provenance provider, prompt và token usage khá đầy đủ.

Tuy nhiên chưa nên chạy 150 term-sense chính thức. Có 6 vấn đề phải sửa trước pilot và 6 vấn đề nên sửa trước test set.

---

# P0 — Phải sửa trước pilot/live run

## P0-1. Context Selector không bảo đảm đủ C1–C5

**Vị trí**

- `runtime/selection.py:99–112`

Code sort theo loại context rồi lấy 5 phần tử đầu. Nếu có hai context `definition`,
một context `same_sense_difficult` có thể bị đẩy sang replacement.

Đã tái hiện bằng test cô lập:

```text
Input types:
definition, definition, typical_usage,
domain_collocation, syntactic_variation,
same_sense_difficult

Selected:
definition, definition, typical_usage,
domain_collocation, syntactic_variation

Omitted:
same_sense_difficult
```

**Tác động**

- vi phạm contract C1–C5;
- C có thể thiên về các context dễ;
- robustness ở tail context không được kiểm tra.

**Sửa**

Chọn theo hai vòng:

1. chọn tối đa một context tốt nhất cho từng required type;
2. dùng context còn lại để fill slot thiếu.

Thêm flag:

```text
CONTEXT_TYPE_COVERAGE_INCOMPLETE
```

và lưu `missing_context_types`.

---

## P0-2. Translator external error vẫn có thể bị tính điểm

**Vị trí**

- `contracts/responses.py:533–545`
- `runtime/aggregation.py:20–31`
- `runtime/engine.py:614–628`

Context Judge có thể trả:

```json
{
  "judgeability": "JUDGEABLE",
  "flags": {
    "translator_external_error": true,
    "insufficient_context": true
  }
}
```

Schema chấp nhận, nhưng `compute_context_result()` bỏ qua hai flag này và vẫn tính điểm.

**Tác động**

Vi phạm nguyên tắc:

> lỗi của Trial Translator không được làm candidate bị trừ oan.

**Sửa**

Cross-field validation:

```text
translator_external_error = true
→ judgeability phải là INVALID_TRIAL_TRANSLATION

insufficient_context = true
→ judgeability phải là INSUFFICIENT_CONTEXT
```

Hoặc engine phải exclude context và chạy replacement.

---

## P0-3. Chưa có bridge từ freeze 150 term-sense sang runtime

**Vị trí**

- runtime nhận legacy input tại `runtime/engine.py:92–96`;
- dataset freeze tạo `term_senses.jsonl`, `candidate_slots.jsonl`,
  `candidate_instances.jsonl`, `contexts.jsonl`;
- không tìm thấy converter/loader nối hai schema.

Runtime còn dùng `block_id` như `context_id`:

- `runtime/selection.py:52–60`;
- `runtime/engine.py:369`;
- `contracts/run.py:987–995`.

Trong freeze mới, `context_id` là sentence-level ID riêng và có thể có nhiều context
trong cùng một block.

**Tác động**

Gói 150 term-sense chưa thể đi thẳng vào CST V2. Adapter viết vội có thể làm mất
sentence ID hoặc tạo collision context.

**Sửa**

Tạo module:

```text
dataset/runtime_adapter.py
```

API:

```python
freeze_to_context_substitution_input(
    freeze_dir,
    candidate_policy,
) -> D2LContextSubstitutionInputV2
```

Runtime phải dùng canonical `context_id`, không đồng nhất nó với `block_id`.

---

## P0-4. Judge 2 chưa thật sự độc lập theo model family

**Vị trí**

- `providers/base.py:53–59` bắt buộc mọi route là Gemini;
- `runtime/engine.py:772–775` chỉ loại route, không loại model/family;
- `runtime/engine.py:950–969` chỉ phân biệt model ID;
- `runtime/aggregation.py:151–157` không kiểm tra independence status.

Một Judge 2 chạy Gemini qua route khác vẫn có thể được ghi:

```text
CROSS_ROUTE_SAME_MODEL
```

nhưng candidate vẫn có thể nhận:

```text
ELIGIBLE_FOR_COMBINATION
```

**Tác động**

Không đáp ứng claim “khác họ mô hình khi có thể”, và agreement có thể phản ánh
cùng một thiên lệch.

**Sửa**

Thêm vào route:

```json
{
  "provider_id": "...",
  "model_id": "...",
  "model_family": "gemini",
  "independence_group": "google-gemini"
}
```

Second Judge policy phải ưu tiên/exclude theo `independence_group`.
Nếu chỉ có same-family Judge, thêm:

```text
LOW_JUDGE_INDEPENDENCE
```

và Global Validator phải biết đây không phải cross-family agreement.

---

## P0-5. Model runner không an toàn khi chạy song song

**Vị trí**

- `providers/base.py:72–73` lưu mutable lists trên model instance;
- `providers/base.py:144–147` append trực tiếp;
- `runtime/engine.py:122` lấy offset;
- `runtime/engine.py:198` slice attempts theo offset.

Nếu hai CST run dùng chung `FailoverStructuredModel` đồng thời, attempted calls có
thể interleave. Usage và provenance của run A có thể chứa call của run B.

**Tác động**

Đặc biệt nghiêm trọng khi chạy 100–150 sense song song.

**Sửa**

Một trong ba cách:

1. model instance riêng cho mỗi run;
2. `RunCallCollector` riêng truyền qua call stack;
3. lock + run ID, sau đó filter attempt theo run ID.

Không dùng global mutable call history làm nguồn provenance của một run.

---

## P0-6. Legacy measurement adapter có thể xuất sai hard failure

**Vị trí**

- `contracts/run.py:257–269`

`wrong_concept` chỉ xét:

```text
WRONG_SENSE
SEMANTIC_CONTRADICTION
CANDIDATE_INDUCED_DISTORTION
```

nhưng bỏ:

```text
SEMANTIC_EQUIVALENCE_LTE_2
DOMAIN_SENSE_FIT_ZERO
```

Ngoài ra adapter xuất merged label/raw score sau Judge 2 nhưng reason/provenance chỉ
lấy Primary Judge:

- `contracts/run.py:236–251`.

**Tác động**

- candidate fail vì semantic mismatch có thể được export `wrong_concept=false`;
- label do Judge 2 tạo nhưng audit lại trỏ tới Judge 1.

**Sửa**

- derive `wrong_concept` từ toàn bộ `LOCAL_HARD_FLAGS`;
- export primary + secondary provenance;
- tạo `merged_decision_provenance`;
- không dùng primary reason làm reason duy nhất sau disagreement.

---

# P1 — Phải sửa trước validation/test set

## P1-1. Application Contract tin variant do Judge tự khai

**Vị trí**

- `contracts/responses.py:546–582`;
- `contracts/application.py:21–34`, `51–63`.

`variant_observation.surface_used` không được kiểm tra có thật trong
`trial_translation` hay khớp `trial.applied_expansion`.

Đã tái hiện: Judge có thể khai `"pha suy luận"` và hệ thống tạo:

```json
{
  "surface": "pha suy luận",
  "status": "OBSERVED_VALID"
}
```

dù target text không chứa surface này.

**Sửa**

Code phải bind variant vào:

```text
trial.candidate_surface_used
trial.applied_expansion
actual target span
```

Judge chỉ được nhận xét, không được là nguồn duy nhất của surface.

---

## P1-2. Default chỉ chạy canonical candidate

**Vị trí**

- `runtime/engine.py:87–89`

```python
include_target_roles=("canonical",)
```

Thí nghiệm luận văn cần 2–3 candidates/sense. Với default hiện tại:

- alternative không được kiểm định;
- Pairwise thường không chạy;
- dataset có vẻ hoàn thành nhưng thực tế chỉ đánh giá candidate đã chọn sẵn.

**Sửa**

CLI research mode phải:

- bắt buộc explicit target IDs; hoặc
- mặc định `canonical + alternative + pending`;
- in summary rõ số candidate requested/processed/skipped.

---

## P1-3. Threshold demo đang điều khiển runtime

**Vị trí**

- `runtime/engine.py:255–258`;
- `runtime/pairwise.py:37`, `64`;
- `runtime/aggregation.py:118–137`.

Các ngưỡng 0.60/0.70/0.80 và margin 0.067 đã được version hóa nhưng vẫn là heuristic.

**Tác động**

- quyết định gọi Judge 2;
- local status;
- chi phí;
- Pairwise sampling;

đều phụ thuộc số chưa calibration.

**Sửa**

Tách:

```text
DEVELOPMENT_HEURISTIC_POLICY
FROZEN_VALIDATION_POLICY
```

Không chạy test set khi policy status còn
`DEMO_HEURISTIC_REQUIRES_CALIBRATION`.

---

## P1-4. Evidence ID chưa định danh đầy đủ evidence

**Vị trí**

- `runtime/engine.py:896–908`;
- mirror logic ở `contracts/run.py:2128–2140`.

`cst_evidence_id` hash candidate, C, source hashes và prompt hashes, nhưng không hash:

- response hashes;
- raw score vector;
- PASS/MINOR/FAIL distribution;
- contrastive results;
- Judge 2 result;
- application contract.

Hai run khác judgment nhưng cùng C có thể nhận cùng evidence ID.

**Sửa**

ID nên hash canonical candidate evidence package hoặc tối thiểu:

```text
all provider response_sha256
raw_context_scores
labels
local flags
contrastive results
support_set_version
```

---

## P1-5. Source hashes trong candidate provenance chưa đủ

**Vị trí**

- `runtime/engine.py:873–883`;
- `contracts/run.py:2105–2115`.

Explicit `source_hashes` chỉ lấy accepted context và contrastive context, không lấy
excluded/retried context. Prompt hash có thể gián tiếp thay đổi nhưng field
`source_hashes` không phản ánh toàn bộ source đã được xử lý.

**Sửa**

Lưu riêng:

```text
attempted_source_hashes
accepted_source_hashes
excluded_source_hashes
contrastive_source_hashes
```

---

## P1-6. Support set chưa tách positive support và boundary evidence

**Vị trí**

- `evidence/support_set.py:14–24`.

Mọi `context_results` đều được đưa vào `validation_contexts`, kể cả FAIL.

Khi materialize embedding cho TAC, nearest-neighbor tới một FAIL context có thể làm
occurrence mới trông như nằm trong vùng đã chứng nhận.

**Sửa**

Tách:

```json
{
  "positive_support_contexts": ["PASS", "MINOR theo policy"],
  "negative_or_boundary_contexts": ["FAIL"],
  "contrastive_contexts": [...]
}
```

TAC dùng positive support để tính in-distribution; negative/boundary dùng cảnh báo.

---

# P2 — Nên sửa

## P2-1. Official Gemini timeout chưa được áp dụng

**Vị trí**

- `providers/google.py:82–91`.

`HttpOptions(timeout=...)` chỉ được tạo khi có custom `base_url`.
Official route `base_url=None` không nhận timeout setting.

**Sửa**

Luôn tạo `HttpOptions(timeout=...)`, chỉ thêm `base_url` khi có.

---

## P2-2. API key có thể lộ qua dataclass repr

**Vị trí**

- `providers/google.py:14–20`.

`api_key` dùng dataclass default repr.

**Sửa**

```python
api_key: str = field(repr=False)
```

và không log settings object.

---

## P2-3. Literal check cần token boundary và normalization thống nhất

**Vị trí**

- `runtime/engine.py:436–450`.

Hiện tại vừa strict equality cho `candidate_surface_used`, vừa substring casefold cho
target text. Có thể false negative vì Unicode/space hoặc false positive vì substring.

**Sửa**

Tạo một `TargetSurfaceMatcher` dùng:

- Unicode NFC;
- normalized whitespace;
- Vietnamese-aware token boundary;
- explicit expansion binding.

---

## P2-4. Pairwise provenance chưa gắn vào candidate provenance

Pairwise result nằm top-level, nhưng `candidate_provider_provenances()` không yield
pairwise provenance.

Nếu Global Validator dùng pairwise, certificate phải link tới pairwise observation ID.

---

## P2-5. Naming/version cần thống nhất

Hiện đồng thời có:

```text
d2l_context_substitution_v1
context_substitution/v2
schema 2.0.0
support freeze v1
CLI ..._v1.py
```

Các lớp version có thể hợp lệ, nhưng cần một bảng:

| Khái niệm | Version |
|---|---|
| artifact release | ... |
| CST contract | 2.0.0 |
| support freeze schema | 1.0.0 |
| CLI | ... |
| experiment policy | ... |

---

## P2-6. Artifact review bundle chưa đầy đủ

Archive có source package và docs nhưng không có:

- CLI;
- tests;
- dependency contract;
- lock file;
- test output;
- static scan output.

Ngoài ra có `__pycache__`, nên loại khỏi artifact/commit.

---

# Đánh giá tổng thể

| Hạng mục | Đánh giá |
|---|---|
| Tách scope CST khỏi Global Validator | Tốt |
| Contract/schema discipline | Rất tốt |
| Fail-safe và provenance | Khá tốt |
| Context selection đúng C1–C5 | Chưa đạt |
| Trial error attribution | Chưa đạt |
| Judge independence cho claim nghiên cứu | Chưa đạt |
| Sẵn sàng chạy song song 150 sense | Chưa đạt |
| Sẵn sàng pilot 5–10 sense sau P0 | Có |
| Sẵn sàng test set luận văn | Chưa |

## Phán quyết

```text
ARCHITECTURE ALIGNMENT: CONDITIONAL PASS
STATIC SYNTAX: PASS
ARTIFACT REPRODUCIBILITY: INCOMPLETE
PILOT READINESS: BLOCKED UNTIL P0 FIXES
150-SENSE READINESS: BLOCKED
RESEARCH TEST-SET READINESS: BLOCKED UNTIL P0 + P1
```

## Thứ tự sửa khuyến nghị

1. Selector coverage C1–C5.
2. External translator error exclusion.
3. Freeze-to-runtime adapter + canonical context ID.
4. Per-run provider attempt collector.
5. Judge family metadata/policy.
6. Legacy measurement adapter.
7. Variant binding.
8. Research candidate-selection defaults.
9. Freeze calibration policy.
10. Live probe 3 providers.

---

## Rework closure (2026-07-28)

This file preserves the original review findings. The implementation was
reworked as Context Substitution V2.1 without running the 150 term-sense set
and without making live provider calls.

Closed findings:

- P0-1: deterministic selector coverage prioritizes C1-C5 and records missing
  types explicitly.
- P0-2: judgeability and translator/context error flags are cross-validated;
  non-judgeable evidence cannot enter scoring.
- P0-3: `D2LContextSubstitutionInputV2` and `dataset/runtime_adapter.py` bridge
  validated freeze bundles while retaining independent `context_id` and
  physical block/range provenance.
- P0-4: routes carry model-family and independence-group identity; Judge 2
  excludes the primary route, family, and independence group.
- P0-5: provenance uses a context-local per-run call collector rather than
  slices of shared mutable history.
- P0-6: all local hard flags project to `wrong_concept`; the legacy V1
  projection rejects merged two-judge evidence instead of emitting lossy
  provenance.
- P1-1/P1-2: variants bind to the actual translated surface; research-mode
  defaults include canonical, alternative, and pending candidates.
- P1-3: development and frozen calibration policies are separate. A frozen
  test-set run is rejected unless it binds a calibration artifact hash.
- P1-4/P1-5: candidate IDs hash the complete evidence package; provenance
  partitions selector, attempted, accepted, excluded, and contrastive source
  hashes and includes response hashes and pairwise observation IDs.
- P1-6: positive support, negative/boundary evidence, and contrastive evidence
  are separate contract fields.
- P2-1/P2-2/P2-3/P2-4: official-route timeout, API-key repr redaction,
  token-boundary surface matching, and pairwise candidate linkage are covered.

The release/version mapping is recorded in `VERSION_MATRIX.md`. Live ShopAI,
CKey, and official Gemini probes remain intentionally unrun; the CLI still
requires explicit `--allow-api`.

## V2.2 real-dataset rework closure

The follow-up review blockers are closed in the active V2.2 contract:

- `runtime_adapter.py` dispatches the real Validation-ready V3 and Pilot V1.1
  schemas, validates the exact Pilot-to-V3 parent, and emits a self-hashed
  zero-API adapter receipt;
- frozen selector authority is available only from a complete immutable human
  review artifact; the current pending review pack is not promoted;
- calibration authority comes from `CSTCalibrationArtifactV1`; zero or forged
  dataset/gold hashes and policies below the measured precision floor reject;
- Context Judge identity is pinned but no longer hard-coded to Gemini, allowing
  independent model families through the three registered transport routes;
- raw provider outputs are captured before parsing in a content-addressed
  ledger and are mandatory in frozen execution;
- missing contrastive contexts and incomplete context-type coverage cannot
  yield `ELIGIBLE_FOR_COMBINATION`;
- the V2.2 CLI includes real-dataset validation/adaptation and remains API-off
  unless `context-run --allow-api` is explicit.

See `REAL_DATASET_ADAPTER_REWORK_V1.md` for exact source hashes, row counts,
commands, and the remaining human-review boundary.
