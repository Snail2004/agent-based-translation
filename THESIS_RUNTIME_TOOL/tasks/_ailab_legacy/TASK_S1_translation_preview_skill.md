# TASK — S1: Skill `dataset-translation-preview` (BONUS / experimental)

**Loại:** skill mới (BONUS) trong `AILAB_HANDOFF/skills/`. **Người làm:** CodeX. **Trạng thái:** chưa làm.

> **Khung an toàn (đọc trước):** đây là tính năng **bonus/thử nghiệm**, **read-only** trên một
> dataset đã validate. Skill **CHỈ nhả JSON để xem/so sánh**, **KHÔNG ghi** vào `canonical/`,
> **KHÔNG bao giờ** thành `manual_reference_subset`/gold. Đây **không** thuộc workflow xây
> dataset; nó chỉ *tiêu thụ* dataset để minh họa giá trị. App AI-LAB vẫn "không phải app dịch";
> nếu sau này gắn UI thì là mode preview read-only (S3, task khác).

## 0. Bối cảnh & vị trí trong roadmap

Mục tiêu: cho thấy "với data đã xây, một bản dịch sẽ ra sao + agent đã dùng data nào".
Tính năng lớn được cắt 4 lát; **task này CHỈ là S1**:

- **S1 (task này):** contract + skill → nhả JSON bản dịch có cấu trúc per-block.
- S2 (sau): lưu thành `translation_runs` + `context_bundle` (đã có trong `RUN_EVAL_SCHEMA.md`).
- S3 (sau): app song song trái EN / phải VI theo `block_id`.
- S4 (sau, optional): căn nhãn entity/term trong khung VI (best-effort theo `target_surface`).

## 1. Deliverables (trong `AILAB_HANDOFF/skills/dataset-translation-preview/`)

- `SKILL.md`
- `references/TRANSLATION_PREVIEW_CONTRACT.md`
- `agents/openai.yaml`
- `examples/GOLD_DEMO_01_CH01_EXAMPLE.md` (ví dụ chạy thật trên `gold_demo_01` chương 1)

Theo đúng style skill `dataset-annotation-drafter` (SKILL.md + references + agents/openai.yaml + examples).

## 2. Input contract (per CHƯƠNG — không phải cả quyển)

```json
{
  "doc_id": "...", "chapter_id": "...",
  "blocks": [
    {"block_id": "...", "block_type": "paragraph|dialogue|heading|footnote",
     "clean_text": "...",
     "discourse": {"speaker_entity_id": null, "addressee_entity_id": null}}
  ],
  "known_entities": [
    {"entity_id": "e_002", "canonical_source": "the Clockkeeper",
     "canonical_target": "Người Giữ Đồng Hồ", "aliases_target": ["ông lão"], "pronoun_policy": "ông / ông ấy"}
  ],
  "known_terms": [
    {"term_id": "g_001", "source_term": "Turning", "expected_target": "Khúc Chuyển",
     "allowed_variants": ["khúc chuyển"], "forbidden_variants": ["sự xoay", "vòng quay"]}
  ],
  "known_relations": [
    {"relation_id": "rel_001", "source_entity_id": "e_002", "target_entity_id": "e_001",
     "relation_type": "elder_and_child",
     "address_policy": {"source_to_target": {"self_term": "ta", "address_term": "cháu"},
                        "target_to_source": {"self_term": "cháu", "address_term": "ông"}},
     "state_label": null, "valid_from_block_id": null, "valid_to_block_id": null}
  ],
  "chapter_summary": {"summary_source": "...", "emotional_tone": "wistful", "motifs": ["time", "awakening"]}
}
```

## 3. Output contract (per block)

