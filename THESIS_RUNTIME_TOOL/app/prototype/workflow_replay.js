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
  const COMPONENT_IDS = new Set(["translation", "evaluation", "publication"]);
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
        const cursorKey = `${component.component_id}:${component.component_run_id}:${component.component_attempt_index}`;
        const expectedComponentSeq = Number(componentCursors.get(cursorKey) || 0) + 1;
        if (component.component_seq !== expectedComponentSeq) addError(errors, "component_seq", `${path}.component.component_seq`, `expected ${expectedComponentSeq}`);
        componentCursors.set(cursorKey, component.component_seq);
        if (!COMPONENT_IDS.has(component.component_id)) addError(errors, "component_id", `${path}.component.component_id`, "unknown component");
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
    return events.map(event => ({
      ...event,
      ts: event.accepted_at,
      stage: event.stage_id || "",
      attempt_id: event.component?.component_attempt_id ?? null,
    }));
  }

  async function validatePackage(input) {
    const manifest = deepClone(input?.manifest || null);
    const artifactIndex = deepClone(input?.artifactIndex || null);
    const parentEvents = deepClone(input?.events || []);
    const artifactBodies = deepClone(input?.artifacts || {});
    const validatedArtifacts = deepClone(input?.validatedArtifacts || {});
    const errors = [];

    validateManifestShape(manifest, errors);
    if (isObject(manifest)) {
      await verifyNestedHash(manifest, ["integrity", "manifest_sha256"], manifest?.integrity?.manifest_sha256, "manifest_hash", "$manifest.integrity.manifest_sha256", errors);
    }
    await validateEvents(parentEvents, manifest, errors);
    const artifactRows = validateArtifactIndexShape(artifactIndex, manifest, parentEvents, errors);
    if (isObject(artifactIndex)) {
      await verifyNestedHash(artifactIndex, ["integrity", "artifact_index_sha256"], artifactIndex?.integrity?.artifact_index_sha256, "artifact_index_hash", "$artifact_index.integrity.artifact_index_sha256", errors);
      if (manifest?.artifact_index_sha256 !== artifactIndex?.integrity?.artifact_index_sha256) addError(errors, "artifact_index_binding", "$manifest.artifact_index_sha256", "manifest and artifact index hash differ");
    }
    const scoring = isObject(manifest)
      ? await validateScoringArtifacts(artifactRows, artifactBodies, validatedArtifacts, manifest, errors)
      : { handoff: null, receipt: null, receiptStatus: null, inputSetSha256: null, arms: [], reports: [] };
    const operationalFacts = parentEvents.filter(event => containsOperationalFact(event?.payload)).map(event => ({
      seq: event.seq,
      stage_id: event.stage_id,
      event: event.event,
      payload: event.payload,
    }));
    const checkpoints = parentEvents.filter(event => event?.event === "checkpoint" || Object.prototype.hasOwnProperty.call(event?.payload || {}, "checkpoint"));
    const valid = errors.length === 0;
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
      operationalFacts: valid ? operationalFacts : [],
      latestCheckpoint: valid && checkpoints.length ? checkpoints[checkpoints.length - 1] : null,
    });
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
    validatePackage,
    fetchJson,
    fetchJsonLines,
  });
})(window);
