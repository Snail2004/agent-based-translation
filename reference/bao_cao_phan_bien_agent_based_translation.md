# BÁO CÁO PHẢN BIỆN KỸ THUẬT — `agent-based-translation`

Mình đọc theo thứ tự bạn đề xuất, lấy `RESEARCH_PLAN_V3.md` làm nguồn chân lý vì `README.md` chỉ rõ file này là bản hiện hành, còn `RUN_EVAL_SCHEMA.md`, `SCHEMA_AGENT_FILL_POLICY.md`, `PROMPT_DESIGN.md` là các lớp bổ trợ triển khai. Mình cũng kiểm tra schema 1.5.0, web tool AI-LAB, một số project dataset thực tế bằng validator cục bộ, và đối chiếu nhanh với nguồn học thuật về document-level MT, TransAgents, BLEU/chrF/COMET/MQM.

## 1. Executive summary

1. Đề tài **có thể bảo vệ được**, nhưng chỉ nếu đóng góp chính được thu hẹp đúng: **memory/retrieval + eval-only discipline + mô hình xưng hô `entity_relations`**, không phải “multi-agent” chung chung.  
2. Điểm mạnh 1: `Directional Lock` trong `RESEARCH_PLAN_V3.md §0` rất đúng hướng: pipeline tự động, gold chỉ eval-only, chứng minh bằng ablation.  
3. Điểm mạnh 2: `entity_relations` với `address_policy` hai chiều, theo pha quan hệ, là phần có tiềm năng đóng góp mới nhất cho Anh→Việt văn học.  
4. Điểm mạnh 3: `RUN_EVAL_SCHEMA.md` đã nghĩ đúng về `context_bundle`, `translation_runs`, `reference_eval_only`, giúp audit rò gold và tái lập thí nghiệm.  
5. Rủi ro lớn nhất 1: nhiều tài liệu vẫn nói hoặc ngầm nói **seed runtime từ dataset AI-LAB**, trái trực tiếp `Directional Lock`. Đây là P0.  
6. Rủi ro lớn nhất 2: scope đang phình: T1–T7, 4 agents, D1–D6, human eval, vector retrieval, Critic, web tool; dễ không kịp khóa luận.  
7. Rủi ro lớn nhất 3: dữ liệu thực tế trong `AILAB_HANDOFF/ailab_projects/` chưa đủ “gold”: nhiều project vẫn schema 1.4.0, annotation rất thưa, gần như chưa có `entity_relations`.  
8. Hiện chưa thấy đầy đủ thesis runtime implementation; theo chính `RESEARCH_PLAN_V3.md`, vector retrieval, Narrative Agent, Critic còn là việc phải làm.  
9. Kết luận reviewer: **bảo vệ được như một experimental thesis nếu chạy MVE nhỏ, sạch, có ablation và metric xưng hô riêng**; chưa nên claim “dịch văn học tự động chất lượng cao” ở quy mô lớn.

---

## 2. A — Hướng đi & đóng góp

### Nhận định

