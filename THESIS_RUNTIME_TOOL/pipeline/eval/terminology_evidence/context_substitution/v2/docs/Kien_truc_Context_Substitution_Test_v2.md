# KIẾN TRÚC CONTEXT SUBSTITUTION TEST — PHIÊN BẢN 2
## Evidence Provider cho kiểm định ứng viên thuật ngữ Anh–Việt

**Phiên bản:** 2.0  
**Vai trò trong luận văn:** Thành phần tạo **Contextual Evidence (C)** cho Terminology Validator trung tâm  
**Phạm vi:** Kiểm định một ứng viên tiếng Việt trong một `sense_id` và `scope_id` xác định  
**Không thuộc phạm vi:** Tự động quyết định glossary cuối, đánh giá chất lượng dịch tổng quát, hoặc tái kiểm định web/corpus evidence

---

## 1. Định vị kiến trúc

Context Substitution Test (CST) là một **Evidence Provider**.

Nó trả lời câu hỏi:

> Với một nghĩa nguồn và phạm vi đã xác định, ứng viên tiếng Việt này có duy trì đúng khái niệm, dùng tự nhiên và ổn định trong các ngữ cảnh thực hay không?

CST **không** tự trả lời:

> Ứng viên này có được đóng dấu vào glossary chính thức hay không?

Quyết định glossary cuối thuộc về Terminology Validator toàn cục:

```text
Contextual Evidence C
        +
Vietnamese Attestation Evidence E
        +
Global Hard Gates
        +
Calibrated Decision Policy
        ↓
AUTO_APPROVED / PROVISIONAL /
HUMAN_REVIEW / REJECTED / SPLIT_REQUIRED
```

CST chỉ cung cấp:

- điểm contextual evidence;
- phân bố PASS/MINOR/FAIL;
- hard flags cục bộ;
- quan sát ranh giới sense;
- allowed variants được quan sát;
- support set phục vụ Term Application Check;
- provenance và versioning;
- khuyến nghị cho Global Validator.

---

## 2. Nguyên tắc thiết kế

1. **Sense before term**  
   Không kiểm định một từ đứng riêng. Mọi đánh giá phải gắn với `sense_id`.

2. **Scope-limited certification**  
   Kết quả CST chỉ có hiệu lực trong vùng ngữ cảnh đã kiểm tra.

3. **Evidence provider, not final decider**  
   CST không tự quyết định trạng thái glossary cuối.

4. **Same-sense contexts score; contrastive contexts bound scope**  
   Context cùng nghĩa được tính điểm. Context khác nghĩa dùng kiểm tra ranh giới.

5. **Translator errors must not punish candidates**  
   Lỗi ngoài thuật ngữ trong bản dịch thử phải được tách khỏi lỗi do ứng viên.

6. **LLM scores components; code decides labels**  
   LLM trả điểm thành phần và cờ lỗi. Code tự tính tổng, nhãn và hard flags.

7. **Hard failures override averages**  
   Sai khái niệm hoặc sai sense không được điểm naturalness bù lại.

8. **Judge may abstain**  
   Context thiếu dữ liệu hoặc trial translation không hợp lệ phải được quyền từ chối chấm.

9. **Calibration over arbitrary thresholds**  
   CST xuất feature và score; ngưỡng vận hành do validation set quyết định.

10. **Everything is versioned and auditable**  
    Context, prompt, model, rubric, embedding và policy đều phải có phiên bản.

---

## 3. Quan hệ với Terminology Validator và TAC

### 3.1. Quan hệ với Terminology Validator

```text
Candidate generation
        ↓
Sense definition
        ↓
Context Substitution Test ─────→ Contextual Evidence C
        ↓
Attestation Evidence ──────────→ Evidence E
        ↓
Global Gate Engine
        ↓
Calibrated Decision Policy
        ↓
Versioned Certified Glossary
```

### 3.2. Quan hệ với Term Application Check

CST tạo một **certificate support set** gồm các context đã dùng để kiểm định.

TAC sử dụng support set này để kiểm tra occurrence mới:

```text
Validated contexts from CST
        ↓
Context embeddings / centroid / nearest-neighbor index
        ↓
New term occurrence during translation
        ↓
In-distribution → deterministic compliance check
Out-of-distribution → sense classifier escalation
```

`AUTO_APPROVED` không có nghĩa “đúng trong mọi câu”.

Nó có nghĩa:

> Ứng viên đã được chứng minh phù hợp trong vùng ngữ cảnh đại diện của sense và scope đã xác định.

---

## 4. Hợp đồng đầu vào

### 4.1. Schema cấp ứng viên

