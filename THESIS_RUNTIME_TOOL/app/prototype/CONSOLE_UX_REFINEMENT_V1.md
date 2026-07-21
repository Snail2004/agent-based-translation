# Agent Console UX Refinement V1

Status: IMPLEMENTED through Phase D
Owner: App UI
Runtime impact: none
API/provider calls: 0

## 1. Mục tiêu

Console phải giúp người dùng trả lời nhanh năm câu hỏi:

1. Run đang chạy thật hay đang replay?
2. Run đang ở stage/phase nào, vì sao stage được chạy hoặc bị bỏ qua?
3. Sự kiện quan trọng nào vừa xảy ra?
4. Bộ nhớ và bản dịch đã thay đổi đến đâu?
5. Kết quả/gate hiện tại nói lên điều gì?

V1 tối ưu thứ bậc thông tin và khả năng điều hướng giữa các panel. Không thêm
panel cố định mới và không biến Console thành màn hình cấu hình pipeline.

## 2. Ranh giới

### 2.1 Trong phạm vi

- Làm rõ chế độ live và replay.
- Liên kết Event Stream, Stages và Run Ledger.
- Thêm chế độ tập trung vào một block.
- Thêm preset log `Quan trọng` và `Tất cả`.
- Làm rõ trạng thái phase và lý do stage bị bỏ qua.
- Thêm tùy chọn ngôn ngữ hệ thống `VI`/`EN`.
- Giữ nguyên tên/ký hiệu kỹ thuật chuẩn bằng tiếng Anh.
- Thêm tooltip/chú thích cho metric và thuật ngữ khó hiểu.
- Ghi nhớ bố cục và cung cấp thao tác đặt lại.
- Cải thiện khả năng truy cập bằng bàn phím và reduced motion.

### 2.2 Ngoài phạm vi

- Không đổi event schema, pipeline, checkpoint hoặc dữ liệu đã persist.
- Không suy diễn event còn thiếu từ snapshot.
- Không dịch source text, target text, artifact content hoặc raw log payload.
- Không đổi công thức hay ý nghĩa metric.
- Không tính lại score/cost/usage trong UI.
- Không thêm provider/API call.
- Không nối report modal trong cùng thay đổi này.
- Không sửa Cockpit.

## 3. Nguyên tắc thiết kế

1. **Một vùng chính, chi tiết theo ngữ cảnh.** Không thêm sidebar thường trực.
2. **Dữ liệu thật và cách trình bày tách biệt.** Replay/pacing/focus chỉ đổi UI.
3. **Không che nguồn gốc.** Raw identifiers, artifact names và metric IDs không
   bị dịch hoặc đổi nghĩa.
4. **Progress không được giả.** Chỉ hiển thị tiến độ từ event/artifact đã nhận.
5. **Drill-down thay cho nhồi chữ.** Tóm tắt trước, tooltip/detail khi cần.
6. **Trạng thái phải nhất quán giữa các panel.**

## 4. Ngôn ngữ hệ thống

### 4.1 Phạm vi của tùy chọn `VI`/`EN`

Ngôn ngữ hệ thống chỉ đổi phần giao diện:

- tên nút và menu;
- nhãn panel;
- empty/error/help text;
- mô tả tooltip;
- câu diễn giải trạng thái phase;
- lý do stage bị bỏ qua ở dạng thân thiện.

Ngôn ngữ hệ thống không được đổi:

- source và bản dịch;
- tên model/provider;
- `run_id`, `stage_id`, `block_id`, `window_id`;
- event name và artifact filename;
- enum/status được persist;
- metric ID và tên chuẩn tiếng Anh;
- nội dung raw log.

Giá trị mặc định là `VI`. Lựa chọn được lưu cục bộ theo trình duyệt và không
được ghi vào project/run.

### 4.2 Thuật ngữ và metric chuẩn

Các metric giữ nguyên identifier và tên tiếng Anh. Ví dụ:

- `TC` — `Term Consistency`
- `TA` — `Term Adherence`
- `TC-Occ` — `Term Consistency at Occurrence Level`
- `TA-Occ` — `Term Adherence at Occurrence Level`
- `SF-QE`
- `SF-BT`

Quy tắc hiển thị:

- Không dịch `TC` thành một ký hiệu tiếng Việt khác.
- Ở vùng đủ rộng: hiển thị `TC · Term Consistency`.
- Ở vùng hẹp: hiển thị `TC`.
- Hover hoặc focus bàn phím mở một popover chú thích.
- Không chỉ hiển thị mỗi `TC`/`TA` mà không có đường truy cập chú thích.

### 4.3 Metric glossary dùng chung

UI có một registry duy nhất cho thuật ngữ chuẩn, ví dụ:

```js
{
  "TC": {
    "canonicalName": "Term Consistency",
    "explanation": {
      "vi": "Mức độ một thuật ngữ được dịch nhất quán trong phạm vi đo.",
      "en": "How consistently a term is translated within the measured scope."
    },
    "higherIsBetter": true,
    "scope": "run"
  }
}
```

