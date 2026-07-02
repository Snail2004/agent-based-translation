# Thang điểm đánh giá dịch văn học/truyện dài bằng LLM

Theo mình, **thang điểm đủ thuyết phục hội đồng không nên là một metric duy nhất**, mà nên là **một hệ đánh giá lai**: có điểm số tự động để chạy nhiều, có rubric người chấm để làm “ground truth”, có LLM-judge để hỗ trợ phân tích lỗi, và có bộ test riêng cho truyện dài. Lý do là các paper mới về literary MT đều chỉ ra rằng metric bề mặt như BLEU không đo tốt “độ hay”, còn ngay cả MQM/COMET cũng có điểm mù khi văn bản dịch đã ở mức cao hoặc mang tính văn học.

## Kết luận ngắn gọn

Với luận án của bạn, mình đề xuất thang chính là:

**Narrative Translation Quality Score — NTQS / 100**

Trong đó:

| Nhóm điểm | Trọng số | Đo cái gì |
|---|---:|---|
| **Faithfulness / Adequacy** | 30 | Dịch đúng nghĩa, không thêm/bớt/sai quan hệ, không hallucinate |
| **Narrative Consistency** | 20 | Nhất quán nhân vật, đại từ, xưng hô, quan hệ, timeline, thuật ngữ xuyên chương |
| **Vietnamese Fluency & Naturalness** | 15 | Tiếng Việt tự nhiên, mượt, đúng ngữ pháp, không “dịch máy” |
| **Literary Style / Độ hay** | 20 | Giữ giọng văn, nhịp câu, sắc thái, hình ảnh, cảm xúc, tác dụng nghệ thuật |
| **Cultural & Terminology Handling** | 10 | Thành ngữ, ẩn dụ, tên riêng, văn hóa, thuật ngữ, xưng hô |
| **Format / Completeness** | 5 | Không sót đoạn, không lỗi định dạng, không mất thoại |

Và có **critical gate**: nếu có lỗi nghiêm trọng như dịch ngược nghĩa, nhầm nhân vật chính, mất đoạn quan trọng, hallucinate sự kiện không có trong gốc, thì điểm tối đa bị chặn, ví dụ không quá 60/100 dù văn phong có hay.

Đây sẽ thuyết phục hơn vì nó kết hợp tinh thần của **MQM** cho lỗi dịch, **SQM/BWS** cho đánh giá chất lượng tổng thể và độ ưa thích, **COMET/XCOMET/GEMBA-MQM** cho tự động hóa, và các hướng mới như **DITING / TransProQA / TransAgents** cho web novel, literary translation và long-context translation.

---

## Vì sao không nên chỉ dùng BLEU, ROUGE, BERTScore?

BLEU/ROUGE/chrF vẫn nên báo cáo để hội đồng thấy bạn có baseline truyền thống, nhưng **không nên lấy làm điểm chính**. BLEU dựa vào độ giống n-gram với bản tham chiếu, nên nó phạt những bản dịch diễn đạt khác mà vẫn đúng nghĩa; với văn học và tiếng Việt, một câu hay thường có nhiều cách diễn đạt hợp lệ. Bản thân SacreBLEU ra đời vì BLEU rất nhạy với tokenization và cấu hình tiền xử lý, làm điểm giữa các paper khó so sánh nếu báo cáo không chuẩn.

BERTScore tốt hơn BLEU ở chỗ dùng embedding ngữ cảnh để đo tương đồng ý nghĩa, còn BLEURT và COMET là learned metrics được huấn luyện để dự đoán đánh giá của con người. COMET đặc biệt quan trọng vì được dùng rộng rãi trong MT evaluation và có các biến thể dựa trên Direct Assessment, HTER, MQM. Nhưng ngay cả COMET cũng không nên là “trọng tài tối cao”, vì paper “Pitfalls and Outlooks in Using COMET” chỉ ra điểm COMET có thể bị ảnh hưởng bởi phiên bản phần mềm, precision, dữ liệu rỗng, language mismatch, domain bias và cách báo cáo.

