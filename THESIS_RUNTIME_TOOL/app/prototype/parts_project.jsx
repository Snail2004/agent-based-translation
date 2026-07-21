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

const QUICK_IMPORT_SOURCE_RE = /\.(txt|epub|md|markdown|html|htm)$/i;

function quickImportSourceFormat(filename) {
  const lower = String(filename || "").toLowerCase();
  if (/\.(md|markdown)$/.test(lower)) return "markdown";
  if (/\.(html|htm)$/.test(lower)) return "html";
  if (lower.endsWith(".epub")) return "epub";
  return "txt";
}

function QuickImportModal({ projects, onClose, onCreateProject, onUploadSource, onExtract, onOpenAdvanced }) {
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
      setFileError("Hiện tại hệ thống nhận file EPUB, Markdown, HTML hoặc TXT.");
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
        if (!created?.doc_id) throw new Error("Không thể tạo project mới.");
        targetDocId = created.doc_id;
        setCreatedDocId(targetDocId);
      }

      if (!uploaded) {
        setBusy("upload");
        const uploadedResult = await onUploadSource(file, false, targetDocId);
        if (!uploadedResult) throw new Error("Không thể tải file nguồn lên project.");
        uploaded = true;
        setSourceUploaded(true);
      }

      setBusy("extract");
      const extractResult = await onExtract(false, targetDocId);
      if (!extractResult) throw new Error("Không thể trích xuất cấu trúc tài liệu.");
      setResult({
        docId: targetDocId,
        chapters: Number(extractResult.document?.chapters || 0),
        blocks: Number(extractResult.document?.blocks || 0),
      });
      setBusy("");
    } catch (err) {
      setBusy("");
      setError(err?.message || String(err));
    }
  }

  const actions = step === 1 ? <>
    <button className="btn" type="button" onClick={close}>Hủy</button>
    <button className="btn primary" type="button" disabled={!canContinue} onClick={() => setStep(2)}>Tiếp tục <Ic.arrowRight size={13} /></button>
  </> : step === 2 ? <>
    <button className="btn" type="button" onClick={() => setStep(1)}>Quay lại</button>
    <button className="btn primary" type="button" onClick={runImport}><Ic.play size={13} />Tạo và trích xuất</button>
  </> : result ? <>
    {onOpenAdvanced && <button className="btn" type="button" onClick={onOpenAdvanced}><Ic.folder size={13} />Project / Source</button>}
    <button className="btn primary" type="button" onClick={close}>Mở danh sách block</button>
  </> : error ? <>
    <button className="btn" type="button" onClick={() => setStep(createdDocId ? 2 : 1)}>
      <Ic.chevRight size={13} style={{ transform: "rotate(180deg)" }} />{createdDocId ? "Xem lại" : "Sửa thông tin"}
    </button>
    <button className="btn primary" type="button" onClick={runImport}><Ic.refresh size={13} />Thử lại</button>
  </> : <button className="btn primary" type="button" disabled><span className="as-spin" />Đang xử lý</button>;

  return (
    <Modal title="Nhập tài liệu mới" icon={Ic.upload} className="quick-import-modal" onClose={close} actions={actions}>
      <div className="quick-import-steps" aria-label={`Bước ${step} trên 3`}>
        {["Nguồn", "Xác nhận", "Trích xuất"].map((label, index) => {
          const number = index + 1;
          const state = number === step ? "active" : number < step ? "done" : "";
          return <div key={label} className={`quick-import-step ${state}`}><span>{number < step ? <Ic.check size={11} /> : number}</span><b>{label}</b></div>;
        })}
      </div>

      {step === 1 && <div className="quick-import-pane">
        <label className={`quick-import-drop${file ? " has-file" : ""}`}
          onDragOver={event => event.preventDefault()} onDrop={handleDrop}>
          <input type="file" accept=".txt,.epub,.md,.markdown,.html,.htm" onChange={event => selectFile(event.target.files?.[0])} />
          <span className="quick-import-drop-icon"><Ic.upload size={18} /></span>
          <span className="quick-import-drop-copy">
            <b>{file ? file.name : "Chọn hoặc kéo thả tài liệu"}</b>
            <em>{file ? quickImportFileSize(file.size) : "EPUB, Markdown, HTML hoặc TXT"}</em>
          </span>
          <span className="btn sm">Chọn file</span>
        </label>
        {fileError && <div className="quick-import-error"><Ic.alert size={12} />{fileError}</div>}

        <div className="form-grid quick-import-fields">
          <label className="form-field">
            <span className="form-label">Tên tài liệu</span>
            <input value={title} onChange={event => { setTitleTouched(true); setTitle(event.target.value); }} placeholder="Tên hiển thị" />
          </label>
          <label className="form-field">
            <span className="form-label">Project ID</span>
            <input className="mono" value={docId} onChange={event => { setIdTouched(true); setDocId(event.target.value); }} placeholder="document_id" />
            {docId && !validDocId && <span className="field-error">Chỉ dùng chữ, số, dấu chấm, gạch ngang hoặc gạch dưới.</span>}
            {validDocId && duplicateDocId && <span className="field-error">Project ID này đã tồn tại.</span>}
          </label>
        </div>

        <div className="quick-import-profile-label">Profile xử lý</div>
        <div className="quick-import-profile" role="group" aria-label="Chọn profile xử lý">
          <button type="button" className={profile === "technical" ? "active" : ""} aria-pressed={profile === "technical"} onClick={() => setProfile("technical")}>
            <Ic.layers size={14} /><span><b>Kỹ thuật</b><em>Thuật ngữ và cấu trúc tài liệu</em></span>
          </button>
          <button type="button" className={profile === "literary" ? "active" : ""} aria-pressed={profile === "literary"} onClick={() => setProfile("literary")}>
            <Ic.book size={14} /><span><b>Văn học</b><em>Nhân vật và mạch kể chuyện</em></span>
          </button>
        </div>
        <div className="quick-import-note"><Ic.lock size={12} />Tài liệu được tạo thành project mới; dataset đang mở không bị thay đổi.</div>
      </div>}

      {step === 2 && <div className="quick-import-pane">
        <div className="quick-import-summary">
          <div><span>File nguồn</span><b>{file?.name}</b><em>{quickImportFileSize(file?.size)}</em></div>
          <div><span>Project</span><b className="mono">{docId}</b><em>{title}</em></div>
          <div><span>Profile</span><b>{profile === "technical" ? "Kỹ thuật" : "Văn học"}</b><em>{profile === "technical" ? "technical" : "literature"}</em></div>
        </div>
        <div className="quick-import-note"><Ic.alert size={12} />Nếu tài liệu có cấu trúc không rõ, dùng Project / Source sau khi nhập để kiểm tra và chuẩn hóa.</div>
      </div>}

      {step === 3 && <div className="quick-import-pane">
        <div className={`quick-import-result${error ? " failed" : result ? " complete" : ""}`}>
          <span className="quick-import-result-icon">{error ? <Ic.xCircle size={22} /> : result ? <Ic.checkCircle size={22} /> : <span className="as-spin" />}</span>
          <div>
            <b>{error ? "Chưa thể hoàn tất" : result ? "Đã tạo dữ liệu có cấu trúc" : busy === "create" ? "Đang tạo project" : busy === "upload" ? "Đang tải file nguồn" : "Đang tách chương và block"}</b>
            <p>{error || (result ? `${result.chapters} chương/unit · ${result.blocks} block` : "Không đóng cửa sổ trong khi dữ liệu đang được ghi.")}</p>
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
  chapters,
  blocks,
  errors,
  onSelectProject,
  onCreateProject,
  onUpdateProject,
  onDeleteProject,
  onPatchDoc,
  onUploadSource,
  onBack,
  onExtract,
  readOnly,
}) {
  const [file, setFile] = React.useState(null);
  const [confirmOverwrite, setConfirmOverwrite] = React.useState(false);
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [newDocId, setNewDocId] = React.useState("");
  const [projectNote, setProjectNote] = React.useState("");
  const meta = docInfo.metadata || {};
  const prov = docInfo.provenance || {};
  const extracted = blocks.length > 0;
  const localProjects = (projects || []).filter(project => (
    project?.source !== "thesis" && !String(project?.doc_id || "").startsWith("thesis:")
  ));
  const selectedProject = localProjects.find(p => p.doc_id === activeDocId);
  const protectedProject = activeDocId === "gold_demo_01";

  React.useEffect(() => {
    setProjectNote(selectedProject?.note || "");
  }, [selectedProject?.doc_id, selectedProject?.note]);

  function patchMetadata(patch) {
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
    if (!activeDocId) return;
    await onUpdateProject(activeDocId, { note: projectNote });
  }

  async function uploadSelected() {
    if (!file) return;
    await onUploadSource(file, false);
  }

  function startExtract() {
    if (extracted) setConfirmOverwrite(true);
    else onExtract(false);
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
          <button className="btn" onClick={onBack}><Ic.arrowRight size={13} style={{ transform: "rotate(180deg)" }} />Back to workspace</button>
        </div>
      </div>

      <div className="project-wrap">
        <div className="project-headline">
          <div>
            <div className="project-kicker">Project / Source</div>
            <h1>Prepare source for the thesis pipeline</h1>
            <p>Choose a local project, upload a TXT, EPUB, Markdown, or HTML source, record source metadata, then extract it into canonical chapters and blocks. Re-extracting requires confirmation because it can overwrite the current document draft.</p>
          </div>
          <div className="source-state">
            <div className="srcstat-row"><span className={"ss-dot " + (selectedProject ? "ok" : "bad")} /><span className="ss-label">Project</span><span className="ss-val mono">{activeDocId || "not selected"}</span></div>
            <div className="srcstat-row"><span className={"ss-dot " + (extracted ? "ok" : "bad")} /><span className="ss-label">Extracted</span><span className="ss-val mono">{extracted ? `${blocks.length} blocks · ${chapters.length} ch` : "not yet"}</span></div>
            <div className="srcstat-row"><span className={"ss-dot " + (errors.length ? "bad" : "ok")} /><span className="ss-label">Validation</span><span className={"ss-val mono " + (errors.length ? "bad" : "")}>{errors.length ? `${errors.length} issue(s)` : "no report issues"}</span></div>
          </div>
        </div>

        <div className="project-grid">
          <section className="project-panel">
            <div className="panel-title"><Ic.folder size={14} />Local project</div>
            <div className="project-picker">
              <div className="project-section">
                <div className="project-section-title">Open existing</div>
                <FormField label="open project">
                <select value={activeDocId || ""} onChange={e => onSelectProject(e.target.value)}>
                  {!localProjects.length && <option value="">No local projects yet</option>}
                  {localProjects.map(p => <option key={p.doc_id} value={p.doc_id}>{p.doc_id} · {p.status}</option>)}
                </select>
                </FormField>
              </div>
              <div className="project-section">
                <div className="project-section-title">Create new</div>
                <div className="project-create-row">
                  <FormField label="new doc_id">
                <input value={newDocId} placeholder="my_novel_01" onChange={e => setNewDocId(e.target.value)} />
                  </FormField>
                  <button className="btn primary project-create-btn" disabled={!newDocId.trim()} onClick={createFromForm}><Ic.plus size={13} />Create project</button>
                </div>
              </div>
            </div>
            <div className="project-admin">
              <div className="project-admin-head">
                <span>Project info</span>
                <span className="mono">{activeDocId || "no_project"}</span>
              </div>
              <div className="project-form project-info-form">
                <FormField label="project note">
                <textarea
                    className="project-note"
                    value={projectNote}
                    disabled={!activeDocId || readOnly}
                    rows={4}
                    placeholder="Short note for task owner, source choice, known issues..."
                    onChange={e => setProjectNote(e.target.value)}
                  />
                </FormField>
              </div>
              <div className="project-admin-actions">
                <button className="btn" disabled={!activeDocId || readOnly} onClick={saveProjectSettings}>
                  <Ic.save size={13} />Save project info
                </button>
                <button className="btn danger" disabled={!activeDocId || protectedProject || readOnly} onClick={() => setConfirmDelete(true)}>
                  <Ic.trash size={13} />Delete project
                </button>
              </div>
              {protectedProject && (
                <div className="project-admin-note">
                  <Ic.lock size={11} />
                  <span>The golden sample is protected and cannot be deleted.</span>
                </div>
              )}
            </div>
          </section>

          <section className="project-panel">
            <div className="panel-title"><Ic.doc size={14} />Metadata / provenance</div>
            <div className="project-form">
              <FormField label="title"><input value={meta.title || ""} disabled={readOnly} onChange={e => patchMetadata({ title: e.target.value })} /></FormField>
              <FormField label="author"><input value={meta.author || ""} disabled={readOnly} onChange={e => patchMetadata({ author: e.target.value })} /></FormField>
              <FormField label="domain"><input value={meta.domain || ""} disabled={readOnly} onChange={e => patchMetadata({ domain: e.target.value })} /></FormField>
              <FormField label="genre"><input value={meta.genre || ""} disabled={readOnly} onChange={e => patchMetadata({ genre: e.target.value })} /></FormField>
              <FormField label="source_format">
                <select value={meta.source_format || "txt"} disabled={readOnly} onChange={e => patchMetadata({ source_format: e.target.value })}>
                  <option value="txt">txt</option>
                  <option value="epub">epub</option>
                  <option value="markdown">markdown</option>
                  <option value="html">html</option>
                  <option value="pdf">pdf (not extractable in MVP)</option>
                </select>
              </FormField>
              <FormField label="license"><input value={meta.license || ""} disabled={readOnly} onChange={e => patchMetadata({ license: e.target.value })} /></FormField>
              <FormField label="source_url"><input value={meta.source_url || ""} disabled={readOnly} onChange={e => patchMetadata({ source_url: e.target.value })} /></FormField>
              <FormField label="contamination_risk">
                <select value={meta.contamination_risk || ""} disabled={readOnly} onChange={e => patchMetadata({ contamination_risk: e.target.value })}>
                  <option value="">not set</option>
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                </select>
              </FormField>
            </div>
            <div className="readonly-strip">
              <span className="lockfield"><span className="lf-k"><Ic.lock size={9} />raw_sha256</span><span className="lf-v">{prov.raw_sha256 || meta.raw_sha256 || "created after extract"}</span></span>
              <span className="lockfield"><span className="lf-k"><Ic.lock size={9} />pipeline</span><span className="lf-v">{prov.pipeline_version || meta.pipeline_version || "pending"}</span></span>
            </div>
          </section>

          <section className="project-panel">
            <div className="panel-title"><Ic.upload size={14} />Source file</div>
            <div className="source-drop">
              <Ic.file size={22} />
              <div>
                <div className="source-drop-title">TXT / EPUB / Markdown / HTML source</div>
                <div className="source-drop-sub">Backend rejects PDF/OCR/layout extraction in this MVP.</div>
              </div>
              <input type="file" accept=".txt,.epub,.md,.markdown,.html,.htm" disabled={readOnly} onChange={e => setFile(e.target.files?.[0] || null)} />
            </div>
            <div className="extract-actions">
              <button className="btn" onClick={() => setFile(null)}>Clear</button>
              <button className="btn" disabled={!file || !activeDocId || readOnly} onClick={uploadSelected}><Ic.upload size={13} />Upload source</button>
              <button className="btn primary" disabled={!activeDocId || readOnly} onClick={startExtract}><Ic.play size={13} />Extract</button>
            </div>
            <div className="extract-note">
              <Ic.alert size={12} />
              <span>Extraction creates the canonical chapters and blocks used by both thesis profiles. Re-extracting can discard reviewed edits.</span>
            </div>
          </section>

        </div>
      </div>

      {confirmOverwrite && (
        <Modal title="Confirm re-extract" icon={Ic.alert} tone="bad" onClose={() => setConfirmOverwrite(false)}
          actions={<>
            <button className="btn" onClick={() => setConfirmOverwrite(false)}>Cancel</button>
            <button className="btn primary" onClick={() => { setConfirmOverwrite(false); onExtract(true); }}>Overwrite draft</button>
          </>}>
          <p>Re-extracting can overwrite <span className="mono">document.json</span> and invalidate edited clean text, spans, and review state.</p>
          <p className="muted">Use this only when the source file or extraction settings changed.</p>
        </Modal>
      )}

      {confirmDelete && (
        <Modal title="Delete project" icon={Ic.alert} tone="bad" onClose={() => setConfirmDelete(false)}
          actions={<>
            <button className="btn" onClick={() => setConfirmDelete(false)}>Cancel</button>
            <button className="btn danger" onClick={() => { setConfirmDelete(false); onDeleteProject(activeDocId, activeDocId); }}>
              Delete {activeDocId}
            </button>
          </>}>
          <p>This removes the local project folder for <span className="mono">{activeDocId}</span>, including raw source, canonical files, working drafts, logs, and exports.</p>
          <p className="muted">This cannot be undone. Export or copy the project first if you need to preserve it.</p>
        </Modal>
      )}

    </div>
  );
}

window.ProjectSourceScreen = ProjectSourceScreen;
window.QuickImportModal = QuickImportModal;