```json
{
  "candidate_id": "term-inference-vi-01",
  "source_term": "inference",
  "candidate_translation": "suy luận",
  "sense_id": "model_execution",
  "scope_id": "machine_learning",

  "sense_contract": {
    "definition_en": "The process in which a trained model produces outputs for new inputs.",
    "definition_source": "corpus_induced",
    "definition_provenance": [
      "d2l-ch03-b015",
      "d2l-ch03-b021"
    ],
    "definition_review_status": "VERIFIED",
    "sense_inventory_version": "sense-v2"
  },

  "part_of_speech": "noun",
  "source_occurrences": [
    "d2l-ch03-b015-s02",
    "d2l-ch03-b021-s01"
  ],

  "candidate_generation": {
    "generator_model": "model-family-a",
    "prompt_version": "candidate-gen-v2",
    "run_id": "cand-run-001"
  }
}
```

### 4.2. Trường bắt buộc

| Trường | Ý nghĩa |
|---|---|
| `candidate_id` | Định danh ứng viên |
| `source_term` | Thuật ngữ nguồn |
| `candidate_translation` | Ứng viên tiếng Việt |
| `sense_id` | Nghĩa cụ thể đang xét |
| `scope_id` | Phạm vi lĩnh vực |
| `definition_en` | Định nghĩa sense |
| `definition_provenance` | Bằng chứng tạo sense |
| `definition_review_status` | Mức xác minh định nghĩa |
| `sense_inventory_version` | Phiên bản inventory |
| `part_of_speech` | Từ loại |
| `source_occurrences` | Vị trí xuất hiện trong corpus |

### 4.3. Điều kiện về sense definition

Kết quả CST là:

> **conditional on correct sense identification**

Nếu:

```text
definition_review_status = UNVERIFIED
```

thì CST vẫn có thể chạy, nhưng đầu ra phải gắn:

```text
SENSE_DEFINITION_UNVERIFIED
```

và Global Validator không được dùng CST một mình để auto-approve.

---

## 5. Kiến trúc tổng thể

```text
Term + Candidate + sense_id + scope_id
                    ↓
             Context Selector
       ┌────────────┴────────────┐
       ↓                         ↓
5 same-sense contexts     1–2 contrastive contexts
       ↓                         ↓
 Trial Translator          Sense Boundary Test
       ↓                         ↓
Trial Translation          OUT_OF_SCOPE /
 Quality Gate              SENSE_BOUNDARY_DETECTED
       ↓
 Context Judge
       ↓
Schema Validator
       ↓
Code computes:
- raw score
- PASS / MINOR / FAIL
- local hard flags
       ↓
Same-Sense Aggregator
       ↓
Optional second Judge
       ↓
Contextual Evidence Package
       ↓
Global Terminology Validator
```

---

## 6. Hai nhóm context

### 6.1. Same-sense evaluation contexts

Các context thuộc đúng `sense_id` đang xét.

Chúng được dùng để tính contextual evidence.

Ví dụ:

```text
Term: inference
Sense: execution of a trained ML model
Candidate: suy luận
```

Context phù hợp:

```text
During inference, model parameters remain fixed.
The model performs inference on unseen samples.
Inference latency was reduced significantly.
The inference server receives batched requests.
```

### 6.2. Contrastive contexts

Các context thuộc nghĩa khác hoặc ở ranh giới nghĩa.

Ví dụ:

```text
Statistical inference estimates properties of a population.
Logical inference derives a conclusion from premises.
```

Contrastive contexts không được cộng vào điểm C của `model_execution`.

Chúng dùng để trả lời:

- ứng viên có nằm ngoài scope không;
- source surface có nhiều sense không;
- inventory hiện tại có cần tách sense không;
- context nào thuộc tail distribution;
- glossary có nguy cơ áp dụng quá rộng không.

### 6.3. Quy tắc

```text
Nếu entry chưa có sense_id rõ:
    khác nghĩa đáng kể
    → SENSE_BOUNDARY_DETECTED
    → đề xuất SPLIT_REQUIRED cho Global Gate Engine

Nếu entry đã có sense_id rõ:
    candidate không phù hợp với sense khác
    → OUT_OF_SCOPE
    → không trừ điểm C
```

---

## 7. Context Selector

### 7.1. Cấu hình mặc định

```text
5 same-sense contexts
1–2 contrastive contexts
```

Mức tối thiểu:

```text
3 same-sense contexts
1 contrastive context
```

Nếu ít hơn ba same-sense context hợp lệ:

```text
CONTEXT_EVIDENCE_INSUFFICIENT
```

### 7.2. Năm loại same-sense context