Với tiếng Việt, mình sẽ báo cáo thêm **chrF/chrF++** vì metric ký tự thường đỡ phụ thuộc vào tách từ hơn BLEU, nhưng vẫn chỉ dùng như chỉ báo phụ. Gần đây có paper 2026 so sánh ChrF++ và BLEU trong ngữ cảnh low-resource, kết luận hai metric cho tín hiệu bổ sung chứ không metric nào đủ một mình.

---

## Nền tảng học thuật mạnh nhất: MQM + SQM/BWS + learned metrics

**MQM** là khung đánh giá lỗi dịch có tính chuẩn nhất để đưa vào luận án. MQM không chỉ cho một điểm chung chung, mà phân lỗi theo loại: accuracy, fluency, terminology, style, locale convention, v.v., rồi gán severity như minor/major/critical. Paper 2024 về Multi-Range Theory nói MQM có hai trụ cột: error typology và scoring model, đồng thời MQM đã được dùng trong các shared task của WMT cho human và automatic translation evaluation.

Tuy nhiên, với **văn học**, MQM không đủ. Paper “How Good Are LLMs for Literary Translation, Really?” rất đáng trích trong luận án của bạn: họ tạo LITEVAL-CORPUS với hơn 2k paragraph, nhiều bản dịch người và output từ 9 MT systems, rồi thấy MQM theo kiểu non-literary không phản ánh tốt chất lượng văn học; trong khi **Best-Worst Scaling** với người chấm và **Scalar Quality Metric** với dịch giả chuyên nghiệp lại phân biệt bản dịch người tốt hơn rõ rệt. Paper này là nguồn rất mạnh để bạn biện minh rằng “độ hay” cần một lớp đánh giá riêng, không thể gộp hết vào lỗi MQM.

Vì vậy, mình đề xuất:

**MQM dùng để bắt lỗi.  
SQM/BWS dùng để đo cảm nhận tổng thể và độ hay.  
COMET/XCOMET/GEMBA dùng để tự động hóa và phân tích rộng.**

---

## Thang 0–5 cho từng tiêu chí

Bạn có thể dùng thang 0–5 cho mỗi chiều, sau đó nhân trọng số ra /100.

### 1. Faithfulness / Adequacy — 30 điểm

| Điểm | Mô tả |
|---:|---|
| 5 | Giữ đầy đủ nghĩa, quan hệ, hàm ý, sắc thái quan trọng; không thêm/bớt thông tin |
| 4 | Có vài lệch nhẹ nhưng không làm sai nội dung chính |
| 3 | Hiểu được ý chính nhưng mất/sai một số chi tiết quan trọng |
| 2 | Nhiều chỗ sai nghĩa, thêm/bớt, dịch mơ hồ sai hướng |
| 1 | Sai phần lớn nội dung |
| 0 | Không dịch đúng hoặc hallucinate nghiêm trọng |

Tự động hỗ trợ bằng **COMET / COMETKiwi / XCOMET**. COMET học từ human judgments và dùng source + hypothesis + reference; COMETKiwi/QE hữu ích khi không có reference; XCOMET thêm khả năng phát hiện error span và phân loại lỗi, rất hợp để debug từng câu.

### 2. Narrative Consistency — 20 điểm

Đây là phần cực kỳ quan trọng cho repo của bạn vì dịch truyện dài không chỉ đúng từng câu. Cần chấm:

- Nhân vật có bị nhầm giới tính, vai vế, xưng hô không.
- Quan hệ nhân vật có giữ đúng theo timeline không.
- Đại từ zero-pronoun, “he/she/they/you/I”, người nói/người nghe có đúng không.
- Tên riêng, danh xưng, biệt hiệu có nhất quán không.
- Một motif, lời hứa, thuật ngữ, sự kiện đã xuất hiện trước đó có được giữ nhất quán không.

