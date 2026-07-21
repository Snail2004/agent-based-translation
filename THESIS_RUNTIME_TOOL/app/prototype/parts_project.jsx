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

function quickImportSourceFormat(filename) {
  const lower = String(filename || "").toLowerCase();
  if (/\.(md|markdown)$/.test(lower)) return "markdown";
  if (/\.(html|htm)$/.test(lower)) return "html";
  if (lower.endsWith(".epub")) return "epub";
  if (lower.endsWith(".pdf")) return "pdf";
  return "txt";
}

function QuickImportModal({ projects, onClose, onCreateProject, onUploadSource, onNormalize, onOpenStructure, onOpenAdvanced }) {
  const [step, setStep] = React.useState(1);
  const [file, setFile] = React.useState(null);
  const [profile, setProfile] = React.useState("literary");
  const [docId, setDocId] = React.useState("");
  const [title, setTitle] = React.useState("");
  const [idTouched, setIdTouched] = React.useState(false);
  const [titleTouched, setTitleTouched] = React.useState(false);
  const [fileError, setFileError] = React.useState("");
  const [busy, setBusy] = React.useState("");
  const [error, setError] = React.useState("");
  const [createdDocId, setCreatedDocId] = React.useState("");
  const [sourceUploaded, setSourceUploaded] = React.useState(false);
  const [result, setResult] = React.useState(null);

  const supportedFile = file && QUICK_IMPORT_SOURCE_RE.test(file.name || "");
  const validDocId = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(docId.trim());
  const duplicateDocId = (projects || []).some(project => project.doc_id === docId.trim());
  const canContinue = !!supportedFile && validDocId && !duplicateDocId && !!title.trim();
  const close = () => { if (!busy) onClose(); };

  React.useEffect(() => {
    function onKey(event) {
      if (event.key === "Escape" && !busy) close();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy]);

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

  function handleDrop(event) {
    event.preventDefault();
    selectFile(event.dataTransfer?.files?.[0]);
  }

  async function runImport() {
    if (!canContinue && !createdDocId) return;
    setStep(3);
    setError("");
    let targetDocId = createdDocId;
    let uploaded = sourceUploaded;
    try {
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
      setError(err?.message || String(err));
    }
  }

  const actions = step === 1 ? <>
    <button className="btn" type="button" onClick={close}>{uiText("Hủy", "Cancel")}</button>
    <button className="btn primary" type="button" disabled={!canContinue} onClick={() => setStep(2)}>{uiText("Tiếp tục", "Continue")} <Ic.arrowRight size={13} /></button>
  </> : step === 2 ? <>
    <button className="btn" type="button" onClick={() => setStep(1)}>{uiText("Quay lại", "Back")}</button>
    <button className="btn primary" type="button" onClick={runImport}><Ic.sparkle size={13} />{uiText("Tạo và chuẩn hóa", "Create and normalize")}</button>
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
        {[["Nguồn", "Source"], ["Xác nhận", "Confirm"], ["Chuẩn hóa", "Normalize"]].map(([vi, en], index) => {
          const number = index + 1;
          const state = number === step ? "active" : number < step ? "done" : "";
          return <div key={en} className={`quick-import-step ${state}`}><span>{number < step ? <Ic.check size={11} /> : number}</span><b>{uiText(vi, en)}</b></div>;
        })}
      </div>

      {step === 1 && <div className="quick-import-pane">
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

        <div className="form-grid quick-import-fields">
          <label className="form-field">
            <span className="form-label">{uiText("Tên tài liệu", "Document title")}</span>
            <input value={title} onChange={event => { setTitleTouched(true); setTitle(event.target.value); }} placeholder={uiText("Tên hiển thị", "Display title")} />
          </label>
          <label className="form-field">
            <span className="form-label">Project ID</span>
            <input className="mono" value={docId} onChange={event => { setIdTouched(true); setDocId(event.target.value); }} placeholder="document_id" />
            {docId && !validDocId && <span className="field-error">{uiText("Chỉ dùng chữ, số, dấu chấm, gạch ngang hoặc gạch dưới.", "Use only letters, numbers, periods, hyphens, or underscores.")}</span>}
            {validDocId && duplicateDocId && <span className="field-error">{uiText("Project ID này đã tồn tại.", "This project ID already exists.")}</span>}
          </label>
        </div>

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
      </div>}

      {step === 2 && <div className="quick-import-pane">
        <div className="quick-import-summary">
          <div><span>{uiText("File nguồn", "Source file")}</span><b>{file?.name}</b><em>{quickImportFileSize(file?.size)}</em></div>
          <div><span>{uiText("Dự án", "Project")}</span><b className="mono">{docId}</b><em>{title}</em></div>
          <div><span>{uiText("Hồ sơ", "Profile")}</span><b>{profile === "technical" ? uiText("Kỹ thuật", "Technical") : uiText("Văn học", "Literary")}</b><em>{profile === "technical" ? "technical" : "literature"}</em></div>
        </div>
        <div className="quick-import-note"><Ic.alert size={12} />{uiText("Nếu tài liệu có cấu trúc không rõ, dùng Project / Source sau khi nhập để kiểm tra và chuẩn hóa.", "If the document structure is unclear, use Project / Source after import to review and normalize it.")}</div>
      </div>}

      {step === 3 && <div className="quick-import-pane">
        <div className={`quick-import-result${error ? " failed" : result ? " complete" : ""}`}>
          <span className="quick-import-result-icon">{error ? <Ic.xCircle size={22} /> : result ? <Ic.checkCircle size={22} /> : <span className="as-spin" />}</span>
          <div>
            <b>{error ? uiText("Chưa thể hoàn tất", "Could not finish") : result ? uiText("Đã tạo managed source package", "Managed source package created") : busy === "create" ? uiText("Đang tạo project", "Creating project") : busy === "upload" ? uiText("Đang tải file nguồn", "Uploading source file") : uiText("Đang chuẩn hóa bằng cấu hình server", "Normalizing with the server configuration")}</b>
            <p>{error || (result ? `${result.mode}${result.reused ? " · reused" : ""}` : uiText("Không đóng cửa sổ trong khi dữ liệu đang được ghi.", "Keep this window open while data is being written."))}</p>
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
            <div className="srcstat-row"><span className={"ss-dot " + (sourcePackageStatus?.source ? "ok" : "bad")} /><span className="ss-label">{uiText("Nguồn", "Source")}</span><span className="ss-val mono">{sourcePackageLoading ? uiText("đang tải", "loading") : sourcePackageStatus?.source ? `${sourcePackageStatus.source.format} · ${sourcePackageStatus.source.filename}` : uiText("thiếu", "missing")}</span></div>
            <div className="srcstat-row"><span className={"ss-dot " + (sourceManaged ? "ok" : sourceLegacy ? "bad" : "")} /><span className="ss-label">{uiText("Vòng đời", "Lifecycle")}</span><span className="ss-val mono">{sourceMode || uiText("không khả dụng", "unavailable")}</span></div>
            {sourcePackageError && <div className="srcstat-row"><span className="ss-dot bad" /><span className="ss-label">Backend</span><span className="ss-val mono bad">{sourcePackageError}</span></div>}
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