| Mã | Loại | Mục đích |
|---|---|---|
| C1 | Definition / first occurrence | Xác nhận khái niệm |
| C2 | Typical usage | Kiểm tra cách dùng phổ biến |
| C3 | Domain collocation | Kiểm tra cụm ghép chuyên ngành |
| C4 | Syntactic variation | Kiểm tra cấu trúc khác |
| C5 | Same-sense difficult case | Kiểm tra trường hợp khó cùng nghĩa |

Ví dụ:

```text
C1: Inference is the process of generating predictions from a trained model.
C2: The model performs inference on unseen samples.
C3: Inference latency was reduced significantly.
C4: These examples are processed during inference.
C5: The last inference batch contains fewer examples.
```

### 7.3. Selector không được tối ưu theo candidate

Để tránh selection bias:

- chọn context dựa trên source term và sense;
- không dùng điểm trial translation để chọn câu “dễ”;
- không chọn lại context chỉ vì candidate bị điểm thấp;
- chỉ thay context khi source/trial không hợp lệ;
- lưu toàn bộ context bị loại và lý do.

### 7.4. Kiểm tra đa dạng

Code phải xác minh:

- đủ các loại context;
- context cùng sense;
- không trùng block;
- không quá giống nhau;
- không thiếu chủ ngữ/vị ngữ quan trọng;
- không lấy câu bị cắt từ bảng;
- khi cần, kèm một câu trước hoặc sau;
- term span và offset xác định được.

### 7.5. Loại context gần trùng

```text
Nếu similarity(context_i, context_j) > threshold:
    giữ context có provenance hoặc informativeness tốt hơn
    chọn context thay thế
```

Threshold phải được version hóa:

```text
context_dedup_policy_version
```

---

## 8. Trial Translator

### 8.1. Nhiệm vụ

Trial Translator tạo bản dịch thử bắt buộc sử dụng candidate.

Đầu vào:

```json
{
  "source_sentence": "Inference latency was reduced significantly.",
  "source_term": "inference",
  "candidate_translation": "suy luận",
  "sense_id": "model_execution",
  "scope_id": "machine_learning",
  "candidate_policy": {
    "must_use": true,
    "allow_expansion": true
  }
}
```

Đầu ra:

```json
{
  "trial_translation": "Độ trễ suy luận đã được giảm đáng kể.",
  "candidate_surface_used": "suy luận",
  "candidate_usage_confirmed": true,
  "applied_expansion": null
}
```

### 8.2. Quy tắc

Trial Translator phải:

- giữ đủ nội dung nguồn;
- dùng candidate được chỉ định;
- không thay bằng candidate khác;
- không thêm giải thích;
- không thay đổi sense;
- chỉ dùng expansion khi policy cho phép;
- trả JSON theo schema;
- không trả self-score cho Judge.

### 8.3. Tách model roles

Khuyến nghị:

```text
Candidate Generator: model family A
Trial Translator: model family A hoặc B
Context Judge 1: model family B
Context Judge 2: model family C
```

Nếu không thể dùng model family khác nhau, provenance phải ghi:

```text
independence_level = REPEATED_SAME_MODEL
```

Không được mô tả hai lần gọi cùng model là “hai validator hoàn toàn độc lập”.

---

## 9. Trial Translation Quality Gate

### 9.1. Mục đích

Không để lỗi của Trial Translator làm candidate bị trừ oan.

### 9.2. Các trạng thái

```text
VALID
INVALID_CANDIDATE_USAGE
EXTERNAL_TRANSLATION_ERROR
INCOMPLETE_TRANSLATION
ADDED_MEANING
AMBIGUOUS_SOURCE
SCHEMA_INVALID
```

### 9.3. Đầu ra

```json
{
  "trial_status": "VALID",
  "candidate_usage_valid": true,
  "external_translation_error": false,
  "missing_content": false,
  "added_content": false,
  "reason": "Bản dịch thử giữ đủ nghĩa và sử dụng đúng ứng viên."
}
```

### 9.4. Retry policy

```text
VALID
    → Context Judge

INVALID lần 1
    → regenerate đúng một lần

INVALID lần 2
    → loại context
    → chọn context thay thế
```

Không retry vô hạn.

### 9.5. Không tính context không hợp lệ

```text
trial_status != VALID
→ không sinh score
→ không tính vào C
→ lưu reason
```

---

## 10. Context Judge

### 10.1. Nhiệm vụ

Context Judge đánh giá mức phù hợp của candidate trong trial translation.

Judge chỉ trả:

- điểm thành phần;
- cờ lỗi;
- judgeability;
- evidence span;
- reason ngắn;
- variant observation nếu có.

Judge không trả:

```text
total
PASS/MINOR/FAIL
final glossary decision
confidence probability
AUTO_APPROVED
```

---

## 11. Rubric một context

Mỗi context tối đa 10 điểm.

| Tiêu chí | Điểm tối đa | Ý nghĩa |
|---|---:|---|
| Semantic equivalence | 4 | Có cùng khái niệm nguồn không |
| Domain and sense fit | 2 | Có đúng sense và lĩnh vực không |
| Collocation naturalness | 2 | Có tự nhiên trong tiếng Việt chuyên ngành không |
| Grammatical fit | 1 | Có phù hợp cấu trúc không |
| No candidate-induced distortion | 1 | Candidate có gây sai lệch không |
| **Tổng** | **10** | Code tính |

### 11.1. Semantic equivalence: 0–4

| Điểm | Mô tả |
|---:|---|
| 4 | Cùng khái niệm và phạm vi nghĩa |
| 3 | Cùng khái niệm, khác biệt nhỏ |
| 2 | Chỉ đúng một phần hoặc quá rộng/quá hẹp |
| 1 | Gần nghĩa bề mặt nhưng sai khái niệm |
| 0 | Mâu thuẫn hoặc hoàn toàn sai |

### 11.2. Domain and sense fit: 0–2

| Điểm | Mô tả |
|---:|---|
| 2 | Đúng sense và đúng domain |
| 1 | Có thể dùng nhưng còn mơ hồ |
| 0 | Thuộc sense hoặc domain khác |

### 11.3. Collocation naturalness: 0–2

| Điểm | Mô tả |
|---:|---|
| 2 | Tự nhiên và phù hợp cách dùng chuyên ngành |
| 1 | Hiểu được nhưng hơi gượng |
| 0 | Không tự nhiên hoặc hầu như không dùng |

### 11.4. Grammatical fit: 0–1

| Điểm | Mô tả |
|---:|---|
| 1 | Phù hợp từ loại và cấu trúc |
| 0 | Gây sai cấu trúc hoặc cần expansion nhưng không có |

### 11.5. No candidate-induced distortion: 0–1

| Điểm | Mô tả |
|---:|---|
| 1 | Candidate không trực tiếp làm sai lệch |
| 0 | Candidate làm mất, thêm hoặc đổi nghĩa |

Chỉ lỗi do candidate mới làm giảm tiêu chí này.

---

## 12. Judgeability

Các trạng thái:

```text
JUDGEABLE
INSUFFICIENT_CONTEXT
INVALID_SOURCE
INVALID_TRIAL_TRANSLATION
AMBIGUOUS_SENSE
SENSE_DEFINITION_UNCERTAIN
```

Nếu không phải `JUDGEABLE`:

```text
không sinh raw score
không tính vào C
chọn context thay thế khi có thể
```

---

## 13. Schema đầu ra của Context Judge

```json
{
  "context_id": "d2l-ch03-b015-s02",
  "candidate_id": "term-inference-vi-01",
  "judgeability": "JUDGEABLE",

  "scores": {
    "semantic_equivalence": 4,
    "domain_sense_fit": 2,
    "collocation_naturalness": 2,
    "grammatical_fit": 1,
    "no_candidate_induced_distortion": 1
  },

  "flags": {
    "semantic_contradiction": false,
    "wrong_sense": false,
    "candidate_induced_distortion": false,
    "translator_external_error": false,
    "insufficient_context": false
  },

  "evidence": {
    "source_span": "Inference latency",
    "target_span": "Độ trễ suy luận"
  },

  "variant_observation": {
    "surface_used": "suy luận",
    "requires_expansion": false,
    "suggested_expansion": null
  },

  "reason": "Ứng viên biểu thị đúng quá trình suy luận của mô hình và tạo thành collocation tự nhiên."
}
```

---

## 14. Code Validator

### 14.1. Schema checks

Code phải kiểm tra:

- đủ trường;
- điểm đúng phạm vi;
- ID hợp lệ;
- sense không bị Judge đổi;
- candidate không bị thay;
- judgeability hợp lệ;
- cờ lỗi nhất quán;
- reason tồn tại khi có lỗi;
- output parse được;
- model và prompt version tồn tại.

### 14.2. Raw score

```text
raw_score =
semantic_equivalence
+ domain_sense_fit
+ collocation_naturalness
+ grammatical_fit
+ no_candidate_induced_distortion
```

```text
0 ≤ raw_score ≤ 10
```

---

## 15. Context-level hard flags

Hard flags cục bộ:

```text
CONTEXT_SEMANTIC_MISMATCH
CONTEXT_WRONG_SENSE
CONTEXT_CONTRADICTION
CANDIDATE_INDUCED_DISTORTION
INVALID_TRIAL
INSUFFICIENT_CONTEXT
SENSE_DEFINITION_UNCERTAIN
```

Quy tắc:

```text
semantic_equivalence <= 2
    → CONTEXT_SEMANTIC_MISMATCH
    → FAIL

domain_sense_fit == 0
    → CONTEXT_WRONG_SENSE
    → FAIL

semantic_contradiction == true
    → CONTEXT_CONTRADICTION
    → FAIL

candidate_induced_distortion == true
    → CANDIDATE_INDUCED_DISTORTION
    → FAIL
```

CST không tự xử lý:

```text
TARGET_COLLISION
INSUFFICIENT_ATTESTATION
GLOBAL_JUDGE_DISAGREEMENT
REJECTED_CANDIDATE_CONFLICT
```

Các gate này thuộc Global Gate Engine.

---

## 16. PASS / MINOR / FAIL

### PASS

```text
raw_score >= 8
semantic_equivalence >= 3
domain_sense_fit >= 1
không có local hard flag
```

### MINOR

```text
6 <= raw_score <= 7
semantic_equivalence >= 3
domain_sense_fit >= 1
không có local hard flag
```

### FAIL

```text
raw_score <= 5
hoặc có local hard flag blocking
```

Các nhãn này chỉ mô tả **một context**, không phải trạng thái glossary.

---

## 17. Tổng hợp contextual evidence

### 17.1. Không dùng thang `/30`

Phiên bản 2 xuất score chuẩn hóa:

\[
C = \frac{1}{n}\sum_{i=1}^{n}\frac{r_i}{10}
\]

Trong đó:

- \(r_i\): raw score của context thứ \(i\);
- \(n\): số same-sense context hợp lệ.

```text
0 ≤ C ≤ 1
```

Ví dụ:

```text
raw scores = [9, 9, 8, 7, 9]

C = (9 + 9 + 8 + 7 + 9) / 50
  = 0.84
```

### 17.2. Thống kê bắt buộc

```json
{
  "C": 0.84,
  "raw_context_scores": [9, 9, 8, 7, 9],
  "pass_count": 4,
  "minor_count": 1,
  "fail_count": 0,
  "minimum_raw_score": 7,
  "maximum_raw_score": 9,
  "score_range": 2,
  "valid_context_count": 5,
  "invalid_context_count": 0
}
```

### 17.3. Không diễn giải như xác suất

Không được viết:

```text
C = 0.84
→ thuật ngữ đúng 84%
```

C chỉ là:

> normalized contextual support score

---

## 18. Contextual Evidence Status

CST có thể tạo trạng thái cục bộ:

```text
CONTEXT_SUPPORTED
CONTEXT_CONDITIONAL
CONTEXT_UNSUPPORTED
CONTEXT_UNJUDGEABLE
SENSE_BOUNDARY_DETECTED
```

Đây không phải glossary decision.

### 18.1. Heuristic ban đầu

Có thể dùng cho demo:

```text
CONTEXT_SUPPORTED
- C cao
- không FAIL
- không local hard flag
- đủ context

CONTEXT_CONDITIONAL
- có MINOR
- C trung bình
- không blocking flag

CONTEXT_UNSUPPORTED
- có FAIL rõ
- C thấp
- wrong sense hoặc contradiction

CONTEXT_UNJUDGEABLE
- thiếu context hợp lệ
- sense definition chưa đủ
```

Ngưỡng số cụ thể phải được khóa sau calibration.

---

## 19. Calibration

CST không cố định `24/30`, `21/30` hoặc `18/30`.

Validation set dùng để xác định:

- C threshold;
- minimum pass count;
- maximum allowed MINOR;
- sensitivity khi một nhãn bị đảo;
- precision/coverage của `CONTEXT_SUPPORTED`;
- mức ổn định khi đổi Judge;
- mức độ tương quan với human labels.

Nên báo cáo:

- precision–coverage curve;
- threshold sensitivity;
- decision flip rate khi thay một nhãn;
- bootstrap confidence intervals;
- inter-rater agreement;
- C distribution theo label người.

---

## 20. Contrastive Sense Test

### 20.1. Đầu ra

```json
{
  "contrastive_context_id": "stats-ch01-b04-s02",
  "tested_sense_id": "statistical_inference",
  "candidate_translation": "suy luận",
  "result": "OUT_OF_SCOPE",
  "reason": "Context thuộc statistical inference, khác với model execution."
}
```

Các nhãn:

```text
APPLICABLE_TO_OTHER_SENSE
OUT_OF_SCOPE
SEPARATE_SENSE_REQUIRED
AMBIGUOUS
```