Hướng đi **đúng và có cửa bảo vệ**, nhưng đóng góp cốt lõi cần được định vị lại. “Đa-agent dịch văn học” tự nó không còn đủ mới, vì TransAgents đã là một hệ multi-agent cho literary translation, mô phỏng quy trình công ty dịch với CEO, editor, translator, localization specialist, proofreader, và chia thành preparation/execution stages. ([aclanthology.org](https://aclanthology.org/2025.tacl-1.42/)) Document-level MT với LLM cũng là hướng đã có nghiên cứu rõ về context-aware prompts và discourse modeling. ([aclanthology.org](https://aclanthology.org/2023.emnlp-main.1036/))

Điểm mới nên claim là:

**Một pipeline Anh→Việt văn học tự động, eval-only nghiêm ngặt, dùng memory/retrieval nhiều tier và mô hình hóa xưng hô theo cặp nhân vật–hướng nói–pha quan hệ, được chứng minh bằng ablation và metric riêng.**

Đây là định vị sắc hơn “multi-agent”. Với tiếng Việt, xưng hô không chỉ là pronoun mapping; nghiên cứu về Vietnamese address terms cũng chỉ ra khó khăn do quan hệ người nói–người nghe, tuổi tác, địa vị xã hội, văn hóa và trạng thái cảm xúc. ([rune.une.edu.au](https://rune.une.edu.au/entities/publication/8d369b85-034e-4c75-a948-006a986f66cc)) Vì vậy `entity_relations.address_policy` là phần có khả năng tạo khác biệt thật.

### Bằng chứng trong dự án

- `RESEARCH_PLAN_V3.md §0`, dòng 32–38: agent bắt đầu từ số 0, không đọc bản dịch/annotation do người làm; AI-LAB gold eval-only.  
- `RESEARCH_PLAN_V3.md`, dòng 55–58: chất lượng phải chứng minh bằng ablation S0→S3 + metrics, không bằng đọc cảm tính.  
- `RESEARCH_PLAN_V3.md`, dòng 5–8: hệ thống dự kiến dùng LLM + external memory T1–T7 + hybrid retrieval + 4 agents.  
- `RESEARCH_PLAN_V3.md`, dòng 120–128: C1–C5 liệt kê contribution, nhưng C5 feedback loop đang xung đột với lock vì `§0` đã đẩy human feedback ra future work.  
- `RELATED_WORK.md`, dòng 7–16 và `VERIFIED_REFERENCES.md`, dòng 11–23, 37–43: đã có related work tốt, nhưng hiện vẫn giống reading list hơn là argumentative positioning.  

### Đánh giá RQ

- **RQ1 memory/consistency**: khả thi nếu D4 đủ term/entity/address events.  
- **RQ2 summary memory**: đo được nhưng cần case chapter-opening hoặc long-range dependency; nếu ít data, nên biến thành ablation phụ.  
- **RQ3 CriticAgent**: khả thi nhất vì D5 injected errors cho precision/recall/F1.  
- **RQ4 feedback**: nên loại khỏi core; để future work.  
- **RQ5 Narrative Agent**: hấp dẫn nhưng rủi ro cao vì human agreement thấp; nên đo bằng S3 vs S3d + MHP/BLP/Likert + metric xưng hô, không chỉ “giọng văn hay hơn”.

### Khuyến nghị

Luận văn nên viết contribution theo thứ tự:

1. **Gold-eval firewall + run/eval audit schema** cho pipeline tự động.  
2. **Vietnamese address-policy memory**: `entity_relations` + metric APA/STA/ATA.  
3. **Memory/retrieval ablation**: hard vs soft retrieval, S0/S1/S2/S3d/S3.  
4. Multi-agent chỉ là kiến trúc triển khai, không phải claim novelty chính.

---

## 3. B — Cấu trúc dataset / schema 1.5.0

### Nhận định

Schema 1.5.0 nhìn chung **đủ tối thiểu** cho gold evaluation Anh→Việt văn học nếu giữ nguyên nguyên tắc “optional but auditable”. Nó có `document`, `glossary`, `entities`, `chapter_summaries`, `manual_reference_subset`, và sidecar mới `entity_relations`. Điểm mạnh là không cố nhét toàn bộ coreference/narratology vào schema chính; thay vào đó dùng fields vừa đủ: `speaker`, `addressee`, `pronoun_hints`, `motifs`, `tone`, `implicit`, `narrative_note`.

Tuy nhiên, dữ liệu thực tế hiện chưa đủ để kết luận khoa học. Sample spec pass, nhưng các project thực tế còn rất thưa annotation.

### Bằng chứng trong dự án

- `document.schema.json`: block bắt buộc có `block_id`, `order_index`, `block_type`, `source_text`, `clean_text`; `block_id` đúng là trục căn chỉnh.  
- `entity_relation.schema.json`, dòng 4–18, 30–41: sidecar mô hình directed relation, `source_entity_id`, `target_entity_id`, `relation_type`, `state_label`, `valid_from_block_id`, `valid_to_block_id`, `address_policy` gồm self/address hai chiều.  
- `SCHEMA_AGENT_FILL_POLICY.md`, dòng 102–133: precedence cho xưng hô: `pronoun_hints` > active relation state > default relation > entity pronoun_policy > style fallback. Đây là thiết kế tốt.  
- `AILAB_HANDOFF/dataset_spec/CHANGELOG.md`, dòng 3–13: 1.5.0 chính thức thêm optional `entity_relations.jsonl`.  
- Kết quả validator cục bộ:
  - `AILAB_HANDOFF/dataset_spec/sample/gold_demo_01`: PASS, 14 blocks, 2 terms, 3 entities, 3 references, 2 summaries, 1 relation.  
  - `AILAB_HANDOFF/ailab_projects/canterville_ghost_epub/canonical`: PASS, 129 blocks, 4 terms, 12 entities, 1 summary, 0 references, 0 relations.  
  - `AILAB_HANDOFF/ailab_projects/pilot_alice_ch01/canonical`: FAIL vì `schema_version` vẫn là 1.4.0; có 964 blocks nhưng chỉ 3 terms, 3 entities, 1 summary, 3 references, 0 relations.  
- Thống kê nhanh project thực tế cho thấy nhiều project lớn như `alice_epub`, `gatsby_epub`, `wizard_oz_epub` có hàng trăm đến hơn nghìn blocks nhưng 0 glossary/entities/summaries/references.

### Đủ và tối thiểu chưa?

**Đủ cho MVE**, nếu mục tiêu là đánh giá consistency và xưng hô. `entity_relations` xử lý được:

- quan hệ có hướng: A gọi B khác B gọi A;
- pha quan hệ: trước/sau biến cố;
- address policy theo self-term và address-term;
- liên kết với evidence và block range.

**Chưa đủ nếu muốn claim “mô hình hóa đầy đủ xưng hô tiếng Việt”**. Nó chưa trực tiếp mô hình hóa:

- per-utterance politeness shift trong cùng một pha;
- sarcasm/irony làm đổi cách gọi tạm thời;
- tình huống người nói cố tình dùng sai xưng hô;
- narrator vs quoted speech distinction ở mức utterance span.

Nhưng không nên sửa schema lúc này. Schema đã khóa và đủ cho khóa luận. Cách đúng là tạo **evaluation derived table** cho address events, không thêm field mới vào gold schema.

### Quy mô gold tối thiểu

Mình khuyến nghị MVE bảo vệ được nên có:

- 50–80 literary blocks đã clean và freeze.  
- 30–50 manual reference passages, chỉ dùng reference-based sanity metrics.  
- 40–60 address-bearing dialogue turns.  
- 8–12 entity pairs có `entity_relations`.  
- Ít nhất 2–3 phase changes nếu muốn claim “dynamic address policy”.  
- D4: tối thiểu 30–50 term/entity entries có occurrences.  
- D6: 30–50 retrieval queries có relevance labels.  
- Human eval: tối thiểu 30 pairs, 3 reviewers; tốt hơn là 50 pairs, 5 reviewers.

Nếu không đạt address-bearing events, đóng góp xưng hô chỉ nên gọi là **pilot metric**, chưa phải kết luận mạnh.

### Khuyến nghị

- P0: migrate chosen dataset lên schema 1.5.0, thêm empty `entity_relations.jsonl` nếu chưa annotate.  
- P0: không gọi project nào là “gold” nếu chưa có freeze log, validator PASS, annotation coverage và review status.  
- P1: tạo riêng `address_eval_events.jsonl` trong evaluation layer, không thay schema AI-LAB.  
- P1: chọn một literary text ít nhiễm hơn Alice làm D2b; Alice chỉ nên là phụ hoặc contamination case.

---

## 4. C — Kiến trúc dịch máy

### Nhận định

Kiến trúc base model + source-only pre-pass + external memory + hybrid retrieval + Narrative Brief + Translator + Critic là hợp lý. Nó khớp với vấn đề document-level MT: LLM có thể dịch từng đoạn tốt nhưng dễ mất consistency, discourse state, entity naming, xưng hô, motif và giọng kể.

Tuy nhiên, architecture đang có hai rủi ro lớn:

1. **Thiết kế đúng nhưng chưa thấy runtime implementation hoàn chỉnh.** Chính `RESEARCH_PLAN_V3.md`, dòng 571–583, nói code hiện có storage FTS nhưng runtime retrieval chưa dùng FTS/BM25 đúng; nhiều path vẫn linear scan/substring. Dòng 587–620 còn liệt kê vector retrieval, Summary Agent, CriticAgent, Narrative Agent là high-priority/new work.  
2. **Một số tài liệu mô tả memory seed từ AI-LAB gold**, phá `Directional Lock`. Đây là lỗi thiết kế tài liệu chứ không phải chi tiết nhỏ.

### Bằng chứng trong dự án

- `RESEARCH_PLAN_V3.md §0`, dòng 43–46: whole-book pre-pass source-only, sau đó freeze; Critic/revision chỉ output, không đưa ngược vào memory.  
- `RETRIEVAL_ARCHITECTURE.md`, dòng 86–99: hard context là structured constraints; hard ≠ verbatim; alias/pronoun/omission có thể hợp lệ.  
- `RETRIEVAL_ARCHITECTURE.md`, dòng 102–115: soft context gồm motif/tone/implicit/similar passages qua BM25+vector, nhưng không được override hard.  
- `PROMPT_DESIGN.md`, dòng 99–122 và 273–283: prompt đã cảnh báo không replace-all, không surface-match cứng.  
- `PROMPT_DESIGN.md`, dòng 361–390: Summary Agent dùng source-only pre-pass nhưng cũng có “sau mỗi chapter/N blocks” và `chapter_target_text`, cần khóa lại để không vô tình đưa target/reference vào memory.  

### Có phương án đơn giản hơn không?

Có. Một MVE mạnh hơn về khoa học có thể chỉ cần:

- **S0**: chunk translation independent.  
- **S2**: auto-generated T1/T2/entity_relations hard constraints + previous agent outputs T5.  
- **S2+C**: thêm Critic.  
- **S3d**: thêm summary/basic retrieval nhưng không Narrative Brief/vector.  
- **S3**: full retrieval + Narrative Brief + Critic.

Nếu S2+C đã thắng phần lớn, thesis vẫn có kết luận giá trị: xưng hô/term/entity hard memory + critic mới là nguồn lợi chính. Nếu S3 thắng S3d, mới claim Narrative Agent/vector retrieval có giá trị riêng.

### Có phương án mạnh hơn mà vẫn khả thi không?

Có nhưng nên để P2:

- thêm reranker nhẹ cho D6;
- dùng cross-encoder/multilingual embedding tốt hơn;
- automatic brief faithfulness check;
- COMET/xCOMET hoặc COMET-Kiwi nếu license/compute cho phép.

Không nên fine-tune hoặc train model trong core vì `RESEARCH_PLAN_V3.md`, dòng 16–23, đã khóa “no training LLM”.

### Hard retrieval = copy verbatim?

Tài liệu đã nhận diện đúng, nhưng cần cơ chế cụ thể hơn. “Hard ≠ verbatim” không được chỉ nằm trong prompt. Cần:

- hard constraints lưu `expected_target`, `allowed_variants`, `forbidden_variants`;  
- entity/address check dùng allowed sets, không dùng exact canonical-only;  
- term checker phân biệt named entity, pronoun, zero subject, alias;  
- Critic không flag sai nếu target dùng lược chủ ngữ tự nhiên;  
- `context_bundle` log rõ evidence nào được dùng, nhưng không đưa full gold/reference text.

### Khuyến nghị

- P0: runtime V1 = T1–T4 source-only frozen; T5 append-only từ output của agent; T7 append-only issue log; không update T3/T4 sau Critic.  
- P0: đổi tên mọi biến `chapter_target_text` trong core thành `previous_agent_translation_text_or_null`; cấm `reference_vi`/manual reference trong prompt.  
- P1: triển khai exact + BM25 trước; vector chỉ claim nếu D6 chứng minh retrieval tốt.  
- P1: tạo “gold firewall test”: mọi `context_bundle` phải không chứa ID/hash/text từ `manual_reference_subset` hoặc AI-LAB annotation target.

---

## 5. D — Số lượng & vai trò agent

### Nhận định

4 LLM agents hiện tại là hợp lý: Summary, Narrative Understanding, Translator, Critic. Không nên thêm Localization Agent, Proofreader Agent, Relation Agent riêng vào core vì sẽ biến khóa luận thành orchestration demo khó chứng minh. TransAgents đã dùng nhiều vai trò hơn; nếu mình bắt chước số agent, dự án sẽ yếu hơn về novelty. ([aclanthology.org](https://aclanthology.org/2025.tacl-1.42/))

### Bằng chứng trong dự án

- `RESEARCH_PLAN_V3.md`, dòng 378–392: xác định 4 LLM agents và nói infrastructure không tính là agent.  
- `PROMPT_DESIGN.md`, dòng 11–26: thống nhất 4 agents + optional feedback; repair/extraction/consolidation subcalls không tính là agent.  
- `PROMPT_DESIGN.md`, dòng 891–898: đã quyết định không tách localization/proofreader; Critique + Judgment merge trong Critic. Đây là quyết định đúng.  

### Ranh giới trách nhiệm nên khóa

| Agent/module | Nên làm | Không nên làm |
|---|---|---|
| Source Analyzer / Summary Agent | source-only pre-pass, tạo T1/T2/T3/T4 runtime mirror | đọc gold/reference; cập nhật summary bằng human translation |
| Narrative Agent | tạo `Interpretation Brief` 150–300 tokens từ source + retrieved context | dịch; sửa bản dịch; tự thêm fact không có evidence |
| Translator | dịch block với context pack, log memory refs | tự sửa schema/memory; gọi gold |
| Critic | phát hiện lỗi, rule check, đề xuất repair bounded | rewrite không kiểm soát; update T1–T4 |
| Repair subcall | sửa theo issue list, max_retry=1 | mở vòng lặp vô hạn |
| Retriever/Coverage Checker | infra deterministic; chọn context | tính là agent để tăng số lượng |

### Ablation đề xuất

| Config | Thành phần | Mục đích chứng minh | Metric chính |
|---|---|---|---|
| S0 | Base LLM, từng block độc lập | baseline “normal translation” | chrF/COMET trên ref subset, APA, ECS, MQM |
| S1 | S0 + previous block/chapter raw context | kiểm tra context thô có đủ chưa | APA/ECS, omission/addition, cost |
| S2 | T1/T2/T5 hard memory auto-generated, no summary/vector/narrative/critic | giá trị của structured memory cơ bản | TAR, ECS, APA |
| S2+C | S2 + Critic/Repair | giá trị riêng của Critic | D5 P/R/F1, MQM error reduction |
| S3d | S3 nhưng no Narrative Agent/vector brief | baseline mạnh để kiểm tra narrative layer | MHP, Likert narrative, APA |
| S3 | Full: source-only T1–T4, hard+soft retrieval, Narrative Brief, Translator, Critic | hệ thống chính | tất cả metrics |
| S3a | S3 no chapter summary T4 | giá trị của summary memory | chapter-opening blocks, ECS, MHP |
| S3b | S3 no Critic | giá trị Critic trong full setting | MQM, D5, APA repair |
| S3r | S3 no `entity_relations/address_policy` | giá trị riêng của mô hình xưng hô | APA/STA/ATA/phase accuracy |
| R-exact/BM25/vector | retrieval-only on D6 | giá trị từng kênh retrieval | Recall@K, MRR, NDCG |

S3c feedback không nên nằm trong core; giữ là future. `RESEARCH_PLAN_V3.md §9`, dòng 681, và roadmap có nhắc S3c/E4, nhưng `§0` đã khóa feedback khỏi core.

---

## 6. E — Phương pháp đánh giá

### Nhận định

Bộ metric hiện có khá đầy đủ nhưng cần phân tầng rõ: reference-based metrics chỉ là sanity check, human/MQM/xưng hô mới là bằng chứng chính cho văn học Anh→Việt.

BLEU là metric cổ điển cho automatic MT evaluation; chrF là character n-gram F-score; COMET là neural MT evaluation framework dùng source, hypothesis và reference để dự đoán chất lượng gần human judgement; MQM là error typology chuẩn hóa để phân loại lỗi dịch và tạo structured quality data. ([aclanthology.org](https://aclanthology.org/P02-1040/)) Với văn học, các metric này không đủ để kết luận “hay”, nhưng vẫn hữu ích để phát hiện hệ thống làm hỏng adequacy/fluency.

### Bằng chứng trong dự án

- `RESEARCH_PLAN_V3.md`, dòng 816–867: đã liệt kê BLEU, chrF, COMET/BERTScore, GEMBA-DA, TAR/ECS, Critic P/R/F1, MQM, MHP, BLP, Likert, MATTR/MTLD.  
- `RUN_EVAL_SCHEMA.md`, dòng 83–99: có `evaluation_runs`, `metric_name`, `judge_model`, CI fields; dòng 95–96 yêu cầu judge_model khác translator.  
- `RESEARCH_PLAN_V3.md`, dòng 1052–1108: human protocol có blind A/B, random order, MHP/BLP/Likert/MQM, Fleiss’ κ.  
- `DATASET_DESIGN.md`, dòng 740–760: đã có công thức TAR/ECS.  
- Thiếu: metric xưng hô formal chưa được định nghĩa đầy đủ.

### Metric xưng hô nên đo thế nào?

Tạo evaluation layer, không sửa schema AI-LAB:

`address_event = (block_id, speaker_id, addressee_id, active_relation_id, state_label, expected_self_terms, expected_address_terms, allowed_omission, evidence_span_optional)`

Sau đó đo:

| Metric | Công thức/gợi ý | Ý nghĩa |
|---|---|---|
| APA — Address Policy Accuracy | event đúng khi self-term và address-term đều thuộc allowed set, hoặc omission hợp lệ | metric chính |
| STA — Self-Term Accuracy | self-term đúng / events applicable | “tôi/tao/mình/con/em/anh…” |
| ATA — Address-Term Accuracy | address-term đúng / events applicable | “anh/chị/em/cô/ngài/mày…” |
| DIR-ERR | dùng nhầm policy chiều ngược / applicable events | bắt lỗi A→B dùng B→A |
| Phase Adaptation Accuracy | đúng trong N blocks sau phase boundary / phase-boundary events | đo đổi vai theo pha |
| Pair Consistency | 1 − illegal switches trong cùng active phase | đo ổn định |
| Omission Handling | omission hợp lệ vs omission gây mơ hồ | tránh phạt lược chủ ngữ tiếng Việt tự nhiên |

Cần allowed variants, ví dụ `{tôi, mình, ta}` hoặc `{cậu, anh, ngài}` tùy quan hệ. Không nên bắt exact string duy nhất.

### Human eval

Thiết kế tối thiểu:

- 30–50 paired samples, stratified: dialogue-rich, chapter-opening, narrative-rich, relation phase shift, high retrieval need.  
- 3 reviewers là sàn; 5 reviewers tốt hơn.  
- MHP: target-only, chọn bản đọc tự nhiên hơn.  
- BLP/MQM: source+2 translations, đánh adequacy/error.  
- A/B swap và ẩn system.  
- Báo cáo agreement; nếu κ thấp, không giấu, dùng majority + qualitative error analysis.  
- Không dùng cùng LLM vừa dịch vừa làm judge. `RUN_EVAL_SCHEMA.md`, dòng 95–96, đã ghi đúng điểm này.

### Kiểm định thống kê

Không nên chỉ dùng paired t-test như `RESEARCH_PLAN_V3.md`, dòng 953–968. Với n nhỏ và preference data:

- continuous metrics: paired bootstrap CI hoặc Wilcoxon signed-rank;  
- win/loss/tie preference: sign test hoặc bootstrap over paired samples;  
- APA/STA/ATA: McNemar hoặc bootstrap CI cho paired binary outcomes;  
- multiple ablations: báo cáo effect size và CI, không chỉ p-value;  
- D6 retrieval: bootstrap query-level CI cho Recall@K/MRR/NDCG.

### Baseline

Tối thiểu cần S0, S1, S2, S3d, S3. Có thể thêm một baseline “vanilla LLM with previous chapter summary only”, nhưng không để nó phá MVE. Published Vietnamese translations không nên dùng làm metric reference vì contamination/copyright; chỉ dùng phân tích định tính nếu license cho phép.

---

## 7. F — Bộ nhớ & truy hồi

### Nhận định

T1–T7 và `context_bundle` là phần có tư duy tốt. Thiết kế chia hard/soft context giúp tránh hai lỗi thường gặp: nhồi quá nhiều context vào prompt và biến glossary thành replace-all. Tuy nhiên, một số tài liệu đang lẫn “AI-LAB gold seed” với “runtime memory mirror”; phải sửa trước khi triển khai.

### Bằng chứng trong dự án

- `RUN_EVAL_SCHEMA.md`, dòng 30–51: `context_bundle` snapshot exact context per block/run, gồm memory refs, retrieved evidence, brief, resolved_prompt hash, token breakdown. Đây là audit layer rất quan trọng.  
- `RUN_EVAL_SCHEMA.md`, dòng 115–128: `reference_eval_only` cách ly target text; chỉ `evaluation_runs` được truy cập.  
- `RETRIEVAL_ARCHITECTURE.md`, dòng 118–141: Context Pack tách `global_core`, `hard_constraints`, `soft_context`, `brief`, `coverage`, `retrieval_log_ref`.  
- `RETRIEVAL_ARCHITECTURE.md`, dòng 183–199: RetrievalLog cần lưu query, selected IDs, rank, reason.  
- `RETRIEVAL_ARCHITECTURE.md`, dòng 203–212: D6 có Recall@K/MRR/NDCG cho retrieval relevance.  

### Hard ≠ verbatim có bảo đảm chưa?

Ở mức tài liệu: có. Ở mức cơ chế: chưa đủ. Cần enforce bằng:

1. Context pack tách hard constraints khỏi soft passages.  
2. Hard constraints chứa allowed variants/forbidden variants, không chỉ canonical target.  
3. Critic rule check dùng policy-aware matching.  
4. Evaluation APA có `allowed_omission`.  
5. Translator prompt không được nói “luôn dịch X thành Y” nếu X là entity/pronoun có thể lược hoặc đổi theo ngữ cảnh.  
6. `context_bundle` phải lưu `constraint_type`: term, entity, address_policy, narrative_hint, previous_agent_output.  
7. Không đưa human `reference_vi` hoặc manual notes vào bất kỳ `context_bundle` runtime nào.

### Khuyến nghị

- Tạo `memory_source` enum trong runtime DB, không nhất thiết trong AI-LAB schema: `source_only_auto`, `agent_previous_output`, `eval_gold_forbidden`, `human_gold_forbidden`.  
- Thêm test: nếu context bundle chứa `manual_reference_subset`, run fail.  
- D6 phải chạy trước khi claim S3: nếu retrieval Recall@K thấp thì S3 không thắng không chứng minh Narrative Agent sai; chỉ chứng minh retriever yếu.

---

## 8. G — Rủi ro & đe dọa tính hợp lệ

| Mức | Rủi ro | Bằng chứng | Tác động | Giảm thiểu |
|---|---|---|---|---|
| P0 | Rò gold vào runtime | `RETRIEVAL_ARCHITECTURE.md`, dòng 20–31 nói load dataset JSON trước run; `DATASET_DESIGN.md`, dòng 482, 500–504 nói seed runtime memory từ dataset JSON | Làm hỏng toàn bộ claim causal | Sửa docs/code: AI-LAB JSON chỉ eval; runtime mirror tự sinh source-only; test no-leak |
| P0 | Scope quá lớn | `RESEARCH_PLAN_V3.md` có T1–T7, S0–S3, D1–D6, Critic, Narrative, vector, human eval | Không kịp bảo vệ, kết quả rời rạc | MVE: D2/D4/D5/D6 nhỏ; S0/S2/S3d/S3/S3r |
| P0 | Gold dataset thực tế chưa sẵn | project counts: nhiều dataset schema 1.4.0, annotation 0 hoặc rất ít, relation 0 | Không có cơ sở đo | Chọn 1–2 docs, annotate sâu, freeze, validator PASS |
| P1 | Contamination từ Alice/public domain nổi tiếng | `RESEARCH_PLAN_V3.md`, dòng 807–812; `DATASET_DESIGN_AGENT_REFERENCE.md`, dòng 891–917 | S0 có thể “nhớ” bản dịch, làm sai kết luận | D2b ít phổ biến, contamination probe, không dùng Alice một mình |
| P1 | Human eval variance cao | `RESEARCH_PLAN_V3.md`, dòng 1052–1108; `DATASET_DESIGN_AGENT_REFERENCE.md`, dòng 1185–1196 | Preference không đủ power | 50 pairs, 3–5 reviewers, CI, qualitative disagreement analysis |
| P1 | Vector retrieval yếu | `RESEARCH_PLAN_V3.md`, dòng 571–583 và 587–620 | S3 không thắng vì retriever, không vì hypothesis sai | D6 intrinsic trước; fallback BM25; log retrieval |
| P1 | “Hard = copy” bug | `RETRIEVAL_ARCHITECTURE.md`, dòng 86–99; `PROMPT_DESIGN.md`, dòng 99–122 | Dịch gượng, xưng hô sai | allowed variants, policy-aware checker, APA với omission |
| P2 | LLM judge bias | `RUN_EVAL_SCHEMA.md`, dòng 95–96 đã cảnh báo judge khác translator | Kết quả dễ bị nghi ngờ | human eval + blind judge + không dùng metric duy nhất |
| P2 | Tài liệu drift | WEB spec, task docs, dataset docs mâu thuẫn | Reviewer thấy thiếu kiểm soát | Bảng mâu thuẫn P0/P1 và sửa trước nộp |

---

## 9. H — Nhất quán giữa tài liệu

Nhận định thẳng: hiện có **mâu thuẫn nghiêm trọng** giữa `Directional Lock` và một số tài liệu phụ. Nếu không sửa, reviewer có thể kết luận dự án tự mâu thuẫn về phương pháp.

### Bảng MÂU THUẪN giữa các tài liệu

| File | Mục | Mâu thuẫn / trôi lệch | Đề xuất sửa |
|---|---|---|---|
| `RESEARCH_PLAN_V3.md` vs `RETRIEVAL_ARCHITECTURE.md` | V3 §0 dòng 32–38 vs Retrieval §B dòng 20–31 | V3 nói gold eval-only; Retrieval nói dataset JSON dùng làm offline seed trước run | Thay bằng: runtime chỉ dùng source document; glossary/entities/summaries runtime tự sinh source-only; AI-LAB sidecars chỉ vào `reference_eval_only`/eval |
| `RESEARCH_PLAN_V3.md` vs `DATASET_DESIGN.md` | V3 §0 vs Dataset dòng 482, 500–504 | Dataset nói runtime memory T1–T7 seed từ dataset JSON | Sửa “seed” thành “evaluation mapping”; thêm warning: không import gold vào runtime |
| `RESEARCH_PLAN_V3.md` nội bộ | §0 dòng 43–46 vs pipeline §6 dòng 557–560 | §0 nói pre-pass source-only rồi freeze; pipeline update T3/T4, Human Review, Feedback Agent trong loop | V1: T1–T4 frozen; T5/T7 append-only; human feedback future |
| `RESEARCH_PLAN_V3.md` nội bộ | §0 dòng 50–54 vs §9 dòng 681, E4 dòng 1003–1026, roadmap S3c | §0 đẩy feedback ra future; S3 vẫn chứa feedback loop/S3c | S3 core bỏ feedback; rename future experiment thành S3+FB |
| `PROMPT_DESIGN.md` | Summary Agent dòng 361–390 | Có `chapter_target_text` và “sau mỗi chapter/N blocks”, dễ bị hiểu là target/human text vào memory | Đổi thành `previous_agent_translation_text_or_null`; trong benchmark core set null/source-only |
| `RETRIEVAL_ARCHITECTURE.md` + `PROMPT_DESIGN.md` | hard context dùng `locked/human_verified` | Trong runtime không được có status human_verified từ AI-LAB | Runtime dùng `auto_frozen`, `auto_confidence`; AI-LAB status chỉ eval |
| `AILAB_HANDOFF/WEB_TOOL_SPEC.md` | dòng 41–72, 221–225 | Spec nói canonical/export gồm 5 files, thiếu `entity_relations.jsonl` | Cập nhật thành 6 files, trong đó `entity_relations` optional |
| `AILAB_HANDOFF/README.md` | dòng 54–66 | Output list thiếu `entity_relations` | Cập nhật README theo schema 1.5.0 |
| `tasks/TASK_entity_relations_apply_backend.md` vs code thực tế | task nói backend apply chưa làm | Code `config.py` dòng 20–27, `dataset_io.py` dòng 130–138, `annotation_flow.py` dòng 900–968 đã đọc/ghi relations | Archive hoặc mark completed để tránh reviewer tưởng implementation thiếu |
| `DATASET_DESIGN_AGENT_REFERENCE.md` vs `DATASET_DESIGN.md` | Agent reference dòng 43–50, 56–58; Dataset dòng 50–74, 876–889 | File cũ nói D1/D3 bắt buộc, D6 tùy chọn, không cần dev set; file mới nói D2/D4/D5/D6 là core nhỏ và cần dev/test split | Chọn `DATASET_DESIGN.md` mới hơn làm canonical; ghi rõ D1/D3 optional sanity/extension |
| Project datasets vs schema 1.5.0 | `AILAB_HANDOFF/ailab_projects/*` | nhiều canonical vẫn `schema_version` 1.4.0; validator fail | Migration script restamp + add empty relations; không gọi là gold-ready |
| `RUN_EVAL_SCHEMA.md` vs existing storage | dòng 156–177 | Existing `translation_records` overwrite by block_id, không giữ S0–S3 × draft/revised | Implement `translation_runs` trước mọi benchmark |

---

## 10. I — Lộ trình: minimal viable experiment

### MVE có thể bảo vệ sớm

**Mục tiêu:** chứng minh hệ thống tự động, không rò gold, có lợi ích đo được từ memory/retrieval và xưng hô.

1. **Chọn corpus nhỏ nhưng sạch**  
   Một literary document 50–80 blocks, ưu tiên ít contamination hơn Alice; Canterville hiện pass schema nhưng cần thêm annotations. Alice chỉ dùng phụ hoặc contamination probe.

2. **Freeze gold eval**  
   Validator PASS, schema 1.5.0, freeze log, block_id bất biến. Annotate: 30–50 terms/entities, 8–12 relation pairs, 40–60 address events, 30–50 manual references.

3. **Implement run/eval trước runtime fancy**  
   Tạo `translation_runs`, `context_bundle`, `evaluation_runs`, `reference_eval_only`. Nếu không có run/eval audit, ablation sẽ không đáng tin.

4. **Runtime source-only pre-pass**  
   Tạo T1/T2/T3/T4 runtime mirror từ source text. Không đọc AI-LAB gold sidecars. T5 chỉ từ output của agent.

5. **Chạy S0/S2/S3d/S3/S3r**  
   Có thể bỏ S1 nếu thiếu thời gian; nhưng S0/S2/S3d/S3/S3r là tối thiểu để chứng minh memory, narrative, xưng hô.

6. **Đánh giá**  
   TAR/ECS/APA/STA/ATA, D6 Recall@K/MRR/NDCG, D5 Critic P/R/F1, chrF/COMET trên reference subset, MHP/Likert/MQM trên 30–50 pairs.

7. **Viết kết luận theo effect size**  
   Nếu S3 không thắng S3d nhưng S3r thua S3 ở APA, thesis vẫn có đóng góp: address-policy memory có ích, narrative retrieval chưa chứng minh được.

### Ưu tiên tiếp theo

- Làm P0 trước: no-leak, schema migration, run/eval DB, chọn MVE dataset.  
- Làm P1 sau: address metric, D6, Critic D5, human eval form.  
- Làm P2 cuối: vector tuning, reranker, D3 technical, UI polish.

---

## 11. J — Hướng phát triển / đóng góp mới có kỷ luật

| Xếp hạng | Ý tưởng | Mới ở đâu | Tôn trọng lock? | Chi phí | Cách đo | Phân loại | Nên làm |
|---:|---|---|---|---|---|---|---|
| 1 | **Address Policy Accuracy for EN→VI literary translation** | Biến `entity_relations` thành metric định lượng cho self/address terms theo cặp, chiều, pha quan hệ; sát điểm đau tiếng Việt | Có; gold chỉ eval | Medium: cần 40–100 address events | S3 vs S3r; APA/STA/ATA/DIR-ERR/phase accuracy | Đủ tầm đóng góp chính | Làm ngay |
| 2 | **Gold firewall / context audit** | Đóng góp phương pháp: chứng minh pipeline không rò gold bằng `context_bundle` hashes/IDs | Có; củng cố lock | Low–Medium | 100% runs không chứa reference/gold IDs; negative tests fail đúng | Đóng góp phụ nhưng rất quan trọng | Làm ngay |
| 3 | **Retrieval diagnostic suite D6 cho narrative memory** | Tách lỗi retriever khỏi lỗi translator; ít dự án khóa luận làm attribution rõ | Có | Medium: 30–50 queries | Recall@K, MRR, NDCG; extrinsic S3/S3d | Đóng góp phụ mạnh | Làm ngay nếu kịp |
| 4 | **Phase-boundary stress set cho xưng hô** | Tập test nhỏ quanh cảnh quan hệ đổi pha: xa lạ→thân, kính trọng→khinh miệt, chủ–tớ đảo chiều | Có | Medium–High vì phải tìm/annotate cảnh | Phase Adaptation Accuracy trong N blocks sau boundary | Có thể thành phụ/main nếu đủ data | Làm sau MVE |
| 5 | **Interpretation Brief faithfulness/utility score** | Đo brief có grounded vào evidence không và có giúp dịch không | Có | Medium: cần human/LLM audit 30–50 briefs | Brief support rate, correlation với APA/MHP, S3 vs no-brief | Đóng góp phụ | Để sau |
| 6 | **Adaptive context trigger** | Giảm token bằng chỉ gọi Narrative/soft retrieval ở block khó | Có | Low–Medium | token cost giảm bao nhiêu với quality delta ≤ ngưỡng | Cải tiến kỹ thuật nhỏ | Để sau |
| 7 | **Fine-tuning model trên gold/reference** | Có thể tăng chất lượng nhưng phá thesis premise | Không, trừ khi nới lock | High | Không phù hợp ablation eval-only | Bỏ khỏi core | Bỏ |

---

## 12. Bảng KHÓA vs MỞ

| Nhóm | Quyết định | Trạng thái | Nhận xét reviewer |
|---|---|---|---|
| Directional Lock | Gold AI-LAB eval-only; runtime tự động từ zero | **KHÓA** | Không đụng. Mọi doc trái lock phải sửa. |
| `block_id` | Trục căn chỉnh bất biến | **KHÓA** | Đúng; nên dùng xuyên suốt source/translation/gold/eval. |
| Hai track AI-LAB vs THESIS RUNTIME | Tách nghiêm ngặt | **KHÓA** | Đây là điều làm thesis đáng tin. |
| Schema AI-LAB 1.5.0 | Không thêm/bớt field lúc này | **KHÓA** | Chỉ thêm eval-derived tables ngoài schema nếu cần. |
| No training/fine-tuning | Base LLM + prompting/retrieval | **KHÓA** | Hợp với scope khóa luận. |
| Human feedback loop | Không thuộc core | **KHÓA cho core, mở future** | Xóa khỏi S3 core; để S3+FB future. |
| Số agent | 4 agents chính | **Nên giữ** | Không thêm agent chỉ để “đa-agent hơn”. |
| RQ4 feedback | Có nên làm không | **MỞ nhưng khuyên bỏ core** | Nếu giữ sẽ phá scope và lock. |
| D1/D3 | Có bắt buộc không | **MỞ** | Với MVE, D2/D4/D5/D6 quan trọng hơn; D1 là sanity, D3 là extension. |
| Vector retrieval | Có claim chính không | **MỞ** | Chỉ claim nếu D6 chứng minh. |
| Dataset chính | Alice hay D2b | **MỞ** | Không dùng Alice một mình vì contamination. |
| Metric xưng hô | Cách operationalize | **MỞ cần quyết định** | Nên khóa sớm APA/STA/ATA + allowed omission. |
| Human eval size | 30 hay 50 pairs; 3 hay 5 reviewers | **MỞ** | Quyết theo nhân lực; báo cáo power/limitation. |

---

## 13. Khuyến nghị ƯU TIÊN

### P0 — Làm ngay, nếu không thesis dễ bị bác

1. **Sửa toàn bộ mâu thuẫn rò gold** trong `RETRIEVAL_ARCHITECTURE.md`, `DATASET_DESIGN.md`, `PROMPT_DESIGN.md`, `RESEARCH_PLAN_V3.md`.  
2. **Định nghĩa runtime memory mirror**: T1–T4 source-only auto; T5 agent-output only; T7 issue log; AI-LAB gold không import.  
3. **Implement `translation_runs` + `context_bundle` + `reference_eval_only`** trước khi chạy benchmark.  
4. **Migrate dataset được chọn lên schema 1.5.0**, validator PASS, freeze log.  
5. **Loại feedback khỏi S3 core**; rename thành future S3+FB.  
6. **Chọn MVE dataset và annotate sâu**, không chạy rộng trên 10 sách chưa gold.

### P1 — Làm sau P0, tạo kết quả bảo vệ được

1. Viết formal metric xưng hô: APA/STA/ATA/DIR-ERR/phase accuracy.  
2. Tạo `address_eval_events` từ `entity_relations` + discourse.  
3. Tạo D6 retrieval relevance set 30–50 queries.  
4. Chạy S0/S2/S3d/S3/S3r với same model/temp/seed.  
5. Tạo D5 injected errors cho Critic, 30–50 lỗi là đủ pilot.  
6. Human eval 30–50 pairs, blind A/B, 3–5 reviewers, CI/stat tests.

### P2 — Tốt nhưng không để làm trễ MVE

1. Reranker/vector tuning nâng cao.  
2. D3 technical document.  
3. D1 sentence-level benchmark lớn.  
4. Adaptive context trigger.  
5. UI polish cho AI-LAB.  
6. LLM-judge nâng cao kiểu GEMBA-MQM/xCOMET nếu compute/license ổn.

---

## 14. Câu hỏi mở cho sinh viên tự quyết

1. Đóng góp chính muốn đặt vào đâu: **address-policy xưng hô**, **retrieval/narrative memory**, hay **strict eval-only methodology**? Reviewer khuyên chọn xưng hô + eval-only làm trục chính.  
2. D2 chính là tác phẩm nào? Nếu vẫn dùng Alice, D2b là gì để giảm contamination?  
3. Có đủ nhân lực cho 5 reviewers không, hay chỉ 3 reviewers và chấp nhận limitation?  
4. Với xưng hô, omission khi nào được tính là đúng? Cần guideline rõ trước khi chấm.  
5. Có giữ D3 technical trong khóa luận không, hay dồn lực vào D2 literary?  
6. Vector retrieval có phải claim chính không? Nếu không, BM25+exact có thể đủ cho MVE.  
7. Nếu S3 không hơn S3d về MHP nhưng hơn S3r về APA, luận văn sẽ xoay conclusion sang xưng hô hay vẫn cố claim narrative?  
8. Có chấp nhận kết quả âm không? Nên viết trước: “nếu S3≈S3d, đó là bằng chứng Narrative Brief chưa đủ hoặc retrieval yếu, không phải thất bại toàn đề tài.”

---

## 15. Kết luận phản biện

Đề tài **có nền tốt và đáng làm**, nhưng phải dọn sạch ba điểm trước khi bảo vệ: **rò gold trong tài liệu**, **scope phình**, và **dataset chưa gold-ready**. Phần có giá trị nhất không phải là “nhiều agent”, mà là khả năng biến một hiện tượng rất Việt — xưng hô theo quan hệ, chiều nói và pha truyện — thành memory structure và metric đo được. Nếu khóa luận chứng minh được S3 thắng S3r/S2 ở APA/phase accuracy, đồng thời không rò gold nhờ `context_bundle`, thì đây là một đóng góp đủ sắc cho tốt nghiệp.
