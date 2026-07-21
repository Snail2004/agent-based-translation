/* ===== MANAGED SOURCE PACKAGE: backend-authoritative normalization workflow ===== */

const SOURCE_PACKAGE_CLASSIFICATIONS = [
  { id: "translate", label: "Dịch" },
  { id: "preserve", label: "Giữ nguyên" },
  { id: "exclude", label: "Loại" },
  { id: "review", label: "Cần xem lại" },
];

const SOURCE_PACKAGE_MODE_META = {
  unmanaged_draft: { label: "Nguồn đã tải", tone: "pending", step: 0 },
  managed_draft: { label: "Bản nháp đang kiểm tra", tone: "review", step: 2 },
  managed_finalized_pre_run: { label: "Đã chốt trước run", tone: "ready", step: 3 },
  managed_run_started_frozen: { label: "Đã chạy · chỉ đọc", tone: "frozen", step: 4 },
  legacy_only: { label: "Project legacy", tone: "legacy", step: -1 },
};

function sourcePackageErrorDetail(error) {
  const first = error?.errors?.[0] || error?.payload?.errors?.[0] || {};
  return {
    code: String(first.code || "request_failed"),
    message: String(first.message || error?.message || "Request failed."),
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
  return SOURCE_PACKAGE_CLASSIFICATIONS.find(item => item.id === value)?.label || value || "Chưa công bố";
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
  const modeMeta = SOURCE_PACKAGE_MODE_META[mode] || { label: mode || "Chưa có trạng thái", tone: "unknown", step: -1 };
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
  const overlayReady = !!(
    publicationOverlay
    && typeof publicationOverlay === "object"
    && publicationOverlay.schema_version === "canonical_translation_overlay_v1"
  );
  const exportReason = !frozen
    ? "Chỉ xuất sau khi run đầu tiên đã đóng băng source package."
    : !overlayReady
      ? "Chưa có producer/relay authoritative canonical_translation_overlay_v1 trong App state/API."
      : "Xuất HTML/Markdown từ overlay authoritative.";

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
          setNotice({ tone: "warn", text: "Cấu trúc đã thay đổi ở lượt khác. Review mới đã được tải; hãy xem lại trước khi gửi lại." });
        } else if (detail.code.includes("frozen")) {
          setNotice({ tone: "warn", text: "Backend đã đóng băng source package. Mọi control biên soạn đã chuyển sang chỉ đọc." });
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
    await mutate("normalize", () => api.normalizeSourcePackage(docId), status?.managed ? "Backend đã xác nhận lại candidate bất biến." : "Đã tạo managed source package." );
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
    await mutate("update", () => api.applySourcePackageCorrections(docId, expectedMutationBody([action])), "Đã tạo revision đổi tên/phân loại." );
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
    const ok = await mutate("split", () => api.applySourcePackageCorrections(docId, expectedMutationBody([action])), "Đã tạo revision tách unit tại block boundary." );
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
    const ok = await mutate("merge", () => api.applySourcePackageCorrections(docId, expectedMutationBody([action])), "Đã tạo revision gộp hai unit liền kề." );
    if (ok) setModal(null);
  }

  async function saveHierarchy() {
    if (!selectedUnit || !canHierarchy || parentDraft === currentParentId) return;
    const action = parentDraft
      ? { action_type: "set_parent", child_unit_id: selectedUnit.unit_id, parent_unit_id: parentDraft }
      : { action_type: "clear_parent", child_unit_id: selectedUnit.unit_id };
    if (!hierarchySupported.includes(action.action_type)) return;
    await mutate("hierarchy", () => api.applySourcePackageHierarchy(docId, expectedMutationBody([action])), parentDraft ? "Đã tạo revision quan hệ cha-con." : "Đã xóa parent trong hierarchy revision." );
  }

  async function finalize() {
    if (!reviewReady || !managedDraft) return;
    const body = {
      expected_state_sha256: review.expected.state_sha256,
      expected_candidate_tree_sha256: review.expected.candidate_tree_sha256,
      expected_report_sha256: review.expected.report_sha256,
      expected_hierarchy_sha256: review.expected.hierarchy_sha256 ?? null,
      approved: true,
      user: String(user || "local"),
    };
    const ok = await mutate("finalize", () => api.finalizeSourcePackage(docId, body), "Đã chốt source package trước run." );
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
      setNotice({ tone: "good", text: `Runtime đã sẵn sàng${prepared?.job_id ? ` · ${prepared.job_id}` : ""}.` });
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
      setNotice({ tone: "good", text: result?.reused ? "Đã dùng lại publication cùng nội dung." : "Đã tạo publication HTML/Markdown." });
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
    <section className="source-package-workspace" aria-label="Cấu trúc source package">
      <header className="sp-commandbar">
        <div className="sp-command-title">
          <span className="sp-command-icon"><Ic.layers size={16} /></span>
          <div><b>Cấu trúc</b><span className="mono">{docId || "no_project"}</span></div>
          <span className={`sp-lifecycle-badge ${modeMeta.tone}`}><span />{modeMeta.label}</span>
        </div>
        <div className="sp-command-actions">
          <button className="btn icon-only tip" type="button" data-tip="Tải lại status và review từ backend" aria-label="Tải lại cấu trúc" disabled={!!busy} onClick={() => load()}><Ic.refresh size={13} /></button>
          {status?.normalize_allowed === true && !finalized && !frozen && (
            <button className="btn" type="button" disabled={!!busy} onClick={normalize}><Ic.sparkle size={13} />{status?.managed ? "Chuẩn hóa lại" : "Chuẩn hóa"}</button>
          )}
          <button className="btn" type="button" disabled={!canSplit || !!busy} title={!canSplit ? "Chọn unit có ít nhất hai block và được backend hỗ trợ split." : ""} onClick={openSplit}><Ic.sliders size={13} />Tách</button>
          <button className="btn" type="button" disabled={!canMerge || !!busy} title={!canMerge ? "Chỉ gộp unit đang chọn với unit liền sau." : ""} onClick={openMerge}><Ic.layers size={13} />Gộp</button>
          <button className="btn primary" type="button" disabled={!managedDraft || !reviewReady || !!busy} onClick={() => setModal({ kind: "finalize" })}><Ic.checkCircle size={13} />Chốt cấu trúc</button>
          {finalized && <button className="btn primary" type="button" disabled={!!busy} onClick={prepareRuntime}><Ic.bolt size={13} />Chuẩn bị runtime</button>}
          {runtime?.prepared && <button className="btn" type="button" disabled={!!busy || !onOpenRunControl} onClick={() => onOpenRunControl(runtime)}><Ic.play size={13} />Run Control</button>}
          <button className="btn" type="button" disabled={!frozen || !overlayReady || !!busy} title={exportReason} onClick={() => setModal({ kind: "publication" })}><Ic.upload size={13} />Xuất tài liệu</button>
        </div>
      </header>

      <div className="sp-lifecycle-track" aria-label="Source package lifecycle">
        {["Nguồn", "Chuẩn hóa", "Kiểm tra", "Đã chốt", "Đã chạy"].map((label, index) => (
          <div key={label} className={modeMeta.step === index ? "current" : modeMeta.step > index ? "done" : ""}>
            <span>{modeMeta.step > index ? <Ic.check size={10} /> : index + 1}</span><b>{label}</b>
          </div>
        ))}
      </div>

      {notice && <div className={`sp-banner ${notice.tone}`} role="status"><Ic.checkCircle size={13} /><span>{notice.text}</span></div>}
      {error && <div className="sp-banner bad" role="alert"><Ic.alert size={13} /><span><b className="mono">{error.code}</b>{error.message}</span></div>}
      {blockingContract && <div className="sp-banner bad" role="alert"><Ic.lock size={13} /><span><b>Review contract không đầy đủ</b>UI đã khóa mutation vì thiếu expected hashes hoặc report.units từ backend.</span></div>}

      {loading ? (
        <div className="sp-loading" aria-live="polite"><span className="as-spin" /><b>Đang tải status và review từ backend…</b></div>
      ) : legacy ? (
        <div className="sp-empty legacy">
          <Ic.clock size={24} />
          <h2>Project legacy không được chuyển ngầm</h2>
          <p>Backend trả về <span className="mono">legacy_only</span>. Managed normalize bị khóa để bảo toàn dữ liệu cũ.</p>
          <button className="btn" type="button" onClick={onOpenLegacy}><Ic.arrowRight size={13} />Mở workspace legacy</button>
        </div>
      ) : status?.managed !== true ? (
        <div className="sp-empty">
          <Ic.file size={24} />
          <h2>{status?.source ? "Nguồn đã sẵn sàng để chuẩn hóa" : "Chưa có tệp nguồn"}</h2>
          <p>{status?.source
            ? `${status.source.filename || "Tệp nguồn"} · ${String(status.source.format || "").toUpperCase()}`
            : "Tải TXT, EPUB, Markdown, HTML hoặc PDF trong Project / Source trước khi chuẩn hóa."}</p>
          <div>
            <button className="btn" type="button" onClick={onOpenProjectSource}><Ic.upload size={13} />Project / Source</button>
            {status?.normalize_allowed === true && <button className="btn primary" type="button" disabled={!!busy} onClick={normalize}><Ic.sparkle size={13} />Chuẩn hóa</button>}
          </div>
        </div>
      ) : (
        <>
          <div className="sp-summaryline">
            {review?.report?.integrity?.unit_count !== undefined && <span><b>{review.report.integrity.unit_count}</b> unit</span>}
            {skeleton?.statistics?.block_count !== undefined && <span><b>{skeleton.statistics.block_count}</b> block</span>}
            {review?.report?.integrity?.issue_count !== undefined && <span><b>{review.report.integrity.issue_count}</b> issue</span>}
            {status?.source?.format && <span><b>{String(status.source.format).toUpperCase()}</b> source</span>}
            {frozen && <span className="frozen"><Ic.lock size={11} /><b>Read-only tuyệt đối</b></span>}
          </div>

          <div className="sp-workgrid">
            <aside className="sp-unit-pane" aria-label="Danh sách unit">
              <div className="sp-pane-head"><span>Outline / unit</span><em>{units.length}</em></div>
              <div className="sp-unit-list" role="listbox" aria-label="Unit cấu trúc">
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
                      <span className="sp-unit-meta">{Array.isArray(unit.block_ids) ? `${unit.block_ids.length} block` : ""}{unitIssueCount ? ` · ${unitIssueCount} flag` : ""}</span>
                    </button>
                  );
                })}
              </div>
            </aside>

            <main className="sp-preview-pane">
              <div className="sp-pane-head"><span>Evidence của unit</span><em>{selectedUnit?.chapter_id || "—"}</em></div>
              {selectedUnit ? <>
                <div className="sp-preview-title"><div><span>Unit đang chọn</span><h2>{selectedUnit.title || selectedUnit.unit_id}</h2></div><span className="mono">{selectedUnit.unit_id}</span></div>

                <section className="sp-flat-section">
                  <div className="sp-section-title"><Ic.list size={13} /><b>Block boundary</b><span>{selectedBlockIds.length}</span></div>
                  <div className="sp-block-sequence">
                    {selectedBlockIds.map((blockId, index) => <div key={blockId}><span>{index + 1}</span><code>{blockId}</code>{index > 0 && canSplit ? <button type="button" className="btn sm" onClick={() => {
                      openSplit();
                      setModal(current => ({ ...current, atBlockId: blockId }));
                    }}>Tách tại đây</button> : null}</div>)}
                  </div>
                  {!selectedBlockIds.length && <p className="sp-muted">Review payload không công bố block IDs cho unit này.</p>}
                </section>

                {!!selectedIssues.length && <section className="sp-flat-section">
                  <div className="sp-section-title"><Ic.alert size={13} /><b>Issue / review flags</b><span>{selectedIssues.length}</span></div>
                  <div className="sp-issue-list">{selectedIssues.map(issue => <div key={issue.issue_id || `${issue.code}-${issue.target_id}`}>
                    <b className="mono">{issue.code}</b><span>{issue.scope}{issue.target_id ? ` · ${issue.target_id}` : ""}</span>
                    {Array.isArray(issue.evidence) && issue.evidence.length ? <ul>{issue.evidence.map(item => <li key={item}>{item}</li>)}</ul> : null}
                  </div>)}</div>
                </section>}

                {!!selectedCandidates.length && <section className="sp-flat-section">
                  <div className="sp-section-title"><Ic.search size={13} /><b>Candidate evidence</b><span>{selectedCandidates.length}</span></div>
                  <div className="sp-candidate-list">{selectedCandidates.map(candidate => <div key={candidate.candidate_id}>
                    <b>{candidate.title || candidate.candidate_kind || candidate.candidate_id}</b>
                    <span className="mono">{candidate.candidate_id}</span>
                    <em>{[candidate.source_signal, candidate.resolution_status].filter(Boolean).join(" · ")}</em>
                    {Array.isArray(candidate.signals) && candidate.signals.length ? <small>{candidate.signals.join(" · ")}</small> : null}
                  </div>)}</div>
                </section>}

                {!!navigation.length && <details className="sp-flat-details">
                  <summary><Ic.book size={13} /><b>TOC / navigation evidence</b><span>{navigation.length}</span></summary>
                  <div className="sp-navigation-list">{navigation.map(row => <div key={row.entry_id}>
                    <span style={{ paddingLeft: `${Math.max(0, Number(row.depth) || 0) * 12}px` }}>{row.title || row.entry_id}</span>
                    <em>{row.resolution_status || ""}</em>
                  </div>)}</div>
                </details>}
              </> : <div className="sp-empty-inline">Backend chưa trả unit để review.</div>}
            </main>

            <aside className="sp-detail-pane">
              <div className="sp-pane-head"><span>Chi tiết unit</span><em>{frozen || finalized ? "read-only" : "draft"}</em></div>
              {selectedUnit ? <div className="sp-detail-body">
                {(frozen || finalized) && <div className="sp-readonly-note"><Ic.lock size={12} /><span>{frozen ? "Run đầu tiên đã khóa revision này vĩnh viễn." : "Đã chốt cấu trúc; chỉ còn prepare/run."}</span></div>}
                <label className="sp-field"><span>Tiêu đề</span><input value={titleDraft} maxLength={500} disabled={!canUpdate || !!busy} onChange={event => setTitleDraft(event.target.value)} /></label>
                <label className="sp-field"><span>Classification</span><select value={classificationDraft} disabled={!canUpdate || !!busy} onChange={event => setClassificationDraft(event.target.value)}>
                  {!classificationDraft && <option value="">Backend chưa công bố</option>}
                  {SOURCE_PACKAGE_CLASSIFICATIONS.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
                </select></label>
                <button className="btn primary sp-save-unit" type="button" disabled={saveDisabled || !!busy} onClick={saveUnit}><Ic.save size={13} />Lưu revision unit</button>

                <div className="sp-detail-divider" />
                <label className="sp-field"><span>Parent unit</span><select value={parentDraft} disabled={!canHierarchy || !!busy} onChange={event => setParentDraft(event.target.value)}>
                  <option value="">Không có parent</option>
                  {parentChoices.map(unit => <option key={unit.unit_id} value={unit.unit_id}>{unit.title || unit.unit_id}</option>)}
                </select></label>
                <button className="btn" type="button" disabled={!canHierarchy || parentDraft === currentParentId || !!busy} onClick={saveHierarchy}><Ic.layers size={13} />Lưu hierarchy</button>

                <div className="sp-detail-divider" />
                <dl className="sp-unit-facts">
                  {selectedUnit.role !== undefined && <><dt>Role</dt><dd>{selectedUnit.role}</dd></>}
                  {selectedUnit.confidence !== undefined && <><dt>Confidence</dt><dd>{selectedUnit.confidence}</dd></>}
                  {selectedUnit.review_required !== undefined && <><dt>Review</dt><dd>{selectedUnit.review_required ? "required" : "not required"}</dd></>}
                  {selectedUnit.chapter_id && <><dt>Chapter</dt><dd className="mono">{selectedUnit.chapter_id}</dd></>}
                </dl>
              </div> : <div className="sp-empty-inline">Chọn một unit.</div>}
            </aside>
          </div>

          <details className="sp-technical" open={technicalOpen} onToggle={event => setTechnicalOpen(event.currentTarget.open)}>
            <summary><Ic.lock size={12} /><span>Chi tiết kỹ thuật</span><em>IDs và hashes từ backend</em></summary>
            <dl>
              {status?.state_sha256 && <><dt>state</dt><dd title={status.state_sha256}>{sourcePackageShortHash(status.state_sha256)}</dd></>}
              {review?.expected?.candidate_tree_sha256 && <><dt>candidate tree</dt><dd title={review.expected.candidate_tree_sha256}>{sourcePackageShortHash(review.expected.candidate_tree_sha256)}</dd></>}
              {review?.expected?.report_sha256 && <><dt>review report</dt><dd title={review.expected.report_sha256}>{sourcePackageShortHash(review.expected.report_sha256)}</dd></>}
              {review?.expected && Object.prototype.hasOwnProperty.call(review.expected, "hierarchy_sha256") && <><dt>hierarchy</dt><dd title={review.expected.hierarchy_sha256 || "null"}>{review.expected.hierarchy_sha256 ? sourcePackageShortHash(review.expected.hierarchy_sha256) : "null"}</dd></>}
              {status?.candidate?.candidate_id && <><dt>candidate</dt><dd>{status.candidate.candidate_id}</dd></>}
              {status?.run_start?.run_id && <><dt>frozen run</dt><dd>{status.run_start.run_id}</dd></>}
            </dl>
          </details>

          {frozen && !overlayReady && <div className="sp-export-gap"><Ic.alert size={13} /><div><b>Xuất tài liệu đang fail-closed</b><span>Thiếu producer/relay authoritative <code>canonical_translation_overlay_v1</code> trong App state/API. UI không dựng overlay từ preview, translation rows, report hay đường dẫn máy khách.</span></div></div>}
          {publication && <section className="sp-publication-result" aria-live="polite">
            <div><Ic.checkCircle size={16} /><span><b>{publication.reused ? "Publication đã tồn tại" : "Đã tạo publication"}</b><code>{publication.publication_id}</code></span></div>
            {artifacts.length ? <div className="sp-artifact-list">{artifacts.map(([kind, artifact]) => <div key={kind}><b>{kind}</b><span className="mono">{typeof artifact === "string" ? artifact : artifact?.path || artifact?.relative_path || "Backend không công bố path"}</span></div>)}</div> : null}
          </section>}
        </>
      )}

      {modal?.kind === "split" && <Modal title="Tách unit tại block boundary" icon={Ic.sliders} onClose={() => !busy && setModal(null)} actions={<>
        <button className="btn" type="button" disabled={!!busy} onClick={() => setModal(null)}>Hủy</button>
        <button className="btn primary" type="button" disabled={!!busy || !modal.atBlockId || !modal.leftTitle.trim() || !modal.rightTitle.trim()} onClick={applySplit}>Xác nhận tách</button>
      </>}>
        <div className="sp-modal-grid">
          <label className="sp-field"><span>Bắt đầu unit phải tại block</span><select value={modal.atBlockId} onChange={event => setModal({ ...modal, atBlockId: event.target.value })}>{selectedBlockIds.slice(1).map(blockId => <option key={blockId}>{blockId}</option>)}</select></label>
          <label className="sp-field"><span>Tiêu đề phần trái</span><input value={modal.leftTitle} onChange={event => setModal({ ...modal, leftTitle: event.target.value })} /></label>
          <label className="sp-field"><span>Phân loại phần trái</span><select value={modal.leftClassification} onChange={event => setModal({ ...modal, leftClassification: event.target.value })}>{SOURCE_PACKAGE_CLASSIFICATIONS.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
          <label className="sp-field"><span>Tiêu đề phần phải</span><input value={modal.rightTitle} onChange={event => setModal({ ...modal, rightTitle: event.target.value })} /></label>
          <label className="sp-field"><span>Phân loại phần phải</span><select value={modal.rightClassification} onChange={event => setModal({ ...modal, rightClassification: event.target.value })}>{SOURCE_PACKAGE_CLASSIFICATIONS.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
        </div>
      </Modal>}

      {modal?.kind === "merge" && <Modal title="Gộp hai unit liền kề" icon={Ic.layers} onClose={() => !busy && setModal(null)} actions={<>
        <button className="btn" type="button" disabled={!!busy} onClick={() => setModal(null)}>Hủy</button>
        <button className="btn primary" type="button" disabled={!!busy || !modal.title.trim()} onClick={applyMerge}>Xác nhận gộp</button>
      </>}>
        <p><span className="mono">{selectedUnit?.unit_id}</span> + <span className="mono">{nextUnit?.unit_id}</span></p>
        <div className="sp-modal-grid">
          <label className="sp-field"><span>Tiêu đề unit mới</span><input value={modal.title} onChange={event => setModal({ ...modal, title: event.target.value })} /></label>
          <label className="sp-field"><span>Classification</span><select value={modal.classification} onChange={event => setModal({ ...modal, classification: event.target.value })}>{SOURCE_PACKAGE_CLASSIFICATIONS.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
        </div>
      </Modal>}

      {modal?.kind === "finalize" && <Modal title="Chốt cấu trúc trước run" icon={Ic.checkCircle} onClose={() => !busy && setModal(null)} actions={<>
        <button className="btn" type="button" disabled={!!busy} onClick={() => setModal(null)}>Xem lại</button>
        <button className="btn primary" type="button" disabled={!!busy} onClick={finalize}>Chốt cấu trúc</button>
      </>}>
        <p>Backend sẽ tạo finalization revision từ đúng state/tree/report/hierarchy hashes của review hiện tại.</p>
        <p className="muted">Sau khi chốt, UI chỉ cho chuẩn bị runtime và chạy pipeline; không chỉnh cấu trúc trong revision này.</p>
      </Modal>}

      {modal?.kind === "publication" && <Modal title="Xuất tài liệu" icon={Ic.upload} onClose={() => !busy && setModal(null)} actions={<>
        <button className="btn" type="button" disabled={!!busy} onClick={() => setModal(null)}>Hủy</button>
        <button className="btn primary" type="button" disabled={!!busy || !overlayReady} onClick={publish}>Xuất HTML / Markdown</button>
      </>}>
        <p>Body gửi nguyên object <code>canonical_translation_overlay_v1</code> authoritative đang có trong App state. UI không sửa, thêm row hoặc dựng lại overlay.</p>
        <p className="muted">Publication tạo output mới và không ghi đè source package.</p>
      </Modal>}
    </section>
  );
}

window.SourcePackageWorkspace = SourcePackageWorkspace;