### 20.2. Vai trò

- không cộng vào C;
- không tự quyết glossary;
- tạo đề xuất cho Global Gate Engine;
- bổ sung `scope_note`;
- bổ sung forbidden sense list;
- tạo dữ liệu huấn luyện hoặc calibration cho TAC.

---

## 21. Judge thứ hai có điều kiện

Chỉ gọi Judge 2 khi:

```text
C gần threshold
có MINOR hoặc FAIL
Judge 1 báo ambiguity
hai candidates gần nhau
độ phân tán cao
sense boundary không rõ
```

Chính sách:

```text
PASS vs PASS
    → PASS

PASS vs MINOR
    → conservative MINOR

PASS vs FAIL
    → GLOBAL HUMAN_REVIEW FLAG

MINOR vs FAIL
    → GLOBAL HUMAN_REVIEW FLAG

FAIL vs FAIL
    → FAIL
```

Provenance phải ghi:

```json
{
  "judge_1_model_family": "family-b",
  "judge_2_model_family": "family-c",
  "independence_level": "CROSS_FAMILY"
}
```

---

## 22. Pairwise tie-breaker

Pairwise chỉ dùng khi hai candidates gần nhau.

Đầu ra:

```text
CONTEXTUAL_PREFERENCE_A
CONTEXTUAL_PREFERENCE_B
TIE
```

Không được trả:

```text
FINAL_GLOSSARY_WINNER
```

Vì Attestation Evidence và Global Gates có thể thay đổi quyết định cuối.

---

## 23. Allowed variants và application notes

CST có thể quan sát:

- canonical form;
- expansion hợp lệ;
- variant hình thái;
- variant cần review;
- variant gây sai nghĩa.

Ví dụ:

```json
{
  "canonical_target": "suy luận",
  "allowed_variants": [
    {
      "surface": "quá trình suy luận",
      "status": "OBSERVED_VALID",
      "context_ids": ["ctx-01", "ctx-04"]
    },
    {
      "surface": "bước suy luận",
      "status": "PROPOSED",
      "context_ids": ["ctx-05"]
    }
  ],
  "disallowed_variants": [
    {
      "surface": "suy diễn logic",
      "reason": "Wrong sense for model execution"
    }
  ],
  "application_notes": [
    {
      "condition": "when referring to the process as a countable stage",
      "recommended_form": "bước suy luận"
    }
  ]
}
```

Variant do Judge đề xuất không tự động được seal.

---

## 24. Certificate Support Set cho TAC

CST phải xuất support set:

```json
{
  "certificate_support_set": {
    "validation_contexts": [
      {
        "context_id": "d2l-ch03-b015-s02",
        "context_type": "domain_collocation",
        "raw_score": 9,
        "label": "PASS",
        "embedding_ref": "vec://term-inference/ctx-01"
      }
    ],
    "embedding_model_version": "multilingual-e5-v1",
    "context_centroid_ref": "vec://term-inference/centroid-v1",
    "nearest_context_policy_version": "ood-policy-v1",
    "support_set_version": "support-v1"
  }
}
```

TAC dùng support set để:

- tính nearest-context similarity;
- phát hiện occurrence OOD;
- chỉ gọi sense classifier khi cần;
- đo biên hiệu lực của certificate.

---

## 25. Hợp đồng đầu ra CST v2

```json
{
  "candidate_id": "term-inference-vi-01",
  "source_term": "inference",
  "candidate_translation": "suy luận",
  "sense_id": "model_execution",
  "scope_id": "machine_learning",

  "sense_contract": {
    "definition_en": "The process in which a trained model produces outputs for new inputs.",
    "definition_review_status": "VERIFIED",
    "sense_inventory_version": "sense-v2"
  },

  "contextual_evidence": {
    "C": 0.84,
    "raw_context_scores": [9, 9, 8, 7, 9],
    "pass_count": 4,
    "minor_count": 1,
    "fail_count": 0,
    "minimum_raw_score": 7,
    "maximum_raw_score": 9,
    "score_range": 2,
    "status": "CONTEXT_SUPPORTED"
  },

  "context_flags": [],

  "sense_boundary_observations": [
    {
      "contrastive_sense_id": "statistical_inference",
      "result": "OUT_OF_SCOPE"
    }
  ],

  "application_contract": {
    "canonical_target": "suy luận",
    "allowed_variants": [
      {
        "surface": "quá trình suy luận",
        "status": "OBSERVED_VALID"
      },
      {
        "surface": "bước suy luận",
        "status": "PROPOSED"
      }
    ],
    "disallowed_variants": [
      {
        "surface": "suy diễn logic",
        "reason": "Wrong sense"
      }
    ]
  },

  "certificate_support_set": {
    "validation_context_ids": [
      "d2l-ch03-b015-s02",
      "d2l-ch03-b021-s01"
    ],
    "embedding_model_version": "embedding-v1",
    "support_set_version": "support-v1",
    "ood_policy_version": "ood-v1"
  },

  "recommendation_to_global_validator": "ELIGIBLE_FOR_COMBINATION",
  "final_glossary_decision": null,

  "provenance": {
    "cst_run_id": "cst-2026-001",
    "rubric_version": "cst-rubric-v2",
    "aggregation_policy_version": "cst-aggregate-v2",
    "context_selector_version": "selector-v2",
    "trial_prompt_hash": "sha256:...",
    "judge_prompt_hash": "sha256:...",
    "source_hashes": ["sha256:..."],
    "model_ids": ["..."]
  }
}
```

