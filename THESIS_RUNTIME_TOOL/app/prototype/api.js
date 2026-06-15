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
    return localStorage.getItem(STORAGE_BASE) || DEFAULT_BASE;
  }

  function jsonHeaders() {
    return { "Content-Type": "application/json" };
  }

  async function request(path, options) {
    const opts = options || {};
    const init = {
      method: opts.method || "GET",
      headers: opts.headers || {},
    };
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
      throw new ApiError("Backend offline or unreachable.", {
        ok: false,
        errors: [{ code: "network_error", message: err.message || String(err) }],
      }, 0);
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
    createProject: (payload) => request("/projects", { method: "POST", body: payload }),
    getProject: (docId) => request(`/projects/${encodeURIComponent(docId)}`),
    patchProject: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}`, { method: "PATCH", body: payload }),
    deleteProject: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}`, { method: "DELETE", body: payload || {} }),
    uploadSource: (docId, file, overwrite) => {
      const form = new FormData();
      form.append("file", file);
      if (overwrite) form.append("overwrite", "true");
      return request(`/projects/${encodeURIComponent(docId)}/source`, { method: "POST", formData: form });
    },
    extract: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/extract`, { method: "POST", body: payload || {} }),
    normalizeCandidateParts: (docId) => request(`/projects/${encodeURIComponent(docId)}/normalize/candidate-parts`, { method: "POST", body: {} }),
    getNormalizeAgentPlan: (docId) => request(`/projects/${encodeURIComponent(docId)}/normalize/agent-plan`),
    importStructurePlan: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/normalize/plan`, { method: "POST", body: payload || {} }),
    applyNormalizedStructure: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/normalize/apply`, { method: "POST", body: payload || {} }),
    buildAnnotationInput: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/annotate/input`, { method: "POST", body: payload || {} }),
    getAnnotationAgentCandidate: (docId, chapterId) => request(`/projects/${encodeURIComponent(docId)}/annotate/candidate?chapter_id=${encodeURIComponent(chapterId)}`),
    resolveAnnotationCandidate: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/annotate/resolve`, { method: "POST", body: payload || {} }),
    applyAnnotationCandidate: (docId, payload) => request(`/projects/${encodeURIComponent(docId)}/annotate/apply`, { method: "POST", body: payload || {} }),
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
