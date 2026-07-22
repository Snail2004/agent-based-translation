/* ===== PROJECT / SOURCE SCREEN: project selection, upload, metadata, extract ===== */

function quickImportStem(filename) {
  return String(filename || "document").replace(/\.[^.]+$/, "").trim() || "document";
}

function quickImportDocId(filename, projects) {
  const stem = quickImportStem(filename)
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, "_")
    .replace(/^[_.-]+|[_.-]+$/g, "") || "document";
  const existing = new Set((projects || []).map(project => project.doc_id));
  if (!existing.has(stem)) return stem;
  let suffix = 2;
  while (existing.has(`${stem}_${suffix}`)) suffix += 1;
  return `${stem}_${suffix}`;
}

function quickImportFileSize(bytes) {
  const size = Number(bytes || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

const QUICK_IMPORT_SOURCE_RE = /\.(txt|epub|md|markdown|html|htm|pdf)$/i;

const QUICK_IMPORT_D2L_FILES = [
  { field: "source", vi: "Nguồn đã đánh dấu", en: "Marked source", filename: "d2l_full_book_en_marked_v1.md", accept: ".md" },
  { field: "block_map", vi: "Bản đồ block", en: "Block map", filename: "block_map.json", accept: ".json" },
  { field: "manifest", vi: "Manifest", en: "Manifest", filename: "manifest.json", accept: ".json" },
];

function quickImportErrorDetail(error) {
  const first = error?.errors?.[0] || error?.payload?.errors?.[0] || {};
  return {
    code: String(first.code || "request_failed"),
    message: String(first.message || error?.message || uiText("Yêu cầu thất bại.", "Request failed.")),
    status: Number(error?.status || 0),
  };
}

function quickImportSourceFormat(filename) {
  const lower = String(filename || "").toLowerCase();
  if (/\.(md|markdown)$/.test(lower)) return "markdown";
  if (/\.(html|htm)$/.test(lower)) return "html";
  if (lower.endsWith(".epub")) return "epub";
  if (lower.endsWith(".pdf")) return "pdf";
  return "txt";
}

function QuickImportModal({ projects, activeDocId, onClose, onCreateProject, onUploadSource, onNormalize, onImportD2LPresegmented, onOpenStructure, onOpenAdvanced }) {
  const [step, setStep] = React.useState(1);
  const [mode, setMode] = React.useState("standard");
  const [file, setFile] = React.useState(null);
  const [d2lFiles, setD2lFiles] = React.useState({ source: null, block_map: null, manifest: null });
  const [d2lFileErrors, setD2lFileErrors] = React.useState({});
  const [profile, setProfile] = React.useState("literary");
  const [docId, setDocId] = React.useState("");
  const [title, setTitle] = React.useState("");
  const [idTouched, setIdTouched] = React.useState(false);
  const [titleTouched, setTitleTouched] = React.useState(false);
  const [fileError, setFileError] = React.useState("");
  const [busy, setBusy] = React.useState("");
  const [error, setError] = React.useState(null);
  const [createdDocId, setCreatedDocId] = React.useState("");
  const [sourceUploaded, setSourceUploaded] = React.useState(false);
  const [result, setResult] = React.useState(null);

  const supportedFile = file && QUICK_IMPORT_SOURCE_RE.test(file.name || "");
  const validDocId = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(docId.trim());
  const existingProject = (projects || []).find(project => project.doc_id === docId.trim());
  const activeReusableProject = (projects || []).find(project => project.doc_id === activeDocId && project.status === "created");
  const reuseExisting = mode === "d2l-presegmented" && existingProject?.status === "created";
  const duplicateDocId = !!existingProject;
  const d2lFilesComplete = QUICK_IMPORT_D2L_FILES.every(item => !!d2lFiles[item.field]);
  const d2lFilesUnique = new Set(Object.values(d2lFiles).filter(Boolean).map(item => `${item.name}:${item.size}:${item.lastModified}`)).size === 3;
  const d2lReady = d2lFilesComplete && d2lFilesUnique && Object.values(d2lFileErrors).every(value => !value);
  const canContinue = mode === "d2l-presegmented"
    ? d2lReady && validDocId && (!duplicateDocId || reuseExisting) && !!title.trim()
    : !!supportedFile && validDocId && !duplicateDocId && !!title.trim();
  const close = () => { if (!busy) onClose(); };

  React.useEffect(() => {
    function onKey(event) {
      if (event.key === "Escape" && !busy) close();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy]);

  React.useEffect(() => {
    if (mode !== "d2l-presegmented" || idTouched || !activeReusableProject) return;
    setDocId(activeReusableProject.doc_id);
    if (!titleTouched) setTitle(activeReusableProject.title || activeReusableProject.metadata?.title || activeReusableProject.doc_id);
  }, [mode, idTouched, titleTouched, activeReusableProject?.doc_id, activeReusableProject?.title, activeReusableProject?.metadata?.title]);

  function selectFile(nextFile) {
    if (!nextFile) return;
    if (!QUICK_IMPORT_SOURCE_RE.test(nextFile.name || "")) {
      setFile(null);
      setFileError(uiText("Hệ thống nhận TXT, EPUB, Markdown, HTML hoặc PDF.", "Supported formats: TXT, EPUB, Markdown, HTML, or PDF."));
      return;
    }
    setFile(nextFile);
    setFileError("");
    if (!idTouched) setDocId(quickImportDocId(nextFile.name, projects));
    if (!titleTouched) setTitle(quickImportStem(nextFile.name));
  }

  function selectD2LFile(field, nextFile) {
    const descriptor = QUICK_IMPORT_D2L_FILES.find(item => item.field === field);
    if (!descriptor || !nextFile) return;
    const nextErrors = { ...d2lFileErrors };
    if (nextFile.name !== descriptor.filename) {
      setD2lFiles(current => ({ ...current, [field]: null }));
      nextErrors[field] = uiText(`Tên file phải chính xác là ${descriptor.filename}.`, `The filename must be exactly ${descriptor.filename}.`);
      setD2lFileErrors(nextErrors);
      return;
    }
    const duplicate = Object.entries(d2lFiles).some(([otherField, otherFile]) => (
      otherField !== field && otherFile && (
        otherFile === nextFile
        || (otherFile.name === nextFile.name && otherFile.size === nextFile.size && otherFile.lastModified === nextFile.lastModified)
      )
    ));
    if (duplicate) {
      setD2lFiles(current => ({ ...current, [field]: null }));
      nextErrors[field] = uiText("Không thể dùng cùng một file cho hai trường.", "The same file cannot be used for two fields.");
      setD2lFileErrors(nextErrors);
      return;
    }
    delete nextErrors[field];
    setD2lFiles(current => ({ ...current, [field]: nextFile }));
    setD2lFileErrors(nextErrors);
    if (field === "source") {
      if (!idTouched) setDocId(activeReusableProject?.doc_id || quickImportDocId(nextFile.name, projects));
      if (!titleTouched) setTitle(activeReusableProject?.title || activeReusableProject?.metadata?.title || quickImportStem(nextFile.name));
    }
  }

  function handleD2LDrop(field, event) {
    event.preventDefault();
    selectD2LFile(field, event.dataTransfer?.files?.[0]);
  }

  function handleDrop(event) {
    event.preventDefault();
    selectFile(event.dataTransfer?.files?.[0]);
  }

  async function runImport() {
    if (!canContinue && !createdDocId) return;
    setStep(3);
    setError(null);
    let targetDocId = createdDocId || (mode === "d2l-presegmented" && reuseExisting ? docId.trim() : "");
    let uploaded = sourceUploaded;
    try {
      if (mode === "d2l-presegmented") {
        if (!targetDocId) {
          setBusy("create");
          const created = await onCreateProject(docId.trim(), {
            title: title.trim(),
            author: "",
            domain: "technical",
            genre: "technical",
            source_format: "markdown",
            license: "",
            source_url: "",
            contamination_risk: "low",
          }, { activate: false, throwOnError: true });
          if (!created?.doc_id) throw new Error(uiText("Không thể tạo project mới.", "Could not create the new project."));
          targetDocId = created.doc_id;
          setCreatedDocId(targetDocId);
        }
        if (!createdDocId && targetDocId) setCreatedDocId(targetDocId);
        setBusy("import-d2l");
        const imported = await onImportD2LPresegmented(targetDocId, d2lFiles);
        if (!imported) throw new Error(uiText("Backend không trả kết quả import.", "The backend did not return an import result."));
        setResult({
          docId: targetDocId,
          mode: "d2l_presegmented",
          reused: imported.reused === true,
        });
        setBusy("");
        if (onOpenStructure) await onOpenStructure(targetDocId, { reload: true });
        return;
      }
      if (!targetDocId) {
        setBusy("create");
        const sourceFormat = quickImportSourceFormat(file.name);
        const metadata = {
          title: title.trim(),
          author: "",
          domain: profile === "technical" ? "technical" : "literature",
          genre: profile === "technical" ? "technical" : "novel",
          source_format: sourceFormat,
          license: "",
          source_url: "",
          contamination_risk: "low",
        };
        const created = await onCreateProject(docId.trim(), metadata, { activate: false });
        if (!created?.doc_id) throw new Error(uiText("Không thể tạo project mới.", "Could not create the new project."));
        targetDocId = created.doc_id;
        setCreatedDocId(targetDocId);
      }

      if (!uploaded) {
        setBusy("upload");
        const uploadedResult = await onUploadSource(file, false, targetDocId);
        if (!uploadedResult) throw new Error(uiText("Không thể tải file nguồn lên project.", "Could not upload the source file to the project."));
        uploaded = true;
        setSourceUploaded(true);
      }

      setBusy("normalize");
      const normalizeResult = await onNormalize(targetDocId);
      if (!normalizeResult) throw new Error(uiText("Không thể chuẩn hóa source package.", "Could not normalize the source package."));
      setResult({
        docId: targetDocId,
        mode: normalizeResult.mode || "managed_draft",
        reused: normalizeResult.reused === true,
      });
      setBusy("");
      if (onOpenStructure) await onOpenStructure(targetDocId);
    } catch (err) {
      setBusy("");
      setError(quickImportErrorDetail(err));
    }
  }

  const actions = step === 1 ? <>
    <button className="btn" type="button" onClick={close}>{uiText("Hủy", "Cancel")}</button>
    <button className="btn primary" type="button" disabled={!canContinue} onClick={() => setStep(2)}>{uiText("Tiếp tục", "Continue")} <Ic.arrowRight size={13} /></button>
  </> : step === 2 ? <>
    <button className="btn" type="button" onClick={() => setStep(1)}>{uiText("Quay lại", "Back")}</button>
    <button className="btn primary" type="button" onClick={runImport}><Ic.sparkle size={13} />{mode === "d2l-presegmented" ? uiText("Nhập gói D2L", "Import D2L bundle") : uiText("Tạo và chuẩn hóa", "Create and normalize")}</button>
  </> : result ? <>
    {onOpenAdvanced && <button className="btn" type="button" onClick={onOpenAdvanced}><Ic.folder size={13} />{uiText("Dự án / Nguồn", "Project / Source")}</button>}
    <button className="btn primary" type="button" onClick={close}>{uiText("Mở Cấu trúc", "Open Structure")}</button>
  </> : error ? <>
    <button className="btn" type="button" onClick={() => setStep(createdDocId ? 2 : 1)}>
      <Ic.chevRight size={13} style={{ transform: "rotate(180deg)" }} />{createdDocId ? uiText("Xem lại", "Review") : uiText("Sửa thông tin", "Edit details")}
    </button>
    <button className="btn primary" type="button" onClick={runImport}><Ic.refresh size={13} />{uiText("Thử lại", "Retry")}</button>
  </> : <button className="btn primary" type="button" disabled><span className="as-spin" />{uiText("Đang xử lý", "Processing")}</button>;

  return (
    <Modal title={uiText("Nhập tài liệu mới", "Import new document")} icon={Ic.upload} className="quick-import-modal" onClose={close} actions={actions}>
      <div className="quick-import-steps" aria-label={uiText(`Bước ${step} trên 3`, `Step ${step} of 3`)}>
        {[["Nguồn", "Source"], ["Xác nhận", "Confirm"], mode === "d2l-presegmented" ? ["Nhập gói", "Import"] : ["Chuẩn hóa", "Normalize"]].map(([vi, en], index) => {
          const number = index + 1;
          const state = number === step ? "active" : number < step ? "done" : "";
          return <div key={en} className={`quick-import-step ${state}`}><span>{number < step ? <Ic.check size={11} /> : number}</span><b>{uiText(vi, en)}</b></div>;
        })}
      </div>

      {step === 1 && <div className="quick-import-pane">
        <div className="quick-import-mode-label">{uiText("Cách nhập", "Import method")}</div>
        <div className="quick-import-mode" role="group" aria-label={uiText("Chọn cách nhập", "Choose import method")}>
          <button type="button" data-testid="quick-import-standard" className={mode === "standard" ? "active" : ""} aria-pressed={mode === "standard"} onClick={() => { setMode("standard"); setError(null); }}>
            <Ic.upload size={14} /><span><b>{uiText("Tài liệu thông thường", "Standard document")}</b><em>{uiText("TXT, EPUB, Markdown, HTML hoặc PDF", "TXT, EPUB, Markdown, HTML, or PDF")}</em></span>
          </button>
          <button type="button" data-testid="quick-import-d2l" className={mode === "d2l-presegmented" ? "active" : ""} aria-pressed={mode === "d2l-presegmented"} onClick={() => { setMode("d2l-presegmented"); setError(null); }}>
            <Ic.layers size={14} /><span><b>{uiText("Gói D2L đã phân đoạn", "D2L pre-segmented bundle")}</b><em>{uiText("Giữ nguyên chapter, block ID và thứ tự nguồn", "Preserves chapters, block IDs, and source order")}</em></span>
          </button>
        </div>

        {mode === "standard" ? <>
          <label className={`quick-import-drop${file ? " has-file" : ""}`}
            onDragOver={event => event.preventDefault()} onDrop={handleDrop}>
            <input type="file" accept=".txt,.epub,.md,.markdown,.html,.htm,.pdf" onChange={event => selectFile(event.target.files?.[0])} />
            <span className="quick-import-drop-icon"><Ic.upload size={18} /></span>
            <span className="quick-import-drop-copy">
              <b>{file ? file.name : uiText("Chọn hoặc kéo thả tài liệu", "Choose or drop a document")}</b>
              <em>{file ? quickImportFileSize(file.size) : "TXT, EPUB, Markdown, HTML hoặc PDF"}</em>
            </span>
            <span className="btn sm">{uiText("Chọn file", "Choose file")}</span>
          </label>
          {fileError && <div className="quick-import-error"><Ic.alert size={12} />{fileError}</div>}
        </> : <>
          <div className="quick-import-d2l-intro"><Ic.lock size={12} /><span>{uiText("Backend sẽ giữ nguyên 22 chương, block_id và order_index; UI không gửi options hoặc ZIP.", "The backend preserves the 22 chapters, block IDs, and order indexes; the UI sends no options or ZIP.")}</span></div>
          <div className="quick-import-d2l-files" aria-label={uiText("Ba file của gói D2L", "Three D2L bundle files")}>
            {QUICK_IMPORT_D2L_FILES.map(item => {
              const selected = d2lFiles[item.field];
              const fieldError = d2lFileErrors[item.field];
              return <div key={item.field} className={`quick-import-file-pick${selected ? " has-file" : ""}${fieldError ? " has-error" : ""}`}>
                <label onDragOver={event => event.preventDefault()} onDrop={event => handleD2LDrop(item.field, event)}>
                  <input type="file" accept={item.accept} data-testid={`quick-import-d2l-${item.field}`} onChange={event => selectD2LFile(item.field, event.target.files?.[0])} />
                  <span className="quick-import-drop-icon"><Ic.file size={17} /></span>
                  <span className="quick-import-drop-copy">
                    <b>{selected ? selected.name : uiText(item.vi, item.en)}</b>
                    <em>{selected ? quickImportFileSize(selected.size) : item.filename}</em>
                  </span>
                  <span className="btn sm">{uiText("Chọn file", "Choose file")}</span>
                </label>
                {fieldError && <div className="quick-import-error"><Ic.alert size={12} />{fieldError}</div>}
              </div>;
            })}
          </div>
        </>}

        <div className="form-grid quick-import-fields">
          <label className="form-field">
            <span className="form-label">{uiText("Tên tài liệu", "Document title")}</span>
            <input value={title} onChange={event => { setTitleTouched(true); setTitle(event.target.value); }} placeholder={uiText("Tên hiển thị", "Display title")} />
          </label>
          <label className="form-field">
            <span className="form-label">Project ID</span>
            <input className="mono" value={docId} onChange={event => { setIdTouched(true); setDocId(event.target.value); }} placeholder="document_id" />
            {docId && !validDocId && <span className="field-error">{uiText("Chỉ dùng chữ, số, dấu chấm, gạch ngang hoặc gạch dưới.", "Use only letters, numbers, periods, hyphens, or underscores.")}</span>}
            {validDocId && duplicateDocId && !reuseExisting && <span className="field-error">{uiText("Project ID này đã tồn tại và không ở trạng thái có thể thử nhập lại.", "This project ID already exists and is not in a state eligible for another import attempt.")}</span>}
            {validDocId && reuseExisting && <span className="field-hint">{uiText("Project đã tạo nhưng chưa hoàn tất sẽ được dùng lại; backend vẫn kiểm tra source package, runtime và run trước khi ghi.", "The created but incomplete project will be reused; the backend still validates its source package, runtime, and runs before writing.")}</span>}
          </label>
        </div>

        {mode === "standard" ? <>
          <div className="quick-import-profile-label">{uiText("Hồ sơ xử lý", "Processing profile")}</div>
          <div className="quick-import-profile" role="group" aria-label={uiText("Chọn hồ sơ xử lý", "Choose a processing profile")}>
            <button type="button" className={profile === "technical" ? "active" : ""} aria-pressed={profile === "technical"} onClick={() => setProfile("technical")}>
              <Ic.layers size={14} /><span><b>{uiText("Kỹ thuật", "Technical")}</b><em>{uiText("Thuật ngữ và cấu trúc tài liệu", "Terminology and document structure")}</em></span>
            </button>
            <button type="button" className={profile === "literary" ? "active" : ""} aria-pressed={profile === "literary"} onClick={() => setProfile("literary")}>
              <Ic.book size={14} /><span><b>{uiText("Văn học", "Literary")}</b><em>{uiText("Nhân vật và mạch kể chuyện", "Characters and narrative flow")}</em></span>
            </button>
          </div>
          <div className="quick-import-note"><Ic.lock size={12} />{uiText("Tài liệu được tạo thành project mới; dataset đang mở không bị thay đổi.", "The document is created as a new project; the open dataset is not changed.")}</div>
        </> : <div className="quick-import-note"><Ic.lock size={12} />{reuseExisting
          ? uiText("Có thể thử tiếp tục project đã tạo nhưng chưa hoàn tất; backend quyết định có thể reuse hay phải chặn.", "A created but incomplete project can be retried; the backend decides whether it can be reused or must be blocked.")
          : uiText("Project phải mới và chưa có source package, runtime hoặc run cũ.", "The project must be new and have no existing source package, runtime, or run.")}</div>}
      </div>}

      {step === 2 && <div className="quick-import-pane">
        {mode === "d2l-presegmented" ? <div className="quick-import-summary quick-import-d2l-summary">
          {QUICK_IMPORT_D2L_FILES.map(item => <div key={item.field}><span>{uiText(item.vi, item.en)}</span><b>{d2lFiles[item.field]?.name}</b><em>{quickImportFileSize(d2lFiles[item.field]?.size)}</em></div>)}
          <div><span>{uiText("Dự án", "Project")}</span><b className="mono">{docId}</b><em>{title}</em></div>
        </div> : <div className="quick-import-summary">
          <div><span>{uiText("File nguồn", "Source file")}</span><b>{file?.name}</b><em>{quickImportFileSize(file?.size)}</em></div>
          <div><span>{uiText("Dự án", "Project")}</span><b className="mono">{docId}</b><em>{title}</em></div>
          <div><span>{uiText("Hồ sơ", "Profile")}</span><b>{profile === "technical" ? uiText("Kỹ thuật", "Technical") : uiText("Văn học", "Literary")}</b><em>{profile === "technical" ? "technical" : "literature"}</em></div>
        </div>}
        <div className="quick-import-note"><Ic.alert size={12} />{mode === "d2l-presegmented" ? uiText("Request sẽ chạy đồng bộ; không giả lập phần trăm tiến độ. Giữ cửa sổ mở cho đến khi backend trả kết quả.", "The request is synchronous; no percentage progress is simulated. Keep the window open until the backend returns.") : uiText("Nếu tài liệu có cấu trúc không rõ, dùng Project / Source sau khi nhập để kiểm tra và chuẩn hóa.", "If the document structure is unclear, use Project / Source after import to review and normalize it.")}</div>
      </div>}

      {step === 3 && <div className="quick-import-pane">
        <div className={`quick-import-result${error ? " failed" : result ? " complete" : ""}`}>
          <span className="quick-import-result-icon">{error ? <Ic.xCircle size={22} /> : result ? <Ic.checkCircle size={22} /> : <span className="as-spin" />}</span>
          <div>
            <b>{error ? uiText("Chưa thể hoàn tất", "Could not finish") : result ? uiText("Đã nhập managed source package", "Managed source package imported") : busy === "create" ? uiText("Đang tạo project", "Creating project") : busy === "upload" ? uiText("Đang tải file nguồn", "Uploading source file") : busy === "import-d2l" ? uiText("Đang nhập gói D2L", "Importing D2L bundle") : uiText("Đang chuẩn hóa bằng cấu hình server", "Normalizing with the server configuration")}</b>
            <p>{error?.message || (result ? `${result.mode}${result.reused ? " · reused" : ""}` : uiText("Không đóng cửa sổ trong khi dữ liệu đang được ghi.", "Keep this window open while data is being written."))}</p>
            {error?.code && <code className="quick-import-error-code">{error.code}{error.status ? ` · HTTP ${error.status}` : ""}</code>}
            {(createdDocId || result?.docId) && <span className="mono">{result?.docId || createdDocId}</span>}
          </div>
        </div>
      </div>}
    </Modal>
  );
}

function ProjectSourceScreen({
  projects,
  activeDocId,
  docInfo,
  onSelectProject,
  onCreateProject,
  onUpdateProject,
  onDeleteProject,
  onPatchDoc,
  onUploadSource,
  onImportD2LPresegmented,
  onBack,
  onOpenStructure,
  readOnly,
  locale,
  onLocaleChange,
}) {
  const [file, setFile] = React.useState(null);
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [newDocId, setNewDocId] = React.useState("");
  const [projectNote, setProjectNote] = React.useState("");
  const [sourcePackageStatus, setSourcePackageStatus] = React.useState(null);
  const [sourcePackageLoading, setSourcePackageLoading] = React.useState(false);
  const [sourcePackageError, setSourcePackageError] = React.useState("");
  const [d2lRecoveryFiles, setD2lRecoveryFiles] = React.useState({ source: null, block_map: null, manifest: null });
  const [d2lRecoveryErrors, setD2lRecoveryErrors] = React.useState({});
  const [d2lRecoveryBusy, setD2lRecoveryBusy] = React.useState(false);
  const [d2lRecoveryError, setD2lRecoveryError] = React.useState("");
  const [d2lRecoveryResult, setD2lRecoveryResult] = React.useState(null);
  const meta = docInfo.metadata || {};
  const prov = docInfo.provenance || {};
  const localProjects = (projects || []).filter(project => (
    project?.source !== "thesis" && !String(project?.doc_id || "").startsWith("thesis:")
  ));
  const selectedProject = localProjects.find(p => p.doc_id === activeDocId);
  const protectedProject = activeDocId === "gold_demo_01";
  const sourceMode = String(sourcePackageStatus?.mode || "");
  const sourceManaged = sourcePackageStatus?.managed === true;
  const sourceFrozen = sourceMode === "managed_run_started_frozen";
  const sourceFinalized = sourceMode === "managed_finalized_pre_run";
  const sourceLegacy = sourceMode === "legacy_only";
  const projectEditingLocked = readOnly || sourcePackageLoading || !sourcePackageStatus || sourceFinalized || sourceFrozen;
  const sourceUploadLocked = readOnly || sourcePackageLoading || !sourcePackageStatus || sourceManaged || sourceLegacy;
  const d2lRecoveryVisible = !!activeDocId
    && sourcePackageStatus?.mode === "unmanaged_draft"
    && !sourcePackageStatus?.source;
  const d2lRecoveryComplete = QUICK_IMPORT_D2L_FILES.every(item => !!d2lRecoveryFiles[item.field]);
  const d2lRecoveryUnique = new Set(Object.values(d2lRecoveryFiles).filter(Boolean).map(item => `${item.name}:${item.size}:${item.lastModified}`)).size === 3;
  const d2lRecoveryReady = d2lRecoveryVisible
    && !readOnly
    && !sourcePackageLoading
    && !d2lRecoveryBusy
    && !!onImportD2LPresegmented
    && d2lRecoveryComplete
    && d2lRecoveryUnique
    && Object.values(d2lRecoveryErrors).every(value => !value);

  async function refreshSourcePackageStatus() {
    if (!activeDocId) {
      setSourcePackageStatus(null);
      setSourcePackageError("");
      return;
    }
    setSourcePackageLoading(true);
    try {
      const nextStatus = await window.AILAB_API.getSourcePackageStatus(activeDocId);
      setSourcePackageStatus(nextStatus);
      setSourcePackageError("");
    } catch (error) {
      const first = error?.errors?.[0] || error?.payload?.errors?.[0] || {};
      setSourcePackageStatus(null);
      setSourcePackageError([first.code, first.message || error?.message].filter(Boolean).join(" · "));
    } finally {
      setSourcePackageLoading(false);
    }
  }

  React.useEffect(() => {
    setProjectNote(selectedProject?.note || "");
  }, [selectedProject?.doc_id, selectedProject?.note]);

  React.useEffect(() => {
    setFile(null);
    setD2lRecoveryFiles({ source: null, block_map: null, manifest: null });
    setD2lRecoveryErrors({});
    setD2lRecoveryError("");
    setD2lRecoveryResult(null);
    refreshSourcePackageStatus();
  }, [activeDocId]);

  function patchMetadata(patch) {
    if (projectEditingLocked) return;
    onPatchDoc({ metadata: patch });
  }

  async function createFromForm() {
    const docId = newDocId.trim();
    if (!docId) return;
    const created = await onCreateProject(docId, {
      title: meta.title || docId,
      author: meta.author || "",
      domain: meta.domain || "literature",
      genre: meta.genre || "novel",
      source_format: meta.source_format || "txt",
      license: meta.license || "",
      source_url: meta.source_url || "",
      contamination_risk: meta.contamination_risk || "low",
    });
    if (created) setNewDocId("");
  }

  async function saveProjectSettings() {
    if (!activeDocId || projectEditingLocked) return;
    await onUpdateProject(activeDocId, { note: projectNote });
  }

  async function uploadSelected() {
    if (!file) return;
    const uploaded = await onUploadSource(file, false);
    if (uploaded) {
      setFile(null);
      await refreshSourcePackageStatus();
    }
  }

  function selectD2LRecoveryFile(field, nextFile) {
    const descriptor = QUICK_IMPORT_D2L_FILES.find(item => item.field === field);
    if (!descriptor || !nextFile) return;
    const nextErrors = { ...d2lRecoveryErrors };
    if (nextFile.name !== descriptor.filename) {
      setD2lRecoveryFiles(current => ({ ...current, [field]: null }));
      nextErrors[field] = uiText(`Tên file phải chính xác là ${descriptor.filename}.`, `The filename must be exactly ${descriptor.filename}.`);
      setD2lRecoveryErrors(nextErrors);
      setD2lRecoveryError("");
      return;
    }
    const duplicate = Object.entries(d2lRecoveryFiles).some(([otherField, otherFile]) => (
      otherField !== field && otherFile && (
        otherFile === nextFile
        || (otherFile.name === nextFile.name && otherFile.size === nextFile.size && otherFile.lastModified === nextFile.lastModified)
      )
    ));
    if (duplicate) {
      setD2lRecoveryFiles(current => ({ ...current, [field]: null }));
      nextErrors[field] = uiText("Không thể dùng cùng một file cho hai trường.", "The same file cannot be used for two fields.");
      setD2lRecoveryErrors(nextErrors);
      setD2lRecoveryError("");
      return;
    }
    delete nextErrors[field];
    setD2lRecoveryFiles(current => ({ ...current, [field]: nextFile }));
    setD2lRecoveryErrors(nextErrors);
    setD2lRecoveryError("");
  }

  async function importD2LRecovery() {
    if (!d2lRecoveryReady) return;
    setD2lRecoveryBusy(true);
    setD2lRecoveryError("");
    setD2lRecoveryResult(null);
    try {
      const result = await onImportD2LPresegmented(activeDocId, d2lRecoveryFiles);
      setD2lRecoveryResult({ reused: result?.reused === true });
      setD2lRecoveryFiles({ source: null, block_map: null, manifest: null });
      await refreshSourcePackageStatus();
      if (onOpenStructure) await onOpenStructure(activeDocId, { reload: true });
    } catch (error) {
      const first = error?.errors?.[0] || error?.payload?.errors?.[0] || {};
      setD2lRecoveryError([first.code, first.message || error?.message].filter(Boolean).join(" · "));
    } finally {
      setD2lRecoveryBusy(false);
    }
  }

  return (
    <div className="project-screen">
      <div className="project-topbar">
        <div className="tb-left">
          <span className="tb-app"><span className="tb-logo">▧</span>Thesis <span className="tb-app-sub">Runtime Tool</span></span>
          <span className="tb-sep" />
          <span className="tb-doc"><Ic.folder size={13} className="faint" /><span className="mono">{activeDocId || "no_project"}</span></span>
        </div>
        <div className="tb-right">
          <ThesisLocaleSwitch locale={locale} onChange={onLocaleChange} compact />
          <button className="btn" onClick={onBack}><Ic.arrowRight size={13} style={{ transform: "rotate(180deg)" }} />{uiText("Về workspace", "Back to workspace")}</button>
        </div>
      </div>

      <div className="project-wrap">
        <div className="project-headline">
          <div>
            <div className="project-kicker">{uiText("Dự án / Nguồn", "Project / Source")}</div>
            <h1>{uiText("Chuẩn bị nguồn cho pipeline khóa luận", "Prepare the source for the thesis pipeline")}</h1>
            <p>{uiText("Chọn dự án, tải TXT, EPUB, Markdown, HTML hoặc PDF, rồi mở workspace Cấu trúc để backend chuẩn hóa, kiểm tra và chốt source package trước khi chạy.", "Choose a project, upload TXT, EPUB, Markdown, HTML, or PDF, then open the Structure workspace so the backend can normalize, review, and finalize the source package before a run.")}</p>
          </div>
          <div className="source-state">
            <div className="srcstat-row"><span className={"ss-dot " + (selectedProject ? "ok" : "bad")} /><span className="ss-label">{uiText("Dự án", "Project")}</span><span className="ss-val mono">{activeDocId || uiText("chưa chọn", "not selected")}</span></div>
            <div className="srcstat-row"><span className={"ss-dot " + (sourcePackageStatus?.source ? "ok" : "bad")} /><span className="ss-label">{uiText("Nguồn", "Source")}</span><span className="ss-val mono">{sourcePackageLoading ? uiText("đang tải", "loading") : sourcePackageError ? uiText("chưa xác định", "unknown") : sourcePackageStatus?.source ? `${sourcePackageStatus.source.format} · ${sourcePackageStatus.source.filename}` : uiText("thiếu", "missing")}</span></div>
            <div className="srcstat-row"><span className={"ss-dot " + (sourceManaged ? "ok" : sourceLegacy ? "bad" : "")} /><span className="ss-label">{uiText("Vòng đời", "Lifecycle")}</span><span className="ss-val mono">{sourceMode || uiText("không khả dụng", "unavailable")}</span></div>
            {sourcePackageError && <div className="srcstat-row"><span className="ss-dot bad" /><span className="ss-label">Backend</span><span className="ss-val mono bad">{sourcePackageError}</span></div>}
            {sourcePackageError && <div className="source-state-actions"><button className="btn sm" type="button" disabled={sourcePackageLoading} onClick={refreshSourcePackageStatus}><Ic.refresh size={11} />{uiText("Tải lại trạng thái", "Reload status")}</button></div>}
          </div>
        </div>

        <div className="project-grid">
          <section className="project-panel">
            <div className="panel-title"><Ic.folder size={14} />{uiText("Dự án cục bộ", "Local project")}</div>
            <div className="project-picker">
              <div className="project-section">
                <div className="project-section-title">{uiText("Mở dự án hiện có", "Open existing")}</div>
                <FormField label={uiText("mở dự án", "open project")}>
                <select value={activeDocId || ""} onChange={e => onSelectProject(e.target.value)}>
                  {!localProjects.length && <option value="">{uiText("Chưa có dự án cục bộ", "No local projects yet")}</option>}
                  {localProjects.map(p => <option key={p.doc_id} value={p.doc_id}>{p.doc_id} · {p.status}</option>)}
                </select>
                </FormField>
              </div>
              <div className="project-section">
                <div className="project-section-title">{uiText("Tạo mới", "Create new")}</div>
                <div className="project-create-row">
                  <FormField label={uiText("doc_id mới", "new doc_id")}>
                <input value={newDocId} placeholder="my_novel_01" onChange={e => setNewDocId(e.target.value)} />
                  </FormField>
                  <button className="btn primary project-create-btn" disabled={!newDocId.trim()} onClick={createFromForm}><Ic.plus size={13} />{uiText("Tạo dự án", "Create project")}</button>
                </div>
              </div>
            </div>
            <div className="project-admin">
              <div className="project-admin-head">
                <span>{uiText("Thông tin dự án", "Project info")}</span>
                <span className="mono">{activeDocId || "no_project"}</span>
              </div>
              <div className="project-form project-info-form">
                <FormField label={uiText("ghi chú dự án", "project note")}>
                <textarea
                    className="project-note"
                    value={projectNote}
                    disabled={!activeDocId || projectEditingLocked}
                    rows={4}
                    placeholder={uiText("Ghi chú ngắn cho người phụ trách, lựa chọn nguồn, vấn đề đã biết...", "Short note for task owner, source choice, known issues...")}
                    onChange={e => setProjectNote(e.target.value)}
                  />
                </FormField>
              </div>
              <div className="project-admin-actions">
                <button className="btn" disabled={!activeDocId || projectEditingLocked} onClick={saveProjectSettings}>
                  <Ic.save size={13} />{uiText("Lưu thông tin dự án", "Save project info")}
                </button>
                <button className="btn danger" disabled={!activeDocId || protectedProject || projectEditingLocked} onClick={() => setConfirmDelete(true)}>
                  <Ic.trash size={13} />{uiText("Xóa dự án", "Delete project")}
                </button>
              </div>
              {protectedProject && (
                <div className="project-admin-note">
                  <Ic.lock size={11} />
                  <span>{uiText("Mẫu vàng được bảo vệ và không thể xóa.", "The golden sample is protected and cannot be deleted.")}</span>
                </div>
              )}
            </div>
          </section>

          <section className="project-panel">
            <div className="panel-title"><Ic.doc size={14} />{uiText("Siêu dữ liệu / nguồn gốc", "Metadata / provenance")}</div>
            <div className="project-form">
              <FormField label={uiText("tiêu đề", "title")}><input value={meta.title || ""} disabled={projectEditingLocked} onChange={e => patchMetadata({ title: e.target.value })} /></FormField>
              <FormField label={uiText("tác giả", "author")}><input value={meta.author || ""} disabled={projectEditingLocked} onChange={e => patchMetadata({ author: e.target.value })} /></FormField>
              <FormField label={uiText("lĩnh vực", "domain")}><input value={meta.domain || ""} disabled={projectEditingLocked} onChange={e => patchMetadata({ domain: e.target.value })} /></FormField>
              <FormField label={uiText("thể loại", "genre")}><input value={meta.genre || ""} disabled={projectEditingLocked} onChange={e => patchMetadata({ genre: e.target.value })} /></FormField>
              <FormField label="source_format">
                <select value={meta.source_format || "txt"} disabled={projectEditingLocked} onChange={e => patchMetadata({ source_format: e.target.value })}>
                  <option value="txt">txt</option>
                  <option value="epub">epub</option>
                  <option value="markdown">markdown</option>
                  <option value="html">html</option>
                  <option value="pdf">pdf</option>
                </select>
              </FormField>
              <FormField label={uiText("giấy phép", "license")}><input value={meta.license || ""} disabled={projectEditingLocked} onChange={e => patchMetadata({ license: e.target.value })} /></FormField>
              <FormField label="source_url"><input value={meta.source_url || ""} disabled={projectEditingLocked} onChange={e => patchMetadata({ source_url: e.target.value })} /></FormField>
              <FormField label="contamination_risk">
                <select value={meta.contamination_risk || ""} disabled={projectEditingLocked} onChange={e => patchMetadata({ contamination_risk: e.target.value })}>
                  <option value="">{uiText("chưa đặt", "not set")}</option>
                  <option value="low">{uiText("thấp", "low")}</option>
                  <option value="medium">{uiText("trung bình", "medium")}</option>
                  <option value="high">{uiText("cao", "high")}</option>
                </select>
              </FormField>
            </div>
            <div className="readonly-strip">
              <span className="lockfield"><span className="lf-k"><Ic.lock size={9} />raw_sha256</span><span className="lf-v">{prov.raw_sha256 || meta.raw_sha256 || uiText("tạo sau khi trích xuất", "created after extract")}</span></span>
              <span className="lockfield"><span className="lf-k"><Ic.lock size={9} />pipeline</span><span className="lf-v">{prov.pipeline_version || meta.pipeline_version || uiText("đang chờ", "pending")}</span></span>
            </div>
          </section>

          <section className="project-panel">
            <div className="panel-title"><Ic.upload size={14} />{uiText("File nguồn", "Source file")}</div>
            <div className="source-drop">
              <Ic.file size={22} />
              <div>
                <div className="source-drop-title">TXT / EPUB / Markdown / HTML / PDF</div>
                <div className="source-drop-sub">{uiText("Backend Source Package chọn bộ chuẩn hóa và giữ nguyên byte nguồn.", "The Source Package backend selects the normalizer and preserves source bytes.")}</div>
              </div>
              <input type="file" accept=".txt,.epub,.md,.markdown,.html,.htm,.pdf" disabled={sourceUploadLocked} onChange={e => setFile(e.target.files?.[0] || null)} />
            </div>
            <div className="extract-actions">
              <button className="btn" disabled={!file} onClick={() => setFile(null)}>{uiText("Bỏ chọn", "Clear")}</button>
              <button className="btn" disabled={!file || !activeDocId || sourceUploadLocked} onClick={uploadSelected}><Ic.upload size={13} />{uiText("Tải nguồn", "Upload source")}</button>
              <button className="btn primary" disabled={!activeDocId || readOnly} onClick={() => onOpenStructure(activeDocId)}><Ic.layers size={13} />{uiText("Mở Cấu trúc", "Open Structure")}</button>
            </div>
            <div className="extract-note">
              {sourceFrozen ? <Ic.lock size={12} /> : <Ic.alert size={12} />}
              <span>{sourceFrozen
                ? uiText("Lần chạy đầu tiên đã đóng băng source package; nguồn và cấu trúc hiện chỉ đọc.", "The first run froze the source package; the source and structure are now read-only.")
                : sourceManaged
                  ? uiText("Source package đã được quản lý; không thể ghi đè nguồn. Tạo dự án/revision mới nếu cần thay nguồn.", "The source package is managed and its source cannot be overwritten. Create a new project/revision to replace the source.")
                  : sourceLegacy
                    ? uiText("Dự án legacy giữ luồng cũ; UI không tự chuyển sang managed normalize.", "Legacy projects keep their existing flow; the UI does not automatically convert them to managed normalization.")
                     : uiText("Sau khi tải, dùng Cấu trúc để chuẩn hóa với body {} và kiểm tra theo trạng thái backend.", "After upload, use Structure to normalize with body {} and review using backend status.")}</span>
            </div>
            {d2lRecoveryVisible && <div className="project-d2l-recovery" data-testid="project-d2l-recovery">
              <div className="project-d2l-recovery-head">
                <div>
                  <b>{uiText("Khôi phục nhập gói D2L", "Recover D2L bundle import")}</b>
                  <span>{uiText("Project này đã tồn tại nhưng chưa có source. Chọn lại đúng ba file để tiếp tục, không cần tạo project mới.", "This project exists but has no source. Select the exact three files to continue; no new project is needed.")}</span>
                </div>
                <span className="mono">source_missing</span>
              </div>
              <div className="quick-import-d2l-intro"><Ic.lock size={12} /><span>{uiText("Request chạy đồng bộ; UI không đọc fixture/filesystem và không giả lập phần trăm tiến độ.", "The request is synchronous; the UI does not read fixtures/filesystem or simulate percentage progress.")}</span></div>
              <div className="quick-import-d2l-files" aria-label={uiText("Ba file D2L để khôi phục", "Three D2L recovery files")}>
                {QUICK_IMPORT_D2L_FILES.map(item => {
                  const selected = d2lRecoveryFiles[item.field];
                  const fieldError = d2lRecoveryErrors[item.field];
                  return <div key={item.field} className={`quick-import-file-pick${selected ? " has-file" : ""}${fieldError ? " has-error" : ""}`}>
                    <label>
                      <input type="file" accept={item.accept} data-testid={`project-d2l-${item.field}`} disabled={d2lRecoveryBusy} onChange={event => selectD2LRecoveryFile(item.field, event.target.files?.[0])} />
                      <span className="quick-import-drop-icon"><Ic.file size={17} /></span>
                      <span className="quick-import-drop-copy"><b>{selected ? selected.name : uiText(item.vi, item.en)}</b><em>{selected ? quickImportFileSize(selected.size) : item.filename}</em></span>
                      <span className="btn sm">{uiText("Chọn file", "Choose file")}</span>
                    </label>
                    {fieldError && <div className="quick-import-error"><Ic.alert size={12} />{fieldError}</div>}
                  </div>;
                })}
              </div>
              {d2lRecoveryError && <div className="quick-import-error" role="alert"><Ic.alert size={12} /><span>{d2lRecoveryError}</span></div>}
              {d2lRecoveryResult && <div className="quick-import-note" role="status"><Ic.checkCircle size={12} /><span>{d2lRecoveryResult.reused ? uiText("Backend đã xác nhận lại đúng package trước đó.", "The backend reconfirmed the exact previous package.") : uiText("Đã nhập package vào project này.", "The package was imported into this project.")}</span></div>}
              <div className="extract-actions project-d2l-recovery-actions">
                <button className="btn primary" type="button" data-testid="project-d2l-import" disabled={!d2lRecoveryReady} onClick={importD2LRecovery}><Ic.upload size={13} />{d2lRecoveryBusy ? uiText("Đang nhập gói…", "Importing bundle…") : uiText("Nhập gói D2L vào project này", "Import D2L into this project")}</button>
              </div>
            </div>}
          </section>

        </div>
      </div>

      {confirmDelete && (
        <Modal title={uiText("Xóa dự án", "Delete project")} icon={Ic.alert} tone="bad" onClose={() => setConfirmDelete(false)}
          actions={<>
            <button className="btn" onClick={() => setConfirmDelete(false)}>{uiText("Hủy", "Cancel")}</button>
            <button className="btn danger" onClick={() => { setConfirmDelete(false); onDeleteProject(activeDocId, activeDocId); }}>
              {uiText("Xóa", "Delete")} {activeDocId}
            </button>
          </>}>
          <p>{uiText("Thao tác này xóa thư mục dự án cục bộ", "This removes the local project folder for")} <span className="mono">{activeDocId}</span>{uiText(", gồm nguồn thô, file canonical, bản nháp làm việc, log và file xuất.", ", including raw source, canonical files, working drafts, logs, and exports.")}</p>
          <p className="muted">{uiText("Không thể hoàn tác. Hãy xuất hoặc sao chép dự án trước nếu cần giữ lại.", "This cannot be undone. Export or copy the project first if you need to preserve it.")}</p>
        </Modal>
      )}

    </div>
  );
}

window.ProjectSourceScreen = ProjectSourceScreen;
window.QuickImportModal = QuickImportModal;