Điểm bắt buộc:

```json
"final_glossary_decision": null
```

---

## 26. Pseudocode

```python
def run_context_substitution_test(candidate, corpus):
    validate_candidate_contract(candidate)

    if candidate.sense_contract.definition_review_status == "INVALID":
        return build_unjudgeable_result(
            candidate,
            flag="SENSE_DEFINITION_INVALID"
        )

    same_sense_contexts = select_same_sense_contexts(
        candidate=candidate,
        corpus=corpus,
        required_types=[
            "definition",
            "typical_usage",
            "domain_collocation",
            "syntactic_variation",
            "same_sense_difficult"
        ]
    )

    contrastive_contexts = select_contrastive_contexts(
        candidate=candidate,
        corpus=corpus,
        max_items=2
    )

    results = []
    rejected_contexts = []

    for context in same_sense_contexts:
        trial = generate_trial_translation(
            candidate=candidate,
            context=context
        )

        gate = validate_trial_translation(
            candidate=candidate,
            context=context,
            trial=trial
        )

        if gate.status != "VALID":
            trial = regenerate_trial_once(
                candidate=candidate,
                context=context
            )
            gate = validate_trial_translation(
                candidate=candidate,
                context=context,
                trial=trial
            )

        if gate.status != "VALID":
            rejected_contexts.append({
                "context_id": context.id,
                "reason": gate.status
            })
            continue

        judge_1 = run_context_judge(
            candidate=candidate,
            context=context,
            trial=trial,
            judge_slot=1
        )

        judge_1 = validate_judge_schema(judge_1)

        if judge_1.judgeability != "JUDGEABLE":
            rejected_contexts.append({
                "context_id": context.id,
                "reason": judge_1.judgeability
            })
            continue

        raw_score = compute_raw_score(judge_1.scores)

        label, local_flags = apply_context_policy(
            raw_score=raw_score,
            scores=judge_1.scores,
            flags=judge_1.flags
        )

        result = {
            "context_id": context.id,
            "raw_score": raw_score,
            "label": label,
            "local_flags": local_flags,
            "judge_1": judge_1
        }

        if needs_second_judge(result):
            judge_2 = run_context_judge(
                candidate=candidate,
                context=context,
                trial=trial,
                judge_slot=2
            )
            result = reconcile_judges(
                result=result,
                judge_2=validate_judge_schema(judge_2)
            )

        results.append(result)

    if len(results) < 3:
        return build_context_evidence_package(
            candidate=candidate,
            status="CONTEXT_UNJUDGEABLE",
            results=results,
            rejected_contexts=rejected_contexts,
            final_glossary_decision=None
        )

    contextual_summary = aggregate_contextual_evidence(results)

    contrastive_summary = run_contrastive_sense_tests(
        candidate=candidate,
        contexts=contrastive_contexts
    )

    variants = extract_variant_observations(results)

    support_set = build_certificate_support_set(
        candidate=candidate,
        valid_results=results
    )

    local_status = assign_calibrated_context_status(
        contextual_summary=contextual_summary,
        contrastive_summary=contrastive_summary,
        threshold_version="context-threshold-v2"
    )

    return build_context_evidence_package(
        candidate=candidate,
        contextual_summary=contextual_summary,
        contrastive_summary=contrastive_summary,
        variants=variants,
        support_set=support_set,
        local_status=local_status,
        recommendation_to_global_validator=make_global_recommendation(
            contextual_summary,
            contrastive_summary
        ),
        final_glossary_decision=None
    )
```

---

## 27. Ví dụ

### 27.1. Input

```text
Source term: inference
Candidate: suy luận
Sense: model execution
Scope: machine learning
```

### 27.2. Same-sense results