Mỗi mục tối thiểu có:

- `canonicalName`;
- mô tả `vi` và `en`;
- hướng tốt hơn (`higherIsBetter`, `lowerIsBetter` hoặc `neutral`);
- phạm vi đo;
- đơn vị/miền giá trị nếu có;
- nguồn artifact nếu UI biết chắc.

Tooltip không tự tạo công thức. Khi chưa có định nghĩa đã khóa, hiển thị
`Chưa có chú thích đã xác thực` thay vì đoán.

## 5. Cấu trúc Console

### 5.1 Thanh trên cùng

Giữ:

- quay về Workspace;
- run selector;
- system health;
- điều khiển live/replay;
- replay progress và tốc độ.

Thêm:

- selector ngôn ngữ `VI`/`EN` trong menu Theme/Preferences;
- nhãn chế độ rõ ràng:
  - `LIVE` khi theo event đang chạy;
  - `REPLAY` khi phát lại run đã lưu;
  - `PAUSED` chỉ là trạng thái phát, không thay thế nhãn `REPLAY`.

Trong replay, toàn bộ Console có dấu hiệu thị giác nhẹ nhưng liên tục; không
dùng màu cảnh báo đỏ vì replay không phải lỗi.

### 5.2 Overview bên trái

Chỉ giữ thông tin tổng quan:

- run state;
- arm;
- stage/event count;
- cost/budget;
- warning/error;
- API/provider health gọn.

Không đưa memory record hoặc kết quả score chi tiết vào đây.

### 5.3 Event Stream ở giữa

Thêm preset:

- `Quan trọng`: stage start/done/fail, warning/error, gate, checkpoint, commit,
  block ready, memory delta.
- `Tất cả`: mọi event, vẫn giữ heartbeat grouping.

Preset chỉ là bộ lọc UI, không xóa event. Người dùng vẫn có thể lọc thêm theo
stage, agent, severity và heartbeat.

Khi click một event:

- event có `block_id` -> chọn block tương ứng trong Run Ledger;
- event có `stage` -> highlight stage bên phải;
- event có `artifact` -> highlight Latest Artifact;
- event có memory delta -> chọn record tương ứng trong Memory Ledger.

Nếu target chưa xuất hiện ở cursor replay hiện tại, UI giải thích thay vì nhảy
đến dữ liệu tương lai.

### 5.4 Run Ledger phía dưới

Giữ các tab:

- `Bộ nhớ`;
- `Bản dịch`.

Trong `Bản dịch` giữ:

- `Dịch`;
- `Song song`;
- `3 cột`;
- lựa chọn arm;
- pacing;
- Follow.

Thêm chế độ `Focus block`:

- click header block hoặc nút focus để mở block đó rộng trong Run Ledger;
- Source/S0/S1 chỉ hiển thị các arm đang tồn tại;
- không tạo S0/S1 giả cho run một arm;
- Escape hoặc nút Back trở về danh sách;
- block mới đến không đẩy block đang focus;
- badge báo số block mới đang chờ.

Trên màn hình hẹp, `3 cột` không được là mặc định. UI ưu tiên `Song song` hoặc
`Focus block`.

### 5.5 Stages và Results bên phải

Stages:

- click stage -> lọc Event Stream;
- hover/focus status `skipped` -> hiện reason code và diễn giải thân thiện;
- không dịch raw reason code;
- stage đang active nổi bật hơn stage done.

Results:

- giữ metric ID và canonical English name;
- tooltip giải thích theo ngôn ngữ hệ thống;
- gom metric theo nhóm thay vì danh sách phẳng khi số lượng tăng;
- không tính lại metric từ block text;
- click artifact/metric chỉ mở read-only detail từ persisted artifact.

### 5.6 Banner phase

Banner phải tách hai ý:

1. artifact/phase nào đã sẵn sàng;
2. replay/live cursor hiện đang ở đâu.

Ví dụ:

```text
Phase 1 đã sẵn sàng · Replay hiện ở Phase 2 / Cascade
```

Không dùng câu `Phase 1 xong` đơn lẻ khi stage bên phải đang chạy ở phase khác.

## 6. Bố cục và khả năng đọc

- Các panel trái, phải và Run Ledger tiếp tục co giãn/thu gọn.
- Divider giữ màu tối nhẹ như giao diện hiện tại.
- Handle chỉ nổi rõ khi hover/focus/drag.
- Lưu theo trình duyệt:
  - chiều rộng sidebar;
  - chiều cao Run Ledger;
  - panel collapsed;
  - tab Run Ledger;
  - chế độ Dịch/Song song/3 cột;
  - ngôn ngữ hệ thống.
- Có `Đặt lại bố cục` trong Preferences.
- Không lưu replay cursor như một phần project.

