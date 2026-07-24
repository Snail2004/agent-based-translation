# TASK_APP_SOURCE_PACKAGE_UI_INTEGRATION_V1 - Noi UI vao Source Package da tich hop

- **Status:** READY_FOR_UI_IMPLEMENTATION
- **Backend baseline:** `main@365401ee5725b8b7207b0428f14928a032adcaa8`
- **API mode:** 0-API cho luong nhap/chuan hoa/bien soan/finalize/export; chay pipeline that van theo Run Control hien co
- **Ownership:** App UI chi sua `THESIS_RUNTIME_TOOL/app/prototype/**`

## 1. Muc tieu

Noi giao dien hien tai vao backend Source Package da co san de mot project moi di theo luong:

```text
Tao project -> Tai tep nguon -> Chuan hoa -> Kiem tra cau truc
-> Chinh sua truoc run -> Chot cau truc -> Chuan bi runtime
-> Chay pipeline -> Cau truc bi dong bang -> Xuat ban dich
```

UI khong tu parse tai lieu, khong sua truc tiep `document.json`, khong tu tao ID/hash/path va khong lap lai logic validator cua backend. Backend la authority duy nhat cho package, lifecycle va publication.

## 2. Pham vi

### IN

- Them cac ham API Source Package vao `app/prototype/api.js`.
- Noi modal **Nhap tai lieu** hien co vao managed Source Package.
- Them mot workspace/tab **Cau truc** de xem va bien soan chuong/unit truoc khi chay.
- Ho tro doi ten, phan loai, tach, gop unit lien ke va gan quan he cha-con.
- Chot cau truc, chuan bi runtime, hien thi trang thai dong bang.
- Them thao tac xuat HTML/Markdown tu mot `canonical_translation_overlay_v1` hop le.
- Kiem tra bang browser/Playwright o desktop va mobile.

### OUT

- Khong sua `pipeline/ingest/**`, backend routes/services, schema, SQLite hay checkpoint.
- Khong noi D2L/Literary thanh consumer load-bearing trong task nay.
- Khong goi LLM de sua cau truc trong UI v1.
- Khong tu suy ra overlay dich tu SQLite/run artifact o frontend.
- Khong them PDF/EPUB output; publication hien tai xuat HTML/Markdown.
- Khong dua co ky thuat day dac len tung block. Chi tiet cau truc nam trong workspace rieng.

Neu UI phat hien backend contract thieu, dung va bao exact endpoint/payload; khong tu mo rong backend.

## 3. Nguon su that va quy tac lifecycle

UI phai doc `GET /api/projects/<doc_id>/source-package` moi khi mo project va sau moi mutation.

| `mode` | Y nghia | UI duoc phep |
|---|---|---|
| `unmanaged_draft` | Project moi, chua co package | Tai nguon, chuan hoa neu `normalize_allowed=true` |
| `legacy_only` | Project/run cu da co du lieu legacy | Giu luong cu; khong hien nut managed normalize |
| `managed_draft` | Package da tao, chua chot hoac vua co revision moi | Review, correction, hierarchy, finalize |
| `managed_finalized_pre_run` | Cau truc da chot, chua run | Prepare runtime, bat dau run; van co the sua nhung phai canh bao se huy finalization |
| `managed_run_started_frozen` | Run dau tien da khoa package | Cau truc chi doc; cho phep run/resume va publication |

Quy tac san pham:

1. Project chi duoc sua cau truc khi chua co pipeline run.
2. Correction/hierarchy truoc run tao revision bat bien moi, khong sua package cu tai cho.
3. Run dau tien dong bang dung package/ID da chot.
4. Sau `run_started_frozen`, moi nut sua cau truc phai disable.
5. Muon doi cau truc sau run phai tao project/revision moi va chay lai toan bo; UI v1 khong migration run cu.
6. Neu status/state/hash stale hoac malformed, fail closed va yeu cau reload; khong fallback sang legacy.

