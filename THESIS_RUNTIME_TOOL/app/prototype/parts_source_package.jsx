/* ===== MANAGED SOURCE PACKAGE: backend-authoritative normalization workflow ===== */

const SOURCE_PACKAGE_CLASSIFICATIONS = [
  { id: "translate", vi: "Dịch", en: "Translate" },
  { id: "preserve", vi: "Giữ nguyên", en: "Preserve" },
  { id: "exclude", vi: "Loại", en: "Exclude" },
  { id: "review", vi: "Cần xem lại", en: "Review required" },
];

const SOURCE_PACKAGE_MODE_META = {
  unmanaged_draft: { vi: "Nguồn đã tải", en: "Source uploaded", tone: "pending", step: 0 },
  managed_draft: { vi: "Bản nháp đang kiểm tra", en: "Draft under review", tone: "review", step: 2 },
  managed_finalized_pre_run: { vi: "Đã chốt trước run", en: "Finalized before run", tone: "ready", step: 3 },
  managed_run_started_frozen: { vi: "Đã chạy · chỉ đọc", en: "Run started · read-only", tone: "frozen", step: 4 },
  legacy_only: { vi: "Project legacy", en: "Legacy project", tone: "legacy", step: -1 },
};

function sourcePackageErrorDetail(error) {
  const first = error?.errors?.[0] || error?.payload?.errors?.[0] || {};
  return {
    code: String(first.code || "request_failed"),
    message: String(first.message || error?.message || uiText("Yêu cầu thất bại.", "Request failed.")),
    status: Number(error?.status || 0),
  };
}

function sourcePackageShortHash(value) {
  const text = String(value || "");
  return text ? `${text.slice(0, 10)}…${text.slice(-6)}` : "—";
}

function sourcePackageClassification(unit) {
  if (unit?.review_required === true || unit?.translation_policy === "review") return "review";
  const policy = String(unit?.translation_policy || "");
  return SOURCE_PACKAGE_CLASSIFICATIONS.some(item => item.id === policy) ? policy : "";
}

function sourcePackageClassificationLabel(value) {
  const item = SOURCE_PACKAGE_CLASSIFICATIONS.find(row => row.id === value);
  return item ? uiText(item.vi, item.en) : value || uiText("Chưa công bố", "Not reported");
}

function sourcePackageExpectedReady(review) {
  const expected = review?.expected;
  return !!(
    expected
    && typeof expected.state_sha256 === "string" && expected.state_sha256.length > 0
    && typeof expected.candidate_tree_sha256 === "string" && expected.candidate_tree_sha256.length > 0
    && typeof expected.report_sha256 === "string" && expected.report_sha256.length > 0
    && Array.isArray(review?.report?.units)
  );
}

