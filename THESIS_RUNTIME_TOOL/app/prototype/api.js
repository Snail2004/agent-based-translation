(function () {
  const DEFAULT_BASE = "http://127.0.0.1:5000/api";
  const STORAGE_BASE = "ailab.api.base";

  class ApiError extends Error {
    constructor(message, payload, status) {
      super(message);
      this.name = "ApiError";
      this.payload = payload || null;
      this.status = status || 0;
      this.errors = (payload && payload.errors) || [];
      this.warnings = (payload && payload.warnings) || [];
    }
  }

  function baseUrl() {
    const configured = localStorage.getItem(STORAGE_BASE);
    if (!configured) return DEFAULT_BASE;
    try {
      const url = new URL(configured);
      const loopback = url.hostname === "127.0.0.1" || url.hostname === "localhost" || url.hostname === "::1";
      if (loopback && configured !== DEFAULT_BASE) {
        localStorage.removeItem(STORAGE_BASE);
        return DEFAULT_BASE;
      }
      return configured;
    } catch (_err) {
      localStorage.removeItem(STORAGE_BASE);
      return DEFAULT_BASE;
    }
  }

  function jsonHeaders() {
    return { "Content-Type": "application/json" };
  }

  const SOURCE_PACKAGE_REVIEW_BINDING_FIELDS = [
    "state_sha256",
    "candidate_tree_sha256",
    "document_sha256",
    "structure_sha256",
    "report_sha256",
  ];

  function sourcePackageUnitBlocksPath(docId, unitId, expected, offset, limit) {
    const query = new URLSearchParams();
    for (const field of SOURCE_PACKAGE_REVIEW_BINDING_FIELDS) {
      const value = expected && expected[field];
      if (typeof value !== "string" || !value.trim()) {
        throw new ApiError("Every current structure-review binding is required.", {
          ok: false,
          errors: [{
            code: "source_package_review_binding_required",
            message: `Review binding ${field} is required exactly once.`,
          }],
        }, 0);
      }
      query.set(field, value);
    }
    query.set("offset", String(offset));
    query.set("limit", String(limit));
    return `/projects/${encodeURIComponent(docId)}/source-package/review/units/${encodeURIComponent(unitId)}/blocks?${query.toString()}`;
  }

  async function request(path, options) {
    const opts = options || {};
    const init = {
      method: opts.method || "GET",
      headers: opts.headers || {},
    };
    const timeoutMs = Number.isFinite(opts.timeoutMs) && opts.timeoutMs > 0
      ? opts.timeoutMs
      : 0;
    const controller = timeoutMs > 0 && typeof AbortController !== "undefined"
      ? new AbortController()
      : null;
    let timeoutId = null;
    if (controller) {
      init.signal = controller.signal;
      timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    }
    if (opts.formData) {
      init.body = opts.formData;
    } else if (opts.body !== undefined) {
      init.headers = { ...jsonHeaders(), ...init.headers };
      init.body = JSON.stringify(opts.body);
    }

    let response;
    try {
      response = await fetch(baseUrl() + path, init);
    } catch (err) {
      if (controller?.signal.aborted) {
        throw new ApiError("Backend did not return status in time. Retry after checking the backend process.", {
          ok: false,
          errors: [{
            code: "request_timeout",
            message: `Backend did not respond within ${Math.ceil(timeoutMs / 1000)} seconds.`,
          }],
        }, 0);
      }
      throw new ApiError("Backend offline or unreachable.", {
        ok: false,
        errors: [{ code: "network_error", message: err.message || String(err) }],
      }, 0);
    } finally {
      if (timeoutId !== null) clearTimeout(timeoutId);
    }

    let payload;
    try {
      payload = await response.json();
    } catch (err) {
      throw new ApiError("Backend returned non-JSON response.", {
        ok: false,
        errors: [{ code: "invalid_json", message: err.message || String(err) }],
      }, response.status);
    }

    if (!response.ok || !payload.ok) {
      const first = payload.errors && payload.errors[0];
      throw new ApiError(first?.message || "API request failed.", payload, response.status);
    }
    return payload.data;
  }

  async function requestBlob(path) {
    let response;
    try {
      response = await fetch(baseUrl() + path);
    } catch (err) {
      throw new ApiError("Backend offline or unreachable.", {
        ok: false,
        errors: [{ code: "network_error", message: err.message || String(err) }],
      }, 0);
    }
    if (!response.ok) {
      let payload = null;
      try { payload = await response.json(); } catch (_err) {}
      const first = payload?.errors?.[0];
      throw new ApiError(first?.message || "Download failed.", payload, response.status);
    }
    return response.blob();
  }

  const API = {
    ApiError,
    get baseUrl() { return baseUrl(); },
    setBaseUrl(value) {
      if (value) localStorage.setItem(STORAGE_BASE, value);
      else localStorage.removeItem(STORAGE_BASE);
    },
    health: () => request("/health"),
    listProjects: () => request("/projects"),
    listThesisDatasets: () => request("/thesis/datasets"),
    getThesisDataset: (jobId, params) => {
      const query = new URLSearchParams(params || {});
      const suffix = query.toString() ? `?${query.toString()}` : "";
      return request(`/thesis/datasets/${encodeURIComponent(jobId)}${suffix}`);
    },
    getThesisRegistryOverlay: (jobId, params) => {
      const query = new URLSearchParams(params || {});
      const suffix = query.toString() ? `?${query.toString()}` : "";
      return request(`/thesis/overlay/${encodeURIComponent(jobId)}${suffix}`);
    },
    getThesisObservability: (jobId) => request(`/thesis/observability/${encodeURIComponent(jobId)}`),
    listThesisObservabilityCalls: (jobId) => request(`/thesis/observability/${encodeURIComponent(jobId)}/calls`),
    getThesisObservabilityCall: (jobId, callId) => request(`/thesis/observability/${encodeURIComponent(jobId)}/calls/${encodeURIComponent(callId)}`),
    getVersion: () => request("/version"),
    listThesisRuns: () => request("/thesis/runs"),
    createThesisRun: (payload) => request("/thesis/runs", { method: "POST", body: payload || {} }),
    getThesisRun: (runId) => request(`/thesis/runs/${encodeURIComponent(runId)}`),
    getThesisRunLog: (runId, offset) => request(`/thesis/runs/${encodeURIComponent(runId)}/log?offset=${encodeURIComponent(offset || 0)}`),
    getThesisRunEvents: (runId, offset, maxBytes) => {
      const query = new URLSearchParams({ offset: String(offset || 0) });
      if (maxBytes) query.set("max_bytes", String(maxBytes));
      return request(`/thesis/runs/${encodeURIComponent(runId)}/events?${query.toString()}`);
    },
    getThesisRunPromptPreview: (params) => {
      const query = new URLSearchParams(params || {});
      return request(`/thesis/runs/prompt-preview?${query.toString()}`);
    },
    getThesisRunManifest: (runId) => request(`/thesis/runs/${encodeURIComponent(runId)}/manifest`),
    getThesisRunBlockPreview: (runId, limit = 100) => request(`/thesis/runs/${encodeURIComponent(runId)}/block-preview?limit=${encodeURIComponent(limit)}`),
    getThesisRunWatchlist: (runId) => request(`/thesis/runs/${encodeURIComponent(runId)}/watchlist`),
    getThesisRunReportSummary: (runId) => request(`/thesis/runs/${encodeURIComponent(runId)}/report-summary`),
    pauseThesisRun: (runId) => request(`/thesis/runs/${encodeURIComponent(runId)}/pause`, { method: "POST", body: {} }),
    unpauseThesisRun: (runId) => request(`/thesis/runs/${encodeURIComponent(runId)}/pause`, { method: "DELETE", body: {} }),
    cancelThesisRun: (runId) => request(`/thesis/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", body: {} }),
    getThesisOneButtonEstimate: (params) => {
      const query = new URLSearchParams(params || {});
      return request(`/thesis/runs/estimate-preview?${query.toString()}`);
    },
    getThesisResumeEstimate: (runId) => request(`/thesis/runs/estimate-preview?resume_run_id=${encodeURIComponent(runId)}`),
    resumeThesisRun: (runId, payload) => request(`/thesis/runs/${encodeURIComponent(runId)}/resume`, { method: "POST", body: payload || {} }),
    createProject: (payload) => request("/projects", { method: "POST", body: payload }),
    getProject: (docId) => request(`/projects/${encodeURIComponent(docId)}`),
    getProjectRuntime: (docId) => request(`/projects/${encodeURIComponent(docId)}/runtime`, { timeoutMs: 30000 }),
    prepareProjectRuntime: (docId) => request(`/projects/${encodeURIComponent(docId)}/runtime/prepare`, { method: "POST", body: {} }),
    getSourcePackageStatus: (docId) => request(`/projects/${encodeURIComponent(docId)}/source-package`, { timeoutMs: 240000 }),
    normalizeSourcePackage: (docId) => request(`/projects/${encodeURIComponent(docId)}/source-package/normalize`, { method: "POST", body: {} }),
    getSourcePackageReview: (docId) => request(`/projects/${encodeURIComponent(docId)}/source-package/review`, { timeoutMs: 240000 }),
    getSourcePackageUnitBlocks: (docId, unitId, expected, offset = 0, limit = 200) => request(sourcePackageUnitBlocksPath(docId, unitId, expected, offset, limit), { timeoutMs: 60000 }),
    applySourcePackageCorrections: (docId, body) => request(`/projects/${encodeURIComponent(docId)}/source-package/corrections`, { method: "POST", body }),
    applySourcePackageHierarchy: (docId, body) => request(`/projects/${encodeURIComponent(docId)}/source-package/hierarchy`, { method: "POST", body }),
    finalizeSourcePackage: (docId, body) => request(`/projects/${encodeURIComponent(docId)}/source-package/finalize`, { method: "POST", body }),
    publishSourcePackage: (docId, overlay) => request(`/projects/${encodeURIComponent(docId)}/source-package/publications`, { method: "POST", body: overlay }),
    patchProject: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}`, { method: "PATCH", body: payload }),
    deleteProject: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}`, { method: "DELETE", body: payload || {} }),
    uploadSource: (docId, file, overwrite) => {
      const form = new FormData();
      form.append("file", file);
      if (overwrite) form.append("overwrite", "true");
      return request(`/projects/${encodeURIComponent(docId)}/source`, { method: "POST", formData: form });
    },
    importD2LPresegmentedSourcePackage: (docId, files) => {
      const expected = {
        source: "d2l_full_book_en_marked_v1.md",
        block_map: "block_map.json",
        manifest: "manifest.json",
      };
      const form = new FormData();
      for (const [field, filename] of Object.entries(expected)) {
        const file = files?.[field];
        if (!file || file.name !== filename) {
          throw new ApiError(`Field ${field} must contain ${filename}.`, {
            ok: false,
            errors: [{
              code: "d2l_presegmented_filename_invalid",
              message: `Field ${field} must contain exactly one file named ${filename}.`,
            }],
          }, 0);
        }
        form.append(field, file, filename);
      }
      return request(`/projects/${encodeURIComponent(docId)}/source-package/import-d2l-presegmented`, {
        method: "POST",
        formData: form,
      });
    },
    extract: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/extract`, { method: "POST", body: payload || {} }),
    getTranslationPreviewInput: (docId, chapterId) => request(`/projects/${encodeURIComponent(docId)}/translation-preview/input?chapter_id=${encodeURIComponent(chapterId)}`),
    getSavedTranslationPreviewInput: (docId, chapterId) => request(`/projects/${encodeURIComponent(docId)}/translation-preview/input/saved?chapter_id=${encodeURIComponent(chapterId)}`),
    importTranslationPreviewRun: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/translation-preview/runs`, { method: "POST", body: payload || {} }),
    importAgentTranslationPreviewRun: (docId, chapterId) => request(`/projects/${encodeURIComponent(docId)}/translation-preview/runs/agent-output`, { method: "POST", body: { chapter_id: chapterId } }),
    listTranslationPreviewRuns: (docId) => request(`/projects/${encodeURIComponent(docId)}/translation-preview/runs`),
    loadTranslationPreviewRun: (docId, runId) => request(`/projects/${encodeURIComponent(docId)}/translation-preview/runs/${encodeURIComponent(runId)}`),
    getJob: (docId, jobId) => request(`/projects/${encodeURIComponent(docId)}/jobs/${encodeURIComponent(jobId)}`),
    getDataset: (docId) => request(`/projects/${encodeURIComponent(docId)}/dataset`),
    getHistory: (docId) => request(`/projects/${encodeURIComponent(docId)}/history`),
    undo: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/undo`, { method: "POST", body: payload || {} }),
    redo: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/redo`, { method: "POST", body: payload || {} }),
    validate: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/validate`, { method: "POST", body: payload || {} }),
    migrateSchema: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/migrate-schema`, { method: "POST", body: payload || {} }),
    patchMetadata: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/metadata`, { method: "PATCH", body: payload }),
    patchBlock: (docId, blockId, payload) => request(`/projects/${encodeURIComponent(docId)}/blocks/${encodeURIComponent(blockId)}`, { method: "PATCH", body: payload }),
    patchBlockNotes: (docId, blockId, payload) => request(`/projects/${encodeURIComponent(docId)}/blocks/${encodeURIComponent(blockId)}/notes`, { method: "PATCH", body: payload }),
    patchReview: (docId, blockId, payload) => request(`/projects/${encodeURIComponent(docId)}/review/blocks/${encodeURIComponent(blockId)}`, { method: "PATCH", body: payload }),
    addGlossary: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/glossary/from-selection`, { method: "POST", body: payload }),
    patchGlossary: (docId, termId, payload) => request(`/projects/${encodeURIComponent(docId)}/glossary/${encodeURIComponent(termId)}`, { method: "PATCH", body: payload }),
    deleteGlossary: (docId, termId, payload) => request(`/projects/${encodeURIComponent(docId)}/glossary/${encodeURIComponent(termId)}`, { method: "DELETE", body: payload || {} }),
    addEntity: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/entities/from-selection`, { method: "POST", body: payload }),
    patchEntity: (docId, entityId, payload) => request(`/projects/${encodeURIComponent(docId)}/entities/${encodeURIComponent(entityId)}`, { method: "PATCH", body: payload }),
    deleteEntity: (docId, entityId, payload) => request(`/projects/${encodeURIComponent(docId)}/entities/${encodeURIComponent(entityId)}`, { method: "DELETE", body: payload || {} }),
    createRelation: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/relations`, { method: "POST", body: payload }),
    patchRelation: (docId, relationId, payload) => request(`/projects/${encodeURIComponent(docId)}/relations/${encodeURIComponent(relationId)}`, { method: "PATCH", body: payload }),
    deleteRelation: (docId, relationId, payload) => request(`/projects/${encodeURIComponent(docId)}/relations/${encodeURIComponent(relationId)}`, { method: "DELETE", body: payload || {} }),
    patchSummary: (docId, chapterId, payload) => request(`/projects/${encodeURIComponent(docId)}/summary/${encodeURIComponent(chapterId)}`, { method: "PATCH", body: payload }),
    saveReferenceDraft: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/references/draft`, { method: "POST", body: payload }),
    reviewReference: (docId, referenceId, payload) => request(`/projects/${encodeURIComponent(docId)}/references/${encodeURIComponent(referenceId)}/review`, { method: "POST", body: payload }),
    lockReference: (docId, referenceId, payload) => request(`/projects/${encodeURIComponent(docId)}/references/${encodeURIComponent(referenceId)}/lock`, { method: "POST", body: payload || {} }),
    exportProject: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/export`, { method: "POST", body: payload || {} }),
    exportProjectWithPreviews: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/export-with-previews`, { method: "POST", body: payload || {} }),
    downloadExport: (docId, filename) => requestBlob(`/projects/${encodeURIComponent(docId)}/exports/${encodeURIComponent(filename)}`),
    freezeProject: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/freeze`, { method: "POST", body: payload || {} }),
  };

  window.AILAB_API = API;
})();
