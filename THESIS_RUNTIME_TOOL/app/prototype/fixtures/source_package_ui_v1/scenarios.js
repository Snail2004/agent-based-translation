(function () {
  window.__SOURCE_PACKAGE_UI_DEV__ = true;

  const realApi = window.AILAB_API;
  const ApiError = realApi.ApiError;
  const params = new URLSearchParams(window.location.search || "");
  const scenario = params.get("scenario") || "draft";
  const overlayMode = params.get("overlay") || "missing";
  const previewMode = params.get("preview") || "valid";
  const slowMode = params.get("delay") === "slow";
  const latencyMode = scenario === "latency";
  const syncFailureMode = scenario === "sync-failure";
  const ambiguityCode = params.get("ambiguity") || "";
  const normalizeAmbiguityMode = scenario === "normalize-ambiguous"
    && ["request_timeout", "network_error", "invalid_json"].includes(ambiguityCode);
  const refreshFailureMode = normalizeAmbiguityMode && params.get("refresh") === "fail";
  const fixtureDelays = {
    status: latencyMode ? 400 : slowMode ? 250 : 0,
    review: latencyMode ? 5000 : slowMode ? 1600 : 0,
    blocks: latencyMode ? 12000 : slowMode ? 1800 : 0,
    mutation: latencyMode ? 5000 : slowMode ? 1200 : 0,
  };
  const fixtureMetrics = { unitBlockRequests: 0, mutations: 0, statusRequests: 0 };
  let syncFailureDelivered = false;
  let normalizeAmbiguityDelivered = false;
  const docId = "source_package_ui_demo";
  const hash = char => String(char).repeat(64);
  const clone = value => JSON.parse(JSON.stringify(value));

  function waitForFixture(ms, signal) {
    if (!ms) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const cancel = () => {
        window.clearTimeout(timer);
        signal?.removeEventListener("abort", cancel);
        reject(new ApiError("Fixture request cancelled.", {
          ok: false,
          errors: [{ code: "request_cancelled", message: "Fixture request cancelled." }],
        }, 0));
      };
      const finish = () => {
        signal?.removeEventListener("abort", cancel);
        resolve();
      };
      const timer = window.setTimeout(finish, ms);
      if (!signal) return;
      if (signal.aborted) cancel();
      else signal.addEventListener("abort", cancel, { once: true });
    });
  }

  localStorage.setItem("ailab.doc_id", docId);
  localStorage.setItem("ailab.center_mode", "structure");
  localStorage.setItem("ailab.user", "UI Fixture Reviewer");

  const blockPreviewRows = [
    { block_id: "demo_ch01_b001", block_type: "heading", source_text: "Preface" },
    { block_id: "demo_ch01_b002", block_type: "paragraph", source_text: "When Mr. Hiram B. Otis bought Canterville Chase, everyone told him he was doing a very foolish thing." },
    { block_id: "demo_ch02_b001", block_type: "heading", source_text: "CHAPTER I · Arrival at Canterville Chase" },
    { block_id: "demo_ch02_b002", block_type: "paragraph", source_text: "The evening was warm and clear as the family approached the old house through the long avenue." },
    { block_id: "demo_ch02_b003", block_type: "dialogue", source_text: "\u201cThere is no such thing as a ghost, sir,\u201d said the Minister." },
    { block_id: "demo_ch03_b001", block_type: "heading_candidate", source_text: "A new boundary was detected here, but the printed heading is faint." },
    { block_id: "demo_ch03_b002", block_type: "paragraph", source_text: "A red stain appeared once more upon the library floor in the morning light." },
    { block_id: "demo_ch03_b003", block_type: "paragraph", source_text: "The family paused before deciding whether this passage belonged to the next chapter." },
  ];

  const baseUnits = [
    {
      unit_id: "u0001",
      chapter_id: "demo_ch01",
      order_index: 0,
      title: "Preface",
      block_ids: ["demo_ch01_b001", "demo_ch01_b002"],
      role: "frontmatter",
      translation_policy: "preserve",
      confidence: 0.96,
      review_required: false,
      issue_codes: [],
    },
    {
      unit_id: "u0002",
      chapter_id: "demo_ch02",
      order_index: 1,
      title: "Chapter I · Arrival",
      block_ids: ["demo_ch02_b001", "demo_ch02_b002", "demo_ch02_b003"],
      role: "body",
      translation_policy: "translate",
      confidence: 0.91,
      review_required: false,
      issue_codes: ["toc_heading_mismatch"],
    },
    {
      unit_id: "u0003",
      chapter_id: "demo_ch03",
      order_index: 2,
      title: "Untitled boundary",
      block_ids: ["demo_ch03_b001", "demo_ch03_b002", "demo_ch03_b003"],
      role: "unknown",
      translation_policy: "review",
      confidence: 0.62,
      review_required: true,
      issue_codes: ["unit_flagged_review", "unit_low_confidence", "unit_unknown_role"],
    },
  ];
  const parentByUnit = {};

  function reportFor(units) {
    const issues = units.flatMap(unit => (Array.isArray(unit.issue_codes) ? unit.issue_codes : []).map(code => ({
      issue_id: `fixture_${unit.unit_id}_${code}`,
      code,
      scope: "unit",
      target_id: unit.unit_id,
      evidence: code === "toc_heading_mismatch"
        ? ["toc_title:Chapter I", "candidate_title:Chapter I · Arrival"]
        : [`source_format:pdf`, `chapter_id:${unit.chapter_id}`],
    })));
    const outline = units.map(unit => ({
      unit_id: unit.unit_id,
      chapter_id: unit.chapter_id,
      order_index: unit.order_index,
      title: unit.title,
      first_block_id: unit.block_ids[0],
      last_block_id: unit.block_ids[unit.block_ids.length - 1],
      block_count: unit.block_ids.length,
      parent_unit_id: parentByUnit[unit.unit_id] || null,
    }));
    return {
      schema_version: "draft_structure_report_v1",
      doc_id: docId,
      editable: true,
      inputs: {},
      units,
      issues,
      global_skeleton: {
        schema_version: "draft_structure_global_skeleton_v1",
        doc_id: docId,
        inputs: {},
        policy: {},
        outline,
        navigation: [
          { entry_id: "nav_01", order_index: 0, title: "Preface", normalized_title: "preface", depth: 0, parent_id: null, target_file: "source.pdf", target_anchor: null, source_mapped_block_id: "demo_ch01_b001", candidate_block_ids: ["demo_ch01_b001"], resolved_block_id: "demo_ch01_b001", resolution_status: "resolved" },
          { entry_id: "nav_02", order_index: 1, title: "Chapter I", normalized_title: "chapter i", depth: 0, parent_id: null, target_file: "source.pdf", target_anchor: null, source_mapped_block_id: "demo_ch02_b001", candidate_block_ids: ["demo_ch02_b001"], resolved_block_id: "demo_ch02_b001", resolution_status: "resolved" },
        ],
        candidates: [
          { candidate_id: "candidate_heading_02", candidate_kind: "heading", source_signal: "font_geometry", source_ref: "page:4", title: "Chapter I", unit_ids: ["u0002"], block_ids: ["demo_ch02_b001"], at_block_id: "demo_ch02_b001", resolution_status: "accepted", signals: ["font_size_jump", "toc_alignment"], input_identity_sha256: hash("7") },
          { candidate_id: "candidate_heading_03", candidate_kind: "heading", source_signal: "font_geometry", source_ref: "page:7", title: "Untitled boundary", unit_ids: ["u0003"], block_ids: ["demo_ch03_b001"], at_block_id: "demo_ch03_b001", resolution_status: "review", signals: ["weak_heading_signal"], input_identity_sha256: hash("8") },
        ],
        issues: [],
        statistics: {
          unit_count: units.length,
          block_count: units.reduce((total, unit) => total + unit.block_ids.length, 0),
          navigation_entry_count: 2,
          navigation_unresolved_count: 0,
          navigation_mismatch_ratio: 0,
          candidate_count: 2,
          issue_count: issues.length,
        },
        integrity: { candidate_count: 2, issue_count: issues.length, payload_sha256: hash("9") },
      },
      integrity: {
        unit_count: units.length,
        issue_count: issues.length,
        payload_sha256: hash(String((revision % 9) + 1)),
      },
    };
  }

  function issueQueueFor(report, expected) {
    const rows = report.issues.map((issue, orderIndex) => {
      const unit = report.units.find(row => row.unit_id === issue.target_id) || null;
      const targetBlockId = unit?.block_ids?.[0] || null;
      const documentBlockPosition = targetBlockId
        ? report.units.flatMap(row => row.block_ids).indexOf(targetBlockId)
        : null;
      return {
        issue_id: issue.issue_id,
        order_index: orderIndex,
        code: issue.code,
        scope: issue.scope,
        target_id: issue.target_id ?? null,
        target_unit_id: unit?.unit_id || null,
        target_block_id: targetBlockId,
        related_unit_ids: unit ? [unit.unit_id] : [],
        related_block_ids: targetBlockId ? [targetBlockId] : [],
        navigation: {
          unit_id: unit?.unit_id || report.units[0]?.unit_id || null,
          block_id: targetBlockId || report.units[0]?.block_ids?.[0] || null,
          unit_order_index: unit?.order_index ?? report.units[0]?.order_index ?? null,
          unit_block_position: targetBlockId ? 0 : (report.units[0]?.block_ids?.length ? 0 : null),
          document_block_position: documentBlockPosition >= 0 ? documentBlockPosition : (report.units[0]?.block_ids?.length ? 0 : null),
        },
        evidence: clone(issue.evidence || []),
      };
    });
    return {
      schema_version: "source_package_issue_queue_v1",
      doc_id: docId,
      inputs: clone(expected),
      rows,
      integrity: { row_count: rows.length, payload_sha256: hash("9") },
    };
  }

  let units = clone(baseUnits);
  let revision = 0;
  let staleOnce = scenario === "stale";
  let blockStaleOnce = previewMode === "stale";
  let runtime = { project_id: docId, prepared: false };
  let mode = scenario === "unmanaged" || normalizeAmbiguityMode
    ? "unmanaged_draft"
    : scenario === "legacy"
      ? "legacy_only"
      : scenario === "finalized"
        ? "managed_finalized_pre_run"
        : scenario === "frozen"
          ? "managed_run_started_frozen"
          : "managed_draft";

  function lifecycleForMode() {
    if (mode === "managed_finalized_pre_run") return "finalized_pre_run";
    if (mode === "managed_run_started_frozen") return "run_started_frozen";
    return "draft";
  }

  function statusPayload() {
    if (mode === "legacy_only") {
      return { schema_version: "source_package_status_v1", doc_id: docId, mode, managed: false, normalize_allowed: false, reason: "legacy_project_evidence_exists", evidence: ["document.json"] };
    }
    if (mode === "unmanaged_draft") {
      return { schema_version: "source_package_status_v1", doc_id: docId, mode, managed: false, normalize_allowed: true, source: { filename: "source.pdf", format: "pdf", sha256: hash("1") }, reason: null };
    }
    const lifecycle = lifecycleForMode();
    return {
      schema_version: "source_package_status_v1",
      doc_id: docId,
      mode,
      managed: true,
      normalize_allowed: lifecycle === "draft",
      corrections_allowed: lifecycle === "draft",
      hierarchy_allowed: lifecycle === "draft",
      finalization_allowed: lifecycle === "draft",
      lifecycle,
      pipeline_run_count: lifecycle === "run_started_frozen" ? 1 : 0,
      source: { filename: "source.pdf", format: "pdf", sha256: hash("1") },
      candidate: { candidate_id: `srcpkg_${hash("2")}`, tree_sha256: hash("2"), relative_path: `working/source_package_candidates/srcpkg_${hash("2")}` },
      package: {
        schema_version: "canonical_source_package_v1",
        sha256: hash("3"),
        relative_path: "working/source_package_candidates/demo",
        document: { schema_version: "canonical_document_v1", sha256: hash("4") },
        structure: { schema_version: "canonical_structure_v1", sha256: hash("5") },
      },
      draft_structure: { report: { schema_version: "draft_structure_report_v1", sha256: hash(String((revision % 9) + 1)) } },
      policies: {},
      state_sha256: hash(String.fromCharCode(97 + (revision % 6))),
      revision: {
        latest_decision_sha256: hash("d"),
        hierarchy: { schema_version: "source_package_hierarchy_overlay_v1", sha256: revision ? hash("e") : null, relative_path: revision ? "working/source_package_hierarchy/demo.json" : null },
        finalization: { schema_version: "source_package_finalization_v1", sha256: mode === "managed_finalized_pre_run" || mode === "managed_run_started_frozen" ? hash("f") : null, relative_path: null },
        authority: "os_locked_first_run",
        load_bearing: lifecycle === "run_started_frozen",
      },
      latest_decision: { operation: mode === "managed_finalized_pre_run" ? "finalize_pre_run" : "correction", authority: { kind: "human", identifier: "UI Fixture Reviewer" } },
      ...(lifecycle === "run_started_frozen" ? { run_start: { schema_version: "source_package_run_start_v1", sha256: hash("6"), run_id: "run_fixture_001", job_id: "job_fixture_001", runtime_manifest_sha256: hash("5") } } : {}),
    };
  }

  function reviewPayload() {
    const frozen = mode === "managed_run_started_frozen";
    const report = reportFor(clone(units));
    const currentStatus = statusPayload();
    const expected = {
      state_sha256: currentStatus.state_sha256,
      candidate_tree_sha256: currentStatus.candidate.tree_sha256,
      document_sha256: currentStatus.package.document.sha256,
      structure_sha256: currentStatus.package.structure.sha256,
      report_sha256: report.integrity.payload_sha256,
      hierarchy_sha256: revision ? hash("e") : null,
    };
    return {
      schema_version: "source_package_review_v1",
      doc_id: docId,
      lifecycle: lifecycleForMode(),
      pipeline_run_count: frozen ? 1 : 0,
      authority: "explicit_human_approval_required",
      experimental: { scope: frozen ? "run_started_frozen" : "os_locked_pre_run", load_bearing: frozen },
      expected,
      supported_actions: frozen || mode === "managed_finalized_pre_run" ? [] : ["update_unit", "split_unit", "merge_adjacent_units"],
      supported_hierarchy_actions: frozen || mode === "managed_finalized_pre_run" ? [] : ["set_parent", "clear_parent"],
      report,
      issue_queue: issueQueueFor(report, expected),
    };
  }

  function apiError(code, message, status) {
    return new ApiError(message, { ok: false, errors: [{ code, message }] }, status);
  }

  function assertFresh() {
    if (!staleOnce) return;
    staleOnce = false;
    revision += 1;
    units[1].title = "Chapter I · Concurrent revision";
    throw apiError("source_package_correction_stale", "Expected identities differ from the current revision.", 409);
  }

  function applyCorrectionActions(actions) {
    for (const action of actions || []) {
      if (action.action_type === "update_unit") {
        const unit = units.find(row => row.unit_id === action.unit_id);
        if (unit && action.new_title !== null) unit.title = action.new_title;
        if (unit && action.classification !== null) {
          unit.translation_policy = action.classification;
          unit.review_required = action.classification === "review";
        }
      } else if (action.action_type === "split_unit") {
        const index = units.findIndex(row => row.unit_id === action.unit_id);
        const unit = units[index];
        const boundary = unit?.block_ids.indexOf(action.at_block_id) ?? -1;
        if (unit && boundary > 0) {
          units.splice(index, 1,
            { ...unit, unit_id: `${unit.unit_id}_i`, title: action.left_title, block_ids: unit.block_ids.slice(0, boundary), translation_policy: action.left_classification, review_required: action.left_classification === "review" },
            { ...unit, unit_id: `${unit.unit_id}_ii`, title: action.right_title, block_ids: unit.block_ids.slice(boundary), translation_policy: action.right_classification, review_required: action.right_classification === "review" });
        }
      } else if (action.action_type === "merge_adjacent_units") {
        const leftIndex = units.findIndex(row => row.unit_id === action.left_unit_id);
        const right = units[leftIndex + 1];
        if (leftIndex >= 0 && right?.unit_id === action.right_unit_id) {
          const left = units[leftIndex];
          units.splice(leftIndex, 2, { ...left, title: action.new_title, block_ids: [...left.block_ids, ...right.block_ids], translation_policy: action.classification, review_required: action.classification === "review" });
        }
      }
    }
    units = units.map((unit, index) => ({ ...unit, order_index: index }));
    revision += 1;
  }

  const overlay = {
    schema_version: "canonical_translation_overlay_v1",
    doc_id: docId,
    document_sha256: hash("3"),
    translations: baseUnits.flatMap(unit => unit.block_ids.map(blockId => ({
      block_id: blockId,
      text: `Bản dịch fixture ${blockId}`,
      html: `<p>Bản dịch fixture ${blockId}</p>`,
      markdown: `Bản dịch fixture ${blockId}`,
    }))),
  };

  window.SOURCE_PACKAGE_UI_FIXTURE = {
    scenario,
    ambiguityCode: normalizeAmbiguityMode ? ambiguityCode : null,
    overlay: overlayMode === "valid" ? overlay : null,
    metrics: fixtureMetrics,
  };

  window.AILAB_API = {
    ...realApi,
    baseUrl: "fixture://source-package-ui-v1",
    getVersion: async () => ({ backend_version: "fixture-source-package-v1", git_sha: "fixture" }),
    listProjects: async () => [{ doc_id: docId, status: "source_uploaded", source: "local", note: "Managed source fixture" }],
    listThesisDatasets: async () => [],
    listThesisRuns: async () => [],
    getProject: async () => ({ doc_id: docId, metadata: { title: "Managed Source UI Fixture", domain: "literature", source_format: "pdf" }, provenance: {} }),
    getThesisDataset: async (jobId) => ({
      meta: {
        source: "source_package_ui_fixture",
        job_id: jobId,
        counts: { chapters: units.length, blocks: units.reduce((sum, unit) => sum + unit.block_ids.length, 0) },
        available_runs: [],
        selected: {},
      },
      document: {
        doc_id: docId,
        title: "Managed Source UI Fixture",
        source_filename: "fixture.pdf",
        source_lang: "en",
        target_lang: "vi",
        metadata: { source_format: "pdf" },
      },
      chapters: units.map((unit, index) => ({
        chapter_id: unit.chapter_id,
        order: index + 1,
        title: unit.title,
        block_ids: [...unit.block_ids],
      })),
      blocks: units.flatMap(unit => unit.block_ids.map((blockId, index) => ({
        block_id: blockId,
        chapter_id: unit.chapter_id,
        order: index + 1,
        block_type: blockPreviewRows.find(row => row.block_id === blockId)?.block_type || "paragraph",
        text: blockPreviewRows.find(row => row.block_id === blockId)?.source_text || `Fixture source block ${blockId}`,
      }))),
      project_memory: { glossary_entries: [], entities: [], entity_relations: [], summaries: [] },
      eval_only: { gold_glossary: [], references: [] },
      translations: {},
    }),
    getThesisObservability: async (jobId) => ({
      meta: { source: "source_package_ui_fixture", job_id: jobId, read_only: true },
      calls: [],
      usage_daily: [],
      totals: { overall: { calls: 0, total_quota_tokens: 0, cost_usd: 0 } },
    }),
    getThesisRegistryOverlay: async (jobId) => ({
      meta: { source: "source_package_ui_fixture", job_id: jobId, overlay_mode: "localization" },
      source: { glossary_by_id: {}, entities_by_id: {} },
      target_by_config: {},
    }),
    getSourcePackageStatus: async () => {
      fixtureMetrics.statusRequests += 1;
      await waitForFixture(fixtureDelays.status);
      if (refreshFailureMode && normalizeAmbiguityDelivered) {
        throw apiError("network_error", "Fixture withheld authoritative status after an ambiguous normalize result.", 0);
      }
      return clone(statusPayload());
    },
    getSourcePackageReview: async () => {
      await waitForFixture(fixtureDelays.review);
      if (syncFailureMode && fixtureMetrics.mutations > 0 && !syncFailureDelivered) {
        syncFailureDelivered = true;
        throw apiError("fixture_review_unavailable", "Fixture withheld the first post-mutation review.", 503);
      }
      if (!["managed_draft", "managed_finalized_pre_run", "managed_run_started_frozen"].includes(mode)) {
        throw apiError("source_package_not_managed", "Normalize the source package before requesting structure review.", 409);
      }
      return clone(reviewPayload());
    },
    getSourcePackageUnitBlocks: async (_id, unitId, expected, offset = 0, limit = 200, options = {}) => {
      fixtureMetrics.unitBlockRequests += 1;
      await waitForFixture(fixtureDelays.blocks, options.signal);
      if (previewMode === "missing") {
        throw apiError("source_package_unit_blocks_unavailable", "Authoritative unit blocks are unavailable in this fixture state.", 503);
      }
      if (blockStaleOnce) {
        blockStaleOnce = false;
        revision += 1;
        units[1].title = "Chapter I · Refreshed after stale block read";
        throw apiError("source_package_review_stale", "Review identities changed; reload structure review before reading blocks.", 409);
      }
      const currentReview = reviewPayload();
      const bindingFields = ["state_sha256", "candidate_tree_sha256", "document_sha256", "structure_sha256", "report_sha256"];
      const currentBindings = Object.fromEntries(bindingFields.map(field => [field, currentReview.expected[field]]));
      if (bindingFields.some(field => expected?.[field] !== currentBindings[field])) {
        throw apiError("source_package_review_stale", "Review identities changed; reload structure review before reading blocks.", 409);
      }
      const unit = units.find(row => row.unit_id === unitId);
      if (!unit) throw apiError("source_package_review_unit_missing", "The requested unit is not part of the current structure review.", 404);
      const allBlocks = unit.block_ids.map((blockId, orderIndex) => {
        const row = blockPreviewRows.find(item => item.block_id === blockId);
        return {
          block_id: blockId,
          order_index: orderIndex,
          block_type: row?.block_type || "paragraph",
          source_text: row?.source_text || `Fixture source block ${blockId}`,
        };
      });
      const selected = allBlocks.slice(offset, offset + limit);
      const blocks = previewMode === "partial" ? selected.slice(0, -1) : selected;
      const payload = {
        schema_version: "source_package_unit_blocks_v1",
        doc_id: docId,
        lifecycle: lifecycleForMode(),
        pipeline_run_count: mode === "managed_run_started_frozen" ? 1 : 0,
        expected: currentBindings,
        unit: {
          unit_id: unit.unit_id,
          chapter_id: unit.chapter_id,
          order_index: unit.order_index,
          title: unit.title,
          block_count: allBlocks.length,
        },
        blocks,
        pagination: {
          offset,
          limit,
          returned: blocks.length,
          total: allBlocks.length,
          has_more: previewMode === "partial" ? false : offset + blocks.length < allBlocks.length,
        },
      };
      payload.integrity = { block_count: blocks.length, payload_sha256: hash("8") };
      return clone(payload);
    },
    normalizeSourcePackage: async () => {
      fixtureMetrics.mutations += 1;
      await waitForFixture(fixtureDelays.mutation);
      const reused = mode !== "unmanaged_draft";
      mode = "managed_draft";
      if (normalizeAmbiguityMode && !normalizeAmbiguityDelivered) {
        normalizeAmbiguityDelivered = true;
        throw apiError(
          ambiguityCode,
          `Fixture simulated ${ambiguityCode} after the backend may have committed normalization.`,
          ambiguityCode === "invalid_json" ? 200 : 0,
        );
      }
      return { ...clone(statusPayload()), created: !reused, reused };
    },
    applySourcePackageCorrections: async (_id, body) => {
      fixtureMetrics.mutations += 1;
      await waitForFixture(fixtureDelays.mutation);
      assertFresh();
      applyCorrectionActions(body.actions);
      mode = "managed_draft";
      return { ...clone(statusPayload()), decision_created: true, decision_reused: false };
    },
    applySourcePackageHierarchy: async (_id, body) => {
      fixtureMetrics.mutations += 1;
      await waitForFixture(fixtureDelays.mutation);
      assertFresh();
      for (const action of body.actions || []) {
        if (action.action_type === "set_parent") parentByUnit[action.child_unit_id] = action.parent_unit_id;
        if (action.action_type === "clear_parent") delete parentByUnit[action.child_unit_id];
      }
      revision += 1;
      return { ...clone(statusPayload()), decision_created: true, decision_reused: false };
    },
    finalizeSourcePackage: async () => {
      fixtureMetrics.mutations += 1;
      await waitForFixture(fixtureDelays.mutation);
      assertFresh();
      mode = "managed_finalized_pre_run";
      revision += 1;
      return { ...clone(statusPayload()), decision_created: true, decision_reused: false };
    },
    getProjectRuntime: async () => clone(runtime),
    prepareProjectRuntime: async () => {
      runtime = { contract_version: "project_runtime_source_v2", project_id: docId, job_id: "job_fixture_001", prepared: true, created: true, managed_source: { state_sha256: statusPayload().state_sha256 } };
      return clone(runtime);
    },
    publishSourcePackage: async (_id, body) => {
      const overlayFields = Object.keys(body || {}).sort().join(",");
      const translationFieldsValid = Array.isArray(body?.translations) && body.translations.every(row => (
        Object.keys(row || {}).sort().join(",") === "block_id,html,markdown,text"
      ));
      const expectedBlockIds = baseUnits.flatMap(unit => unit.block_ids).sort();
      const overlayBlockIds = Array.isArray(body?.translations) ? body.translations.map(row => row.block_id).sort() : [];
      if (
        body?.schema_version !== "canonical_translation_overlay_v1"
        || overlayFields !== "doc_id,document_sha256,schema_version,translations"
        || !/^[0-9a-f]{64}$/.test(body.document_sha256 || "")
        || !translationFieldsValid
        || overlayBlockIds.join(",") !== expectedBlockIds.join(",")
      ) throw apiError("source_package_publication_invalid", "Publication requires one exact canonical_translation_overlay_v1 JSON object.", 400);
      return {
        schema_version: "source_package_publication_v1",
        doc_id: docId,
        publication_id: `publication_${hash("b")}`,
        relative_path: `working/source_package_publications/publication_${hash("b")}`,
        created: true,
        reused: false,
        artifacts: {
          html: { path: "document.html", sha256: hash("4") },
          markdown: { path: "document.md", sha256: hash("5") },
        },
        lifecycle: "run_started_frozen",
        pipeline_run_count: 1,
      };
    },
  };
})();