function SourcePackageWorkspace({
  docId,
  user,
  api,
  publicationOverlay,
  onOpenProjectSource,
  onOpenLegacy,
  onRuntimePrepared,
  onOpenRunControl,
}) {
  const [status, setStatus] = React.useState(null);
  const [review, setReview] = React.useState(null);
  const [runtime, setRuntime] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [busy, setBusy] = React.useState("");
  const [error, setError] = React.useState(null);
  const [notice, setNotice] = React.useState(null);
  const [selectedUnitId, setSelectedUnitId] = React.useState("");
  const [titleDraft, setTitleDraft] = React.useState("");
  const [classificationDraft, setClassificationDraft] = React.useState("");
  const [parentDraft, setParentDraft] = React.useState("");
  const [draftResetVersion, setDraftResetVersion] = React.useState(0);
  const [technicalOpen, setTechnicalOpen] = React.useState(false);
  const [modal, setModal] = React.useState(null);
  const [publication, setPublication] = React.useState(null);

  const load = React.useCallback(async ({ silent = false } = {}) => {
    if (!docId) {
      setStatus(null);
      setReview(null);
      setRuntime(null);
      setLoading(false);
      return null;
    }
    if (!silent) setLoading(true);
    try {
      const nextStatus = await api.getSourcePackageStatus(docId);
      let nextReview = null;
      if (nextStatus?.managed === true) {
        nextReview = await api.getSourcePackageReview(docId);
      }
      const nextRuntime = await api.getProjectRuntime(docId).catch(() => null);
      setStatus(nextStatus);
      setReview(nextReview);
      setRuntime(nextRuntime);
      setError(null);
      return { status: nextStatus, review: nextReview, runtime: nextRuntime };
    } catch (nextError) {
      setError(sourcePackageErrorDetail(nextError));
      setStatus(null);
      setReview(null);
      return null;
    } finally {
      if (!silent) setLoading(false);
    }
  }, [api, docId]);

  React.useEffect(() => {
    setPublication(null);
    setNotice(null);
    setSelectedUnitId("");
    load();
  }, [docId, load]);

  const units = Array.isArray(review?.report?.units) ? review.report.units : [];
  const issues = Array.isArray(review?.report?.issues) ? review.report.issues : [];
  const skeleton = review?.report?.global_skeleton && typeof review.report.global_skeleton === "object"
    ? review.report.global_skeleton
    : null;
  const outline = Array.isArray(skeleton?.outline) ? skeleton.outline : [];
  const navigation = Array.isArray(skeleton?.navigation) ? skeleton.navigation : [];
  const candidates = Array.isArray(skeleton?.candidates) ? skeleton.candidates : [];
  const selectedUnit = units.find(unit => unit.unit_id === selectedUnitId) || units[0] || null;
  const selectedIndex = selectedUnit ? units.findIndex(unit => unit.unit_id === selectedUnit.unit_id) : -1;
  const nextUnit = selectedIndex >= 0 ? units[selectedIndex + 1] || null : null;
  const selectedOutline = outline.find(row => row.unit_id === selectedUnit?.unit_id) || null;
  const currentParentId = String(selectedOutline?.parent_unit_id || "");
  const selectedBlockIds = Array.isArray(selectedUnit?.block_ids) ? selectedUnit.block_ids : [];
  const selectedIssues = issues.filter(row => row?.scope === "document" || row?.target_id === selectedUnit?.unit_id);
  const pendingReviewUnits = units.filter(unit => unit?.review_required === true || sourcePackageClassification(unit) === "review");
  const selectedCandidates = candidates.filter(row => {
    const unitIds = Array.isArray(row?.unit_ids) ? row.unit_ids : [];
    const blockIds = Array.isArray(row?.block_ids) ? row.block_ids : [];
    return unitIds.includes(selectedUnit?.unit_id) || blockIds.some(blockId => selectedBlockIds.includes(blockId));
  });

  React.useEffect(() => {
    if (!units.length) {
      setSelectedUnitId("");
      return;
    }
    if (!units.some(unit => unit.unit_id === selectedUnitId)) setSelectedUnitId(units[0].unit_id);
  }, [units, selectedUnitId]);

  React.useEffect(() => {
    if (!selectedUnit) return;
    setTitleDraft(String(selectedUnit.title || ""));
    setClassificationDraft(sourcePackageClassification(selectedUnit));
    setParentDraft(currentParentId);
  }, [selectedUnit?.unit_id, selectedUnit?.title, selectedUnit?.translation_policy, selectedUnit?.review_required, currentParentId, draftResetVersion]);

  const mode = String(status?.mode || "");
  const modeMeta = SOURCE_PACKAGE_MODE_META[mode] || { vi: mode || "Chưa có trạng thái", en: mode || "No status", tone: "unknown", step: -1 };
  const frozen = mode === "managed_run_started_frozen" || status?.lifecycle === "run_started_frozen";
  const finalized = mode === "managed_finalized_pre_run" || status?.lifecycle === "finalized_pre_run";
  const managedDraft = mode === "managed_draft" && status?.lifecycle === "draft";
  const legacy = mode === "legacy_only";
  const reviewReady = status?.managed === true && sourcePackageExpectedReady(review);
  const blockingContract = status?.managed === true && !reviewReady;
  const correctionsSupported = Array.isArray(review?.supported_actions) ? review.supported_actions : [];
  const hierarchySupported = Array.isArray(review?.supported_hierarchy_actions) ? review.supported_hierarchy_actions : [];
  const canUpdate = managedDraft && reviewReady && correctionsSupported.includes("update_unit");
  const canSplit = managedDraft && reviewReady && correctionsSupported.includes("split_unit") && selectedBlockIds.length > 1;
  const canMerge = managedDraft && reviewReady && correctionsSupported.includes("merge_adjacent_units") && !!nextUnit;
  const canHierarchy = managedDraft && reviewReady && hierarchySupported.length > 0;
  const canFinalize = managedDraft && reviewReady && status?.finalization_allowed === true;
  const overlayReady = !!(
    publicationOverlay
    && typeof publicationOverlay === "object"
    && publicationOverlay.schema_version === "canonical_translation_overlay_v1"
  );
  const exportReason = !frozen
    ? uiText("Chỉ xuất sau khi run đầu tiên đã đóng băng source package.", "Export is available only after the first run freezes the source package.")
    : !overlayReady
      ? uiText("Chưa có producer/relay authoritative canonical_translation_overlay_v1 trong App state/API.", "No authoritative canonical_translation_overlay_v1 producer or relay is available in App state/API.")
      : uiText("Xuất HTML/Markdown từ overlay authoritative.", "Export HTML/Markdown from the authoritative overlay.");

  async function mutate(kind, action, successMessage) {
    if (busy || blockingContract) return false;
    setBusy(kind);
    setError(null);
    setNotice(null);
    try {
      await action();
      await load({ silent: true });
      setNotice({ tone: "good", text: successMessage });
      return true;
    } catch (nextError) {
      const detail = sourcePackageErrorDetail(nextError);
      if (detail.status === 409) {
        await load({ silent: true });
        setDraftResetVersion(version => version + 1);
        if (detail.code.includes("stale")) {
          setNotice({ tone: "warn", text: uiText("Cấu trúc đã thay đổi ở lượt khác. Dữ liệu kiểm tra mới đã được tải; hãy xem lại trước khi gửi lại.", "The structure changed in another session. A fresh review has been loaded; inspect it before submitting again.") });
        } else if (detail.code.includes("frozen")) {
          setNotice({ tone: "warn", text: uiText("Backend đã đóng băng source package. Mọi control biên soạn đã chuyển sang chỉ đọc.", "The backend froze the source package. All editing controls are now read-only.") });
        }
      }
      setError(detail);
      return false;
    } finally {
      setBusy("");
    }
  }

  function expectedMutationBody(actions) {
    return {
      expected_state_sha256: review.expected.state_sha256,
      expected_candidate_tree_sha256: review.expected.candidate_tree_sha256,
      expected_report_sha256: review.expected.report_sha256,
      approved: true,
      user: String(user || "local"),
      actions,
    };
  }

  async function normalize() {
    await mutate("normalize", () => api.normalizeSourcePackage(docId), status?.managed ? uiText("Backend đã xác nhận lại candidate bất biến.", "The backend reconfirmed the immutable candidate.") : uiText("Đã tạo managed source package.", "Managed source package created.") );
  }

  async function saveUnit() {
    if (!selectedUnit || !canUpdate) return;
    const originalTitle = String(selectedUnit.title || "");
    const originalClass = sourcePackageClassification(selectedUnit);
    const nextTitle = titleDraft.trim();
    const nextClass = classificationDraft;
    if (!nextTitle || (nextTitle === originalTitle && nextClass === originalClass)) return;
    const action = {
      action_type: "update_unit",
      unit_id: selectedUnit.unit_id,
      new_title: nextTitle === originalTitle ? null : nextTitle,
      classification: nextClass === originalClass ? null : nextClass,
    };
    await mutate("update", () => api.applySourcePackageCorrections(docId, expectedMutationBody([action])), uiText("Đã tạo revision đổi tên/phân loại.", "A title/classification revision was created.") );
  }

  async function applySplit() {
    if (modal?.kind !== "split" || !canSplit) return;
    const action = {
      action_type: "split_unit",
      unit_id: selectedUnit.unit_id,
      at_block_id: modal.atBlockId,
      left_title: modal.leftTitle.trim(),
      right_title: modal.rightTitle.trim(),
      left_classification: modal.leftClassification,
      right_classification: modal.rightClassification,
    };
    const ok = await mutate("split", () => api.applySourcePackageCorrections(docId, expectedMutationBody([action])), uiText("Đã tạo revision tách unit tại ranh giới block.", "A revision split the unit at the block boundary.") );
    if (ok) setModal(null);
  }

  async function applyMerge() {
    if (modal?.kind !== "merge" || !canMerge) return;
    const action = {
      action_type: "merge_adjacent_units",
      left_unit_id: selectedUnit.unit_id,
      right_unit_id: nextUnit.unit_id,
      new_title: modal.title.trim(),
      classification: modal.classification,
    };
    const ok = await mutate("merge", () => api.applySourcePackageCorrections(docId, expectedMutationBody([action])), uiText("Đã tạo revision gộp hai unit liền kề.", "A revision merged the adjacent units.") );
    if (ok) setModal(null);
  }

  async function saveHierarchy() {
    if (!selectedUnit || !canHierarchy || parentDraft === currentParentId) return;
    const action = parentDraft
      ? { action_type: "set_parent", child_unit_id: selectedUnit.unit_id, parent_unit_id: parentDraft }
      : { action_type: "clear_parent", child_unit_id: selectedUnit.unit_id };
    if (!hierarchySupported.includes(action.action_type)) return;
    await mutate("hierarchy", () => api.applySourcePackageHierarchy(docId, expectedMutationBody([action])), parentDraft ? uiText("Đã tạo revision quan hệ cha-con.", "A parent-child hierarchy revision was created.") : uiText("Đã xóa unit cha trong revision phân cấp.", "The parent unit was cleared in the hierarchy revision.") );
  }

  async function finalize() {
    if (!canFinalize) return;
    const body = {
      expected_state_sha256: review.expected.state_sha256,
      expected_candidate_tree_sha256: review.expected.candidate_tree_sha256,
      expected_report_sha256: review.expected.report_sha256,
      expected_hierarchy_sha256: review.expected.hierarchy_sha256 ?? null,
      approved: true,
      user: String(user || "local"),
    };
    const ok = await mutate("finalize", () => api.finalizeSourcePackage(docId, body), uiText("Đã chốt source package trước run.", "The source package was finalized before the run.") );
    if (ok) setModal(null);
  }

  async function prepareRuntime() {
    if (!finalized || busy) return;
    setBusy("prepare");
    setError(null);
    try {
      const prepared = await api.prepareProjectRuntime(docId);
      setRuntime(prepared);
      if (onRuntimePrepared) onRuntimePrepared(prepared);
      await load({ silent: true });
      setNotice({ tone: "good", text: uiText("Runtime đã sẵn sàng{job}.", "Runtime is ready{job}.", { job: prepared?.job_id ? ` · ${prepared.job_id}` : "" }) });
    } catch (nextError) {
      setError(sourcePackageErrorDetail(nextError));
    } finally {
      setBusy("");
    }
  }

  async function publish() {
    if (!frozen || !overlayReady || busy) return;
    setBusy("publish");
    setError(null);
    try {
      const result = await api.publishSourcePackage(docId, publicationOverlay);
      setPublication(result);
      setModal(null);
      setNotice({ tone: "good", text: result?.reused ? uiText("Đã dùng lại publication cùng nội dung.", "The content-identical publication was reused.") : uiText("Đã tạo publication HTML/Markdown.", "The HTML/Markdown publication was created.") });
    } catch (nextError) {
      setError(sourcePackageErrorDetail(nextError));
    } finally {
      setBusy("");
    }
  }

  function openSplit() {
    const baseClass = sourcePackageClassification(selectedUnit) || "review";
    setModal({
      kind: "split",
      atBlockId: selectedBlockIds[1] || "",
      leftTitle: String(selectedUnit?.title || "") + " · A",
      rightTitle: String(selectedUnit?.title || "") + " · B",
      leftClassification: baseClass,
      rightClassification: baseClass,
    });
  }

  function openMerge() {
    setModal({
      kind: "merge",
      title: [selectedUnit?.title, nextUnit?.title].filter(Boolean).join(" + "),
      classification: sourcePackageClassification(selectedUnit) || "review",
    });
  }

  function moveUnitFocus(event, index) {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const nextIndex = event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? units.length - 1
        : Math.max(0, Math.min(units.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
    const next = units[nextIndex];
    if (!next) return;
    setSelectedUnitId(next.unit_id);
    requestAnimationFrame(() => document.querySelector(`[data-source-unit="${CSS.escape(next.unit_id)}"]`)?.focus());
  }

  const parentChoices = units.filter(unit => (
    unit.unit_id !== selectedUnit?.unit_id
    && Number(unit.order_index) < Number(selectedUnit?.order_index)
  ));
  const saveDisabled = !canUpdate
    || !titleDraft.trim()
    || (titleDraft.trim() === String(selectedUnit?.title || "") && classificationDraft === sourcePackageClassification(selectedUnit));
  const artifacts = publication?.artifacts && typeof publication.artifacts === "object"
    ? Object.entries(publication.artifacts)
    : [];

  return (
    <section className="source-package-workspace" aria-label={uiText("Cấu trúc source package", "Source package structure")}>
      <header className="sp-commandbar">
        <div className="sp-command-title">
          <span className="sp-command-icon"><Ic.layers size={16} /></span>
          <div><b>{uiText("Cấu trúc", "Structure")}</b><span className="mono">{docId || "no_project"}</span></div>
          <span className={`sp-lifecycle-badge ${modeMeta.tone}`}><span />{uiText(modeMeta.vi, modeMeta.en)}</span>
        </div>
        <div className="sp-command-actions">
          <button className="btn icon-only tip" type="button" data-tip={uiText("Tải lại trạng thái và dữ liệu kiểm tra từ backend", "Reload backend status and review")} aria-label={uiText("Tải lại cấu trúc", "Reload structure")} disabled={!!busy} onClick={() => load()}><Ic.refresh size={13} /></button>
          {status?.normalize_allowed === true && !finalized && !frozen && (
            <button className="btn" type="button" disabled={!!busy} onClick={normalize}><Ic.sparkle size={13} />{status?.managed ? uiText("Chuẩn hóa lại", "Normalize again") : uiText("Chuẩn hóa", "Normalize")}</button>
          )}
          <button className="btn" type="button" disabled={!canSplit || !!busy} title={!canSplit ? uiText("Chọn unit có ít nhất hai block và được backend hỗ trợ tách.", "Select a unit with at least two blocks and backend split support.") : ""} onClick={openSplit}><Ic.sliders size={13} />{uiText("Tách", "Split")}</button>
          <button className="btn" type="button" disabled={!canMerge || !!busy} title={!canMerge ? uiText("Chỉ gộp unit đang chọn với unit liền sau.", "Only the selected unit and its immediate successor can be merged.") : ""} onClick={openMerge}><Ic.layers size={13} />{uiText("Gộp", "Merge")}</button>
          <button className="btn primary" type="button" disabled={!canFinalize || !!busy} onClick={() => setModal({ kind: "finalize" })}><Ic.checkCircle size={13} />{uiText("Chốt cấu trúc", "Finalize structure")}</button>
          {finalized && <button className="btn primary" type="button" disabled={!!busy} onClick={prepareRuntime}><Ic.bolt size={13} />{uiText("Chuẩn bị runtime", "Prepare runtime")}</button>}
          {runtime?.prepared && <button className="btn" type="button" disabled={!!busy || !onOpenRunControl} onClick={() => onOpenRunControl(runtime)}><Ic.play size={13} />{uiText("Điều khiển chạy", "Run Control")}</button>}
          <button className="btn" type="button" disabled={!frozen || !overlayReady || !!busy} title={exportReason} onClick={() => setModal({ kind: "publication" })}><Ic.upload size={13} />{uiText("Xuất tài liệu", "Export document")}</button>
        </div>
      </header>

      <div className="sp-lifecycle-track" aria-label={uiText("Vòng đời source package", "Source package lifecycle")}>
        {[["Nguồn", "Source"], ["Chuẩn hóa", "Normalize"], ["Kiểm tra", "Review"], ["Đã chốt", "Finalized"], ["Đã chạy", "Run started"]].map(([vi, en], index) => {
          const label = uiText(vi, en);
          return (
            <div key={en} className={modeMeta.step === index ? "current" : modeMeta.step > index ? "done" : ""}>
              <span>{modeMeta.step > index ? <Ic.check size={10} /> : index + 1}</span><b>{label}</b>
            </div>
          );
        })}
      </div>

      {notice && <div className={`sp-banner ${notice.tone}`} role="status"><Ic.checkCircle size={13} /><span>{notice.text}</span></div>}
      {error && <div className="sp-banner bad" role="alert"><Ic.alert size={13} /><span><b className="mono">{error.code}</b>{error.message}</span></div>}
      {blockingContract && <div className="sp-banner bad" role="alert"><Ic.lock size={13} /><span><b>{uiText("Dữ liệu kiểm tra chưa đầy đủ", "Incomplete review contract")}</b>{uiText("UI đã khóa mutation vì thiếu expected hashes hoặc report.units từ backend.", "Mutations are locked because expected hashes or backend report.units are missing.")}</span></div>}

      {loading ? (
        <div className="sp-loading" aria-live="polite"><span className="as-spin" /><b>{uiText("Đang tải trạng thái và dữ liệu kiểm tra từ backend…", "Loading backend status and review…")}</b></div>
      ) : legacy ? (
        <div className="sp-empty legacy">
          <Ic.clock size={24} />
          <h2>{uiText("Project legacy không được chuyển ngầm", "Legacy projects are never converted implicitly")}</h2>
          <p>{uiText("Backend trả về", "The backend returned")} <span className="mono">legacy_only</span>. {uiText("Managed normalize bị khóa để bảo toàn dữ liệu cũ.", "Managed normalization is locked to preserve legacy data.")}</p>
          <button className="btn" type="button" onClick={onOpenLegacy}><Ic.arrowRight size={13} />{uiText("Mở workspace legacy", "Open legacy workspace")}</button>
        </div>
      ) : status?.managed !== true ? (
        <div className="sp-empty">
          <Ic.file size={24} />
          <h2>{status?.source ? uiText("Nguồn đã sẵn sàng để chuẩn hóa", "Source is ready for normalization") : uiText("Chưa có tệp nguồn", "No source file")}</h2>
          <p>{status?.source
            ? `${status.source.filename || uiText("Tệp nguồn", "Source file")} · ${String(status.source.format || "").toUpperCase()}`
            : uiText("Tải TXT, EPUB, Markdown, HTML hoặc PDF trong Project / Nguồn trước khi chuẩn hóa.", "Upload TXT, EPUB, Markdown, HTML, or PDF in Project / Source before normalization.")}</p>
          <div>
            <button className="btn" type="button" onClick={onOpenProjectSource}><Ic.upload size={13} />{uiText("Project / Nguồn", "Project / Source")}</button>
            {status?.normalize_allowed === true && <button className="btn primary" type="button" disabled={!!busy} onClick={normalize}><Ic.sparkle size={13} />{uiText("Chuẩn hóa", "Normalize")}</button>}
          </div>
        </div>
      ) : (
        <>
          <div className="sp-summaryline">
            {review?.report?.integrity?.unit_count !== undefined && <span><b>{review.report.integrity.unit_count}</b> {uiText("đơn vị", "units")}</span>}
            {skeleton?.statistics?.block_count !== undefined && <span><b>{skeleton.statistics.block_count}</b> block</span>}
            {review?.report?.integrity?.issue_count !== undefined && <span><b>{review.report.integrity.issue_count}</b> {uiText("vấn đề", "issues")}</span>}
            {status?.source?.format && <span><b>{String(status.source.format).toUpperCase()}</b> {uiText("nguồn", "source")}</span>}
            {frozen && <span className="frozen"><Ic.lock size={11} /><b>{uiText("Chỉ đọc tuyệt đối", "Strictly read-only")}</b></span>}
          </div>

          <div className="sp-workgrid">
            <aside className="sp-unit-pane" aria-label={uiText("Danh sách đơn vị", "Unit list")}>
              <div className="sp-pane-head"><span>{uiText("Dàn ý / đơn vị", "Outline / units")}</span><em>{units.length}</em></div>
              <div className="sp-unit-list" role="listbox" aria-label={uiText("Đơn vị cấu trúc", "Structure units")}>
                {units.map((unit, index) => {
                  const classification = sourcePackageClassification(unit);
                  const unitIssueCount = Array.isArray(unit.issue_codes) ? unit.issue_codes.length : 0;
                  return (
                    <button key={unit.unit_id} type="button" role="option" aria-selected={unit.unit_id === selectedUnit?.unit_id}
                      data-source-unit={unit.unit_id} className={unit.unit_id === selectedUnit?.unit_id ? "selected" : ""}
                      onClick={() => setSelectedUnitId(unit.unit_id)} onKeyDown={event => moveUnitFocus(event, index)}>
                      <span className="sp-unit-order">{Number.isFinite(Number(unit.order_index)) ? Number(unit.order_index) + 1 : index + 1}</span>
                      <span className="sp-unit-main"><b>{unit.title || unit.unit_id}</b><em className="mono">{unit.unit_id}</em></span>
                      <span className={`sp-class ${classification || "unknown"}`}>{sourcePackageClassificationLabel(classification)}</span>
                      <span className="sp-unit-meta">{Array.isArray(unit.block_ids) ? `${unit.block_ids.length} block` : ""}{unitIssueCount ? ` · ${unitIssueCount} ${uiText("cờ", "flags")}` : ""}</span>
                    </button>
                  );
                })}
              </div>
            </aside>

            <main className="sp-preview-pane">
              <div className="sp-pane-head"><span>{uiText("Nội dung và ranh giới", "Content and boundaries")}</span><em>{selectedUnit?.chapter_id || "—"}</em></div>
              {selectedUnit ? <>
                <div className="sp-preview-title"><div><span>{uiText("Đơn vị đang chọn", "Selected unit")}</span><h2>{selectedUnit.title || selectedUnit.unit_id}</h2></div><span className="mono">{selectedUnit.unit_id}</span></div>

                <section className="sp-flat-section">
                  <div className="sp-section-title"><Ic.list size={13} /><b>{uiText("Ranh giới block", "Block boundaries")}</b><span>{selectedBlockIds.length}</span></div>
                  <div className="sp-block-sequence">
                    {selectedBlockIds.map((blockId, index) => <div key={blockId}><span>{index + 1}</span><code>{blockId}</code>{index > 0 && canSplit ? <button type="button" className="btn sm" onClick={() => {
                      openSplit();
                      setModal(current => ({ ...current, atBlockId: blockId }));
                    }}>{uiText("Tách tại đây", "Split here")}</button> : null}</div>)}
                  </div>
                  {!selectedBlockIds.length && <p className="sp-muted">{uiText("Payload review không công bố block ID cho đơn vị này.", "The review payload does not expose block IDs for this unit.")}</p>}
                </section>

                {!!selectedIssues.length && <section className="sp-flat-section">
                  <div className="sp-section-title"><Ic.alert size={13} /><b>{uiText("Vấn đề / cờ kiểm tra", "Issues / review flags")}</b><span>{selectedIssues.length}</span></div>
                  <div className="sp-issue-list">{selectedIssues.map(issue => <div key={issue.issue_id || `${issue.code}-${issue.target_id}`}>
                    <b className="mono">{issue.code}</b><span>{issue.scope}{issue.target_id ? ` · ${issue.target_id}` : ""}</span>
                    {Array.isArray(issue.evidence) && issue.evidence.length ? <ul>{issue.evidence.map(item => <li key={item}>{item}</li>)}</ul> : null}
                  </div>)}</div>
                </section>}

                {!!selectedCandidates.length && <section className="sp-flat-section">
                  <div className="sp-section-title"><Ic.search size={13} /><b>{uiText("Bằng chứng candidate", "Candidate evidence")}</b><span>{selectedCandidates.length}</span></div>
                  <div className="sp-candidate-list">{selectedCandidates.map(candidate => <div key={candidate.candidate_id}>
                    <b>{candidate.title || candidate.candidate_kind || candidate.candidate_id}</b>
                    <span className="mono">{candidate.candidate_id}</span>
                    <em>{[candidate.source_signal, candidate.resolution_status].filter(Boolean).join(" · ")}</em>
                    {Array.isArray(candidate.signals) && candidate.signals.length ? <small>{candidate.signals.join(" · ")}</small> : null}
                  </div>)}</div>
                </section>}

                {!!navigation.length && <details className="sp-flat-details">
                  <summary><Ic.book size={13} /><b>{uiText("Bằng chứng mục lục / điều hướng", "TOC / navigation evidence")}</b><span>{navigation.length}</span></summary>
                  <div className="sp-navigation-list">{navigation.map(row => <div key={row.entry_id}>
                    <span style={{ paddingLeft: `${Math.max(0, Number(row.depth) || 0) * 12}px` }}>{row.title || row.entry_id}</span>
                    <em>{row.resolution_status || ""}</em>
                  </div>)}</div>
                </details>}
              </> : <div className="sp-empty-inline">{uiText("Backend chưa trả đơn vị để review.", "The backend has not returned review units.")}</div>}
            </main>

            <aside className="sp-detail-pane">
              <div className="sp-pane-head"><span>{uiText("Chi tiết đơn vị", "Unit details")}</span><em>{frozen || finalized ? uiText("chỉ đọc", "read-only") : uiText("bản nháp", "draft")}</em></div>
              {selectedUnit ? <div className="sp-detail-body">
                {(frozen || finalized) && <div className="sp-readonly-note"><Ic.lock size={12} /><span>{frozen ? uiText("Run đầu tiên đã khóa revision này vĩnh viễn.", "The first run permanently locked this revision.") : uiText("Đã chốt cấu trúc; chỉ còn chuẩn bị/chạy.", "The structure is finalized; only prepare/run remains.")}</span></div>}
                <label className="sp-field"><span>{uiText("Tiêu đề", "Title")}</span><input value={titleDraft} maxLength={500} disabled={!canUpdate || !!busy} onChange={event => setTitleDraft(event.target.value)} /></label>
                <label className="sp-field"><span>{uiText("Phân loại", "Classification")}</span><select value={classificationDraft} disabled={!canUpdate || !!busy} onChange={event => setClassificationDraft(event.target.value)}>
                  {!classificationDraft && <option value="">{uiText("Backend chưa công bố", "Not reported by backend")}</option>}
                  {SOURCE_PACKAGE_CLASSIFICATIONS.map(item => <option key={item.id} value={item.id}>{uiText(item.vi, item.en)}</option>)}
                </select></label>
                <button className="btn primary sp-save-unit" type="button" disabled={saveDisabled || !!busy} onClick={saveUnit}><Ic.save size={13} />{uiText("Lưu revision đơn vị", "Save unit revision")}</button>

                <div className="sp-detail-divider" />
                <label className="sp-field"><span>{uiText("Đơn vị cha", "Parent unit")}</span><select value={parentDraft} disabled={!canHierarchy || !!busy} onChange={event => setParentDraft(event.target.value)}>
                  <option value="">{uiText("Không có đơn vị cha", "No parent unit")}</option>
                  {parentChoices.map(unit => <option key={unit.unit_id} value={unit.unit_id}>{unit.title || unit.unit_id}</option>)}
                </select></label>
                <button className="btn" type="button" disabled={!canHierarchy || parentDraft === currentParentId || !!busy} onClick={saveHierarchy}><Ic.layers size={13} />{uiText("Lưu phân cấp", "Save hierarchy")}</button>

                <div className="sp-detail-divider" />
                <dl className="sp-unit-facts">
                  {selectedUnit.role !== undefined && <><dt>{uiText("Vai trò", "Role")}</dt><dd>{selectedUnit.role}</dd></>}
                  {selectedUnit.confidence !== undefined && <><dt>{uiText("Độ tin cậy", "Confidence")}</dt><dd>{selectedUnit.confidence}</dd></>}
                  {selectedUnit.review_required !== undefined && <><dt>{uiText("Yêu cầu duyệt", "Review")}</dt><dd>{selectedUnit.review_required ? uiText("bắt buộc", "required") : uiText("không bắt buộc", "not required")}</dd></>}
                  {selectedUnit.chapter_id && <><dt>{uiText("Chương", "Chapter")}</dt><dd className="mono">{selectedUnit.chapter_id}</dd></>}
                </dl>
              </div> : <div className="sp-empty-inline">{uiText("Chọn một đơn vị.", "Select a unit.")}</div>}
            </aside>
          </div>

          <details className="sp-technical" open={technicalOpen} onToggle={event => setTechnicalOpen(event.currentTarget.open)}>
            <summary><Ic.lock size={12} /><span>{uiText("Chi tiết kỹ thuật", "Technical details")}</span><em>{uiText("ID và hash từ backend", "Backend IDs and hashes")}</em></summary>
            <dl>
              {status?.state_sha256 && <><dt>state</dt><dd title={status.state_sha256}>{sourcePackageShortHash(status.state_sha256)}</dd></>}
              {review?.expected?.candidate_tree_sha256 && <><dt>candidate tree</dt><dd title={review.expected.candidate_tree_sha256}>{sourcePackageShortHash(review.expected.candidate_tree_sha256)}</dd></>}
              {review?.expected?.report_sha256 && <><dt>review report</dt><dd title={review.expected.report_sha256}>{sourcePackageShortHash(review.expected.report_sha256)}</dd></>}
              {review?.expected && Object.prototype.hasOwnProperty.call(review.expected, "hierarchy_sha256") && <><dt>hierarchy</dt><dd title={review.expected.hierarchy_sha256 || "null"}>{review.expected.hierarchy_sha256 ? sourcePackageShortHash(review.expected.hierarchy_sha256) : "null"}</dd></>}
              {status?.candidate?.candidate_id && <><dt>candidate</dt><dd>{status.candidate.candidate_id}</dd></>}
              {status?.run_start?.run_id && <><dt>{uiText("run đóng băng", "frozen run")}</dt><dd>{status.run_start.run_id}</dd></>}
            </dl>
          </details>

          {frozen && !overlayReady && <div className="sp-export-gap"><Ic.alert size={13} /><div><b>{uiText("Xuất tài liệu đang fail-closed", "Document export is fail-closed")}</b><span>{uiText("Thiếu producer/relay authoritative", "Missing an authoritative producer/relay for")} <code>canonical_translation_overlay_v1</code> {uiText("trong App state/API. UI không dựng overlay từ preview, translation rows, report hay đường dẫn máy khách.", "in App state/API. The UI never derives an overlay from previews, translation rows, reports, or client filesystem paths.")}</span></div></div>}
          {publication && <section className="sp-publication-result" aria-live="polite">
            <div><Ic.checkCircle size={16} /><span><b>{publication.reused ? uiText("Publication đã tồn tại", "Publication already exists") : uiText("Đã tạo publication", "Publication created")}</b><code>{publication.publication_id}</code></span></div>
            {artifacts.length ? <div className="sp-artifact-list">{artifacts.map(([kind, artifact]) => <div key={kind}><b>{kind}</b><span className="mono">{typeof artifact === "string" ? artifact : artifact?.path || artifact?.relative_path || uiText("Backend không công bố đường dẫn", "Path not reported by backend")}</span></div>)}</div> : null}
          </section>}
        </>
      )}

      {modal?.kind === "split" && <Modal title={uiText("Tách đơn vị tại ranh giới block", "Split unit at a block boundary")} icon={Ic.sliders} onClose={() => !busy && setModal(null)} actions={<>
        <button className="btn" type="button" disabled={!!busy} onClick={() => setModal(null)}>{uiText("Hủy", "Cancel")}</button>
        <button className="btn primary" type="button" disabled={!!busy || !modal.atBlockId || !modal.leftTitle.trim() || !modal.rightTitle.trim()} onClick={applySplit}>{uiText("Xác nhận tách", "Confirm split")}</button>
      </>}>
        <div className="sp-modal-grid">
          <label className="sp-field"><span>{uiText("Đơn vị mới bắt đầu tại block", "New unit starts at block")}</span><select value={modal.atBlockId} onChange={event => setModal({ ...modal, atBlockId: event.target.value })}>{selectedBlockIds.slice(1).map(blockId => <option key={blockId}>{blockId}</option>)}</select></label>
          <label className="sp-field"><span>{uiText("Tiêu đề phần trái", "Left title")}</span><input value={modal.leftTitle} onChange={event => setModal({ ...modal, leftTitle: event.target.value })} /></label>
          <label className="sp-field"><span>{uiText("Phân loại phần trái", "Left classification")}</span><select value={modal.leftClassification} onChange={event => setModal({ ...modal, leftClassification: event.target.value })}>{SOURCE_PACKAGE_CLASSIFICATIONS.map(item => <option key={item.id} value={item.id}>{uiText(item.vi, item.en)}</option>)}</select></label>
          <label className="sp-field"><span>{uiText("Tiêu đề phần phải", "Right title")}</span><input value={modal.rightTitle} onChange={event => setModal({ ...modal, rightTitle: event.target.value })} /></label>
          <label className="sp-field"><span>{uiText("Phân loại phần phải", "Right classification")}</span><select value={modal.rightClassification} onChange={event => setModal({ ...modal, rightClassification: event.target.value })}>{SOURCE_PACKAGE_CLASSIFICATIONS.map(item => <option key={item.id} value={item.id}>{uiText(item.vi, item.en)}</option>)}</select></label>
        </div>
      </Modal>}

      {modal?.kind === "merge" && <Modal title={uiText("Gộp hai đơn vị liền kề", "Merge adjacent units")} icon={Ic.layers} onClose={() => !busy && setModal(null)} actions={<>
        <button className="btn" type="button" disabled={!!busy} onClick={() => setModal(null)}>{uiText("Hủy", "Cancel")}</button>
        <button className="btn primary" type="button" disabled={!!busy || !modal.title.trim()} onClick={applyMerge}>{uiText("Xác nhận gộp", "Confirm merge")}</button>
      </>}>
        <p><span className="mono">{selectedUnit?.unit_id}</span> + <span className="mono">{nextUnit?.unit_id}</span></p>
        <div className="sp-modal-grid">
          <label className="sp-field"><span>{uiText("Tiêu đề đơn vị mới", "New unit title")}</span><input value={modal.title} onChange={event => setModal({ ...modal, title: event.target.value })} /></label>
          <label className="sp-field"><span>{uiText("Phân loại", "Classification")}</span><select value={modal.classification} onChange={event => setModal({ ...modal, classification: event.target.value })}>{SOURCE_PACKAGE_CLASSIFICATIONS.map(item => <option key={item.id} value={item.id}>{uiText(item.vi, item.en)}</option>)}</select></label>
        </div>
      </Modal>}

      {modal?.kind === "finalize" && <Modal title={uiText("Chốt cấu trúc trước run", "Finalize structure before run")} icon={Ic.checkCircle} onClose={() => !busy && setModal(null)} actions={<>
        <button className="btn" type="button" disabled={!!busy} onClick={() => setModal(null)}>{uiText("Xem lại", "Review again")}</button>
        <button className="btn primary" type="button" disabled={!!busy} onClick={finalize}>{uiText("Chốt cấu trúc", "Finalize structure")}</button>
      </>}>
        <p>{uiText("Backend sẽ tạo revision chốt từ đúng hash state/tree/report/hierarchy của review hiện tại.", "The backend will create a finalization revision from the exact state/tree/report/hierarchy hashes in the current review.")}</p>
        {(pendingReviewUnits.length > 0 || issues.length > 0) && <>
          <p><b>{uiText(`Còn ${pendingReviewUnits.length} đơn vị và ${issues.length} vấn đề cần xem lại trước khi xác nhận:`, `${pendingReviewUnits.length} unit(s) and ${issues.length} issue(s) still require review before confirmation:`)}</b></p>
          <div className="sp-issue-list">
            {pendingReviewUnits.map(unit => <div key={`review-${unit.unit_id}`}><b>{unit.title || unit.unit_id}</b><span className="mono">{unit.unit_id} · {sourcePackageClassificationLabel(sourcePackageClassification(unit))}</span>{Array.isArray(unit.issue_codes) && unit.issue_codes.length ? <small>{unit.issue_codes.join(" · ")}</small> : null}</div>)}
            {issues.map((issue, index) => <div key={`issue-${issue.issue_id || issue.code || index}`}><b className="mono">{issue.code || uiText("vấn đề chưa đặt mã", "uncoded issue")}</b><span>{[issue.scope, issue.target_id].filter(Boolean).join(" · ")}</span></div>)}
          </div>
        </>}
        <p className="muted">{uiText("Sau khi chốt, UI chỉ cho chuẩn bị runtime và chạy pipeline; không chỉnh cấu trúc trong revision này.", "After finalization, the UI allows only runtime preparation and pipeline execution; this revision cannot be edited.")}</p>
      </Modal>}

      {modal?.kind === "publication" && <Modal title={uiText("Xuất tài liệu", "Export document")} icon={Ic.upload} onClose={() => !busy && setModal(null)} actions={<>
        <button className="btn" type="button" disabled={!!busy} onClick={() => setModal(null)}>{uiText("Hủy", "Cancel")}</button>
        <button className="btn primary" type="button" disabled={!!busy || !overlayReady} onClick={publish}>{uiText("Xuất HTML / Markdown", "Export HTML / Markdown")}</button>
      </>}>
        <p>{uiText("Body gửi nguyên object", "The request body sends the exact authoritative")} <code>canonical_translation_overlay_v1</code> {uiText("authoritative đang có trong App state. UI không sửa, thêm row hoặc dựng lại overlay.", "object available in App state. The UI does not modify rows or rebuild the overlay.")}</p>
        <p className="muted">{uiText("Publication tạo output mới và không ghi đè source package.", "Publication creates new output and never overwrites the source package.")}</p>
      </Modal>}
    </section>
  );
}

window.SourcePackageWorkspace = SourcePackageWorkspace;