DITING là paper rất sát với hướng này: nó đánh giá web novel theo 6 chiều gồm idiom translation, lexical ambiguity, terminology localization, tense consistency, zero-pronoun resolution và cultural safety; đây gần như là checklist mẫu để bạn chuyển hóa sang EN–VI hoặc CN–VI/VN target.

### 3. Vietnamese Fluency & Naturalness — 15 điểm

| Điểm | Mô tả |
|---:|---|
| 5 | Đọc như tiếng Việt tự nhiên, không lộ cấu trúc ngoại ngữ |
| 4 | Mượt, chỉ hơi cứng ở vài chỗ |
| 3 | Hiểu được nhưng còn nhiều dấu vết dịch máy |
| 2 | Câu Việt gượng, sai collocation, khó đọc |
| 1 | Rất khó đọc |
| 0 | Không thành tiếng Việt tự nhiên |

Phần này nên chấm **target-only** bởi người Việt đọc bản dịch, không nhất thiết nhìn source. Đây gần với ý tưởng **Monolingual Human Preference** của TransAgents: người đọc đơn ngữ đánh giá bản dịch như một văn bản đích độc lập, vì độc giả truyện thực tế cũng tiếp nhận như vậy.

### 4. Literary Style / Độ hay — 20 điểm

Đây là phần bạn đang lo nhất, và đúng là không thể đo hoàn toàn bằng code. Cách thuyết phục nhất là biến “độ hay” thành các tiêu chí nhỏ:

| Tiêu chí con | Câu hỏi chấm |
|---|---|
| **Voice preservation** | Có giữ giọng kể/tính cách tác giả không? |
| **Tone & mood** | Không khí buồn, căng, hài, lãng mạn, huyền bí có giữ được không? |
| **Rhythm** | Nhịp câu, độ ngắn/dài, cao trào có hợp không? |
| **Imagery** | Hình ảnh, ẩn dụ, biểu tượng có còn tác dụng không? |
| **Dialog naturalness** | Thoại có đúng tuổi, quan hệ, vai vế, cảm xúc không? |
| **Aesthetic impact** | Đoạn dịch có tạo cảm xúc tương đương bản gốc không? |

Đây là lý do nên đưa **BWS hoặc pairwise preference** vào. Thay vì bắt người chấm cho điểm tuyệt đối “hay 3.7/5”, bạn đưa 2–4 bản dịch ẩn danh và hỏi: “bản nào hay nhất, bản nào tệ nhất?” BWS thường ổn định hơn rating scale vì người chấm dễ so sánh cực trị hơn là tự căn thang điểm tuyệt đối.

TransProQA cũng rất đáng tham khảo cho phần này vì nó được thiết kế riêng cho literary translation evaluation, dùng QA theo insight của dịch giả văn học, tập trung vào literary devices, cultural understanding và authorial voice, đồng thời báo cáo cải thiện tương quan so với metric SOTA.

### 5. Cultural & Terminology Handling — 10 điểm

Phần này chấm các lỗi như:

- Thành ngữ dịch sát chữ làm mất nghĩa.
- Ẩn dụ bị giải thích quá đà hoặc bị làm phẳng.
- Xưng hô tiếng Việt không đúng quan hệ.
- Danh xưng, tước vị, thuật ngữ thế giới truyện không nhất quán.
- Tên riêng nên giữ, phiên âm, Việt hóa hay chú thích không có quy tắc.

Với truyện dài, tiêu chí này nên được nối trực tiếp với database/memory của repo: nếu glossary hoặc entity memory đã lock một lựa chọn, bản dịch vi phạm sẽ bị trừ điểm rõ ràng.

### 6. Format / Completeness — 5 điểm

Đây là phần nhỏ nhưng cần có để tránh hệ thống đạt điểm ngôn ngữ cao nhưng mất đoạn, thiếu thoại, sai xuống dòng, gộp nhầm paragraph.