| Context | Raw score | Label |
|---|---:|---|
| Definition | 9 | PASS |
| Typical usage | 9 | PASS |
| Domain collocation | 8 | PASS |
| Syntactic variation | 7 | MINOR |
| Same-sense difficult | 9 | PASS |

### 27.3. Aggregate

```text
C = (9 + 9 + 8 + 7 + 9) / 50
  = 0.84
```

### 27.4. Contrastive result

```text
statistical inference
→ OUT_OF_SCOPE
```

### 27.5. Output cục bộ

```json
{
  "C": 0.84,
  "pass_count": 4,
  "minor_count": 1,
  "fail_count": 0,
  "contextual_evidence_status": "CONTEXT_SUPPORTED",
  "sense_boundary": "OUT_OF_SCOPE_FOR_STATISTICAL_INFERENCE",
  "recommendation_to_global_validator": "ELIGIBLE_FOR_COMBINATION",
  "final_glossary_decision": null
}
```

---

## 28. Kiểm thử CST

### 28.1. Gold test set

Tạo 50–100 trường hợp:

- candidate đúng;
- candidate sai sense;
- candidate đúng nhưng gượng;
- candidate quá rộng;
- candidate quá hẹp;
- trial translation lỗi ngoài term;
- context thiếu dữ liệu;
- sense definition sai;
- contrastive context khác sense;
- candidate cần expansion;
- candidate gây distortion.

### 28.2. Human labels

Context-level:

```text
PASS
MINOR
FAIL
NOT_JUDGEABLE
```

Sense-level:

```text
SAME_SENSE
OUT_OF_SCOPE
SPLIT_REQUIRED
AMBIGUOUS
```

Variant-level:

```text
VALID_VARIANT
CONDITIONAL_VARIANT
INVALID_VARIANT
```

### 28.3. Metrics

- accuracy PASS/MINOR/FAIL;
- precision của FAIL;
- recall phát hiện wrong sense;
- judge–human agreement;
- Cohen’s kappa giữa Judge;
- tỷ lệ lỗi Translator bị gán nhầm cho candidate;
- decision flip rate khi đổi một nhãn;
- stability qua nhiều lần chạy;
- cost per candidate;
- context status precision–coverage;
- calibration error nếu dùng probability model;
- invalid-context replacement rate.

---

## 29. MVP

Phiên bản đầu cần:

1. `sense_id`, `scope_id`, `definition_en`;
2. năm same-sense context;
3. một contrastive context;
4. Trial Translator;
5. Trial Translation Quality Gate;
6. một Context Judge;
7. rubric 10 điểm;
8. code tự tính raw score;
9. code tự gắn PASS/MINOR/FAIL;
10. local hard flags;
11. normalized C trong [0,1];
12. provenance;
13. certificate support set;
14. final glossary decision luôn là `null`.

Chưa cần:

- pairwise cho mọi candidate;
- hai Judge cho toàn bộ dữ liệu;
- weighted context types;
- probability đúng;
- automatic variant sealing;
- TAC runtime trong cùng module;
- web/corpus retrieval trong CST.

---

## 30. Nguyên tắc cốt lõi

1. Chỉ same-sense context được tính C.
2. Contrastive context dùng kiểm tra scope.
3. CST không quyết định glossary cuối.
4. CST không dùng `/30` như trọng số cố định.
5. C nằm trong [0,1] và không phải xác suất.
6. Lỗi Trial Translator không làm candidate bị trừ oan.
7. LLM không trả final decision.
8. Code tính điểm, nhãn và hard flags.
9. Sai nghĩa không được naturalness bù lại.
10. Context thiếu dữ liệu phải được từ chối chấm.
11. Sense definition phải có provenance.
12. Judge independence phải được mô tả đúng.
13. Allowed variants phải có trạng thái.
14. Support set phải được xuất cho TAC.
15. Mọi kết quả phải versioned và auditable.

---

## 31. Kết luận

Context Substitution Test phiên bản 2 là một thành phần tạo bằng chứng ngữ cảnh có cấu trúc.

Nó không chứng minh candidate đúng tuyệt đối.

Nó cung cấp:

```text
contextual support score C
PASS/MINOR/FAIL distribution
local hard flags
sense-boundary observations
allowed-variant observations
certificate support set
provenance
recommendation to global validator
```

Kết quả chỉ có hiệu lực trong:

```text
sense_id + scope_id + support_set_version
```

Terminology Validator toàn cục kết hợp CST với Attestation Evidence và Global Gates để ra quyết định glossary cuối.

TAC sử dụng support set của CST để kiểm tra liệu occurrence mới có còn nằm trong vùng ngữ cảnh đã được chứng nhận hay không.