## 7. Khả năng truy cập

- Tooltip mở được bằng hover và keyboard focus.
- Escape đóng tooltip/focus block.
- Có `aria-label` cho icon-only controls.
- Màu không phải tín hiệu duy nhất cho live/replay/error.
- Tôn trọng `prefers-reduced-motion`.
- Không tự cuộn khi Follow đang tắt hoặc người dùng đang focus block.

## 8. Thứ tự triển khai

### Phase A — Language và semantic labels

- Tạo UI locale registry `VI`/`EN`.
- Tạo metric glossary dùng chung.
- Thêm language selector và persistence.
- Thêm tooltip cho `TC`, `TA` và các metric hiện có.
- Không đổi payload/API.

### Phase B — Replay clarity và log presets

- Tách nhãn `LIVE`/`REPLAY`/`PAUSED`.
- Sửa banner phase.
- Thêm preset `Quan trọng`/`Tất cả`.
- Thêm tooltip cho skipped reason.

### Phase C — Cross-panel navigation (implemented)

- Event -> stage/block/artifact/memory.
- Stage -> filtered events.
- Bảo vệ replay cursor khỏi future-data jumps.
- Dev harness thêm một `block_done` từ `block_preview_sample.json` để test block
  navigation cho log lịch sử chưa có `payload.block_id`; golden event log không đổi.
- Event chỉ mang target từ field persisted (`block_id`, `artifact_path`,
  `memory_delta_v1.delta_id`, `stage`); UI không parse message để suy target.
- Block và committed memory record được mở trong Run Ledger và làm nổi rõ.
- Artifact đã xuất hiện được chọn ở panel Results; stage đã xuất hiện lọc Event Stream.
- Khi rewind làm target nằm sau cursor, UI xóa selection và báo target chưa khả dụng;
  replay cursor không bị tự động dịch chuyển.

### Phase D — Focus block và layout persistence (implemented)

- Focus block chỉ trình bày source và các arm thật sự tồn tại; Escape, nút Back
  và nút đóng đều quay lại luồng.
- Block mới không thay block đang focus. Khi focus hoặc Follow tắt, UI giữ vị
  trí đọc và báo số cập nhật đang chờ để người dùng chủ động tiếp tục.
- Bố cục được lưu tại localStorage key `thesis.agentconsole.layout.v1`: chiều
  rộng hai sidebar, chiều cao Run Ledger, trạng thái collapsed, tab/surface,
  chế độ Dịch/Song song/3 cột và locale. Replay cursor không được persist.
- Nút `Đặt lại bố cục` khôi phục toàn bộ giá trị trình bày mặc định mà không
  chạm run/project data.
- Màn hình hẹp tự fallback khỏi `3 cột`; toolbar được wrap và focus view chuyển
  thành một cột để không gây tràn ngang.

Mỗi phase là một commit riêng và phải qua visual/browser QA trước phase sau.

## 9. Acceptance

### 9.1 Ngôn ngữ

1. Đổi `VI`/`EN` chỉ thay chrome/help text.
2. Source, target, IDs, event names và artifact names byte-identical.
3. `TC`, `TA` và canonical English names không bị dịch.
4. Tooltip giải thích đúng locale và dùng được bằng bàn phím.
5. Reload giữ lựa chọn locale.

### 9.2 Live/replay và phase

1. Người dùng phân biệt được live với replay khi nhìn một lần.
2. Replay pause vẫn giữ nhãn replay.
3. Banner không mâu thuẫn với stage cursor hiện tại.
4. UI không tạo progress/event giả.

### 9.3 Điều hướng

1. Click block event chọn đúng block.
2. Click stage lọc đúng event.
3. Click memory delta chọn đúng committed record.
4. Không điều hướng vào dữ liệu nằm sau replay cursor.

### 9.4 Run Ledger

1. Focus block không bị block mới đẩy khỏi vị trí.
2. Follow off giữ vị trí và báo pending.
3. Run một arm không hiện cột arm không tồn tại.
4. Màn hình hẹp không ép ba cột.

### 9.5 Regression

1. Replay pacing hiện tại không đổi.
2. Heartbeat grouping hiện tại không đổi.
3. Memory Delta committed-only không đổi.
4. Không có backend/provider/API call mới.
5. Không mutate persisted run/project data.
6. Browser console không có error.

## 10. Quyết định khóa trước implementation

- `VI`/`EN` là ngôn ngữ giao diện, không phải ngôn ngữ nội dung.
- Metric identifiers và canonical metric names giữ tiếng Anh.
- Chú thích metric là tooltip/popover, không phải đổi tên metric.
- Không thêm sidebar/panel cố định.
- Không gộp dữ liệu canonical source, translation artifact, evaluation report
  hoặc memory delta thành một payload UI tự chế.
- UI chỉ trình bày persisted facts; không tái tính hoặc suy dựng lịch sử.