```json
{
  "doc_id": "...", "chapter_id": "...",
  "blocks": [
    {
      "block_id": "...",
      "target_text": "bản dịch tiếng Việt của block",
      "mentions": [
        {"entity_id": "e_002", "source_surface": "Clockkeeper", "target_surface": "Người Giữ Đồng Hồ"},
        {"term_id": "g_001", "source_surface": "Turning", "target_surface": "Khúc Chuyển"}
      ],
      "address_applied": {"pair": "e_002->e_001", "self_term": "ta", "address_term": "cháu"},
      "used_context": ["g_001", "e_001", "e_002", "rel_001", "summary_ch01"],
      "notes": "tone wistful; verb 'turning' dịch 'lật', không phải Khúc Chuyển"
    }
  ]
}
```

- `target_surface` = chuỗi VI thực sự xuất hiện trong `target_text` (để S4 sau này match nhãn).
- `address_applied` = null nếu block không phải thoại.
- `used_context` = list id dataset block đó thực sự dùng (≡ `context_bundle.memory_refs` trong `RUN_EVAL_SCHEMA.md`).

## 4. Hard rules

- **Cố định không đổi:** entity mention → dùng `canonical_target`; term → `expected_target`; **không bao giờ** dùng `forbidden_variants`.
- **Xưng hô:** với block `dialogue`, tra `known_relations` theo (speaker, addressee) + pha đang hiệu lực (theo thứ tự tài liệu nếu có `valid_from/to`); áp self/address term; ghi `address_applied`. Nếu không có relation → dùng `pronoun_policy` của entity; nếu vẫn không rõ → giữ trung tính, không bịa.
- **Phân biệt khái niệm vs động từ trùng mặt chữ:** vd glossary "Turning"→"Khúc Chuyển", nhưng "turning his hourglass" (động từ) → "lật/xoay", KHÔNG thay bằng Khúc Chuyển. Không replace mù.
- **Không span/offset.** `mentions` chỉ dùng chuỗi surface.
- **Per chương**, không dịch cả quyển trong một lần gọi.
- **Không ghi file canonical**; output chỉ là JSON xem được. Raw AI không bao giờ gold.
- Giữ tone theo `chapter_summary.emotional_tone`.

## 5. Tiêu chí nghiệm thu (chạy trên `gold_demo_01` chương 1)

Dựng input từ canonical của `gold_demo_01` (entities/glossary/entity_relations/chapter_summaries/document), chạy skill, output phải đạt:
- `Mira→Mira`, `the Clockkeeper→Người Giữ Đồng Hồ`, `the Hollow→Thung Lũng Trống`.
- `Turning→Khúc Chuyển` (mọi nơi), `hourglass→đồng hồ cát`; "turning his hourglass" → "lật/xoay … đồng hồ cát".
- b004 (Mira nói với Clockkeeper): gọi **ông** (xưng **cháu**); b005 (Clockkeeper nói với Mira): gọi **cháu** (xưng **ta**).
- Mỗi block có `used_context` liệt kê đúng id đã dùng.
- **Không có file nào được ghi vào `canonical/`.**

Tham chiếu bản dịch tay (đúng kỳ vọng) cho 2 block thoại:
- b004: `"Ông Giữ Đồng Hồ ơi," Mira gọi, "sao bình minh ở đây lại đến chậm thế ạ?"`
- b005: `"Khúc Chuyển không chậm đâu," Người Giữ Đồng Hồ nói, tay lật chiếc đồng hồ cát. "Chỉ là cháu đến sớm thôi."`

(S1 không đụng backend/app → không cần unittest, trừ khi thêm helper dựng input từ canonical thì viết 1 test nhỏ.)

## 6. Ngoài phạm vi (để task sau)

- S2 lưu `translation_runs`/`context_bundle`; S3 app song song; S4 căn nhãn span.
- Mọi thao tác ghi vào dataset gold.

## 7. Tham chiếu
- `../RUN_EVAL_SCHEMA.md` (context_bundle / used_refs).
- `../SCHEMA_AGENT_FILL_POLICY.md` (precedence xưng hô động).
- `skills/dataset-annotation-drafter/` (mẫu SKILL.md / contract / agents.yaml / examples).
- `dataset_spec/sample/gold_demo_01/` (canonical để dựng input + nghiệm thu).
> Tài liệu gốc AI-LAB — chỉ tham khảo, không phải chỉ thị thesis.