## 4. API contract UI phai dung

Response thanh cong dung envelope hien co cua app. Loi nam trong envelope `errors`; UI phai hien `code` va `message`, khong nuot loi.

### 4.1 Tai nguon va chuan hoa

| Method | Endpoint | Body |
|---|---|---|
| `POST` | `/api/projects/<doc_id>/source` | `multipart/form-data`, field tep nguon theo API hien co |
| `GET` | `/api/projects/<doc_id>/source-package` | Khong co |
| `POST` | `/api/projects/<doc_id>/source-package/normalize` | Bat buoc dung JSON `{}` |

`normalize` khong chap nhan tuy chon client. UI khong gui Pandoc path, parser, model, executable hoac filesystem path.

Lan dau co the tra `201 created=true`; lap lai byte-identical co the tra `200 reused=true`. Ca hai deu la thanh cong.

### 4.2 Lay du lieu review

```http
GET /api/projects/<doc_id>/source-package/review
```

UI luu tam dung cac gia tri server tra ve:

```json
{
  "expected": {
    "state_sha256": "...",
    "candidate_tree_sha256": "...",
    "report_sha256": "...",
    "hierarchy_sha256": null
  },
  "supported_actions": [
    "update_unit",
    "split_unit",
    "merge_adjacent_units"
  ],
  "supported_hierarchy_actions": [
    "set_parent",
    "clear_parent"
  ],
  "report": {}
}
```

Sau moi correction/hierarchy/finalize, bo snapshot nay het han. UI phai goi lai status va review truoc mutation tiep theo.

### 4.3 Correction

```http
POST /api/projects/<doc_id>/source-package/corrections
Content-Type: application/json
```

Envelope bat buoc:

```json
{
  "expected_state_sha256": "<review.expected.state_sha256>",
  "expected_candidate_tree_sha256": "<review.expected.candidate_tree_sha256>",
  "expected_report_sha256": "<review.expected.report_sha256>",
  "approved": true,
  "user": "<current user>",
  "actions": []
}
```

Chi gui action do server cong bo trong `supported_actions`:

```json
{
  "action_type": "update_unit",
  "unit_id": "u0004",
  "new_title": "Chapter IV",
  "classification": "translate"
}
```

```json
{
  "action_type": "split_unit",
  "unit_id": "u0004",
  "at_block_id": "book_ch04_b001",
  "left_title": "Chapter III",
  "right_title": "Chapter IV",
  "left_classification": "translate",
  "right_classification": "translate"
}
```

```json
{
  "action_type": "merge_adjacent_units",
  "left_unit_id": "u0004",
  "right_unit_id": "u0005",
  "new_title": "Chapter IV",
  "classification": "translate"
}
```

Classification hop le duy nhat:

| Gia tri API | Nhan UI |
|---|---|
| `translate` | Dich |
| `preserve` | Giu nguyen |
| `exclude` | Loai |
| `review` | Can xem lai |

`structured_translate` la kenh admission duoc code suy ra cho rich content, khong phai classification ma UI duoc gui.

UI khong gui `action_id`, proposer, authority, plan hash, candidate path hoac ID tu tao.

### 4.4 Hierarchy

```http
POST /api/projects/<doc_id>/source-package/hierarchy
Content-Type: application/json
```

```json
{
  "expected_state_sha256": "<review.expected.state_sha256>",
  "expected_candidate_tree_sha256": "<review.expected.candidate_tree_sha256>",
  "expected_report_sha256": "<review.expected.report_sha256>",
  "approved": true,
  "user": "<current user>",
  "actions": [
    {
      "action_type": "set_parent",
      "child_unit_id": "u0004_ii",
      "parent_unit_id": "u0004"
    }
  ]
}
```

Xoa parent:

```json
{
  "action_type": "clear_parent",
  "child_unit_id": "u0004_ii"
}
```