---

## Cách biến thành điểm số “có thể bảo vệ trước hội đồng”

Mình đề xuất công thức:

```text
NTQS = 0.30*Faithfulness
     + 0.20*NarrativeConsistency
     + 0.15*VietnameseFluency
     + 0.20*LiteraryStyle
     + 0.10*CulturalTerminology
     + 0.05*FormatCompleteness
```

Mỗi thành phần được chấm 0–5, rồi chuẩn hóa về /100.

Ví dụ:

```text
component_score = raw_0_to_5 / 5 * 100
```

Sau đó dùng gate:

```text
Nếu có Critical Accuracy Error: NTQS ≤ 60
Nếu hallucinate sự kiện/nhân vật: NTQS ≤ 55
Nếu mất đoạn > 5%: NTQS ≤ 50
Nếu sai speaker/addressee trong thoại quan trọng: NTQS ≤ 70
```

Cách này lấy tinh thần từ MQM: lỗi critical/major/minor không chỉ trừ đều đều, mà lỗi nặng có thể làm bản dịch không đạt dù các phần khác tốt. MQM scoring model và các phát triển 2024–2025 đều nhấn mạnh việc chuyển error type + severity thành điểm chất lượng có kiểm soát, thay vì chỉ cho điểm cảm tính.

---

## Quy trình đánh giá mình khuyên dùng cho luận án

### Tầng A — Automatic metrics để chạy toàn bộ corpus

Báo cáo:

- SacreBLEU
- chrF/chrF++
- BERTScore
- COMET
- COMETKiwi nếu không có reference
- XCOMET để lấy error spans
- GEMBA-MQM hoặc GEMBA V2 như LLM-as-judge phụ

GEMBA và GEMBA-MQM rất đáng trích vì chúng dùng GPT-style evaluation cho MT quality, có thể reference-based hoặc reference-free, và GEMBA-MQM đánh dấu error spans theo MQM. Nhưng cũng nên nói rõ hạn chế: phụ thuộc model proprietary/black-box nếu dùng GPT, nên không dùng làm ground truth duy nhất.

### Tầng B — Human evaluation làm ground truth

Chọn khoảng:

- 200–500 đoạn/câu đại diện.
- Chia theo loại khó: thoại, độc thoại, miêu tả, hành động, thành ngữ, xưng hô, đoạn có nhiều nhân vật.
- Mỗi đoạn có ít nhất 3 người chấm nếu có thể.
- Ít nhất một nhóm là người giỏi tiếng Việt; nếu có điều kiện, một nhóm biết source language.

Freitag et al. cho thấy human evaluation của MT chất lượng cao rất khó, và professional translators có full document context có thể cho ranking khác đáng kể so với crowd workers. Với luận án truyện dài, câu này cực kỳ quan trọng: **người chấm phải có context**, không chỉ nhìn một câu rời.

### Tầng C — BWS/SQM cho “độ hay”

Mỗi mẫu đưa 2–4 bản dịch ẩn danh:

- baseline single LLM
- translation-agent reflect
- hệ của bạn có memory/retrieval
- bản người hoặc reference nếu có

Hỏi người chấm:

1. Bản nào **hay nhất như văn tiếng Việt**?
2. Bản nào **giữ nghĩa tốt nhất**?
3. Bản nào **giữ giọng văn gốc tốt nhất**?
4. Bản nào **tệ nhất**?

Sau đó tính win-rate hoặc Bradley–Terry/BWS score. Paper về literary translation cho thấy BWS và SQM có giá trị hơn MQM sinh viên trong việc phân biệt bản dịch văn học của người và máy.

### Tầng D — Diagnostic challenge set riêng cho truyện dài

Bạn nên tự xây một bộ test EN–VI/VN gồm các loại lỗi mà hệ của bạn muốn giải quyết:

| Loại test | Ví dụ |
|---|---|
| Character consistency | cùng nhân vật đổi tên/biệt danh/xưng hô |
| Relation shift | ban đầu xa lạ, sau thân mật, xưng hô phải đổi |
| Speaker resolution | thoại nhiều người, không ghi speaker |
| Pronoun ambiguity | he/she/they/you trong đoạn mơ hồ |
| Terminology memory | thuật ngữ xuất hiện chương 1, chương 20 vẫn phải giữ |
| Style preservation | đoạn thơ, ẩn dụ, văn cổ, giọng hài |
| Cultural localization | thành ngữ, tục ngữ, danh xưng, nghi lễ |

DITING là mẫu gần nhất cho hướng benchmark theo web novel, còn TransAgents là mẫu gần nhất cho literary long-text multi-agent translation.

---

## Cách trình bày trong luận án để “khó bị bắt bẻ”

Bạn có thể viết luận điểm như sau:

> Vì dịch văn học/truyện dài là tác vụ mở, không tồn tại một bản dịch duy nhất đúng. Do đó, luận án không tối ưu theo một metric bề mặt duy nhất, mà dùng mô hình đánh giá lai gồm: metric tự động để đo tương quan rộng, MQM/ESA để phát hiện lỗi có cấu trúc, SQM/BWS để đo chất lượng cảm nhận và độ hay, cùng benchmark chẩn đoán để đo nhất quán truyện dài.

Câu này được chống lưng bởi nhiều hướng nghiên cứu: COMET/BLEURT/BERTScore cho thấy learned semantic metrics tương quan tốt hơn n-gram; XCOMET và GEMBA-MQM chuyển hướng sang error span để giải thích lỗi; ESA cố gắng cân bằng giữa DA đơn giản và MQM tốn kém; còn các paper literary gần đây chỉ ra MQM/automatic metrics chưa đủ cho “độ văn học”.

---

## Bộ metric mình khuyên dùng cuối cùng

Nếu phải chọn stack thực tế cho repo của bạn:

**Bắt buộc báo cáo:**

1. **SacreBLEU** — baseline truyền thống, để so sánh với paper cũ.
2. **chrF++** — hợp hơn với ngôn ngữ cần linh hoạt tách từ.
3. **COMET** — semantic/reference-based score chính.
4. **COMETKiwi** — reference-free score khi không có bản dịch mẫu.
5. **XCOMET** — error span + severity/category.
6. **Human NTQS /100** — điểm chính của luận án.
7. **BWS win-rate** — điểm “độ hay / preference”.
8. **Narrative consistency accuracy** — bộ test riêng cho nhân vật, xưng hô, glossary, timeline.

**Phụ nhưng rất nên có:**

9. **GEMBA-MQM / GEMBA V2** — LLM judge để phân tích lỗi và pairwise blind comparison.
10. **TransProQA-style QA** — hỏi các câu về voice, literary device, cultural meaning, authorial intent.
11. **DITING-style category report** — idiom, ambiguity, terminology, tense/timeline, pronoun, culture.

---

## Điểm mấu chốt cho câu hỏi “làm sao code chấm được câu văn?”

Câu trả lời học thuật nên là:

**Code không chấm trực tiếp được “cái hay” như con người. Code chỉ có thể học hoặc mô phỏng một phần đánh giá của con người. Vì vậy, luận án phải lấy human evaluation làm chuẩn, rồi dùng automatic metrics như proxy đã được hiệu chuẩn.**

Nói cách khác:

- **Human NTQS** là điểm gốc.
- **COMET/XCOMET/GEMBA/TransProQA** là máy chấm phụ.
- Bạn đo xem máy chấm phụ tương quan với người chấm đến đâu.
- Nếu hệ của bạn cải thiện cả human score, BWS preference, narrative consistency và COMET/XCOMET, thì luận điểm rất mạnh.

Đây sẽ là khung đủ thuyết phục hơn nhiều so với việc nói “mình dùng BLEU/COMET để đo độ hay”.
