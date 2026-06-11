/* ===== PROJECT / SOURCE SCREEN: project selection, upload, metadata, extract ===== */

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
  onBuildNormalizeCandidate,
  onLoadNormalizeAgentPlan,
  onImportNormalizePlan,
  onApplyNormalizePlan,
  onBuildAnnotationInput,
  onLoadAnnotationAgentCandidate,
  onResolveAnnotationCandidate,
  onApplyAnnotationCandidate,
}) {
  const [file, setFile] = React.useState(null);
  const [confirmOverwrite, setConfirmOverwrite] = React.useState(false);
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [confirmNormalizeApply, setConfirmNormalizeApply] = React.useState(false);
  const [newDocId, setNewDocId] = React.useState("");
  const [projectNote, setProjectNote] = React.useState("");
  const [normalizeCandidate, setNormalizeCandidate] = React.useState(null);
  const [normalizePlanText, setNormalizePlanText] = React.useState("");
  const [normalizeResult, setNormalizeResult] = React.useState(null);
  const [normalizeBusy, setNormalizeBusy] = React.useState("");
  const [normalizeReviewed, setNormalizeReviewed] = React.useState(false);
  const [annotationChapterId, setAnnotationChapterId] = React.useState("");
  const [annotationInput, setAnnotationInput] = React.useState(null);
  const [annotationCandidateText, setAnnotationCandidateText] = React.useState("");
  const [annotationResolved, setAnnotationResolved] = React.useState(null);
  const [annotationBusy, setAnnotationBusy] = React.useState("");
  const meta = docInfo.metadata || {};
  const prov = docInfo.provenance || {};
  const extracted = blocks.length > 0;
  const selectedProject = projects.find(p => p.doc_id === activeDocId);
  const protectedProject = activeDocId === "gold_demo_01";
  const normalizerState = selectedProject?.normalizer || {};
  const normalizerHistory = Array.isArray(normalizerState.history)
    ? normalizerState.history.slice().reverse().slice(0, 6)
    : [];

  function normalizerStateLabel() {
    if (normalizerState.applied) return "normalized";
    if (normalizerState.normalized_preview_available) return "preview ready";
    if (normalizerState.agent_plan_available) return "agent plan ready";
    if (normalizerState.candidate_built) return "candidate built";
    return "not normalized";
  }

  function normalizerStateTone() {
    if (normalizerState.low_confidence || normalizerState.needs_human_check) return "warn";
    if (normalizerState.applied) return "done";
    if (normalizerState.normalized_preview_available || normalizerState.agent_plan_available || normalizerState.candidate_built) return "info";
    return "";
  }

  function normalizerEventLabel(event) {
    const labels = {
      candidate_built: "Built candidate",
      plan_imported: "Imported plan",
      applied: "Applied normalized structure",
    };
    return labels[event] || event || "Normalizer event";
  }

  React.useEffect(() => {
    setProjectNote(selectedProject?.note || "");
    setNormalizeCandidate(null);
    setNormalizePlanText("");
    setNormalizeResult(null);
    setNormalizeBusy("");
    setNormalizeReviewed(false);
    setAnnotationInput(null);
    setAnnotationCandidateText("");
    setAnnotationResolved(null);
    setAnnotationBusy("");
  }, [selectedProject?.doc_id, selectedProject?.note]);

  React.useEffect(() => {
    if (!chapters.length) {
      setAnnotationChapterId("");
      return;
    }
    if (!annotationChapterId || !chapters.some(ch => ch.chapter_id === annotationChapterId)) {
      setAnnotationChapterId(chapters[0].chapter_id);
    }
  }, [chapters, annotationChapterId]);

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

  function normalizeWarnings(preview) {
    if (!preview) return [];
    const warnings = [];
    if (preview.low_confidence) warnings.push("low_confidence");
    if (preview.needs_human_check) warnings.push("needs_human_check");
    (preview.flags || []).forEach(flag => warnings.push(flag.reason || flag.flag || "flag"));
    return [...new Set(warnings)];
  }

  async function buildCandidate() {
    if (!activeDocId || !onBuildNormalizeCandidate) return;
    setNormalizeBusy("candidate");
    const candidate = await onBuildNormalizeCandidate();
    if (candidate) {
      setNormalizeCandidate(candidate);
      setNormalizeResult(null);
      setNormalizeReviewed(false);
    }
    setNormalizeBusy("");
  }

  function candidateJson() {
    return normalizeCandidate ? JSON.stringify(normalizeCandidate, null, 2) : "";
  }

  function normalizerPaths() {
    if (!normalizeCandidate) return {};
    const docId = normalizeCandidate.doc_id || activeDocId || "";
    const fallbackRoot = docId ? `ailab_projects/${docId}` : "";
    return {
      ...(normalizeCandidate.paths || {}),
      project_root: normalizeCandidate.paths?.project_root || fallbackRoot,
      candidate_parts: normalizeCandidate.paths?.candidate_parts || (fallbackRoot ? `${fallbackRoot}/working/normalized/candidate_parts.json` : ""),
      agent_structure_plan: normalizeCandidate.paths?.agent_structure_plan || (fallbackRoot ? `${fallbackRoot}/working/normalized/agent_structure_plan.json` : ""),
    };
  }

  function candidatePath() {
    return normalizerPaths().candidate_parts || "";
  }

  function normalizerPrompt() {
    if (!normalizeCandidate) return "";
    const paths = normalizerPaths();
    return [
      "Bạn là Source Structure Normalizer Agent cho dự án AI-LAB.",
      "",
      "Mục tiêu: từ candidate_parts JSON của một nguồn TXT/EPUB, sinh StructurePlan JSON để chuẩn hóa cấu trúc chương/đoạn trước khi đưa vào extractor.",
      "",
      "Trước khi làm, hãy đọc:",
      "1. skills/source-structure-normalizer/SKILL.md",
      "2. skills/source-structure-normalizer/references/STRUCTURE_PLAN_CONTRACT.md",
      "3. Nếu cần ví dụ: skills/source-structure-normalizer/references/CANTERVILLE_EXAMPLE.md",
      "",
      "Nguồn đang xử lý:",
      `- doc_id: ${normalizeCandidate.doc_id || activeDocId || ""}`,
      `- source_format: ${normalizeCandidate.source_format || ""}`,
      `- source_fingerprint: ${normalizeCandidate.source_fingerprint || ""}`,
      `- project folder: ${paths.project_root || ""}`,
      `- candidate_parts: ${paths.candidate_parts || ""}`,
      `- agent_structure_plan: ${paths.agent_structure_plan || ""}`,
      "",
      "Yêu cầu:",
      "- Chỉ dùng đúng candidate_parts đã đưa.",
      "- Echo chính xác `doc_id` và `source_fingerprint`.",
      "- Không dùng plan của TXT cho EPUB hoặc ngược lại.",
      "- Không dịch.",
      "- Không annotate glossary/entity/summary/reference/discourse/block_type.",
      "- Không rewrite, paraphrase, sửa chữ, hoặc tự ý xóa body text.",
      "- Chỉ quyết định bằng `part_index` / `spine_index`.",
      "- Drop front/back matter rõ ràng: title page, author/illustrator credit, TOC lặp, imprint, copyright, Gutenberg license, colophon, publisher ads.",
      "- Chọn chapter heading thật nếu có.",
      "- Không dùng book title, author line, cover, title page, hoặc front matter làm chapter heading.",
      "- Nếu nguồn là truyện một phần không có heading chương thật: để `chapter_headings: []`, đặt confidence thấp hơn và flag `needs_human_check`; backend sẽ fallback thành một chương.",
      "- Nếu title page dính chung body text trong cùng part: không drop part đó, giữ lại và flag.",
      "- Merge chỉ khi các part liền nhau rõ ràng là cùng một đoạn bị tách cơ học.",
      "- Nếu không chắc, giữ part và flag `needs_human_check`, không đoán bừa.",
      "",
      "Output:",
      "- Nếu có quyền ghi file, hãy ghi đúng một JSON object vào `agent_structure_plan` ở trên.",
      "- Nếu không thể ghi file, trả về đúng một JSON object theo StructurePlan contract.",
      "- JSON phải theo StructurePlan contract.",
      "- Không bọc markdown code block.",
      "- Không giải thích ngoài JSON.",
      "",
      "Self-check trước khi trả:",
      "- `source_fingerprint` khớp candidate.",
      "- Mọi `part_index` / `spine_index` đều tồn tại.",
      "- Không part nào vừa drop vừa là heading.",
      "- Heading theo đúng thứ tự đọc.",
      "- Không trộn candidate của TXT và EPUB.",
      "- Body text chính không bị drop.",
      "- `confidence` phản ánh độ chắc chắn.",
    ].join("\n");
  }

  async function copyCandidate() {
    const text = candidateJson();
    if (!text) return;
    await navigator.clipboard.writeText(text);
  }

  async function copyCandidatePath() {
    const text = candidatePath();
    if (!text) return;
    await navigator.clipboard.writeText(text);
  }

  async function copyNormalizerPrompt() {
    const text = normalizerPrompt();
    if (!text) return;
    await navigator.clipboard.writeText(text);
  }

  function downloadCandidate() {
    const text = candidateJson();
    if (!text) return;
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${activeDocId || "project"}_candidate_parts.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  async function loadPlanFile(event) {
    const selected = event.target.files?.[0];
    if (!selected) return;
    const text = await selected.text();
    setNormalizePlanText(text);
    setNormalizeResult(null);
    setNormalizeReviewed(false);
    event.target.value = "";
  }

  async function loadAgentPlanFromWorking() {
    if (!onLoadNormalizeAgentPlan) return;
    setNormalizeBusy("load-agent-plan");
    const result = await onLoadNormalizeAgentPlan();
    if (result?.plan) {
      setNormalizePlanText(JSON.stringify(result.plan, null, 2));
      setNormalizeResult(null);
      setNormalizeReviewed(false);
    }
    setNormalizeBusy("");
  }

  async function validatePlan() {
    if (!normalizePlanText.trim() || !onImportNormalizePlan) return;
    let plan;
    try {
      plan = JSON.parse(normalizePlanText);
    } catch (err) {
      setNormalizeResult({
        ok: false,
        errors: [{ location: "StructurePlan JSON", message: err.message || String(err), severity: "error" }],
        warnings: [],
      });
      return;
    }
    setNormalizeBusy("plan");
    const result = await onImportNormalizePlan(plan);
    setNormalizeResult(result);
    setNormalizeReviewed(false);
    setNormalizeBusy("");
  }

  function startNormalizeApply() {
    if (extracted) setConfirmNormalizeApply(true);
    else applyNormalized(false);
  }

  async function applyNormalized(overwrite) {
    if (!onApplyNormalizePlan) return;
    setNormalizeBusy("apply");
    await onApplyNormalizePlan({ overwrite: !!overwrite });
    setConfirmNormalizeApply(false);
    setNormalizeBusy("");
  }

  function annotationPaths() {
    if (!annotationInput) return {};
    const docId = annotationInput.doc_id || activeDocId || "";
    const safeChapter = (annotationInput.chapter_id || annotationChapterId || "").replace(/[^A-Za-z0-9_-]/g, "_");
    const root = annotationInput.paths?.project_root || `ailab_projects/${docId}`;
    return {
      project_root: root,
      input: annotationInput.paths?.input || `${root}/working/annotation/${safeChapter}_input.json`,
      candidate: annotationInput.paths?.candidate || `${root}/working/annotation/${safeChapter}_candidate.json`,
      resolved: annotationInput.paths?.resolved || `${root}/working/annotation/${safeChapter}_resolved.json`,
    };
  }

  function annotationPrompt() {
    if (!annotationInput) return "";
    const paths = annotationPaths();
    return [
      "You are the AI-LAB Dataset Annotation Drafter Agent.",
      "",
      "Goal: read one chapter annotation input JSON and return one AnnotationCandidate JSON.",
      "",
      "Read first:",
      "1. skills/dataset-annotation-drafter/SKILL.md",
      "2. skills/dataset-annotation-drafter/references/ANNOTATION_CANDIDATE_CONTRACT.md",
      "3. skills/dataset-annotation-drafter/references/ENTITY_GLOSSARY_DECISION_RULES.md",
      "4. skills/dataset-annotation-drafter/references/LINKAGE_RULES.md",
      "",
      "Current source:",
      `- doc_id: ${annotationInput.doc_id || activeDocId || ""}`,
      `- chapter_id: ${annotationInput.chapter_id || annotationChapterId || ""}`,
      `- project folder: ${paths.project_root || ""}`,
      `- annotation_input: ${paths.input || ""}`,
      `- candidate_output_suggested: ${paths.candidate || ""}`,
      "",
      "Hard rules:",
      "- Use only the provided annotation_input JSON.",
      "- Echo doc_id and chapter_id exactly.",
      "- Do not emit spans, start, or end offsets.",
      "- Mention surfaces must be verbatim substrings of clean_text.",
      "- Use left_context/right_context to disambiguate duplicate surfaces.",
      "- Use entity_key consistently; discourse and characters_present_refs must reference entity_key or existing_entity_id.",
      "- Read known_relations if present. Reuse existing_relation_id when a pair already has a relation; otherwise draft relation_candidates only when clear person-person relationship evidence exists.",
      "- Relation candidates must include source_ref, target_ref, relation_type, suggested_address_policy, and evidence. Do not create all-pairs relations.",
      "- Entity has no status. Glossary status is assigned by backend, not by you.",
      "- Do not translate full blocks or create reference_vi/draft_vi.",
      "- Do not annotate references. Do not rewrite source text.",
      "- Avoid dual-tagging a proper-name/place surface as both entity and glossary.",
      "- characters_present_refs should contain person entities only.",
      "",
      "Output:",
      "- If you can write files, write exactly one JSON object to candidate_output_suggested.",
      "- If you cannot write files, return exactly one JSON object.",
      "- No markdown fence and no explanation outside JSON.",
    ].join("\n");
  }

  async function buildAnnotationInput() {
    if (!annotationChapterId || !onBuildAnnotationInput) return;
    setAnnotationBusy("input");
    const result = await onBuildAnnotationInput(annotationChapterId);
    if (result) {
      setAnnotationInput(result);
      setAnnotationResolved(null);
      setAnnotationCandidateText("");
    }
    setAnnotationBusy("");
  }

  async function copyAnnotationInput() {
    if (!annotationInput) return;
    await navigator.clipboard.writeText(JSON.stringify(annotationInput, null, 2));
  }

  async function copyAnnotationPrompt() {
    const text = annotationPrompt();
    if (!text) return;
    await navigator.clipboard.writeText(text);
  }

  async function loadAnnotationCandidateFile(event) {
    const selected = event.target.files?.[0];
    if (!selected) return;
    const text = await selected.text();
    setAnnotationCandidateText(text);
    setAnnotationResolved(null);
    event.target.value = "";
  }

  async function loadAnnotationAgentCandidate() {
    if (!annotationChapterId || !onLoadAnnotationAgentCandidate) return;
    setAnnotationBusy("load-agent-candidate");
    const result = await onLoadAnnotationAgentCandidate(annotationChapterId);
    if (result?.candidate) {
      setAnnotationCandidateText(JSON.stringify(result.candidate, null, 2));
      setAnnotationResolved(null);
    }
    setAnnotationBusy("");
  }

  async function resolveAnnotationCandidate() {
    if (!annotationCandidateText.trim() || !onResolveAnnotationCandidate) return;
    let candidate;
    try {
      candidate = JSON.parse(annotationCandidateText);
    } catch (err) {
      setAnnotationResolved({
        ok: false,
        errors: [{ message: err.message || String(err) }],
      });
      return;
    }
    setAnnotationBusy("resolve");
    const result = await onResolveAnnotationCandidate(candidate);
    setAnnotationResolved(result);
    setAnnotationBusy("");
  }

  async function applyAnnotationCandidate() {
    if (!annotationChapterId || !onApplyAnnotationCandidate) return;
    setAnnotationBusy("apply");
    await onApplyAnnotationCandidate(annotationChapterId);
    setAnnotationBusy("");
  }

  function annotationStatusCounts(items) {
    const rows = Array.isArray(items) ? items : [];
    return {
      ok: rows.filter(item => item.status === "ok").length,
      review: rows.filter(item => item.status && item.status !== "ok").length,
      total: rows.length,
    };
  }

  const normalizePreview = normalizeResult?.preview || null;
  const normalizeInvalid = normalizeResult && normalizeResult.ok === false;
  const reviewRequired = normalizeWarnings(normalizePreview).length > 0;
  const canApplyNormalize = !!normalizePreview && (!reviewRequired || normalizeReviewed);

  return (
    <div className="project-screen">
      <div className="project-topbar">
        <div className="tb-left">
          <span className="tb-app"><span className="tb-logo">▧</span>AILAB <span className="tb-app-sub">Dataset Tool</span></span>
          <span className="tb-sep" />
          <span className="tb-doc"><Ic.folder size={13} className="faint" /><span className="mono">{activeDocId || "no_project"}</span></span>
        </div>
        <div className="tb-right">
          <button className="btn" onClick={onBack} disabled={!extracted}><Ic.arrowRight size={13} style={{ transform: "rotate(180deg)" }} />Back to workspace</button>
        </div>
      </div>

      <div className="project-wrap">
        <div className="project-headline">
          <div>
            <div className="project-kicker">Project / Source</div>
            <h1>Prepare source metadata before annotation</h1>
            <p>Choose a local project, upload a TXT/EPUB source, record source metadata, then run extraction. Re-extracting requires confirmation because it can overwrite the current document draft.</p>
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
                  {projects.map(p => <option key={p.doc_id} value={p.doc_id}>{p.doc_id} · {p.status}</option>)}
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
                    disabled={!activeDocId}
                    rows={4}
                    placeholder="Short note for task owner, source choice, known issues..."
                    onChange={e => setProjectNote(e.target.value)}
                  />
                </FormField>
              </div>
              <div className="project-admin-actions">
                <button className="btn" disabled={!activeDocId} onClick={saveProjectSettings}>
                  <Ic.save size={13} />Save project info
                </button>
                <button className="btn danger" disabled={!activeDocId || protectedProject} onClick={() => setConfirmDelete(true)}>
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
              <FormField label="title"><input value={meta.title || ""} onChange={e => patchMetadata({ title: e.target.value })} /></FormField>
              <FormField label="author"><input value={meta.author || ""} onChange={e => patchMetadata({ author: e.target.value })} /></FormField>
              <FormField label="domain"><input value={meta.domain || ""} onChange={e => patchMetadata({ domain: e.target.value })} /></FormField>
              <FormField label="genre"><input value={meta.genre || ""} onChange={e => patchMetadata({ genre: e.target.value })} /></FormField>
              <FormField label="source_format">
                <select value={meta.source_format || "txt"} onChange={e => patchMetadata({ source_format: e.target.value })}>
                  <option value="txt">txt</option>
                  <option value="epub">epub</option>
                  <option value="html">html</option>
                  <option value="pdf">pdf (not extractable in MVP)</option>
                </select>
              </FormField>
              <FormField label="license"><input value={meta.license || ""} onChange={e => patchMetadata({ license: e.target.value })} /></FormField>
              <FormField label="source_url"><input value={meta.source_url || ""} onChange={e => patchMetadata({ source_url: e.target.value })} /></FormField>
              <FormField label="contamination_risk">
                <select value={meta.contamination_risk || ""} onChange={e => patchMetadata({ contamination_risk: e.target.value })}>
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
                <div className="source-drop-title">TXT / EPUB source</div>
                <div className="source-drop-sub">Backend rejects PDF/OCR/layout extraction in this MVP.</div>
              </div>
              <input type="file" accept=".txt,.epub" onChange={e => setFile(e.target.files?.[0] || null)} />
            </div>
            <div className="extract-actions">
              <button className="btn" onClick={() => setFile(null)}>Clear</button>
              <button className="btn" disabled={!file || !activeDocId} onClick={uploadSelected}><Ic.upload size={13} />Upload source</button>
              <button className="btn primary" disabled={!activeDocId} onClick={startExtract}><Ic.play size={13} />Extract</button>
            </div>
            <div className="extract-note">
              <Ic.alert size={12} />
              <span>Extract only lives here, not in the annotation workspace. Re-extracting can discard reviewed edits.</span>
            </div>
          </section>

          <section className="project-panel normalizer-panel">
            <div className="panel-title"><Ic.layers size={14} />Structure normalizer</div>
            <p className="normalizer-intro">
              Optional pre-extract step for sources with weak chapter structure. The tool only validates and applies an imported StructurePlan; it does not call an LLM.
            </p>
            <div className="normalizer-status-card">
              <div className="normalizer-status-main">
                <span className={"normalizer-state-pill " + normalizerStateTone()}>{normalizerStateLabel()}</span>
                {normalizerState.source_format && <span><b>{normalizerState.source_format}</b> source</span>}
                {normalizerState.source_fingerprint && <span className="mono">{normalizerState.source_fingerprint}</span>}
                {normalizerState.applied && <span><b>{normalizerState.chapters || 0}</b> ch · <b>{normalizerState.blocks || 0}</b> blocks</span>}
                {normalizerState.last_event_at && <span className="faint">{normalizerState.last_event_at}</span>}
              </div>
              {normalizerHistory.length > 0 && (
                <details className="normalizer-history">
                  <summary>Normalization history</summary>
                  {normalizerHistory.map((event, idx) => (
                    <div className="normalizer-history-row" key={`${event.ts || "event"}-${idx}`}>
                      <span className="mono">{event.ts || ""}</span>
                      <span>{normalizerEventLabel(event.event)}</span>
                      <span className="faint">
                        {event.chapters ? `${event.chapters} ch` : ""}
                        {event.blocks ? ` · ${event.blocks} blocks` : ""}
                        {event.parts ? `${event.parts} parts` : ""}
                      </span>
                    </div>
                  ))}
                </details>
              )}
            </div>
            <div className="normalizer-steps">
              <div className="normalizer-step">
                <div className="normalizer-step-head">
                  <span className="step-num">1</span>
                  <div>
                    <div className="step-title">Build candidate parts</div>
                    <div className="step-sub">Use this JSON with the source-structure-normalizer skill.</div>
                  </div>
                </div>
                <div className="normalizer-actions">
                  <button className="btn" disabled={!activeDocId || normalizeBusy === "candidate"} onClick={buildCandidate}>
                    <Ic.layers size={13} />{normalizeBusy === "candidate" ? "Building..." : "Build candidate"}
                  </button>
                  <button className="btn" disabled={!normalizeCandidate} onClick={copyCandidate}><Ic.doc size={13} />Copy JSON</button>
                  <button className="btn" disabled={!candidatePath()} onClick={copyCandidatePath}><Ic.folder size={13} />Copy path</button>
                  <button className="btn" disabled={!normalizeCandidate} onClick={copyNormalizerPrompt}><Ic.sparkle size={13} />Copy prompt</button>
                  <button className="btn" disabled={!normalizeCandidate} onClick={downloadCandidate}><Ic.upload size={13} />Download JSON</button>
                </div>
                {normalizeCandidate && (
                  <div className="normalizer-stats">
                    <span><b>{normalizeCandidate.parts?.length || 0}</b> parts</span>
                    <span><b>{normalizeCandidate.source_format}</b> format</span>
                    <span className="mono">{normalizeCandidate.source_fingerprint}</span>
                  </div>
                )}
              </div>

              <div className="normalizer-step">
                <div className="normalizer-step-head">
                  <span className="step-num">2</span>
                  <div>
                    <div className="step-title">Import StructurePlan</div>
                    <div className="step-sub">Paste/upload JSON, or load the agent-written plan from working/normalized.</div>
                  </div>
                </div>
                <textarea
                  className="json-textarea"
                  value={normalizePlanText}
                  placeholder='{"doc_id":"...","source_fingerprint":"...","chapter_headings":[...]}'
                  rows={7}
                  onChange={e => { setNormalizePlanText(e.target.value); setNormalizeResult(null); setNormalizeReviewed(false); }}
                />
                <div className="normalizer-actions">
                  <button className="btn" disabled={!activeDocId || normalizeBusy === "load-agent-plan"} onClick={loadAgentPlanFromWorking}>
                    <Ic.sparkle size={13} />{normalizeBusy === "load-agent-plan" ? "Loading..." : "Load agent plan"}
                  </button>
                  <label className="btn">
                    <Ic.file size={13} />Load plan file
                    <input className="hidden-file" type="file" accept=".json,application/json" onChange={loadPlanFile} />
                  </label>
                  <button className="btn primary" disabled={!normalizePlanText.trim() || normalizeBusy === "plan"} onClick={validatePlan}>
                    <Ic.checkCircle size={13} />{normalizeBusy === "plan" ? "Validating..." : "Validate plan"}
                  </button>
                </div>
              </div>

              {(normalizeInvalid || normalizePreview) && (
                <div className={"normalizer-preview " + (normalizeInvalid ? "bad" : "")}>
                  <div className="normalizer-preview-head">
                    <span>{normalizeInvalid ? "Plan blocked" : "Preview"}</span>
                    {normalizePreview && <span className="mono">{normalizePreview.body_coverage || "body n/a"}</span>}
                  </div>
                  {normalizeInvalid && (
                    <div className="normalizer-errors">
                      {(normalizeResult.errors || normalizeResult.validation?.errors || []).map((err, idx) => (
                        <div key={idx} className="normalizer-error"><Ic.xCircle size={12} />{err.location ? `${err.location}: ` : ""}{err.message}</div>
                      ))}
                    </div>
                  )}
                  {normalizePreview && (
                    <>
                      <div className="normalizer-summary-grid">
                        <span><b>{normalizePreview.chapters?.length || 0}</b> chapters</span>
                        <span><b>{normalizePreview.dropped?.length || 0}</b> dropped</span>
                        <span><b>{normalizePreview.drop_fraction}</b> drop fraction</span>
                        <span><b>{normalizePreview.content_invariance_ok ? "ok" : "check"}</b> content</span>
                      </div>
                      <div className="normalizer-chapters">
                        {(normalizePreview.chapters || []).slice(0, 12).map(ch => (
                          <div key={ch.order_index} className="normalizer-chapter">
                            <span className="mono">{ch.order_index}</span>
                            <span>{ch.title}</span>
                            <span className="faint">{ch.n_blocks} blocks</span>
                          </div>
                        ))}
                        {(normalizePreview.chapters || []).length > 12 && <div className="muted">+ {(normalizePreview.chapters || []).length - 12} more chapter(s)</div>}
                      </div>
                      {(normalizePreview.dropped || []).length > 0 && (
                        <details className="normalizer-dropped">
                          <summary>Dropped parts</summary>
                          {(normalizePreview.dropped || []).slice(0, 8).map(item => (
                            <div key={item.part_index} className="normalizer-drop">
                              <span className="mono">#{item.part_index}</span>
                              <span>{item.reason}</span>
                              <span className="faint">{item.snippet}</span>
                            </div>
                          ))}
                        </details>
                      )}
                      {reviewRequired && (
                        <label className="normalizer-review">
                          <input type="checkbox" checked={normalizeReviewed} onChange={e => setNormalizeReviewed(e.target.checked)} />
                          <span>I reviewed low-confidence flags: {normalizeWarnings(normalizePreview).join(", ")}</span>
                        </label>
                      )}
                      <div className="normalizer-actions end">
                        <button className="btn primary" disabled={!canApplyNormalize || normalizeBusy === "apply"} onClick={startNormalizeApply}>
                          <Ic.play size={13} />{normalizeBusy === "apply" ? "Applying..." : "Approve & extract normalized"}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          </section>

          <section className="project-panel normalizer-panel">
            <div className="panel-title"><Ic.sparkle size={14} />Annotation drafter</div>
            <p className="normalizer-intro">
              Optional AI-assist step for one chapter. The backend only builds input, resolves spans, and applies resolved candidates; it does not call an LLM.
            </p>
            <div className="normalizer-steps">
              <div className="normalizer-step">
                <div className="normalizer-step-head">
                  <span className="step-num">1</span>
                  <div>
                    <div className="step-title">Build annotation input</div>
                    <div className="step-sub">Send this chapter JSON to the dataset-annotation-drafter skill.</div>
                  </div>
                </div>
                <FormField label="chapter">
                  <select value={annotationChapterId} disabled={!chapters.length} onChange={e => setAnnotationChapterId(e.target.value)}>
                    {chapters.map(ch => <option key={ch.chapter_id} value={ch.chapter_id}>{ch.title || ch.chapter_id} · {ch.block_count || 0} blocks</option>)}
                  </select>
                </FormField>
                <div className="normalizer-actions">
                  <button className="btn" disabled={!annotationChapterId || annotationBusy === "input"} onClick={buildAnnotationInput}>
                    <Ic.layers size={13} />{annotationBusy === "input" ? "Building..." : "Build input"}
                  </button>
                  <button className="btn" disabled={!annotationInput} onClick={copyAnnotationInput}><Ic.doc size={13} />Copy input</button>
                  <button className="btn" disabled={!annotationInput} onClick={copyAnnotationPrompt}><Ic.sparkle size={13} />Copy prompt</button>
                </div>
                {annotationInput && (
                  <div className="normalizer-stats">
                    <span><b>{annotationInput.blocks?.length || 0}</b> blocks</span>
                    <span><b>{annotationInput.known_entities?.length || 0}</b> known entities</span>
                    <span><b>{annotationInput.known_terms?.length || 0}</b> known terms</span>
                    <span><b>{annotationInput.known_relations?.length || 0}</b> known relations</span>
                  </div>
                )}
              </div>

              <div className="normalizer-step">
                <div className="normalizer-step-head">
                  <span className="step-num">2</span>
                  <div>
                    <div className="step-title">Resolve AnnotationCandidate</div>
                    <div className="step-sub">Paste/upload the JSON returned by the skill. Ambiguous or conflicting items will not auto-apply.</div>
                  </div>
                </div>
                <textarea
                  className="json-textarea"
                  value={annotationCandidateText}
                  placeholder='{"doc_id":"...","chapter_id":"...","entity_candidates":[...],"glossary_candidates":[...],"relation_candidates":[...],"discourse_candidates":[...],"summary_candidate":{...}}'
                  rows={7}
                  onChange={e => { setAnnotationCandidateText(e.target.value); setAnnotationResolved(null); }}
                />
                <div className="normalizer-actions">
                  <button className="btn" disabled={!annotationChapterId || annotationBusy === "load-agent-candidate"} onClick={loadAnnotationAgentCandidate}>
                    <Ic.sparkle size={13} />{annotationBusy === "load-agent-candidate" ? "Loading..." : "Load agent candidate"}
                  </button>
                  <label className="btn">
                    <Ic.file size={13} />Load candidate file
                    <input className="hidden-file" type="file" accept=".json,application/json" onChange={loadAnnotationCandidateFile} />
                  </label>
                  <button className="btn primary" disabled={!annotationCandidateText.trim() || annotationBusy === "resolve"} onClick={resolveAnnotationCandidate}>
                    <Ic.checkCircle size={13} />{annotationBusy === "resolve" ? "Resolving..." : "Resolve candidate"}
                  </button>
                </div>
              </div>

              {annotationResolved && (
                <div className={"normalizer-preview " + (annotationResolved.ok === false ? "bad" : "")}>
                  <div className="normalizer-preview-head">
                    <span>{annotationResolved.ok === false ? "Candidate blocked" : "Resolved preview"}</span>
                    {annotationResolved.meta && <span className="mono">{annotationResolved.meta.block_text_hash}</span>}
                  </div>
                  {annotationResolved.ok === false ? (
                    <div className="normalizer-errors">
                      {(annotationResolved.errors || []).map((err, idx) => (
                        <div key={idx} className="normalizer-error"><Ic.xCircle size={12} />{err.message || String(err)}</div>
                      ))}
                    </div>
                  ) : (
                    <>
                      <div className="normalizer-summary-grid">
                        {(() => {
                          const e = annotationStatusCounts(annotationResolved.entities);
                          const g = annotationStatusCounts(annotationResolved.glossary);
                          const d = annotationStatusCounts(annotationResolved.discourse);
                          const s = annotationResolved.summary ? annotationStatusCounts([annotationResolved.summary]) : { ok: 0, review: 0, total: 0 };
                          return (
                            <>
                              <span><b>{e.ok}/{e.total}</b> entities ok</span>
                              <span><b>{g.ok}/{g.total}</b> terms ok</span>
                              <span><b>{d.ok}/{d.total}</b> discourse ok</span>
                              <span><b>{s.ok}/{s.total}</b> summary ok</span>
                            </>
                          );
                        })()}
                      </div>
                      <div className="normalizer-chapters">
                        {(annotationResolved.entities || []).slice(0, 8).map(item => (
                          <div key={item.entity_id} className="normalizer-chapter">
                            <span className="mono">{item.entity_id}</span>
                            <span>{item.canonical_source}</span>
                            <span className={item.status === "ok" ? "faint" : "bad"}>{item.status}</span>
                          </div>
                        ))}
                        {(annotationResolved.glossary || []).slice(0, 8).map(item => (
                          <div key={item.term_id} className="normalizer-chapter">
                            <span className="mono">{item.term_id}</span>
                            <span>{item.source_term}</span>
                            <span className={item.status === "ok" ? "faint" : "bad"}>{item.status}</span>
                          </div>
                        ))}
                      </div>
                      <div className="normalizer-actions end">
                        <button className="btn primary" disabled={annotationBusy === "apply"} onClick={applyAnnotationCandidate}>
                          <Ic.play size={13} />{annotationBusy === "apply" ? "Applying..." : "Apply resolved OK items"}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )}
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

      {confirmNormalizeApply && (
        <Modal title="Apply normalized structure" icon={Ic.alert} tone="bad" onClose={() => setConfirmNormalizeApply(false)}
          actions={<>
            <button className="btn" onClick={() => setConfirmNormalizeApply(false)}>Cancel</button>
            <button className="btn primary" onClick={() => applyNormalized(true)}>Overwrite document</button>
          </>}>
          <p>Applying this normalized structure can overwrite <span className="mono">document.json</span> and reset review state for this project.</p>
          <p className="muted">Use this only before annotation, or after exporting any work you need to keep.</p>
        </Modal>
      )}
    </div>
  );
}

window.ProjectSourceScreen = ProjectSourceScreen;