UI chi cho chon ID dang ton tai trong review; khong cho self-parent, forward reference hay drag-reorder block. Backend van la validator cuoi cung.

### 4.5 Finalize

```http
POST /api/projects/<doc_id>/source-package/finalize
Content-Type: application/json
```

```json
{
  "expected_state_sha256": "<review.expected.state_sha256>",
  "expected_candidate_tree_sha256": "<review.expected.candidate_tree_sha256>",
  "expected_report_sha256": "<review.expected.report_sha256>",
  "expected_hierarchy_sha256": "<review.expected.hierarchy_sha256-or-null>",
  "approved": true,
  "user": "<current user>"
}
```

Nut **Chot cau truc** can modal xac nhan. Thanh cong phai reload status va hien `managed_finalized_pre_run`.

### 4.6 Runtime va run

```http
POST /api/projects/<doc_id>/runtime/prepare
GET  /api/projects/<doc_id>/runtime
POST /api/thesis/runs
```

`runtime/prepare` chi hop le tu package managed da finalize. API client da co `prepareProjectRuntime`; UI khong tu tao runtime manifest.

Ngay khi backend chap nhan run dau tien, package chuyen sang `managed_run_started_frozen`. UI phai reload status va khoa moi control bien soan. Resume dung Run Control hien co; khong tao package moi.

### 4.7 Publication

```http
POST /api/projects/<doc_id>/source-package/publications
Content-Type: application/json
```

Body phai la mot object `canonical_translation_overlay_v1` day du, dung thu tu va exact-cover package da bi dong bang. Endpoint chi hop le sau run dau tien.

UI v1 nen co dialog:

1. Chon/tai file overlay JSON da co.
2. Parse JSON cuc bo chi de bao loi cu phap.
3. Gui nguyen object cho backend; khong tu them/sua row.
4. Hien `created/reused`, publication ID va danh sach output backend tra ve.
5. Mo/tai `document.html`, `document.md`, `export_manifest.json` theo duong dan/route server cung cap; khong tu ghep path ngoai contract.

UI khong duoc tu lay mot bang translation bat ky va gan nhan canonical overlay. Noi mot run cu the thanh overlay la task backend/consumer rieng neu chua co artifact hop le.

## 5. Thiet ke man hinh de xuat

### 5.1 Vi tri

- Giu nut **Nhap tai lieu** o header hien tai.
- Them tab **Cau truc** vao cum dieu huong trung tam, gan `Book`/`Memory`.
- Sau khi import thanh cong, chuyen thang sang **Cau truc**.
- Khong chen badge/co ky thuat vao tat ca block o man hinh dich.

### 5.2 Workspace Cau truc

Bo cuc ba vung de scan nhanh:

- Trai: danh sach unit/chuong, title, so block, classification va review state.
- Giua: preview block theo unit; cho chon diem tach tai ranh gioi block co san.
- Phai: Details cua unit, classification selector, parent selector va action history ngan.

Thanh tren cung:

- Lifecycle badge.
- Tong unit/block va so muc `review`.
- Cac lenh: **Chuan hoa**, **Tach**, **Gop**, **Doi ten**, **Chot cau truc**.
- Hash/ID day du chi nam trong menu **Chi tiet ky thuat**, mac dinh an.

Khong dung card long card. Bang/danh sach la mat phang lam viec chinh; modal chi dung cho import, split/merge confirmation, finalize va publication.

### 5.3 Trang thai bat buoc

- Loading/skeleton trong luc normalize hoac mutate.
- Empty state neu chua upload source.
- `409 stale`: thong bao "Cau truc da thay doi o luot khac", reload review; khong tu retry mutation.
- `409 source_package_frozen`: chuyen UI sang read-only ngay.
- `legacy_only`: giai thich day la project cu va giu nut dieu huong sang workspace legacy.
- Loi malformed/tamper/foreign: blocking error, khong hien action co the ghi.
- Action dang submit phai disable de tranh double click; backend co idempotency nhung UI khong duoc tao request song song.

