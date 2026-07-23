/* Persisted Workflow Live/Replay adapter.
 *
 * Boundary:
 * - consumes only a parent WorkflowManifestV1, WorkflowEventV1 sequence,
 *   WorkflowArtifactIndexV1 and explicitly supplied handoff/receipt bodies;
 * - never scans a client filesystem, reconstructs a stage list, computes an
 *   Evaluation metric/verdict, or aggregates provider usage/cost;
 * - fails closed before the Console sees events when identities, hashes,
 *   sequence, five-arm receipt, or validated report bindings disagree.
 *
 * Production transport is intentionally absent until the Coordinator reserves
 * a read-only backend workflow package API. The 0-API dev harness uses the same
 * adapter against the accepted persisted relay fixture.
 */
(function installWorkflowReplayAdapter(global) {
  "use strict";

  const FLOW_KIND = "translation_evaluation_publication";
  const ARM_ORDER = Object.freeze(["s0", "s1", "community", "google_nmt", "llm_lc"]);
  const EVALUATION_CHAPTER_ORDER = Object.freeze([
    "d2l_preliminaries",
    "d2l_linear_networks",
    "d2l_multilayer_perceptrons",
    "d2l_deep_learning_computation",
    "d2l_convolutional_neural_networks",
  ]);
  const SCORER_ORDER = Object.freeze(["sf_qe", "sf_bt", "pj"]);
  const COMPONENT_IDS = new Set(["translation", "evaluation", "publication"]);
  const RESUME_EVENTS = new Set(["run_resumed", "component_resumed"]);
  const SOURCE_BINDING_ROLES = Object.freeze(["document", "structure_manifest", "asset_manifest", "admitted_projection", "normalization_receipt", "package_seal"]);
  const WORKFLOW_STATUSES = new Set(["pending", "running", "paused", "failed", "succeeded"]);
  const FORBIDDEN_PAYLOAD_KEYS = new Set([
    "api_key", "authorization", "bearer", "bearer_token", "credential", "credentials",
    "gold", "gold_translation", "human_reference", "human_translation", "prompt",
    "raw_prompt", "raw_request", "raw_response", "reference_translation", "request_body",
    "response", "response_body", "secret",
  ]);
  const OPERATIONAL_FACT_RE = /(token|cost|cache|quota|usage)/i;
  const SHA256_RE = /^[0-9a-f]{64}$/;
  const CACHE_STATUSES = new Set(["hit", "miss", "bypass", "unknown"]);
  const CACHE_MECHANISMS = new Set([
    "none", "provider_prompt_cache", "provider_implicit_cache",
    "local_exact_cache", "unknown",
  ]);
  const OPTIONAL_DETAIL_COMPONENTS = new Set(["translation", "evaluation", "publication"]);

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function deepClone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function canonicalJSONString(value) {
    if (value === null) return "null";
    if (typeof value === "string") return JSON.stringify(value.normalize("NFC"));
    if (typeof value === "boolean") return value ? "true" : "false";
    if (typeof value === "number") {
      if (!Number.isFinite(value)) throw new TypeError("non-finite JSON number");
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) return `[${value.map(canonicalJSONString).join(",")}]`;
    if (isObject(value)) {
      return `{${Object.keys(value).sort().map(key => (
        `${JSON.stringify(key.normalize("NFC"))}:${canonicalJSONString(value[key])}`
      )).join(",")}}`;
    }
    throw new TypeError(`unsupported JSON value: ${typeof value}`);
  }

  function canonicalEqual(left, right) {
    try {
      return canonicalJSONString(left) === canonicalJSONString(right);
    } catch (_error) {
      return false;
    }
  }

  async function sha256Text(text) {
    if (!global.crypto || !global.crypto.subtle) throw new Error("Web Crypto SHA-256 is unavailable");
    const bytes = new TextEncoder().encode(text);
    const digest = await global.crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  }

  async function canonicalSha256(value) {
    return sha256Text(canonicalJSONString(value));
  }

  function withoutNested(value, path) {
    const copy = deepClone(value);
    let cursor = copy;
    for (let index = 0; index < path.length - 1; index += 1) {
      if (!isObject(cursor?.[path[index]])) return copy;
      cursor = cursor[path[index]];
    }
    if (isObject(cursor)) delete cursor[path[path.length - 1]];
    return copy;
  }

  function addError(errors, code, path, message) {
    errors.push({ code, path, message });
  }

  function requireObject(value, path, errors) {
    if (!isObject(value)) {
      addError(errors, "type", path, "expected an object");
      return false;
    }
    return true;
  }

  function exactKeys(value, required, path, errors) {
    if (!requireObject(value, path, errors)) return false;
    const expected = new Set(required);
    const missing = required.filter(key => !Object.prototype.hasOwnProperty.call(value, key));
    const unknown = Object.keys(value).filter(key => !expected.has(key));
    if (missing.length) addError(errors, "missing_keys", path, `missing: ${missing.join(", ")}`);
    if (unknown.length) addError(errors, "unknown_keys", path, `unknown: ${unknown.join(", ")}`);
    return !missing.length && !unknown.length;
  }

  function validId(value) {
    return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(value);
  }

  function validateBinding(binding, path, errors) {
    const keys = ["artifact_ref", "artifact_kind", "schema_version", "sha256", "sha256_kind"];
    exactKeys(binding, keys, path, errors);
    if (!isObject(binding)) return null;
    if (typeof binding.artifact_ref !== "string" || !binding.artifact_ref || binding.artifact_ref.includes("\\") || binding.artifact_ref.startsWith("/")) {
      addError(errors, "artifact_ref", `${path}.artifact_ref`, "expected a non-empty relative POSIX reference");
    }
    if (!SHA256_RE.test(String(binding.sha256 || ""))) {
      addError(errors, "sha256", `${path}.sha256`, "expected lowercase SHA-256");
    }
    return binding;
  }

  function inspectPrivatePayload(value, path, errors) {
    if (Array.isArray(value)) {
      value.forEach((child, index) => inspectPrivatePayload(child, `${path}[${index}]`, errors));
      return;
    }
    if (!isObject(value)) return;
    if (value.cost_status === "unknown") {
      ["cost", "cost_usd", "provider_cost_usd"].forEach(key => {
        if (Object.prototype.hasOwnProperty.call(value, key) && value[key] !== null) {
          addError(errors, "unknown_cost", `${path}.${key}`, "unknown cost must remain null");
        }
      });
    }
    Object.entries(value).forEach(([key, child]) => {
      const normalized = key.toLocaleLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
      if (FORBIDDEN_PAYLOAD_KEYS.has(normalized) || normalized.startsWith("raw_prompt") || normalized.startsWith("raw_response")) {
        addError(errors, "private_parent_payload", `${path}.${key}`, "private/raw authority is forbidden in parent events");
      }
      inspectPrivatePayload(child, `${path}.${key}`, errors);
    });
  }

  function containsOperationalFact(value) {
    if (Array.isArray(value)) return value.some(containsOperationalFact);
    if (!isObject(value)) return false;
    return Object.entries(value).some(([key, child]) => OPERATIONAL_FACT_RE.test(key) || containsOperationalFact(child));
  }

  function nullableNonNegativeNumber(value, path, errors, integer = false) {
    if (value === undefined || value === null) return null;
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || (integer && !Number.isInteger(value))) {
      addError(errors, "usage_number", path, integer ? "expected a non-negative integer or null" : "expected a non-negative number or null");
      return null;
    }
    return value;
  }

  function usageValue(row, key) {
    if (Object.prototype.hasOwnProperty.call(row || {}, key)) return row[key];
    const usage = isObject(row?.usage) ? row.usage : null;
    return usage && Object.prototype.hasOwnProperty.call(usage, key) ? usage[key] : undefined;
  }

  function normalizeUsageFacts(row, path, errors) {
    if (!requireObject(row, path, errors)) return null;
    inspectPrivatePayload(row, path, errors);
    const attemptUsageId = row.attempt_usage_id ?? row.usage_id ?? null;
    const cacheObservationId = row.cache_observation_id ?? row.observation_id ?? null;
    const rowId = row.call_id ?? attemptUsageId ?? cacheObservationId;
    if (!validId(rowId)) addError(errors, "usage_id", `${path}.call_id`, "a stable call/cache identity is required");
    const cacheStatus = usageValue(row, "cache_status") ?? row.lookup_status ?? null;
    const cacheMechanism = usageValue(row, "cache_mechanism") ?? row.cache_kind ?? null;
    if (cacheStatus !== null && !CACHE_STATUSES.has(cacheStatus)) {
      addError(errors, "cache_status", `${path}.cache_status`, "unsupported cache status");
    }
    if (cacheMechanism !== null && !CACHE_MECHANISMS.has(cacheMechanism)) {
      addError(errors, "cache_mechanism", `${path}.cache_mechanism`, "unsupported cache mechanism");
    }
    const costStatus = usageValue(row, "cost_status") ?? null;
    const costUsd = nullableNonNegativeNumber(usageValue(row, "cost_usd"), `${path}.cost_usd`, errors);
    if (costStatus === "unknown" && costUsd !== null) {
      addError(errors, "unknown_cost", `${path}.cost_usd`, "unknown cost must remain null");
    }
    return {
      rowId: validId(rowId) ? rowId : `${path}:invalid`,
      rowKind: attemptUsageId ? "call" : cacheObservationId ? "cache" : String(row.row_kind || "call"),
      attemptUsageId,
      cacheObservationId,
      componentId: row.component_id ?? null,
      componentRunId: row.component_run_id ?? null,
      componentAttemptId: row.component_attempt_id ?? null,
      componentAttemptIndex: row.component_attempt_index ?? null,
      componentSeq: row.component_seq ?? row.accepted_through_component_seq ?? null,
      stageId: row.stage_id ?? null,
      agent: row.agent ?? null,
      workId: row.work_id ?? row.current_work_id ?? null,
      logicalRequestId: row.logical_request_id ?? null,
      semanticAttemptIndex: row.semantic_attempt_index ?? null,
      transportRetryOrdinal: row.transport_retry_ordinal ?? null,
      physicalAttemptIndex: row.physical_attempt_index ?? null,
      providerId: row.provider_id ?? null,
      sourceId: row.source_id ?? null,
      sourceRevision: row.source_revision ?? null,
      requestedModelId: row.requested_model_id ?? row.model_id ?? null,
      observedModelId: row.observed_model_id ?? null,
      promptTokens: nullableNonNegativeNumber(usageValue(row, "prompt_tokens"), `${path}.prompt_tokens`, errors, true),
      completionTokens: nullableNonNegativeNumber(usageValue(row, "completion_tokens"), `${path}.completion_tokens`, errors, true),
      reasoningTokens: nullableNonNegativeNumber(usageValue(row, "reasoning_tokens"), `${path}.reasoning_tokens`, errors, true),
      cachedInputTokens: nullableNonNegativeNumber(usageValue(row, "cached_input_tokens"), `${path}.cached_input_tokens`, errors, true),
      totalTokens: nullableNonNegativeNumber(usageValue(row, "total_tokens"), `${path}.total_tokens`, errors, true),
      cacheStatus,
      cacheMechanism,
      providerCallAvoided: row.provider_call_avoided ?? null,
      latencyMs: nullableNonNegativeNumber(usageValue(row, "latency_ms"), `${path}.latency_ms`, errors),
      finishReason: row.finish_reason ?? null,
      outcome: row.outcome ?? null,
      costStatus,
      costUsd,
      currency: usageValue(row, "currency") ?? null,
      binding: row.binding ?? row.usage_binding ?? null,
    };
  }

  function normalizeUsageTotal(row, path, errors) {
    if (!requireObject(row, path, errors)) return null;
    inspectPrivatePayload(row, path, errors);
    const costStatus = usageValue(row, "cost_status") ?? null;
    const costUsd = nullableNonNegativeNumber(usageValue(row, "cost_usd"), `${path}.cost_usd`, errors);
    if (costStatus === "unknown" && costUsd !== null) {
      addError(errors, "unknown_cost", `${path}.cost_usd`, "unknown cost must remain null");
    }
    return {
      componentId: row.component_id ?? null,
      componentRunId: row.component_run_id ?? null,
      stageId: row.stage_id ?? null,
      snapshotSeq: row.snapshot_seq ?? null,
      acceptedThroughComponentSeq: row.accepted_through_component_seq ?? null,
      physicalCallCount: nullableNonNegativeNumber(usageValue(row, "physical_call_count"), `${path}.physical_call_count`, errors, true),
      cacheObservationCount: nullableNonNegativeNumber(usageValue(row, "cache_observation_count"), `${path}.cache_observation_count`, errors, true),
      promptTokens: nullableNonNegativeNumber(usageValue(row, "prompt_tokens"), `${path}.prompt_tokens`, errors, true),
      completionTokens: nullableNonNegativeNumber(usageValue(row, "completion_tokens"), `${path}.completion_tokens`, errors, true),
      reasoningTokens: nullableNonNegativeNumber(usageValue(row, "reasoning_tokens"), `${path}.reasoning_tokens`, errors, true),
      cachedInputTokens: nullableNonNegativeNumber(usageValue(row, "cached_input_tokens"), `${path}.cached_input_tokens`, errors, true),
      totalTokens: nullableNonNegativeNumber(usageValue(row, "total_tokens"), `${path}.total_tokens`, errors, true),
      cacheHitCount: nullableNonNegativeNumber(usageValue(row, "cache_hit_count"), `${path}.cache_hit_count`, errors, true),
      cacheMissCount: nullableNonNegativeNumber(usageValue(row, "cache_miss_count"), `${path}.cache_miss_count`, errors, true),
      unknownAttemptCount: nullableNonNegativeNumber(usageValue(row, "unknown_attempt_count"), `${path}.unknown_attempt_count`, errors, true),
      costStatus,
      costUsd,
      currency: usageValue(row, "currency") ?? null,
      binding: row.binding ?? row.snapshot_binding ?? null,
      sha256: row.snapshot_sha256 ?? row.summary_sha256 ?? null,
    };
  }

  function validateUsageReadModel(value, manifest, errors) {
    if (value === undefined || value === null) {
      return { present: false, calls: [], stageTotals: [], componentTotals: [], workflowTotal: null };
    }
    if (!requireObject(value, "$usage", errors)) {
      return { present: true, calls: [], stageTotals: [], componentTotals: [], workflowTotal: null };
    }
    if (typeof value.schema_id !== "string" || typeof value.schema_version !== "string") {
      addError(errors, "usage_schema", "$usage", "a versioned usage read model is required");
    }
    if (value.workflow_run_id !== manifest?.workflow_run_id) {
      addError(errors, "usage_binding", "$usage.workflow_run_id", "usage workflow identity differs from manifest");
    }
    const backendValidated = value.validated === true || value.validation?.valid === true;
    if (!backendValidated) addError(errors, "usage_unvalidated", "$usage", "backend validation receipt is required");
    inspectPrivatePayload(value, "$usage", errors);
    const rawCalls = value.calls ?? value.call_rows ?? value.physical_calls ?? [];
    const rawStageTotals = value.stage_totals ?? [];
    const rawComponentTotals = value.component_totals ?? [];
    if (!Array.isArray(rawCalls)) addError(errors, "type", "$usage.calls", "expected an array");
    if (!Array.isArray(rawStageTotals)) addError(errors, "type", "$usage.stage_totals", "expected an array");
    if (!Array.isArray(rawComponentTotals)) addError(errors, "type", "$usage.component_totals", "expected an array");
    const identities = new Set();
    const calls = (Array.isArray(rawCalls) ? rawCalls : []).map((row, index) => {
      const normalized = normalizeUsageFacts(row, `$usage.calls[${index}]`, errors);
      if (normalized && identities.has(normalized.rowId)) {
        addError(errors, "usage_duplicate", `$usage.calls[${index}]`, `duplicate usage/cache identity ${normalized.rowId}`);
      }
      if (normalized) identities.add(normalized.rowId);
      return normalized;
    }).filter(Boolean);
    const stageTotals = (Array.isArray(rawStageTotals) ? rawStageTotals : [])
      .map((row, index) => normalizeUsageTotal(row, `$usage.stage_totals[${index}]`, errors))
      .filter(Boolean);
    const componentTotals = (Array.isArray(rawComponentTotals) ? rawComponentTotals : [])
      .map((row, index) => normalizeUsageTotal(row, `$usage.component_totals[${index}]`, errors))
      .filter(Boolean);
    const workflowTotal = value.workflow_total === null || value.workflow_total === undefined
      ? null
      : normalizeUsageTotal(value.workflow_total, "$usage.workflow_total", errors);
    return {
      present: true,
      schemaId: value.schema_id ?? null,
      schemaVersion: value.schema_version ?? null,
      calls,
      stageTotals,
      componentTotals,
      workflowTotal,
      validation: value.validation ?? { valid: value.validated === true },
    };
  }

  function validateOptionalDetails(events, errors) {
    events.forEach((event, eventIndex) => {
      const details = event?.payload?.optional_details;
      if (details === undefined) return;
      const path = `$events[${eventIndex}].payload.optional_details`;
      if (!Array.isArray(details)) {
        addError(errors, "optional_details", path, "expected an array of typed details");
        return;
      }
      details.forEach((detail, detailIndex) => {
        const detailPath = `${path}[${detailIndex}]`;
        exactKeys(detail, ["schema_id", "schema_version", "component_id", "kind", "data"], detailPath, errors);
        if (!isObject(detail)) return;
        if (typeof detail.schema_id !== "string" || typeof detail.schema_version !== "string") {
          addError(errors, "optional_detail_schema", detailPath, "typed detail schema is required");
        }
        if (!OPTIONAL_DETAIL_COMPONENTS.has(detail.component_id) || detail.component_id !== event?.component?.component_id) {
          addError(errors, "optional_detail_component", `${detailPath}.component_id`, "detail authority differs from event component");
        }
        if (typeof detail.kind !== "string" || !detail.kind) addError(errors, "optional_detail_kind", `${detailPath}.kind`, "detail kind is required");
        if (!isObject(detail.data) && !Array.isArray(detail.data)) addError(errors, "optional_detail_data", `${detailPath}.data`, "detail data must be an object or array");
        inspectPrivatePayload(detail.data, `${detailPath}.data`, errors);
      });
    });
  }

  function projectOptionalDetails(payload) {
    return Array.isArray(payload?.optional_details) ? deepClone(payload.optional_details) : [];
  }

  function inspectSetupExposure(value, path, errors) {
    if (Array.isArray(value)) {
      value.forEach((child, index) => inspectSetupExposure(child, `${path}[${index}]`, errors));
      return;
    }
    if (!isObject(value)) return;
    Object.entries(value).forEach(([key, child]) => {
      const normalized = key.toLocaleLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
      if (
        ["api_key", "authorization", "base_url", "credential_path", "endpoint_url", "filesystem_path", "raw_prompt", "secret", "secret_bytes"].includes(normalized)
        || normalized.endsWith("_secret")
      ) {
        addError(errors, "private_setup_field", `${path}.${key}`, "workflow setup may expose only registered IDs and credential-ref status");
      }
      inspectSetupExposure(child, `${path}.${key}`, errors);
    });
  }

  function advertisedOption(row, path, errors) {
    if (!requireObject(row, path, errors)) return null;
    const ref = isObject(row.ref) ? row.ref : row;
    const id = row.option_id ?? row.id ?? ref.id ?? ref.profile_id ?? ref.settings_id;
    const revision = row.revision ?? ref.revision ?? null;
    const sha256 = row.sha256 ?? ref.sha256 ?? null;
    if (!validId(id)) addError(errors, "advertised_option_id", `${path}.id`, "stable option ID is required");
    if (revision !== null && typeof revision !== "string") addError(errors, "advertised_option_revision", `${path}.revision`, "revision must be a string or null");
    if (sha256 !== null && !SHA256_RE.test(String(sha256))) addError(errors, "sha256", `${path}.sha256`, "expected lowercase SHA-256 or null");
    return {
      id: validId(id) ? id : `${path}:invalid`,
      label: String(row.label ?? row.name ?? id ?? ""),
      revision,
      sha256,
      enabled: row.enabled !== false,
      status: row.status ?? null,
      credentialStatus: deepClone(row.credential_status ?? row.credential_ref_status ?? null),
      capabilityStatus: deepClone(row.capability_status ?? row.capability ?? null),
      fixedFacts: deepClone(row.fixed_facts ?? row.facts ?? {}),
      selectionCatalog: deepClone(row.selection_catalog ?? null),
      defaultSelection: deepClone(row.default_selection ?? null),
      constraints: deepClone(row.constraints ?? {}),
      raw: deepClone(row),
    };
  }

  function validateOrderedSubset(value, allowed, minimum, path, errors) {
    if (!Array.isArray(value)) {
      addError(errors, "type", path, "expected an array");
      return [];
    }
    if (value.some(item => typeof item !== "string" || !item)) {
      addError(errors, "selection_id", path, "selection IDs must be non-empty strings");
      return [];
    }
    if (new Set(value).size !== value.length) addError(errors, "duplicate_selection", path, "selection IDs must be unique");
    const selected = new Set(value);
    const canonical = allowed.filter(item => selected.has(item));
    if (canonical.length !== value.length || canonical.some((item, index) => item !== value[index])) {
      addError(errors, "selection_order", path, "selection must be a canonical subset of the advertised order");
    }
    if (value.length < minimum) addError(errors, "selection_cardinality", path, `select at least ${minimum}`);
    return [...value];
  }

  function validateEvaluationOption(option, setupChapterIds, path, errors) {
    if (!option?.enabled) return;
    const facts = option.fixedFacts;
    if (facts?.settings_schema_id !== "EvaluationWorkflowSettingsV1" || facts?.settings_schema_version !== "1.1.0") {
      addError(errors, "evaluation_settings_schema", `${path}.fixed_facts`, "EvaluationWorkflowSettingsV1@1.1.0 is required");
    }
    if (!SHA256_RE.test(String(option.sha256 || ""))) addError(errors, "sha256", `${path}.sha256`, "registered Evaluation option SHA-256 is required");
    const catalog = option.selectionCatalog;
    if (!isObject(catalog)) {
      addError(errors, "evaluation_catalog", `${path}.selection_catalog`, "an ordered Evaluation selection catalog is required");
      return;
    }
    const catalogChapterIds = Array.isArray(catalog.chapter_ids) ? catalog.chapter_ids : [];
    const catalogChapterSet = new Set(catalogChapterIds);
    const canonicalProjectSubset = setupChapterIds.filter(chapterId => catalogChapterSet.has(chapterId));
    if (
      !catalogChapterIds.length
      || catalogChapterIds.some(chapterId => typeof chapterId !== "string" || !chapterId)
      || catalogChapterSet.size !== catalogChapterIds.length
      || !canonicalEqual(catalogChapterIds, canonicalProjectSubset)
    ) {
      addError(errors, "evaluation_chapter_catalog", `${path}.selection_catalog.chapter_ids`, "Evaluation chapters must be a non-empty canonical subset of the project chapter order");
    }
    if (!Array.isArray(catalog.arm_ids) || !canonicalEqual(catalog.arm_ids, ARM_ORDER)) {
      addError(errors, "evaluation_arm_catalog", `${path}.selection_catalog.arm_ids`, "the exact five-arm order is required");
    }
    if (!Array.isArray(catalog.scorer_ids) || !canonicalEqual(catalog.scorer_ids, SCORER_ORDER)) {
      addError(errors, "evaluation_scorer_catalog", `${path}.selection_catalog.scorer_ids`, "the exact scorer order is required");
    }
    const defaults = option.defaultSelection;
    if (!isObject(defaults) || defaults.settings_option_id !== option.id) {
      addError(errors, "evaluation_defaults", `${path}.default_selection`, "server-owned Evaluation defaults are required");
      return;
    }
    const chapters = validateOrderedSubset(defaults.selected_chapter_ids, catalogChapterIds, 1, `${path}.default_selection.selected_chapter_ids`, errors);
    chapters.forEach(chapterId => {
      if (!setupChapterIds.includes(chapterId)) addError(errors, "evaluation_chapter_scope", `${path}.default_selection.selected_chapter_ids`, "default Evaluation chapters must exist in the project");
    });
    const arms = validateOrderedSubset(defaults.selected_arm_ids, ARM_ORDER, 2, `${path}.default_selection.selected_arm_ids`, errors);
    validateOrderedSubset(defaults.selected_scorer_ids, SCORER_ORDER, 1, `${path}.default_selection.selected_scorer_ids`, errors);
    const pair = defaults.highlight_pair;
    if (pair !== null && (!isObject(pair) || pair.baseline_arm_id === pair.candidate_arm_id || !arms.includes(pair.baseline_arm_id) || !arms.includes(pair.candidate_arm_id))) {
      addError(errors, "highlight_pair", `${path}.default_selection.highlight_pair`, "default highlight pair must contain two selected arms");
    }
  }

  function normalizeWorkflowSetup(value) {
    const setup = deepClone(value || null);
    const errors = [];
    if (!requireObject(setup, "$setup", errors)) {
      return Object.freeze({ valid: false, errors, raw: setup });
    }
    if (typeof setup.schema_id !== "string" || typeof setup.schema_version !== "string") {
      addError(errors, "setup_schema", "$setup", "a versioned workflow setup read model is required");
    }
    inspectSetupExposure(setup, "$setup", errors);
    const sourcePackage = deepClone(setup.source_package ?? setup.source ?? {});
    const runtime = deepClone(setup.runtime ?? {});
    const rawChapters = setup.chapters ?? sourcePackage.chapters ?? [];
    if (!Array.isArray(rawChapters)) addError(errors, "type", "$setup.chapters", "expected an array");
    const chapterIds = new Set();
    const chapters = (Array.isArray(rawChapters) ? rawChapters : []).map((row, index) => {
      const path = `$setup.chapters[${index}]`;
      if (!requireObject(row, path, errors)) return null;
      const chapterId = row.chapter_id ?? row.id;
      if (!validId(chapterId)) addError(errors, "chapter_id", `${path}.chapter_id`, "stable chapter ID is required");
      if (chapterIds.has(chapterId)) addError(errors, "duplicate_chapter", `${path}.chapter_id`, "chapter ID repeats");
      chapterIds.add(chapterId);
      return {
        chapterId,
        title: String(row.title ?? row.label ?? chapterId ?? ""),
        blockCount: Number.isInteger(row.block_count) && row.block_count >= 0 ? row.block_count : null,
        selectable: row.selectable !== false,
        selectedByDefault: row.selected === true || row.selected_by_default === true,
        order: Number.isInteger(row.order) ? row.order : index + 1,
      };
    }).filter(Boolean);
    const rawModes = setup.execution_modes ?? [{ id: "dry_run", enabled: true }, { id: "live", enabled: setup.live_start_allowed === true }];
    if (!Array.isArray(rawModes)) addError(errors, "type", "$setup.execution_modes", "expected an array");
    const executionModes = (Array.isArray(rawModes) ? rawModes : []).map((row, index) => {
      const valueRow = typeof row === "string" ? { id: row } : row;
      const id = valueRow?.id ?? valueRow?.mode;
      if (!["dry_run", "live"].includes(id)) addError(errors, "execution_mode", `$setup.execution_modes[${index}]`, "only dry_run or live may be advertised");
      return {
        id,
        label: String(valueRow?.label ?? id ?? ""),
        enabled: valueRow?.enabled !== false && (id !== "live" || setup.live_start_allowed === true),
        reason: valueRow?.reason ?? null,
      };
    });
    function optionList(valueRows, path) {
      if (!Array.isArray(valueRows)) {
        addError(errors, "type", path, "expected an array");
        return [];
      }
      return valueRows.map((row, index) => advertisedOption(row, `${path}[${index}]`, errors)).filter(Boolean);
    }
    const sharedOptions = optionList(setup.shared_options ?? setup.shared_api_options ?? [], "$setup.shared_options");
    const d2lOptions = optionList(setup.d2l_settings_options ?? setup.d2l_options ?? [], "$setup.d2l_settings_options");
    const evaluationOptions = optionList(setup.evaluation_settings_options ?? setup.evaluation_options ?? [], "$setup.evaluation_settings_options");
    evaluationOptions.forEach((option, index) => validateEvaluationOption(option, chapters.map(row => row.chapterId), `$setup.evaluation_settings_options[${index}]`, errors));
    if (!chapters.some(row => row.selectable)) addError(errors, "chapter_catalog", "$setup.chapters", "at least one selectable chapter is required");
    if (!executionModes.some(row => row.id === "dry_run" && row.enabled)) addError(errors, "dry_run_unavailable", "$setup.execution_modes", "0-API dry_run must be advertised");
    if (!sharedOptions.some(row => row.enabled)) addError(errors, "shared_option_unavailable", "$setup.shared_options", "an enabled server-advertised shared option is required");
    if (!d2lOptions.some(row => row.enabled)) addError(errors, "d2l_option_unavailable", "$setup.d2l_settings_options", "an enabled D2L settings preset is required");
    if (!evaluationOptions.some(row => row.enabled)) addError(errors, "evaluation_option_unavailable", "$setup.evaluation_settings_options", "an enabled Evaluation settings preset is required");
    return Object.freeze({
      valid: errors.length === 0,
      errors,
      schemaId: setup.schema_id ?? null,
      schemaVersion: setup.schema_version ?? null,
      projectId: setup.project_id ?? setup.doc_id ?? sourcePackage.project_id ?? null,
      sourcePackage,
      runtime,
      chapters,
      executionModes,
      sharedOptions,
      d2lOptions,
      evaluationOptions,
      defaults: deepClone(setup.defaults ?? {}),
      liveStartAllowed: setup.live_start_allowed === true,
      launchPhase: setup.launch_phase ?? "translation",
      scoringStartAllowed: setup.scoring_start_allowed === true,
      scoringBlockingReasons: Array.isArray(setup.scoring_blocking_reasons) ? [...setup.scoring_blocking_reasons] : [],
      scoringRuntime: deepClone(setup.scoring_runtime ?? {}),
      dryRunAllowed: setup.dry_run_allowed !== false,
      raw: setup,
    });
  }

  function defaultWorkflowSelection(setup) {
    const selectedChapters = setup.chapters.filter(row => row.selectable && row.selectedByDefault);
    const chapters = selectedChapters.length ? selectedChapters : setup.chapters.filter(row => row.selectable);
    const evaluationOptionId = setup.defaults.evaluation?.settings_option_id ?? setup.evaluationOptions.find(row => row.enabled)?.id ?? "";
    const evaluation = setup.evaluationOptions.find(row => row.id === evaluationOptionId && row.enabled) ?? setup.evaluationOptions.find(row => row.enabled);
    const evaluationDefaults = evaluation?.defaultSelection ?? {};
    return {
      executionMode: "dry_run",
      chapterIds: chapters.map(row => row.chapterId),
      sharedOptionId: setup.defaults.shared_option_id ?? setup.sharedOptions.find(row => row.enabled)?.id ?? "",
      d2lOptionId: setup.defaults.d2l_settings_option_id ?? setup.d2lOptions.find(row => row.enabled)?.id ?? "",
      evaluationOptionId: evaluation?.id ?? "",
      evaluationChapterIds: deepClone(evaluationDefaults.selected_chapter_ids ?? []),
      evaluationArmIds: deepClone(evaluationDefaults.selected_arm_ids ?? []),
      evaluationScorerIds: deepClone(evaluationDefaults.selected_scorer_ids ?? []),
      highlightPair: deepClone(evaluationDefaults.highlight_pair ?? null),
      hardTotalTokenCap: setup.defaults.hard_total_token_cap ?? null,
      reservedCostCapUsd: setup.defaults.reserved_cost_cap_usd ?? null,
    };
  }

  function buildWorkflowPreflightRequest(setup, selection) {
    const errors = [];
    if (!setup?.valid) {
      addError(errors, "setup_invalid", "$selection", "workflow setup is invalid");
      return { valid: false, errors, payload: null };
    }
    const mode = setup.executionModes.find(row => row.id === selection?.executionMode && row.enabled);
    if (!mode) addError(errors, "execution_mode", "$selection.execution_mode", "execution mode is not advertised or enabled");
    const selected = new Set(Array.isArray(selection?.chapterIds) ? selection.chapterIds : []);
    const selectableIds = new Set(setup.chapters.filter(row => row.selectable).map(row => row.chapterId));
    if (!selected.size) addError(errors, "chapter_selection", "$selection.chapter_ids", "select at least one chapter");
    selected.forEach(chapterId => {
      if (!selectableIds.has(chapterId)) addError(errors, "chapter_selection", "$selection.chapter_ids", `chapter is not selectable: ${chapterId}`);
    });
    const orderedChapterIds = setup.chapters.filter(row => selected.has(row.chapterId)).map(row => row.chapterId);
    const shared = setup.sharedOptions.find(row => row.id === selection?.sharedOptionId && row.enabled);
    const d2l = setup.d2lOptions.find(row => row.id === selection?.d2lOptionId && row.enabled);
    const evaluation = setup.evaluationOptions.find(row => row.id === selection?.evaluationOptionId && row.enabled);
    if (!shared) addError(errors, "shared_option", "$selection.shared_option_id", "shared option is not advertised or enabled");
    if (!d2l) addError(errors, "d2l_option", "$selection.d2l_settings_option_id", "D2L settings are not advertised or enabled");
    if (!evaluation) addError(errors, "evaluation_option", "$selection.evaluation_settings_option_id", "Evaluation settings are not advertised or enabled");
    const hardCap = selection?.hardTotalTokenCap;
    if (hardCap !== null && hardCap !== "" && (!Number.isInteger(Number(hardCap)) || Number(hardCap) <= 0)) {
      addError(errors, "token_cap", "$selection.hard_total_token_cap", "hard token cap must be a positive integer or null");
    }
    const costCap = selection?.reservedCostCapUsd;
    if (costCap !== null && costCap !== "" && (typeof Number(costCap) !== "number" || !Number.isFinite(Number(costCap)) || Number(costCap) <= 0)) {
      addError(errors, "cost_cap", "$selection.reserved_cost_cap_usd", "reserved cost cap must be a positive number or null");
    }
    const evaluationChapterOrder = Array.isArray(evaluation?.selectionCatalog?.chapter_ids)
      ? evaluation.selectionCatalog.chapter_ids
      : [];
    const evaluationChapterIds = validateOrderedSubset(selection?.evaluationChapterIds, evaluationChapterOrder, 1, "$selection.evaluation.selected_chapter_ids", errors);
    evaluationChapterIds.forEach(chapterId => {
      if (!orderedChapterIds.includes(chapterId)) addError(errors, "evaluation_chapter_scope", "$selection.evaluation.selected_chapter_ids", "Evaluation chapters must be selected for the workflow");
    });
    const selectedArms = validateOrderedSubset(selection?.evaluationArmIds, ARM_ORDER, 2, "$selection.evaluation.selected_arm_ids", errors);
    const selectedScorers = validateOrderedSubset(selection?.evaluationScorerIds, SCORER_ORDER, 1, "$selection.evaluation.selected_scorer_ids", errors);
    const pair = selection?.highlightPair ?? null;
    if (pair !== null) {
      const baseline = pair?.baseline_arm_id;
      const candidate = pair?.candidate_arm_id;
      if (!baseline || !candidate || baseline === candidate || !selectedArms.includes(baseline) || !selectedArms.includes(candidate)) {
        addError(errors, "highlight_pair", "$selection.evaluation.highlight_pair", "highlight pair must contain two distinct selected arms");
      }
    }
    return {
      valid: errors.length === 0,
      errors,
      payload: errors.length ? null : {
        schema_id: "WorkflowSetupSelectionV1",
        schema_version: "1.1.0",
        execution_mode: selection.executionMode,
        chapter_ids: orderedChapterIds,
        shared_option_id: shared.id,
        d2l_settings_option_id: d2l.id,
        evaluation: {
          settings_option_id: evaluation.id,
          selected_chapter_ids: evaluationChapterIds,
          selected_arm_ids: selectedArms,
          selected_scorer_ids: selectedScorers,
          highlight_pair: pair === null ? null : {
            baseline_arm_id: pair.baseline_arm_id,
            candidate_arm_id: pair.candidate_arm_id,
          },
        },
        hard_total_token_cap: hardCap === null || hardCap === "" ? null : Number(hardCap),
        reserved_cost_cap_usd: costCap === null || costCap === "" ? null : Number(costCap),
      },
    };
  }

  async function normalizeWorkflowPreflight(value, setup, selection) {
    const preflight = deepClone(value || null);
    const errors = [];
    if (!requireObject(preflight, "$preflight", errors)) {
      return Object.freeze({ valid: false, errors, raw: preflight });
    }
    if (typeof preflight.schema_id !== "string" || typeof preflight.schema_version !== "string") {
      addError(errors, "preflight_schema", "$preflight", "a versioned preflight read model is required");
    }
    inspectSetupExposure(preflight, "$preflight", errors);
    const serverErrors = Array.isArray(preflight.errors) ? preflight.errors : [];
    if (!Array.isArray(preflight.errors)) addError(errors, "type", "$preflight.errors", "expected an array");
    const launch = deepClone(preflight.launch ?? preflight.sealed_launch ?? {});
    const serverValid = preflight.valid === true && preflight.status === "ready";
    if (serverValid) {
      if (launch.script !== "run_workflow_orchestrator_v1") addError(errors, "launch_script", "$preflight.launch.script", "neutral workflow orchestrator is required");
      if (launch.phase !== "translation") addError(errors, "launch_phase", "$preflight.launch.phase", "Translation is the only supported initial phase");
      if (!validId(launch.preflight_id ?? preflight.preflight_id)) addError(errors, "preflight_id", "$preflight.launch.preflight_id", "sealed preflight identity is required");
      if (!SHA256_RE.test(String(launch.preflight_sha256 ?? preflight.preflight_sha256 ?? ""))) addError(errors, "sha256", "$preflight.launch.preflight_sha256", "sealed preflight SHA-256 is required");
      if (!validId(launch.planned_run_id ?? preflight.planned_run_id)) addError(errors, "planned_run_id", "$preflight.launch.planned_run_id", "planned run ID is required");
      if (typeof (launch.confirm_token ?? preflight.confirm_token) !== "string" || !(launch.confirm_token ?? preflight.confirm_token)) {
        addError(errors, "confirm_token", "$preflight.launch.confirm_token", "short-lived confirmation token is required");
      }
    }
    const requested = buildWorkflowPreflightRequest(setup, selection);
    if (!requested.valid) errors.push(...requested.errors);
    const normalizedSelection = deepClone(preflight.normalized_selection ?? null);
    const evaluationSummary = deepClone(preflight.evaluation_summary ?? null);
    if (requested.valid) {
      if (!isObject(normalizedSelection)) {
        addError(errors, "normalized_selection", "$preflight.normalized_selection", "backend-normalized selection is required");
      } else {
        exactKeys(normalizedSelection, [
          "schema_id", "schema_version", "execution_mode", "chapter_ids",
          "shared_option_id", "d2l_settings_option_id", "evaluation",
          "hard_total_token_cap", "reserved_cost_cap_usd",
        ], "$preflight.normalized_selection", errors);
        const payload = requested.payload;
        if (normalizedSelection.schema_id !== payload.schema_id || normalizedSelection.schema_version !== payload.schema_version) {
          addError(errors, "normalized_selection_schema", "$preflight.normalized_selection", "normalized selection schema differs from the request");
        }
        ["execution_mode", "chapter_ids", "shared_option_id", "d2l_settings_option_id"].forEach(key => {
          if (!canonicalEqual(normalizedSelection[key], payload[key])) {
            addError(errors, "normalized_selection_drift", `$preflight.normalized_selection.${key}`, "normalized selection differs from the sealed request");
          }
        });
        const expectedTokenCap = payload.hard_total_token_cap ?? setup.defaults.hard_total_token_cap ?? null;
        if (normalizedSelection.hard_total_token_cap !== expectedTokenCap) {
          addError(errors, "normalized_selection_drift", "$preflight.normalized_selection.hard_total_token_cap", "normalized token cap differs");
        }
        const expectedCost = payload.reserved_cost_cap_usd;
        const observedCost = normalizedSelection.reserved_cost_cap_usd;
        if (
          (expectedCost === null) !== (observedCost === null)
          || (expectedCost !== null && Number(expectedCost) !== Number(observedCost))
        ) {
          addError(errors, "normalized_selection_drift", "$preflight.normalized_selection.reserved_cost_cap_usd", "normalized cost cap differs");
        }
        const serverEvaluation = normalizedSelection.evaluation;
        if (!isObject(serverEvaluation)) {
          addError(errors, "normalized_evaluation", "$preflight.normalized_selection.evaluation", "normalized Evaluation selection is required");
        } else {
          exactKeys(serverEvaluation, [
            "settings_option_id", "selected_chapter_ids", "selected_arm_ids",
            "selected_scorer_ids", "highlight_pair", "registered_option_sha256",
            "selection_sha256",
          ], "$preflight.normalized_selection.evaluation", errors);
          const evaluationOption = setup.evaluationOptions.find(row => row.id === payload.evaluation.settings_option_id && row.enabled);
          const expectedBasis = {
            ...payload.evaluation,
            registered_option_sha256: evaluationOption?.sha256 ?? null,
          };
          const expectedSelectionSha256 = await canonicalSha256(expectedBasis);
          const expectedEvaluation = {
            ...expectedBasis,
            selection_sha256: expectedSelectionSha256,
          };
          if (!canonicalEqual(serverEvaluation, expectedEvaluation)) {
            addError(errors, "normalized_evaluation_drift", "$preflight.normalized_selection.evaluation", "normalized Evaluation selection or hash differs");
          }
        }
      }
      if (!isObject(evaluationSummary)) {
        addError(errors, "evaluation_summary", "$preflight.evaluation_summary", "Evaluation preflight summary is required");
      } else {
        const normalizedEvaluation = normalizedSelection?.evaluation;
        if (evaluationSummary.schema_id !== "EvaluationWorkflowSettingsV1" || evaluationSummary.schema_version !== "1.1.0") {
          addError(errors, "evaluation_summary_schema", "$preflight.evaluation_summary", "EvaluationWorkflowSettingsV1@1.1.0 summary is required");
        }
        if (evaluationSummary.settings_status !== "pending_scoring_handoff" || evaluationSummary.settings_sha256 !== null) {
          addError(errors, "premature_evaluation_settings", "$preflight.evaluation_summary.settings_sha256", "settings hash must remain null until the scoring handoff exists");
        }
        if (
          !isObject(normalizedEvaluation)
          || evaluationSummary.selection_sha256 !== normalizedEvaluation.selection_sha256
          || evaluationSummary.registered_option_sha256 !== normalizedEvaluation.registered_option_sha256
          || !canonicalEqual(evaluationSummary.selected_chapter_ids, normalizedEvaluation.selected_chapter_ids)
          || !canonicalEqual(evaluationSummary.selected_arm_ids, normalizedEvaluation.selected_arm_ids)
          || !canonicalEqual(evaluationSummary.selected_scorer_ids, normalizedEvaluation.selected_scorer_ids)
          || !canonicalEqual(evaluationSummary.highlight_pair, normalizedEvaluation.highlight_pair)
        ) {
          addError(errors, "evaluation_summary_drift", "$preflight.evaluation_summary", "Evaluation summary differs from normalized selection");
        }
      }
    }
    return Object.freeze({
      valid: serverValid && serverErrors.length === 0 && errors.length === 0,
      errors: [...serverErrors, ...errors],
      warnings: Array.isArray(preflight.warnings) ? preflight.warnings : [],
      liveStartAllowed: preflight.live_start_allowed === true,
      launchPhase: preflight.launch_phase ?? launch.phase ?? null,
      scoringStartAllowed: preflight.scoring_start_allowed === true,
      scoringBlockingReasons: Array.isArray(preflight.scoring_blocking_reasons) ? [...preflight.scoring_blocking_reasons] : [],
      scoringRuntime: deepClone(preflight.scoring_runtime ?? {}),
      normalizedSelection,
      sourceSummary: deepClone(preflight.source_summary ?? preflight.source_package ?? {}),
      sharedSummary: deepClone(preflight.shared_summary ?? preflight.shared_settings ?? {}),
      d2lSummary: deepClone(preflight.d2l_summary ?? preflight.d2l_settings ?? {}),
      evaluationSummary,
      bounds: deepClone(preflight.bounds ?? {}),
      identities: deepClone(preflight.identities ?? {}),
      launch: {
        script: launch.script ?? null,
        phase: launch.phase ?? null,
        preflightId: launch.preflight_id ?? preflight.preflight_id ?? null,
        preflightSha256: launch.preflight_sha256 ?? preflight.preflight_sha256 ?? null,
        confirmToken: launch.confirm_token ?? preflight.confirm_token ?? null,
        plannedRunId: launch.planned_run_id ?? preflight.planned_run_id ?? null,
        workflowRunId: launch.workflow_run_id ?? preflight.workflow_run_id ?? null,
        expiresAt: launch.expires_at ?? preflight.expires_at ?? null,
      },
      raw: preflight,
    });
  }

  async function verifyNestedHash(value, path, declared, errorCode, errorPath, errors) {
    if (!SHA256_RE.test(String(declared || ""))) {
      addError(errors, "sha256", errorPath, "expected lowercase SHA-256");
      return;
    }
    try {
      const observed = await canonicalSha256(withoutNested(value, path));
      if (observed !== declared) addError(errors, errorCode, errorPath, `expected ${declared}; observed ${observed}`);
    } catch (error) {
      addError(errors, "hash_unavailable", errorPath, String(error?.message || error));
    }
  }

  function validateManifestShape(manifest, errors) {
    const required = [
      "schema_id", "schema_version", "workflow_run_id", "flow_kind", "job_id",
      "source_package_bindings", "status", "started_at", "updated_at", "active_stage_id",
      "components", "stages", "resume", "reconstructed", "timing_authority",
      "latest_event_seq", "artifact_index_sha256", "integrity",
    ];
    exactKeys(manifest, required, "$manifest", errors);
    if (!isObject(manifest)) return;
    if (manifest.schema_id !== "WorkflowManifestV1" || manifest.schema_version !== "1.0.0") {
      addError(errors, "schema", "$manifest", "expected WorkflowManifestV1@1.0.0");
    }
    if (!validId(manifest.workflow_run_id)) addError(errors, "workflow_run_id", "$manifest.workflow_run_id", "invalid workflow ID");
    if (manifest.flow_kind !== FLOW_KIND) addError(errors, "flow_kind", "$manifest.flow_kind", `expected ${FLOW_KIND}`);
    if (!WORKFLOW_STATUSES.has(manifest.status)) addError(errors, "status", "$manifest.status", "unsupported parent status");
    if (!Number.isInteger(manifest.latest_event_seq) || manifest.latest_event_seq < 0) {
      addError(errors, "latest_event_seq", "$manifest.latest_event_seq", "expected a non-negative integer");
    }
    if (typeof manifest.reconstructed !== "boolean") addError(errors, "type", "$manifest.reconstructed", "expected boolean");
    const expectedTiming = manifest.reconstructed ? "logical_order_only" : "recorded";
    if (manifest.timing_authority !== expectedTiming) {
      addError(errors, "timing_authority", "$manifest.timing_authority", `expected ${expectedTiming}`);
    }
    if (!SHA256_RE.test(String(manifest.artifact_index_sha256 || ""))) {
      addError(errors, "sha256", "$manifest.artifact_index_sha256", "expected lowercase SHA-256");
    }
    if (!Array.isArray(manifest.stages)) addError(errors, "type", "$manifest.stages", "expected an array");
    if (!Array.isArray(manifest.components)) addError(errors, "type", "$manifest.components", "expected an array");
    if (!Array.isArray(manifest.source_package_bindings) || manifest.source_package_bindings.length !== 6) {
      addError(errors, "source_binding_exact_cover", "$manifest.source_package_bindings", "expected six ordered source-package bindings");
    }
    (Array.isArray(manifest.source_package_bindings) ? manifest.source_package_bindings : []).forEach((row, index) => {
      const path = `$manifest.source_package_bindings[${index}]`;
      exactKeys(row, ["role", "binding"], path, errors);
      if (row?.role !== SOURCE_BINDING_ROLES[index]) addError(errors, "source_binding_order", `${path}.role`, `expected ${SOURCE_BINDING_ROLES[index] || "no extra binding"}`);
      validateBinding(row?.binding, `${path}.binding`, errors);
    });
    const stageIds = new Set();
    (Array.isArray(manifest.stages) ? manifest.stages : []).forEach((stage, index) => {
      const path = `$manifest.stages[${index}]`;
      const keys = ["stage_id", "component_id", "local_stage_id", "order", "label", "producer", "status", "progress", "current_work_id", "artifact_refs"];
      exactKeys(stage, keys, path, errors);
      if (!isObject(stage)) return;
      if (stage.order !== index + 1) addError(errors, "stage_order", `${path}.order`, "stage orders must be contiguous");
      if (!COMPONENT_IDS.has(stage.component_id)) addError(errors, "component_id", `${path}.component_id`, "unknown component");
      if (typeof stage.stage_id !== "string" || !stage.stage_id.startsWith(`${stage.component_id}.`)) {
        addError(errors, "stage_namespace", `${path}.stage_id`, "stage is not component-namespaced");
      }
      if (stageIds.has(stage.stage_id)) addError(errors, "duplicate_stage", `${path}.stage_id`, "stage ID repeats");
      stageIds.add(stage.stage_id);
      if (!WORKFLOW_STATUSES.has(stage.status)) addError(errors, "stage_status", `${path}.status`, "unsupported stage status");
      if (!Array.isArray(stage.artifact_refs) || new Set(stage.artifact_refs).size !== stage.artifact_refs?.length) {
        addError(errors, "artifact_refs", `${path}.artifact_refs`, "expected unique artifact refs");
      }
      if (stage.progress !== null) inspectPrivatePayload(stage.progress, `${path}.progress`, errors);
    });
    if (manifest.active_stage_id !== null && !stageIds.has(manifest.active_stage_id)) {
      addError(errors, "active_stage", "$manifest.active_stage_id", "active stage is undeclared");
    }
    const componentIds = new Set();
    (Array.isArray(manifest.components) ? manifest.components : []).forEach((component, index) => {
      const path = `$manifest.components[${index}]`;
      const keys = ["component_id", "component_run_id", "component_attempt_id", "component_attempt_index", "status", "manifest", "last_component_seq", "terminal", "validator"];
      exactKeys(component, keys, path, errors);
      if (!isObject(component)) return;
      if (!COMPONENT_IDS.has(component.component_id)) addError(errors, "component_id", `${path}.component_id`, "unknown component");
      if (componentIds.has(component.component_id)) addError(errors, "duplicate_component", `${path}.component_id`, "component repeats");
      componentIds.add(component.component_id);
      if (!WORKFLOW_STATUSES.has(component.status)) addError(errors, "component_status", `${path}.status`, "unsupported component status");
      if (component.terminal !== ["failed", "succeeded"].includes(component.status)) {
        addError(errors, "terminal_status", `${path}.terminal`, "terminal flag and status disagree");
      }
      validateBinding(component.manifest, `${path}.manifest`, errors);
    });
    if (!requireObject(manifest.resume, "$manifest.resume", errors)) return;
    exactKeys(manifest.resume, ["available", "component_id"], "$manifest.resume", errors);
    if (typeof manifest.resume.available !== "boolean") addError(errors, "type", "$manifest.resume.available", "expected boolean");
    if (manifest.resume.component_id !== null && !COMPONENT_IDS.has(manifest.resume.component_id)) {
      addError(errors, "resume_component", "$manifest.resume.component_id", "unknown resume component");
    }
  }

  function validateArtifactIndexShape(index, manifest, events, errors) {
    const required = ["schema_id", "schema_version", "workflow_run_id", "flow_kind", "artifacts", "integrity"];
    exactKeys(index, required, "$artifact_index", errors);
    if (!isObject(index)) return [];
    if (index.schema_id !== "WorkflowArtifactIndexV1" || index.schema_version !== "1.0.0") {
      addError(errors, "schema", "$artifact_index", "expected WorkflowArtifactIndexV1@1.0.0");
    }
    if (index.workflow_run_id !== manifest?.workflow_run_id) addError(errors, "workflow_binding", "$artifact_index.workflow_run_id", "workflow ID differs from manifest");
    if (index.flow_kind !== manifest?.flow_kind) addError(errors, "flow_binding", "$artifact_index.flow_kind", "flow kind differs from manifest");
    if (!Array.isArray(index.artifacts)) {
      addError(errors, "type", "$artifact_index.artifacts", "expected an array");
      return [];
    }
    const eventIds = new Set(events.map(event => event?.event_id).filter(Boolean));
    const refs = new Set();
    const parents = new Map();
    index.artifacts.forEach((artifact, position) => {
      const path = `$artifact_index.artifacts[${position}]`;
      exactKeys(artifact, ["binding", "component_artifact_ref", "imported_physical_sha256", "parent_artifact_refs", "producer", "created_event_id"], path, errors);
      if (!isObject(artifact)) return;
      const binding = validateBinding(artifact.binding, `${path}.binding`, errors);
      const ref = binding?.artifact_ref;
      if (refs.has(ref)) addError(errors, "duplicate_artifact_ref", `${path}.binding.artifact_ref`, "artifact ref repeats");
      if (ref) refs.add(ref);
      if (!SHA256_RE.test(String(artifact.imported_physical_sha256 || ""))) addError(errors, "sha256", `${path}.imported_physical_sha256`, "expected SHA-256");
      if (!Array.isArray(artifact.parent_artifact_refs)) addError(errors, "type", `${path}.parent_artifact_refs`, "expected an array");
      parents.set(ref, Array.isArray(artifact.parent_artifact_refs) ? artifact.parent_artifact_refs : []);
      if (artifact.created_event_id !== null && !eventIds.has(artifact.created_event_id)) {
        addError(errors, "created_event", `${path}.created_event_id`, "created event is absent from parent stream");
      }
    });
    parents.forEach((parentRefs, ref) => parentRefs.forEach(parent => {
      if (!refs.has(parent)) addError(errors, "artifact_parent", `$artifact_index.artifacts.${ref}`, `unknown parent ${parent}`);
    }));
    const visiting = new Set();
    const visited = new Set();
    function visit(ref) {
      if (visiting.has(ref)) {
        addError(errors, "artifact_cycle", "$artifact_index.artifacts", `cycle at ${ref}`);
        return;
      }
      if (visited.has(ref)) return;
      visiting.add(ref);
      (parents.get(ref) || []).forEach(visit);
      visiting.delete(ref);
      visited.add(ref);
    }
    refs.forEach(visit);
    (manifest?.stages || []).forEach((stage, stageIndex) => (stage.artifact_refs || []).forEach(ref => {
      if (!refs.has(ref)) addError(errors, "stage_artifact", `$manifest.stages[${stageIndex}].artifact_refs`, `unknown artifact ${ref}`);
    }));
    return index.artifacts;
  }

  async function validateEvents(events, manifest, errors) {
    if (!Array.isArray(events)) {
      addError(errors, "type", "$events", "expected an array");
      return;
    }
    const required = ["schema_id", "schema_version", "event_id", "workflow_run_id", "flow_kind", "seq", "accepted_at", "component", "stage_id", "agent", "event", "severity", "payload", "integrity"];
    const stageIds = new Set((manifest?.stages || []).map(stage => stage.stage_id));
    const componentCursors = new Map();
    let previousHash = null;
    for (let index = 0; index < events.length; index += 1) {
      const event = events[index];
      const path = `$events[${index}]`;
      exactKeys(event, required, path, errors);
      if (!isObject(event)) continue;
      if (event.schema_id !== "WorkflowEventV1" || event.schema_version !== "1.0.0") addError(errors, "schema", path, "expected WorkflowEventV1@1.0.0");
      if (event.seq !== index + 1) addError(errors, "event_seq", `${path}.seq`, `expected ${index + 1}`);
      if (event.event_id !== `workflow_event_${String(index + 1).padStart(8, "0")}`) addError(errors, "event_id", `${path}.event_id`, "event ID does not bind global seq");
      if (event.workflow_run_id !== manifest?.workflow_run_id) addError(errors, "workflow_binding", `${path}.workflow_run_id`, "workflow ID differs from manifest");
      if (event.flow_kind !== manifest?.flow_kind) addError(errors, "flow_binding", `${path}.flow_kind`, "flow kind differs from manifest");
      if (event.stage_id !== null && !stageIds.has(event.stage_id)) addError(errors, "event_stage", `${path}.stage_id`, "event stage is undeclared");
      if (!isObject(event.component)) {
        addError(errors, "type", `${path}.component`, "expected an object");
      } else {
        const component = event.component;
        const componentPath = `${path}.component`;
        exactKeys(component, [
          "component_id", "component_run_id", "component_attempt_id", "component_attempt_index",
          "component_seq", "source_event_id", "source_event_sha256", "source_event_sha256_kind",
          "validator_id", "validator_revision",
        ], componentPath, errors);
        if (!COMPONENT_IDS.has(component.component_id)) addError(errors, "component_id", `${path}.component.component_id`, "unknown component");
        if (!validId(component.component_run_id)) addError(errors, "component_run_id", `${componentPath}.component_run_id`, "invalid component run ID");

        const attemptIndex = component.component_attempt_index;
        const attemptId = component.component_attempt_id;
        const validAttemptIndex = Number.isInteger(attemptIndex) && attemptIndex >= 1;
        const validAttemptId = (Number.isInteger(attemptId) && attemptId >= 1) || validId(attemptId);
        if (!validAttemptIndex) addError(errors, "attempt_identity", `${componentPath}.component_attempt_index`, "expected a positive integer");
        if (!validAttemptId) addError(errors, "attempt_identity", `${componentPath}.component_attempt_id`, "expected a positive integer or stable identifier");
        if (Number.isInteger(attemptId) && validAttemptIndex && attemptId !== attemptIndex) {
          addError(errors, "attempt_identity", componentPath, "numeric attempt ID must equal attempt index");
        }

        const cursorKey = `${component.component_id}:${component.component_run_id}`;
        let cursor = componentCursors.get(cursorKey);
        if (!cursor) {
          cursor = { lastSeq: 0, attemptIndex: null, attemptId: null, attemptIds: new Map() };
          componentCursors.set(cursorKey, cursor);
        }
        const expectedComponentSeq = cursor.lastSeq + 1;
        if (component.component_seq !== expectedComponentSeq) {
          addError(errors, "component_sequence", `${componentPath}.component_seq`, `expected ${expectedComponentSeq}`);
        }
        if (Number.isInteger(component.component_seq) && component.component_seq >= 1) cursor.lastSeq = component.component_seq;

        if (validAttemptIndex && validAttemptId) {
          const knownAttemptId = cursor.attemptIds.get(attemptIndex);
          if (knownAttemptId !== undefined && knownAttemptId !== attemptId) {
            addError(errors, "attempt_identity", componentPath, "one attempt index has multiple attempt IDs");
          } else if (knownAttemptId === undefined) {
            cursor.attemptIds.set(attemptIndex, attemptId);
          }

          let transitionAllowed = true;
          if (cursor.attemptIndex === null) {
            if (attemptIndex !== 1) {
              addError(errors, "attempt_lineage", componentPath, "component event stream must begin at attempt 1");
              transitionAllowed = false;
            }
          } else if (attemptIndex < cursor.attemptIndex) {
            addError(errors, "attempt_lineage", componentPath, "component attempt cannot reopen or regress");
            transitionAllowed = false;
          } else if (attemptIndex > cursor.attemptIndex + 1) {
            addError(errors, "attempt_lineage", componentPath, "component attempt sequence must be contiguous");
            transitionAllowed = false;
          } else if (attemptIndex === cursor.attemptIndex + 1 && !RESUME_EVENTS.has(event.event)) {
            addError(errors, "attempt_lineage", path, "new attempt must begin with an explicit resume event");
            transitionAllowed = false;
          }
          if (attemptIndex === cursor.attemptIndex && cursor.attemptId !== null && cursor.attemptId !== attemptId) {
            addError(errors, "attempt_identity", componentPath, "attempt ID changed within one attempt index");
            transitionAllowed = false;
          }
          if (transitionAllowed) {
            cursor.attemptIndex = attemptIndex;
            cursor.attemptId = attemptId;
          }
        }
      }
      inspectPrivatePayload(event.payload, `${path}.payload`, errors);
      if (!isObject(event.integrity)) {
        addError(errors, "type", `${path}.integrity`, "expected an object");
      } else {
        if (event.integrity.previous_event_sha256 !== previousHash) addError(errors, "event_chain", `${path}.integrity.previous_event_sha256`, "previous event hash drift");
        await verifyNestedHash(event, ["integrity", "event_sha256"], event.integrity.event_sha256, "event_hash", `${path}.integrity.event_sha256`, errors);
        previousHash = event.integrity.event_sha256;
      }
    }
    if (Number(manifest?.latest_event_seq) !== events.length) {
      addError(errors, "incomplete_replay", "$events", `manifest declares ${manifest?.latest_event_seq}; received ${events.length}`);
    }
    const manifestComponents = new Map((manifest?.components || []).map(component => [
      `${component.component_id}:${component.component_run_id}`,
      component,
    ]));
    componentCursors.forEach((cursor, cursorKey) => {
      const component = manifestComponents.get(cursorKey);
      if (!component) {
        addError(errors, "component_binding", "$manifest.components", `event stream component ${cursorKey} is absent from manifest`);
        return;
      }
      if (component.last_component_seq !== cursor.lastSeq) {
        addError(errors, "component_sequence", "$manifest.components", `${cursorKey} declares last_component_seq ${component.last_component_seq}; observed ${cursor.lastSeq}`);
      }
      if (component.component_attempt_index !== cursor.attemptIndex || component.component_attempt_id !== cursor.attemptId) {
        addError(errors, "attempt_identity", "$manifest.components", `${cursorKey} current attempt differs from event lineage`);
      }
    });
    manifestComponents.forEach((component, cursorKey) => {
      if (!componentCursors.has(cursorKey)) addError(errors, "component_binding", "$events", `manifest component ${cursorKey} has no event evidence`);
    });
  }

  async function validateScoringArtifacts(artifactRows, bodies, validatedArtifacts, manifest, errors) {
    const byKind = new Map();
    artifactRows.forEach(row => {
      const kind = row?.binding?.artifact_kind;
      if (kind) {
        const bucket = byKind.get(kind) || [];
        bucket.push(row);
        byKind.set(kind, bucket);
      }
    });
    const handoffRow = (byKind.get("scoring_handoff_v1") || [])[0] || null;
    const receiptRow = (byKind.get("scoring_receipt_v1") || [])[0] || null;
    const handoff = handoffRow ? bodies?.[handoffRow.binding.artifact_ref] : null;
    const receipt = receiptRow ? bodies?.[receiptRow.binding.artifact_ref] : null;
    const scoring = {
      handoff: handoffRow ? handoffRow.binding : null,
      receipt: receiptRow ? receiptRow.binding : null,
      receiptStatus: receipt?.status ?? null,
      inputSetSha256: handoff?.input_set_sha256 ?? null,
      arms: [],
      reports: [],
    };
    if (handoffRow && !handoff) addError(errors, "artifact_body_missing", `$artifacts.${handoffRow.binding.artifact_ref}`, "scoring handoff body is required");
    if (receiptRow && !receipt) addError(errors, "artifact_body_missing", `$artifacts.${receiptRow.binding.artifact_ref}`, "scoring receipt body is required");
    if (receiptRow && !handoffRow) addError(errors, "handoff_missing", "$artifact_index", "scoring receipt lacks its handoff");

    if (handoff) {
      const inputs = Array.isArray(handoff.translation_inputs) ? handoff.translation_inputs : [];
      const arms = inputs.map(row => row?.arm_id);
      if (JSON.stringify(arms) !== JSON.stringify(ARM_ORDER)) addError(errors, "arm_order", "$handoff.translation_inputs", `expected ${ARM_ORDER.join(", ")}`);
      if (handoff.workflow_run_id !== manifest.workflow_run_id || handoff.flow_kind !== manifest.flow_kind) addError(errors, "handoff_binding", "$handoff", "handoff workflow identity drift");
      const handoffRoles = Array.isArray(handoff.source_package_bindings) ? handoff.source_package_bindings.map(row => row?.role) : [];
      if (JSON.stringify(handoffRoles) !== JSON.stringify(SOURCE_BINDING_ROLES)) addError(errors, "source_binding_order", "$handoff.source_package_bindings", "handoff source bindings are incomplete or out of order");
      if (canonicalJSONString(handoff.source_package_bindings || []) !== canonicalJSONString(manifest.source_package_bindings || [])) addError(errors, "source_binding_drift", "$handoff.source_package_bindings", "handoff source bindings differ from parent manifest");
      const admittedBinding = (handoff.source_package_bindings || []).find(row => row?.role === "admitted_projection")?.binding || null;
      const universe = inputs[0]?.coverage ? [inputs[0].coverage.block_universe_sha256, inputs[0].coverage.expected_block_count] : null;
      const artifactRefs = new Set();
      inputs.forEach((input, index) => {
        const path = `$handoff.translation_inputs[${index}]`;
        if (canonicalJSONString(input?.source_binding || null) !== canonicalJSONString(admittedBinding)) addError(errors, "source_binding_drift", `${path}.source_binding`, "arm source differs from admitted projection");
        if (universe && (input?.coverage?.block_universe_sha256 !== universe[0] || input?.coverage?.expected_block_count !== universe[1])) addError(errors, "coverage_universe_drift", `${path}.coverage`, "arms do not cover one admitted universe");
        const coverage = input?.coverage || {};
        const accounted = ["translated_block_count", "preserved_block_count", "excluded_block_count", "review_held_block_count", "missing_block_count", "failed_block_count"]
          .reduce((sum, key) => sum + (Number.isInteger(coverage[key]) ? coverage[key] : 0), 0);
        if (!Number.isInteger(coverage.expected_block_count) || accounted !== coverage.expected_block_count) addError(errors, "coverage_accounting", `${path}.coverage`, "coverage does not exact-cover expected blocks");
        const producer = input?.producer?.component_id;
        if (["s0", "s1"].includes(input?.arm_id) ? producer !== "translation" : producer === "translation") addError(errors, "producer_authority", `${path}.producer.component_id`, "arm producer authority is invalid");
        const ref = input?.translation_artifact?.artifact_ref;
        if (!ref || artifactRefs.has(ref)) addError(errors, "duplicate_artifact_ref", `${path}.translation_artifact.artifact_ref`, "translation artifact refs must be unique");
        artifactRefs.add(ref);
      });
      await verifyNestedHash(handoff, ["integrity", "handoff_sha256"], handoff?.integrity?.handoff_sha256, "handoff_hash", "$handoff.integrity.handoff_sha256", errors);
      const inputHash = await canonicalSha256({ translation_inputs: handoff.translation_inputs || [] });
      if (inputHash !== handoff.input_set_sha256) addError(errors, "input_set_hash", "$handoff.input_set_sha256", "translation input set drift");
      if (handoffRow && handoffRow.binding.sha256 !== handoff?.integrity?.handoff_sha256) addError(errors, "artifact_binding", "$handoff.integrity.handoff_sha256", "handoff hash differs from artifact index");
      scoring.arms = Array.isArray(handoff.translation_inputs) ? handoff.translation_inputs.map(row => ({
        arm_id: row.arm_id,
        translation_artifact: row.translation_artifact || null,
        producer: row.producer || null,
        coverage: row.coverage || null,
      })) : [];
    }
    if (receipt) {
      if (receipt.workflow_run_id !== manifest.workflow_run_id || receipt.flow_kind !== manifest.flow_kind) addError(errors, "receipt_binding", "$receipt", "receipt workflow identity drift");
      await verifyNestedHash(receipt, ["integrity", "receipt_sha256"], receipt?.integrity?.receipt_sha256, "receipt_hash", "$receipt.integrity.receipt_sha256", errors);
      if (receiptRow && receiptRow.binding.sha256 !== receipt?.integrity?.receipt_sha256) addError(errors, "artifact_binding", "$receipt.integrity.receipt_sha256", "receipt hash differs from artifact index");
      if (handoff) {
        if (receipt.accepted_input_set_sha256 !== handoff.input_set_sha256) addError(errors, "handoff_echo", "$receipt.accepted_input_set_sha256", "receipt input-set hash differs from handoff");
        if (canonicalJSONString(receipt.accepted_translation_inputs || []) !== canonicalJSONString(handoff.translation_inputs || [])) addError(errors, "handoff_echo", "$receipt.accepted_translation_inputs", "receipt arms differ from handoff");
        if (receipt?.scoring_handoff?.sha256 !== handoff?.integrity?.handoff_sha256 || receipt?.scoring_handoff?.artifact_ref !== handoffRow?.binding?.artifact_ref) {
          addError(errors, "handoff_binding", "$receipt.scoring_handoff", "receipt does not bind the exact handoff");
        }
      }
    }

    const reportRows = artifactRows.filter(row => {
      const kind = String(row?.binding?.artifact_kind || "");
      return row?.producer?.component_id === "evaluation" && (kind === "full_run_report_v1" || kind === "benchmark_run_report_v1" || kind === "evaluation_report_v1");
    });
    reportRows.forEach(row => {
      const ref = row.binding.artifact_ref;
      const validation = validatedArtifacts?.[ref];
      scoring.reports.push({ ...row.binding, valid: validation?.valid === true });
      if (!validation || validation.valid !== true || validation.sha256 !== row.binding.sha256) {
        addError(errors, "report_invalid", `$validated_artifacts.${ref}`, "Evaluation report lacks a matching backend validation receipt");
      }
    });
    const evaluationSucceeded = (manifest.stages || []).some(stage => stage.component_id === "evaluation" && stage.status === "succeeded")
      || (manifest.components || []).some(component => component.component_id === "evaluation" && component.status === "succeeded");
    if (evaluationSucceeded && (!receiptRow || !reportRows.length)) {
      addError(errors, "evaluation_artifact_incomplete", "$artifact_index", "succeeded Evaluation requires scoring receipt and validated report");
    }
    return scoring;
  }

  async function validateEvaluationScope(value, scoring, manifest, errors) {
    if (value === null || value === undefined) return null;
    exactKeys(value, [
      "schema_id", "schema_version", "settings_option_id",
      "registered_option_sha256", "selection_sha256",
      "selected_chapter_ids", "selected_arm_ids", "selected_scorer_ids",
      "highlight_pair", "scoring_handoff_status", "settings_status",
      "settings_sha256",
    ], "$evaluation_scope", errors);
    if (!isObject(value)) return null;
    if (value.schema_id !== "EvaluationWorkflowScopeReadV1" || value.schema_version !== "1.0.0") {
      addError(errors, "evaluation_scope_schema", "$evaluation_scope", "EvaluationWorkflowScopeReadV1@1.0.0 is required");
    }
    if (!SHA256_RE.test(String(value.registered_option_sha256 || ""))) addError(errors, "sha256", "$evaluation_scope.registered_option_sha256", "registered option SHA-256 is required");
    if (!SHA256_RE.test(String(value.selection_sha256 || ""))) addError(errors, "sha256", "$evaluation_scope.selection_sha256", "selection SHA-256 is required");
    const parentEvaluationChapterOrder = (manifest?.stages || [])
      .filter(stage => stage?.component_id === "evaluation" && String(stage?.local_stage_id || "").startsWith("chapter_"))
      .map(stage => String(stage.local_stage_id).slice("chapter_".length));
    validateOrderedSubset(
      value.selected_chapter_ids,
      parentEvaluationChapterOrder,
      1,
      "$evaluation_scope.selected_chapter_ids",
      errors,
    );
    validateOrderedSubset(value.selected_arm_ids, ARM_ORDER, 2, "$evaluation_scope.selected_arm_ids", errors);
    validateOrderedSubset(value.selected_scorer_ids, SCORER_ORDER, 1, "$evaluation_scope.selected_scorer_ids", errors);
    const pair = value.highlight_pair;
    if (pair !== null && (!isObject(pair) || pair.baseline_arm_id === pair.candidate_arm_id || !value.selected_arm_ids?.includes(pair.baseline_arm_id) || !value.selected_arm_ids?.includes(pair.candidate_arm_id))) {
      addError(errors, "highlight_pair", "$evaluation_scope.highlight_pair", "highlight pair must contain two selected arms");
    }
    const selectionBasis = {
      settings_option_id: value.settings_option_id,
      selected_chapter_ids: value.selected_chapter_ids,
      selected_arm_ids: value.selected_arm_ids,
      selected_scorer_ids: value.selected_scorer_ids,
      highlight_pair: value.highlight_pair,
      registered_option_sha256: value.registered_option_sha256,
    };
    try {
      if (await canonicalSha256(selectionBasis) !== value.selection_sha256) {
        addError(errors, "evaluation_selection_hash", "$evaluation_scope.selection_sha256", "Evaluation selection hash drift");
      }
    } catch (_error) {
      addError(errors, "evaluation_selection_hash", "$evaluation_scope.selection_sha256", "Evaluation selection cannot be hashed");
    }
    const handoffReady = scoring?.handoff !== null;
    if ((value.scoring_handoff_status === "validated") !== handoffReady) {
      addError(errors, "evaluation_handoff_status", "$evaluation_scope.scoring_handoff_status", "handoff status differs from validated parent artifacts");
    }
    if (value.settings_status === "pending_scoring_handoff") {
      if (handoffReady || value.settings_sha256 !== null) addError(errors, "premature_evaluation_settings", "$evaluation_scope", "pending handoff requires null settings hash and no handoff");
    } else if (value.settings_status === "pending_settings_materialization") {
      if (!handoffReady || value.settings_sha256 !== null) addError(errors, "evaluation_settings_status", "$evaluation_scope", "pending materialization requires a handoff and null settings hash");
    } else if (value.settings_status === "materialized") {
      if (!handoffReady || !SHA256_RE.test(String(value.settings_sha256 || ""))) addError(errors, "evaluation_settings_status", "$evaluation_scope", "materialized settings require a handoff and settings SHA-256");
    } else {
      addError(errors, "evaluation_settings_status", "$evaluation_scope.settings_status", "unsupported Evaluation settings status");
    }
    return deepClone(value);
  }

  function evaluationScoreReadiness(evaluationScope, scoring) {
    const blockingReasons = [];
    if (!evaluationScope) {
      blockingReasons.push("evaluation_scope_missing");
    } else {
      if (evaluationScope.scoring_handoff_status !== "validated" || !scoring?.handoff) {
        blockingReasons.push("scoring_handoff_not_validated");
      }
      if (evaluationScope.settings_status !== "materialized") {
        blockingReasons.push("evaluation_settings_not_materialized");
      }
      if (!SHA256_RE.test(String(evaluationScope.settings_sha256 || ""))) {
        blockingReasons.push("evaluation_settings_sha256_missing");
      }
    }
    return Object.freeze({
      allowed: blockingReasons.length === 0,
      blockingReasons: [...new Set(blockingReasons)],
    });
  }

  function buildStagePlan(stages) {
    let phase = 0;
    let previousComponent = null;
    return stages.map((stage, index) => {
      if (stage.component_id !== previousComponent) phase += 1;
      previousComponent = stage.component_id;
      const next = stages[index + 1];
      return {
        id: stage.stage_id,
        label: stage.label,
        phase,
        phaseEnd: Boolean(next && next.component_id !== stage.component_id),
        componentId: stage.component_id,
        producer: stage.producer,
        status: stage.status,
        progress: stage.progress,
        currentWorkId: stage.current_work_id,
        artifactRefs: stage.artifact_refs,
      };
    });
  }

  function projectEvents(events) {
    return events.map(event => {
      const payload = event.payload || {};
      const flatProgress = ["completed", "total", "unit"].every(key => Object.prototype.hasOwnProperty.call(payload, key))
        ? { completed: payload.completed, total: payload.total, unit: payload.unit }
        : null;
      return {
        ...event,
        ts: event.accepted_at,
        stage: event.stage_id || "",
        attempt_id: event.component?.component_attempt_id ?? null,
        attempt_index: event.component?.component_attempt_index ?? null,
        persistedProgress: isObject(payload.progress) ? deepClone(payload.progress) : flatProgress,
        currentWorkId: payload.current_work_id ?? payload.work_id ?? null,
        optionalDetails: projectOptionalDetails(payload),
      };
    });
  }

  async function validatePackage(input) {
    const manifest = deepClone(input?.manifest || null);
    const artifactIndex = deepClone(input?.artifactIndex || null);
    const parentEvents = deepClone(input?.events || []);
    const artifactBodies = deepClone(input?.artifacts || {});
    const validatedArtifacts = deepClone(input?.validatedArtifacts || {});
    const errors = deepClone(input?.transportErrors || []);

    validateManifestShape(manifest, errors);
    if (isObject(manifest)) {
      await verifyNestedHash(manifest, ["integrity", "manifest_sha256"], manifest?.integrity?.manifest_sha256, "manifest_hash", "$manifest.integrity.manifest_sha256", errors);
    }
    await validateEvents(parentEvents, manifest, errors);
    validateOptionalDetails(parentEvents, errors);
    const artifactRows = validateArtifactIndexShape(artifactIndex, manifest, parentEvents, errors);
    if (isObject(artifactIndex)) {
      await verifyNestedHash(artifactIndex, ["integrity", "artifact_index_sha256"], artifactIndex?.integrity?.artifact_index_sha256, "artifact_index_hash", "$artifact_index.integrity.artifact_index_sha256", errors);
      if (manifest?.artifact_index_sha256 !== artifactIndex?.integrity?.artifact_index_sha256) addError(errors, "artifact_index_binding", "$manifest.artifact_index_sha256", "manifest and artifact index hash differ");
    }
    const scoring = isObject(manifest)
      ? await validateScoringArtifacts(artifactRows, artifactBodies, validatedArtifacts, manifest, errors)
      : { handoff: null, receipt: null, receiptStatus: null, inputSetSha256: null, arms: [], reports: [] };
    const evaluationScope = await validateEvaluationScope(deepClone(input?.evaluationScope), scoring, manifest, errors);
    const usage = validateUsageReadModel(deepClone(input?.usage), manifest, errors);
    const cursor = deepClone(input?.cursor || null);
    if (cursor !== null) {
      if (!isObject(cursor)) {
        addError(errors, "cursor", "$cursor", "expected an object or null");
      } else {
        const throughSeq = cursor.through_seq ?? cursor.latest_seq ?? cursor.latest_event_seq;
        if (throughSeq !== undefined && throughSeq !== parentEvents.length) {
          addError(errors, "cursor_sequence", "$cursor.through_seq", `expected ${parentEvents.length}`);
        }
        const chainHead = cursor.event_chain_head_sha256;
        const observedHead = parentEvents.length ? parentEvents[parentEvents.length - 1]?.integrity?.event_sha256 : null;
        if (chainHead !== undefined && chainHead !== observedHead) {
          addError(errors, "cursor_chain", "$cursor.event_chain_head_sha256", "cursor does not bind the accepted event-chain head");
        }
        if (cursor.package_revision_sha256 !== undefined && !SHA256_RE.test(String(cursor.package_revision_sha256 || ""))) {
          addError(errors, "sha256", "$cursor.package_revision_sha256", "expected lowercase SHA-256");
        }
      }
    }
    const actions = deepClone(input?.actions || {});
    inspectPrivatePayload(actions, "$actions", errors);
    const operationalFacts = parentEvents.filter(event => containsOperationalFact(event?.payload)).map(event => ({
      seq: event.seq,
      stage_id: event.stage_id,
      event: event.event,
      payload: event.payload,
    }));
    const checkpoints = parentEvents.filter(event => event?.event === "checkpoint" || Object.prototype.hasOwnProperty.call(event?.payload || {}, "checkpoint"));
    const valid = errors.length === 0;
    const scoreReadiness = valid
      ? evaluationScoreReadiness(evaluationScope, scoring)
      : Object.freeze({ allowed: false, blockingReasons: ["workflow_replay_invalid"] });
    return Object.freeze({
      contract: "workflow_replay_ui_projection_v1",
      valid,
      errors,
      sourceMode: input?.sourceMode === "live" ? "live" : "replay",
      manifest,
      artifactIndex,
      events: valid ? projectEvents(parentEvents) : [],
      stagePlan: valid ? buildStagePlan(manifest?.stages || []) : [],
      artifacts: valid ? artifactRows : [],
      scoring: valid ? scoring : { handoff: null, receipt: null, receiptStatus: null, inputSetSha256: null, arms: [], reports: [] },
      evaluationScope: valid ? evaluationScope : null,
      scoreReadiness,
      usage: valid ? usage : { present: usage.present, calls: [], stageTotals: [], componentTotals: [], workflowTotal: null },
      operationalFacts: valid ? operationalFacts : [],
      latestCheckpoint: valid && checkpoints.length ? checkpoints[checkpoints.length - 1] : null,
      cursor: valid ? cursor : null,
      actions: valid ? actions : {},
      artifactLinks: valid ? deepClone(input?.artifactLinks || {}) : {},
    });
  }

  function replayEnvelopePackage(envelope) {
    return {
      manifest: deepClone(envelope?.manifest || null),
      events: deepClone(envelope?.events || []),
      artifactIndex: deepClone(envelope?.artifact_index || envelope?.artifactIndex || null),
      artifacts: deepClone(envelope?.artifacts || envelope?.artifact_bodies || {}),
      validatedArtifacts: deepClone(envelope?.validated_artifacts || envelope?.validatedArtifacts || {}),
      usage: deepClone(envelope?.usage || null),
      cursor: deepClone(envelope?.cursor || null),
      actions: deepClone(envelope?.actions || {}),
      evaluationScope: deepClone(envelope?.evaluation_scope || envelope?.evaluationScope || null),
      artifactLinks: deepClone(envelope?.artifact_links || envelope?.artifactLinks || {}),
      sourceMode: envelope?.source_mode === "replay" ? "replay" : "live",
    };
  }

  async function mergeReplayEnvelope(previousPackage, envelope) {
    const previous = previousPackage ? deepClone(previousPackage) : null;
    const incoming = replayEnvelopePackage(envelope);
    const errors = [];
    const mergedEvents = Array.isArray(previous?.events) ? previous.events : [];
    const deltaEvents = Array.isArray(incoming.events) ? incoming.events : [];
    if (!Array.isArray(incoming.events)) addError(errors, "type", "$transport.events", "expected an array");
    for (const event of deltaEvents) {
      const seq = event?.seq;
      if (!Number.isInteger(seq) || seq < 1) {
        addError(errors, "event_seq", "$transport.events", "incoming event lacks a positive sequence");
        continue;
      }
      if (seq <= mergedEvents.length) {
        if (canonicalJSONString(mergedEvents[seq - 1]) !== canonicalJSONString(event)) {
          addError(errors, "event_conflict", `$transport.events[seq=${seq}]`, "accepted event bytes changed");
        }
        continue;
      }
      if (seq !== mergedEvents.length + 1) {
        addError(errors, "event_gap", `$transport.events[seq=${seq}]`, `expected ${mergedEvents.length + 1}`);
        continue;
      }
      mergedEvents.push(event);
    }
    const merged = {
      manifest: incoming.manifest || previous?.manifest || null,
      events: mergedEvents,
      artifactIndex: incoming.artifactIndex || previous?.artifactIndex || null,
      artifacts: { ...(previous?.artifacts || {}), ...(incoming.artifacts || {}) },
      validatedArtifacts: { ...(previous?.validatedArtifacts || {}), ...(incoming.validatedArtifacts || {}) },
      usage: incoming.usage || previous?.usage || null,
      cursor: incoming.cursor || previous?.cursor || null,
      actions: incoming.actions || previous?.actions || {},
      evaluationScope: incoming.evaluationScope || previous?.evaluationScope || null,
      artifactLinks: { ...(previous?.artifactLinks || {}), ...(incoming.artifactLinks || {}) },
      sourceMode: incoming.sourceMode || previous?.sourceMode || "live",
    };
    const model = await validatePackage({ ...merged, transportErrors: errors });
    return { package: merged, model };
  }

  async function fetchJson(url) {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}: ${url}`);
    return response.json();
  }

  async function fetchJsonLines(url) {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}: ${url}`);
    const text = await response.text();
    return text.split(/\r?\n/).filter(line => line.trim()).map((line, index) => {
      try { return JSON.parse(line); }
      catch (error) { throw new Error(`invalid JSONL row ${index + 1}: ${error.message}`); }
    });
  }

  global.WorkflowReplayAdapter = Object.freeze({
    ARM_ORDER,
    canonicalJSONString,
    canonicalSha256,
    normalizeWorkflowSetup,
    defaultWorkflowSelection,
    buildWorkflowPreflightRequest,
    normalizeWorkflowPreflight,
    validatePackage,
    mergeReplayEnvelope,
    fetchJson,
    fetchJsonLines,
  });
})(window);