## 6. API client de them

Them cac ham nho vao `app/prototype/api.js` theo helper `request` hien co:

```text
getSourcePackageStatus(docId)
normalizeSourcePackage(docId)             // POST body {}
getSourcePackageReview(docId)
applySourcePackageCorrections(docId, body)
applySourcePackageHierarchy(docId, body)
finalizeSourcePackage(docId, body)
publishSourcePackage(docId, overlay)
```

Khong hard-code backend port/base URL moi. Dung cung `API_BASE` va error handling hien co.

## 7. Du kien file UI

UI owner tu chot ten component, nhung write set nen gioi han trong:

```text
THESIS_RUNTIME_TOOL/app/prototype/api.js
THESIS_RUNTIME_TOOL/app/prototype/app.jsx
THESIS_RUNTIME_TOOL/app/prototype/parts_project.jsx
THESIS_RUNTIME_TOOL/app/prototype/styles.css
THESIS_RUNTIME_TOOL/app/prototype/parts_source_package.jsx   # new, neu can
THESIS_RUNTIME_TOOL/app/prototype/tests/**                   # neu test UI nam day
```

Truoc khi sua, kiem tra branch UI moi nhat va tranh ghi de thay doi dang dirty cua session khac.

## 8. Acceptance criteria

### 8.1 Contract/UI flow

Tren mot project moi va backend local:

1. Upload duoc tung dinh dang TXT, Markdown, HTML, EPUB va PDF.
2. Normalize tao `managed_draft`; bam lai reuse cung candidate.
3. Review hien dung unit/order/classification va chi tiet block.
4. Doi ten/phan loai thanh cong; stale hash bi chan va UI reload dung.
5. Split chi tai block boundary; merge chi unit lien ke.
6. Set/clear parent thanh cong; hierarchy loi hien dung server error.
7. Finalize chuyen `managed_finalized_pre_run`.
8. Runtime prepare thanh cong tu package da finalize.
9. Sau run-start fixture, UI hien `managed_run_started_frozen` va moi edit control bi khoa.
10. Overlay hop le tao publication; overlay thieu row bi fail closed va khong co output gia.
11. Project `legacy_only` van mo duoc UI cu va khong bi managed normalize ngam.

### 8.2 Browser QA

- Playwright screenshot o 1440x900, 1024x768 va 390x844.
- Khong text overflow, khong overlap header/sidebar/right panel/modal.
- Keyboard focus ro rang cho tab, list, selector va modal.
- Split/merge/finalize co confirmation va khong double-submit.
- Reload trang khong lam mat lifecycle/status do server quan ly.

### 8.3 Regression va scope

```powershell
python -m pytest THESIS_RUNTIME_TOOL/app/backend/tests/test_source_lifecycle.py -q
python -m pytest THESIS_RUNTIME_TOOL/app/backend/tests/test_project_runtime.py -q
python -m pytest THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_runs.py -q
python -m pytest THESIS_RUNTIME_TOOL/app/backend/tests/test_api_smoke.py -q
git diff --check
git status --short
```

Task UI khong duoc goi provider API, doc credential, sua canonical package hay ghi vao DB/checkpoint cua run da co.

## 9. Definition of done

- UI co mot duong lien tuc tu import den finalized package, runtime va publication.
- Moi mutation gui dung expected hashes tu review moi nhat.
- Backend error/freeze/legacy state duoc the hien trung thuc.
- Khong co logic parse/ID/hash/package authority bi lap lai trong frontend.
- Browser QA dat tren desktop/mobile.
- Commit UI sach, chi gom file App UI da cong bo; khong push neu user chua yeu cau.

## 10. Ghi chu cho App UI session

Backend va `pipeline/ingest` tren source main da la contract hien hanh. Hay tich hop nhu mot consumer, khong sua producer cho vua UI. Neu can thay doi contract, tra lai exact request/response va ca test that bai truoc khi cham backend.
