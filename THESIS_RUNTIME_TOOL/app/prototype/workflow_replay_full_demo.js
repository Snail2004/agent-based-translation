/* Full-fidelity 0-API Workflow Replay demo.
 *
 * This file builds an in-memory, production-shaped parent package for visual
 * QA. It never calls a provider, writes a run package, or claims live evidence.
 * All facts are explicitly synthetic and are validated by the same
 * WorkflowReplayAdapter used by the production Console.
 */
(function installWorkflowReplayFullDemo(global) {
  "use strict";

  const FLOW_KIND = "translation_evaluation_publication";
  const WORKFLOW_RUN_ID = "workflow_demo_5chapter_complete_v1";
  const JOB_ID = "d2l_run-5-chapter_demo";
  const TRANSLATION_RUN_ID = "translation_demo_run_1";
  const EVALUATION_RUN_ID = "evaluation_demo_run_1";
  const PUBLICATION_RUN_ID = "publication_demo_run_1";
  const BASE_TIME_MS = Date.parse("2026-07-23T01:00:00Z");
  const CHAPTER_IDS = Object.freeze([
    "d2l_preliminaries",
    "d2l_linear_networks",
    "d2l_multilayer_perceptrons",
    "d2l_deep_learning_computation",
    "d2l_convolutional_neural_networks",
  ]);

  const REFS = Object.freeze({
    candidateIndex: `components/translation/${TRANSLATION_RUN_ID}/artifacts/candidate_index.json`,
    glossary: `components/translation/${TRANSLATION_RUN_ID}/artifacts/glossary_seal.json`,
    s0: `components/translation/${TRANSLATION_RUN_ID}/artifacts/d2l/s0.json`,
    s1: `components/translation/${TRANSLATION_RUN_ID}/artifacts/d2l/s1.json`,
    community: "baselines/community.json",
    googleNmt: "baselines/google_nmt.json",
    llmLc: "baselines/llm_lc.json",
    quality: `components/translation/${TRANSLATION_RUN_ID}/artifacts/translation_quality.json`,
    memoryDelta: `components/translation/${TRANSLATION_RUN_ID}/artifacts/memory_delta.json`,
    overlay: `components/translation/${TRANSLATION_RUN_ID}/artifacts/canonical_translation_overlay.json`,
    handoff: "handoffs/scoring_handoff.json",
    receipt: "handoffs/scoring_receipt.json",
    settings: `components/evaluation/${EVALUATION_RUN_ID}/evaluation_workflow_settings.json`,
    report: `components/evaluation/${EVALUATION_RUN_ID}/reports/full_run_report.json`,
    docx: `components/publication/${PUBLICATION_RUN_ID}/outputs/d2l_5chapter_vi.docx`,
    pdf: `components/publication/${PUBLICATION_RUN_ID}/outputs/d2l_5chapter_vi.pdf`,
  });

  const TRANSLATION_STAGES = Object.freeze([
    { local: "preflight", label: "Preflight", producer: "preflight", total: 12, unit: "checks" },
    { local: "b1_candidate_discovery", label: "B1 Candidate Discovery", producer: "b1_candidate_discovery", total: 5, unit: "chapters", refs: [REFS.candidateIndex] },
    { local: "candidate_index", label: "Candidate Index", producer: "candidate_index", total: 312, unit: "candidates", refs: [REFS.candidateIndex] },
    { local: "b2_admission_translation", label: "B2 Admission & Translation", producer: "b2_admission_translation", total: 2355, unit: "blocks" },
    { local: "auditor_morphology", label: "Morphology Auditor", producer: "auditor_morphology", total: 312, unit: "terms" },
    { local: "auditor_target_collision", label: "Target Collision Auditor", producer: "auditor_target_collision", total: 312, unit: "terms" },
    { local: "auditor_multi_target", label: "Multi-target Auditor", producer: "auditor_multi_target", total: 312, unit: "terms" },
    { local: "glossary_seal", label: "Glossary Seal", producer: "glossary_seal", total: 312, unit: "terms", refs: [REFS.glossary] },
    { local: "translator", label: "Translator S0 + S1", producer: "translator", total: 2355, unit: "blocks", refs: [REFS.s0, REFS.s1, REFS.overlay] },
    { local: "translation_quality_audit", label: "Translation Quality Audit", producer: "translation_quality_audit", total: 2355, unit: "blocks", refs: [REFS.quality, REFS.memoryDelta] },
    { local: "scoring_handoff_fragment", label: "Scoring Handoff", producer: "scoring_handoff_fragment", total: 5, unit: "arms", refs: [REFS.handoff] },
  ]);

  const EVALUATION_STAGES = Object.freeze([
    { local: "preflight", label: "Evaluation Preflight", producer: "evaluation_preflight", total: 9, unit: "checks", refs: [REFS.receipt, REFS.settings] },
    ...CHAPTER_IDS.map((chapterId, index) => ({
      local: `chapter_${chapterId}`,
      label: `Score ${index + 1}/5 · ${chapterId.replace(/^d2l_/, "").replaceAll("_", " ")}`,
      producer: "evaluation_chapter_runner",
      total: 5,
      unit: "arms",
    })),
    { local: "aggregation", label: "Aggregate & Final Report", producer: "evaluation_aggregator", total: 3, unit: "scorers", refs: [REFS.report] },
  ]);

  const PUBLICATION_STAGES = Object.freeze([
    { local: "export", label: "Publish DOCX + PDF", producer: "publisher", total: 2, unit: "outputs", refs: [REFS.docx, REFS.pdf] },
  ]);

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function timestampFor(seq) {
    return new Date(BASE_TIME_MS + (seq * 19000)).toISOString();
  }

  function stageRow(componentId, definition, order) {
    return {
      stage_id: `${componentId}.${definition.local}`,
      component_id: componentId,
      local_stage_id: definition.local,
      order,
      label: definition.label,
      producer: definition.producer,
      status: "succeeded",
      progress: { completed: definition.total, total: definition.total, unit: definition.unit },
      current_work_id: null,
      artifact_refs: [...(definition.refs || [])],
    };
  }

  function typedDetail(componentId, kind, data) {
    return {
      schema_id: "WorkflowOptionalDetailV1",
      schema_version: "1.0.0",
      component_id: componentId,
      kind,
      data,
    };
  }

  async function resealEvents(adapter, events) {
    let previous = null;
    for (const event of events) {
      event.integrity.previous_event_sha256 = previous;
      delete event.integrity.event_sha256;
      event.integrity.event_sha256 = await adapter.canonicalSha256(event);
      previous = event.integrity.event_sha256;
    }
  }

  async function prepareScoringArtifacts(adapter, baseHandoff, baseReceipt) {
    const handoff = clone(baseHandoff);
    handoff.workflow_run_id = WORKFLOW_RUN_ID;
    handoff.handoff_id = "handoff_demo_5chapter_v1";
    handoff.created_at = "2026-07-23T01:13:30Z";
    handoff.translation_inputs.forEach(row => {
      row.coverage = {
        block_universe_sha256: "5e0d6b49b38bb6bad417e196381658adc0aa01d7aa10135b944604874dda01c8",
        expected_block_count: 2355,
        translated_block_count: 2118,
        preserved_block_count: 216,
        excluded_block_count: 21,
        review_held_block_count: 0,
        missing_block_count: 0,
        failed_block_count: 0,
      };
      if (row.arm_id === "s0") {
        row.producer.component_run_id = TRANSLATION_RUN_ID;
        row.translation_artifact.artifact_ref = REFS.s0;
      } else if (row.arm_id === "s1") {
        row.producer.component_run_id = TRANSLATION_RUN_ID;
        row.translation_artifact.artifact_ref = REFS.s1;
      } else if (row.arm_id === "community") {
        row.translation_artifact.artifact_ref = REFS.community;
      } else if (row.arm_id === "google_nmt") {
        row.translation_artifact.artifact_ref = REFS.googleNmt;
      } else if (row.arm_id === "llm_lc") {
        row.translation_artifact.artifact_ref = REFS.llmLc;
      }
    });
    handoff.input_set_sha256 = await adapter.canonicalSha256({
      translation_inputs: handoff.translation_inputs,
    });
    handoff.integrity = {};
    handoff.integrity.handoff_sha256 = await adapter.canonicalSha256(handoff);

    const receipt = clone(baseReceipt);
    receipt.workflow_run_id = WORKFLOW_RUN_ID;
    receipt.accepted_at = "2026-07-23T01:13:49Z";
    receipt.accepted_input_set_sha256 = handoff.input_set_sha256;
    receipt.accepted_translation_inputs = clone(handoff.translation_inputs);
    receipt.evaluation_component_attempt_id = "evaluation_demo_attempt_1";
    receipt.evaluation_component_run_id = EVALUATION_RUN_ID;
    receipt.scoring_handoff.artifact_ref = REFS.handoff;
    receipt.scoring_handoff.sha256 = handoff.integrity.handoff_sha256;
    receipt.integrity = {};
    receipt.integrity.receipt_sha256 = await adapter.canonicalSha256(receipt);
    return { handoff, receipt };
  }

  async function buildArtifactHashes(adapter, scoring) {
    const hashes = {};
    for (const [key, ref] of Object.entries(REFS)) {
      hashes[key] = await adapter.canonicalSha256({ demo: true, artifact_ref: ref });
    }
    hashes.handoff = scoring.handoff.integrity.handoff_sha256;
    hashes.receipt = scoring.receipt.integrity.receipt_sha256;
    hashes.report = await adapter.canonicalSha256({
      schema_id: "FullRunReportV1",
      workflow_run_id: WORKFLOW_RUN_ID,
      selected_chapter_ids: CHAPTER_IDS,
      verdict: "PASS",
      accepted_arm_ids: ["s0", "s1", "community", "google_nmt", "llm_lc"],
    });
    return hashes;
  }

  async function buildEvents(adapter, hashes) {
    const events = [];
    const componentSeq = new Map();
    const createdEvents = {};
    const stageDoneEvents = {};
    const runIds = {
      translation: TRANSLATION_RUN_ID,
      evaluation: EVALUATION_RUN_ID,
      publication: PUBLICATION_RUN_ID,
    };

    async function push(componentId, stageId, agent, eventName, payload = {}, severity = "info") {
      const seq = events.length + 1;
      const localSeq = (componentSeq.get(componentId) || 0) + 1;
      componentSeq.set(componentId, localSeq);
      const sourceEventId = `evt_${componentId}_demo_${String(localSeq).padStart(8, "0")}`;
      const event = {
        schema_id: "WorkflowEventV1",
        schema_version: "1.0.0",
        event_id: `workflow_event_${String(seq).padStart(8, "0")}`,
        workflow_run_id: WORKFLOW_RUN_ID,
        flow_kind: FLOW_KIND,
        seq,
        accepted_at: timestampFor(seq),
        component: {
          component_id: componentId,
          component_run_id: runIds[componentId],
          component_attempt_id: 1,
          component_attempt_index: 1,
          component_seq: localSeq,
          source_event_id: sourceEventId,
          source_event_sha256: await adapter.canonicalSha256({ source_event_id: sourceEventId, payload }),
          source_event_sha256_kind: "physical",
          validator_id: `${componentId}.component.validator_v1`,
          validator_revision: "v1",
        },
        stage_id: stageId,
        agent,
        event: eventName,
        severity,
        payload,
        integrity: { previous_event_sha256: null, event_sha256: "" },
      };
      events.push(event);
      return event;
    }

    async function artifactEvent(componentId, stageId, agent, ref, kind, hashKey) {
      const event = await push(componentId, stageId, agent, "artifact_created", {
        artifact_ref: ref,
        artifact_kind: kind,
        sha256: hashes[hashKey],
        sha256_kind: "physical",
      });
      createdEvents[ref] = event.event_id;
      return event;
    }

    await push("translation", null, "d2l_orchestrator", "component_started", {
      selected_chapter_ids: CHAPTER_IDS,
      stage_count: TRANSLATION_STAGES.length,
    });
    for (const definition of TRANSLATION_STAGES) {
      const stageId = `translation.${definition.local}`;
      await push("translation", stageId, definition.producer, "stage_started", {
        current_work_id: `work_${definition.local}`,
        progress: { completed: 0, total: definition.total, unit: definition.unit },
      });
      if (definition.local === "translator") {
        await push("translation", stageId, definition.producer, "request_sent", {
          logical_request_id: "usage_d2l_s1_request",
          work_id: "s1_packet_01",
          physical_attempt_index: 1,
        });
        await push("translation", stageId, definition.producer, "transport_attempt_failed", {
          attempt_usage_id: "usage_d2l_s1_transport_failed_01",
          logical_request_id: "usage_d2l_s1_request",
          semantic_attempt_index: 2,
          transport_retry_ordinal: 0,
          physical_attempt_index: 1,
          work_kind: "translation_packet",
          work_id: "s1_packet_01",
          provider_id: "provider_primary_v1",
          model_id: "provider_translation_model_v1",
          source_id: "shared_source_primary_v1",
          source_revision: "source-rev-demo-1",
          masked_quota_bucket: "quota_bucket_primary",
          latency_ms: 30000,
          prompt_tokens: null,
          completion_tokens: null,
          cached_input_tokens: null,
          reasoning_tokens: null,
          total_tokens: null,
          cost_usd: null,
          cost_status: "unknown",
          reason_code: "timeout",
          retry_class: "transport",
          retry_disposition: "retryable",
        }, "warning");
        await push("translation", stageId, definition.producer, "retry", {
          retry_kind: "transport",
          index: 1,
          max: 2,
          reason_code: "timeout",
          logical_request_id: "usage_d2l_s1_request",
          work_kind: "translation_packet",
          work_id: "s1_packet_01",
        }, "warning");
        await push("translation", stageId, definition.producer, "request_sent", {
          logical_request_id: "usage_d2l_s1_request",
          work_id: "s1_packet_01",
          physical_attempt_index: 2,
        });
        await push("translation", stageId, definition.producer, "response_received", {
          logical_request_id: "usage_d2l_s1_request",
          work_id: "s1_packet_01",
          attempt_usage_id: "usage_d2l_s1",
          physical_attempt_index: 2,
        });
        await push("translation", stageId, definition.producer, "retry_summary", {
          logical_request_id: "usage_d2l_s1_request",
          retry_kind: "transport",
          retry_count: 1,
          outcome: "recovered",
          work_id: "s1_packet_01",
          reason_codes: ["timeout"],
        });
        await push("translation", stageId, definition.producer, "retry", {
          retry_kind: "semantic",
          index: 1,
          max: 1,
          reason_code: "terminology_guard_repair",
          logical_request_id: "usage_d2l_s1_request",
          work_kind: "translation_packet",
          work_id: "s1_packet_01",
        }, "warning");
      }
      const optionalDetails = [];
      if (definition.local === "b2_admission_translation") {
        optionalDetails.push(typedDetail("translation", "d2l_admission", {
          admitted_block_count: 2355,
          held_block_count: 0,
          selected_chapter_count: 5,
          policy_revision: "d2l_admission_v1",
        }));
      }
      if (definition.local === "auditor_multi_target") {
        optionalDetails.push(typedDetail("translation", "d2l_protected_spans", {
          protected_span_count: 118,
          validation_status: "passed",
        }));
      }
      if (definition.local === "glossary_seal") {
        optionalDetails.push(typedDetail("translation", "d2l_glossary", {
          artifact_ref: REFS.glossary,
          status: "sealed",
          term_count: 312,
        }));
      }
      if (definition.local === "translator") {
        optionalDetails.push(typedDetail("translation", "d2l_s0_s1", {
          s0_artifact_ref: REFS.s0,
          s1_artifact_ref: REFS.s1,
          translated_block_count: 2118,
          preserved_block_count: 216,
          excluded_block_count: 21,
        }));
      }
      if (definition.local === "translation_quality_audit") {
        optionalDetails.push(
          typedDetail("translation", "d2l_quality", {
            audit_status: "passed",
            warning_count: 3,
            blocking_issue_count: 0,
          }),
          typedDetail("translation", "d2l_memory", {
            glossary_revision: "glossary_seal_v1",
            committed_delta_count: 27,
            memory_delta_ref: REFS.memoryDelta,
          }),
        );
      }
      const complete = await push("translation", stageId, definition.producer, "stage_completed", {
        progress: { completed: definition.total, total: definition.total, unit: definition.unit },
        ...(optionalDetails.length ? { optional_details: optionalDetails } : {}),
      });
      stageDoneEvents[stageId] = complete.event_id;
      if (definition.local === "glossary_seal") {
        await artifactEvent("translation", stageId, definition.producer, REFS.glossary, "glossary_seal_v1", "glossary");
      } else if (definition.local === "translator") {
        await artifactEvent("translation", stageId, definition.producer, REFS.s0, "translation_artifact", "s0");
        await artifactEvent("translation", stageId, definition.producer, REFS.s1, "translation_artifact", "s1");
        await artifactEvent("translation", stageId, definition.producer, REFS.overlay, "canonical_translation_overlay_v1", "overlay");
      } else if (definition.local === "scoring_handoff_fragment") {
        await artifactEvent("translation", stageId, definition.producer, REFS.handoff, "scoring_handoff_v1", "handoff");
      }
    }
    const translationCheckpoint = await push("translation", "translation.scoring_handoff_fragment", "d2l_orchestrator", "checkpoint", {
      checkpoint: "translation_ready_for_evaluation",
      checkpoint_ref: `components/translation/${TRANSLATION_RUN_ID}/checkpoints/translation_ready.json`,
      checkpoint_sha256: await adapter.canonicalSha256({ checkpoint: "translation_ready_for_evaluation" }),
    });
    createdEvents[REFS.memoryDelta] = translationCheckpoint.event_id;
    await push("translation", null, "d2l_orchestrator", "component_done", {
      outcome: "succeeded",
      artifact_ref: REFS.handoff,
    });

    await push("evaluation", null, "evaluation_runner", "component_started", {
      selected_chapter_ids: CHAPTER_IDS,
      selected_arm_ids: ["s0", "s1", "community", "google_nmt", "llm_lc"],
      selected_scorer_ids: ["sf_qe", "sf_bt", "pj"],
      stage_count: EVALUATION_STAGES.length,
    });
    await artifactEvent("evaluation", "evaluation.preflight", "evaluation_preflight", REFS.receipt, "scoring_receipt_v1", "receipt");
    for (const definition of EVALUATION_STAGES) {
      const stageId = `evaluation.${definition.local}`;
      await push("evaluation", stageId, definition.producer, "stage_started", {
        current_work_id: `evaluation_${definition.local}`,
        progress: { completed: 0, total: definition.total, unit: definition.unit },
      });
      const optionalDetails = [];
      if (definition.local.startsWith("chapter_")) {
        optionalDetails.push(typedDetail("evaluation", "evaluation_progress", {
          completed_chapter_id: definition.local.replace(/^chapter_/, ""),
          completed_arms: 5,
          total_arms: 5,
        }));
      }
      if (definition.local === "aggregation") {
        optionalDetails.push(
          typedDetail("evaluation", "evaluation_metrics", {
            report_ref: REFS.report,
            status: "final",
            metrics: {
              s0: { sf_qe: 0.812, sf_bt: 0.791, pj: 0.824, tc: 0.778, ta: 0.747 },
              s1: { sf_qe: 0.846, sf_bt: 0.818, pj: 0.851, tc: 1.0, ta: 0.705, ta_registry: 0.848 },
            },
          }),
          typedDetail("evaluation", "evaluation_aggregation", {
            aggregation_status: "accepted",
            accepted_arm_count: 5,
            accepted_scorer_count: 3,
          }),
          typedDetail("evaluation", "evaluation_verdict", {
            verdict: "PASS",
            gates_passed: 7,
            gates_total: 7,
          }),
        );
      }
      const complete = await push("evaluation", stageId, definition.producer, "stage_completed", {
        progress: { completed: definition.total, total: definition.total, unit: definition.unit },
        ...(optionalDetails.length ? { optional_details: optionalDetails } : {}),
      });
      stageDoneEvents[stageId] = complete.event_id;
      if (definition.local === "aggregation") {
        await artifactEvent("evaluation", stageId, definition.producer, REFS.report, "full_run_report_v1", "report");
      }
    }
    const evaluationCheckpoint = await push("evaluation", "evaluation.aggregation", "evaluation_runner", "checkpoint", {
      checkpoint: "evaluation_complete",
      checkpoint_ref: `components/evaluation/${EVALUATION_RUN_ID}/checkpoints/evaluation_complete.json`,
      checkpoint_sha256: await adapter.canonicalSha256({ checkpoint: "evaluation_complete" }),
    });
    createdEvents[REFS.settings] = stageDoneEvents["evaluation.preflight"];
    await push("evaluation", null, "evaluation_runner", "component_done", {
      outcome: "succeeded",
      artifact_ref: REFS.report,
    });

    await push("publication", null, "publication_runner", "component_started", {
      input_overlay_ref: REFS.overlay,
      output_count: 2,
    });
    const publicationStageId = "publication.export";
    await push("publication", publicationStageId, "publisher", "stage_started", {
      current_work_id: "publication_outputs",
      progress: { completed: 0, total: 2, unit: "outputs" },
    });
    await artifactEvent("publication", publicationStageId, "publisher", REFS.docx, "publication_docx_v1", "docx");
    await artifactEvent("publication", publicationStageId, "publisher", REFS.pdf, "publication_pdf_v1", "pdf");
    const publicationComplete = await push("publication", publicationStageId, "publisher", "stage_completed", {
      progress: { completed: 2, total: 2, unit: "outputs" },
      optional_details: [
        typedDetail("publication", "publication_outputs", {
          publication_id: "publication_demo_v1",
          source_package_overwritten: false,
          outputs: [
            { format: "docx", artifact_ref: REFS.docx },
            { format: "pdf", artifact_ref: REFS.pdf },
          ],
        }),
      ],
    });
    stageDoneEvents[publicationStageId] = publicationComplete.event_id;
    await push("publication", null, "publication_runner", "component_done", {
      outcome: "succeeded",
      publication_id: "publication_demo_v1",
    });

    await resealEvents(adapter, events);
    return { events, componentSeq, createdEvents, stageDoneEvents, evaluationCheckpoint };
  }

  function usageTotal(overrides = {}) {
    return {
      snapshot_seq: 55,
      accepted_through_component_seq: 1,
      physical_call_count: 0,
      cache_observation_count: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      reasoning_tokens: 0,
      cached_input_tokens: 0,
      total_tokens: 0,
      cache_hit_count: 0,
      cache_miss_count: 0,
      unknown_attempt_count: 0,
      cost_status: "recorded",
      cost_usd: 0,
      currency: "USD",
      snapshot_binding: { authority: "producer_sealed" },
      snapshot_sha256: "a".repeat(64),
      ...overrides,
    };
  }

  function usageCall(spec) {
    return {
      attempt_usage_id: spec.id,
      component_id: spec.component,
      component_run_id: spec.component === "translation" ? TRANSLATION_RUN_ID : EVALUATION_RUN_ID,
      component_attempt_id: 1,
      component_attempt_index: 1,
      component_seq: spec.componentSeq,
      stage_id: spec.stage,
      agent: spec.agent,
      work_id: spec.workId,
      logical_request_id: spec.logicalRequestId || `${spec.id}_request`,
      semantic_attempt_index: spec.semanticAttempt || 1,
      transport_retry_ordinal: spec.transportRetryOrdinal ?? 0,
      physical_attempt_index: spec.physicalAttemptIndex ?? 1,
      provider_id: "provider_primary_v1",
      source_id: "shared_source_primary_v1",
      source_revision: "source-rev-demo-1",
      requested_model_id: spec.component === "translation" ? "translation_model_alias_v1" : "evaluation_model_alias_v1",
      observed_model_id: spec.component === "translation" ? "provider_translation_model_v1" : "provider_evaluation_model_v1",
      prompt_tokens: spec.prompt,
      completion_tokens: spec.completion,
      reasoning_tokens: spec.reasoning ?? null,
      cached_input_tokens: spec.cached || 0,
      total_tokens: spec.total,
      cache_status: spec.cacheStatus || "miss",
      cache_mechanism: spec.cacheStatus === "hit" ? "provider_prompt_cache" : "none",
      provider_call_avoided: false,
      latency_ms: spec.latency,
      finish_reason: "stop",
      outcome: "accepted",
      cost_status: "recorded",
      cost_usd: spec.cost,
      currency: "USD",
      usage_binding: { authority: "producer_sealed" },
    };
  }

  function failedTransportUsage(spec) {
    return {
      ...usageCall({
        ...spec,
        prompt: null,
        completion: null,
        cached: null,
        total: null,
        latency: spec.latency,
        cost: null,
      }),
      reasoning_tokens: null,
      cached_input_tokens: null,
      cache_status: "unknown",
      cache_mechanism: "unknown",
      finish_reason: null,
      outcome: "failed",
      cost_status: "unknown",
      cost_usd: null,
    };
  }

  function buildUsage(manifest, eventCount) {
    const calls = [
      usageCall({ id: "usage_d2l_b1", component: "translation", componentSeq: 4, stage: "translation.b1_candidate_discovery", agent: "b1_candidate_discovery", workId: "b1_chapters", prompt: 1800, completion: 420, total: 2220, latency: 1840, cost: 0.012 }),
      usageCall({ id: "usage_d2l_b2", component: "translation", componentSeq: 8, stage: "translation.b2_admission_translation", agent: "b2_admission_translation", workId: "admission_packets", prompt: 9200, completion: 1800, total: 11000, latency: 4260, cost: 0.058 }),
      usageCall({ id: "usage_d2l_s0", component: "translation", componentSeq: 20, stage: "translation.translator", agent: "translator", workId: "s0_packet_01", prompt: 18400, completion: 5900, total: 24300, latency: 11840, cost: 0.124 }),
      failedTransportUsage({ id: "usage_d2l_s1_transport_failed_01", component: "translation", componentSeq: 23, stage: "translation.translator", agent: "translator", workId: "s1_packet_01", logicalRequestId: "usage_d2l_s1_request", semanticAttempt: 2, transportRetryOrdinal: 0, physicalAttemptIndex: 1, latency: 30000 }),
      usageCall({ id: "usage_d2l_s1", component: "translation", componentSeq: 26, stage: "translation.translator", agent: "translator", workId: "s1_packet_01", logicalRequestId: "usage_d2l_s1_request", prompt: 18100, completion: 6100, cached: 4800, total: 24200, cacheStatus: "hit", semanticAttempt: 2, transportRetryOrdinal: 1, physicalAttemptIndex: 2, latency: 10960, cost: 0.126 }),
      usageCall({ id: "usage_eval_sfbt_reverse", component: "evaluation", componentSeq: 7, stage: "evaluation.chapter_d2l_linear_networks", agent: "sf_bt_runner", workId: "sf_bt_reverse", prompt: 6200, completion: 1800, total: 8000, latency: 5340, cost: 0.045 }),
      usageCall({ id: "usage_eval_sfbt_judge", component: "evaluation", componentSeq: 10, stage: "evaluation.chapter_d2l_multilayer_perceptrons", agent: "sf_bt_runner", workId: "sf_bt_semantic", prompt: 4200, completion: 700, reasoning: 300, total: 5200, latency: 4710, cost: 0.032 }),
      usageCall({ id: "usage_eval_pj_canonical", component: "evaluation", componentSeq: 13, stage: "evaluation.chapter_d2l_deep_learning_computation", agent: "pj_runner", workId: "pj_canonical", prompt: 3100, completion: 450, total: 3550, latency: 2930, cost: 0.022 }),
      usageCall({ id: "usage_eval_pj_reversed", component: "evaluation", componentSeq: 16, stage: "evaluation.chapter_d2l_convolutional_neural_networks", agent: "pj_runner", workId: "pj_reversed", prompt: 3200, completion: 470, total: 3670, latency: 3060, cost: 0.023 }),
      {
        cache_observation_id: "cache_d2l_translation_packet_02",
        component_id: "translation",
        component_run_id: TRANSLATION_RUN_ID,
        component_attempt_id: 1,
        component_attempt_index: 1,
        component_seq: 22,
        stage_id: "translation.translator",
        agent: "translator",
        work_id: "s1_packet_02",
        logical_request_id: "translation_packet_02",
        physical_attempt_index: null,
        provider_id: "provider_primary_v1",
        source_id: "shared_source_primary_v1",
        requested_model_id: "translation_model_alias_v1",
        observed_model_id: null,
        prompt_tokens: null,
        completion_tokens: null,
        reasoning_tokens: null,
        cached_input_tokens: null,
        total_tokens: null,
        cache_status: "hit",
        cache_mechanism: "local_exact_cache",
        provider_call_avoided: true,
        latency_ms: 4,
        finish_reason: null,
        outcome: "cache_hit",
        cost_status: "unknown",
        cost_usd: null,
        currency: "USD",
        usage_binding: { authority: "producer_sealed" },
      },
    ];
    const totals = {
      workflow: { calls: 9, cache: 1, prompt: 64200, completion: 17640, reasoning: 300, cached: 4800, tokens: 82140, hits: 2, misses: 7, unknown: 1, costStatus: "unknown", cost: null },
      translation: { calls: 5, cache: 1, prompt: 47500, completion: 14220, reasoning: 0, cached: 4800, tokens: 61720, hits: 2, misses: 3, unknown: 1, costStatus: "unknown", cost: null },
      evaluation: { calls: 4, cache: 0, prompt: 16700, completion: 3420, reasoning: 300, cached: 0, tokens: 20420, hits: 0, misses: 4, cost: 0.122 },
    };
    function sealedTotal(id, row, extra = {}) {
      return usageTotal({
        snapshot_seq: eventCount,
        accepted_through_component_seq: extra.acceptedSeq || 1,
        physical_call_count: row.calls,
        cache_observation_count: row.cache,
        prompt_tokens: row.prompt,
        completion_tokens: row.completion,
        reasoning_tokens: row.reasoning,
        cached_input_tokens: row.cached,
        total_tokens: row.tokens,
        cache_hit_count: row.hits,
        cache_miss_count: row.misses,
        unknown_attempt_count: row.unknown || 0,
        cost_status: row.costStatus || "recorded",
        cost_usd: row.cost,
        snapshot_sha256: extra.hashChar.repeat(64),
        ...id,
      });
    }
    return {
      schema_id: "WorkflowUsageReadModelV1",
      schema_version: "1.0.0",
      workflow_run_id: manifest.workflow_run_id,
      validated: true,
      validation: { valid: true, validator_id: "neutral_relay.usage.validator_v1" },
      calls,
      stage_totals: [
        sealedTotal({ component_id: "translation", component_run_id: TRANSLATION_RUN_ID, stage_id: "translation.b1_candidate_discovery" }, { calls: 1, cache: 0, prompt: 1800, completion: 420, reasoning: 0, cached: 0, tokens: 2220, hits: 0, misses: 1, cost: 0.012 }, { acceptedSeq: 4, hashChar: "1" }),
        sealedTotal({ component_id: "translation", component_run_id: TRANSLATION_RUN_ID, stage_id: "translation.b2_admission_translation" }, { calls: 1, cache: 0, prompt: 9200, completion: 1800, reasoning: 0, cached: 0, tokens: 11000, hits: 0, misses: 1, cost: 0.058 }, { acceptedSeq: 8, hashChar: "2" }),
        sealedTotal({ component_id: "translation", component_run_id: TRANSLATION_RUN_ID, stage_id: "translation.translator" }, { calls: 3, cache: 1, prompt: 36500, completion: 12000, reasoning: 0, cached: 4800, tokens: 48500, hits: 2, misses: 1, unknown: 1, costStatus: "unknown", cost: null }, { acceptedSeq: 28, hashChar: "3" }),
        sealedTotal({ component_id: "evaluation", component_run_id: EVALUATION_RUN_ID, stage_id: "evaluation.chapter_d2l_linear_networks" }, { calls: 2, cache: 0, prompt: 10400, completion: 2500, reasoning: 300, cached: 0, tokens: 13200, hits: 0, misses: 2, cost: 0.077 }, { acceptedSeq: 10, hashChar: "4" }),
        sealedTotal({ component_id: "evaluation", component_run_id: EVALUATION_RUN_ID, stage_id: "evaluation.chapter_d2l_deep_learning_computation" }, { calls: 2, cache: 0, prompt: 6300, completion: 920, reasoning: 0, cached: 0, tokens: 7220, hits: 0, misses: 2, cost: 0.045 }, { acceptedSeq: 16, hashChar: "5" }),
      ],
      component_totals: [
        sealedTotal({ component_id: "translation", component_run_id: TRANSLATION_RUN_ID }, totals.translation, { acceptedSeq: 31, hashChar: "6" }),
        sealedTotal({ component_id: "evaluation", component_run_id: EVALUATION_RUN_ID }, totals.evaluation, { acceptedSeq: 18, hashChar: "7" }),
      ],
      workflow_total: sealedTotal(
        { component_id: null, component_run_id: null },
        totals.workflow,
        { acceptedSeq: eventCount, hashChar: "8" },
      ),
    };
  }

  async function artifactRow(adapter, spec) {
    return {
      binding: {
        artifact_kind: spec.kind,
        artifact_ref: spec.ref,
        schema_version: spec.schemaVersion || "1.0.0",
        sha256: spec.sha,
        sha256_kind: spec.shaKind || "physical",
      },
      component_artifact_ref: spec.componentRef || spec.ref,
      imported_physical_sha256: await adapter.canonicalSha256({ imported_artifact_ref: spec.ref, sha256: spec.sha }),
      parent_artifact_refs: [...(spec.parents || [])],
      producer: {
        component_attempt_id: spec.attemptId ?? 1,
        component_attempt_index: spec.attemptIndex ?? 1,
        component_id: spec.componentId,
        component_run_id: spec.componentRunId,
        stage_id: spec.stageId,
      },
      created_event_id: spec.createdEventId ?? null,
    };
  }

  async function buildArtifactIndex(adapter, hashes, scoring, eventState) {
    const translatedParentRefs = [REFS.s0, REFS.s1, REFS.community, REFS.googleNmt, REFS.llmLc];
    const rows = [];
    async function add(spec) {
      rows.push(await artifactRow(adapter, spec));
    }
    await add({ kind: "candidate_index_v1", ref: REFS.candidateIndex, sha: hashes.candidateIndex, componentId: "translation", componentRunId: TRANSLATION_RUN_ID, stageId: "translation.candidate_index", createdEventId: eventState.stageDoneEvents["translation.candidate_index"] });
    await add({ kind: "glossary_seal_v1", ref: REFS.glossary, sha: hashes.glossary, componentId: "translation", componentRunId: TRANSLATION_RUN_ID, stageId: "translation.glossary_seal", createdEventId: eventState.createdEvents[REFS.glossary] });
    await add({ kind: "translation_artifact", ref: REFS.s0, sha: scoring.handoff.translation_inputs[0].translation_artifact.sha256, componentId: "translation", componentRunId: TRANSLATION_RUN_ID, stageId: "translation.translator", createdEventId: eventState.createdEvents[REFS.s0] });
    await add({ kind: "translation_artifact", ref: REFS.s1, sha: scoring.handoff.translation_inputs[1].translation_artifact.sha256, componentId: "translation", componentRunId: TRANSLATION_RUN_ID, stageId: "translation.translator", createdEventId: eventState.createdEvents[REFS.s1] });
    await add({ kind: "translation_artifact", ref: REFS.community, sha: scoring.handoff.translation_inputs[2].translation_artifact.sha256, componentId: "neutral_relay", componentRunId: WORKFLOW_RUN_ID, stageId: "relay.baseline_import", attemptId: null, attemptIndex: null, createdEventId: eventState.stageDoneEvents["translation.scoring_handoff_fragment"] });
    await add({ kind: "translation_artifact", ref: REFS.googleNmt, sha: scoring.handoff.translation_inputs[3].translation_artifact.sha256, componentId: "neutral_relay", componentRunId: WORKFLOW_RUN_ID, stageId: "relay.baseline_import", attemptId: null, attemptIndex: null, createdEventId: eventState.stageDoneEvents["translation.scoring_handoff_fragment"] });
    await add({ kind: "translation_artifact", ref: REFS.llmLc, sha: scoring.handoff.translation_inputs[4].translation_artifact.sha256, componentId: "neutral_relay", componentRunId: WORKFLOW_RUN_ID, stageId: "relay.baseline_import", attemptId: null, attemptIndex: null, createdEventId: eventState.stageDoneEvents["translation.scoring_handoff_fragment"] });
    await add({ kind: "translation_quality_report_v1", ref: REFS.quality, sha: hashes.quality, parents: [REFS.s0, REFS.s1], componentId: "translation", componentRunId: TRANSLATION_RUN_ID, stageId: "translation.translation_quality_audit", createdEventId: eventState.stageDoneEvents["translation.translation_quality_audit"] });
    await add({ kind: "memory_delta_v1", ref: REFS.memoryDelta, sha: hashes.memoryDelta, parents: [REFS.glossary], componentId: "translation", componentRunId: TRANSLATION_RUN_ID, stageId: "translation.translation_quality_audit", createdEventId: eventState.createdEvents[REFS.memoryDelta] });
    await add({ kind: "canonical_translation_overlay_v1", ref: REFS.overlay, sha: hashes.overlay, parents: [REFS.s1], componentId: "translation", componentRunId: TRANSLATION_RUN_ID, stageId: "translation.translator", createdEventId: eventState.createdEvents[REFS.overlay] });
    await add({ kind: "scoring_handoff_v1", ref: REFS.handoff, sha: hashes.handoff, shaKind: "canonical:ScoringHandoffV1@1.0.0", parents: translatedParentRefs, componentId: "neutral_relay", componentRunId: WORKFLOW_RUN_ID, stageId: "relay.scoring_handoff", attemptId: null, attemptIndex: null, createdEventId: eventState.createdEvents[REFS.handoff] });
    await add({ kind: "scoring_receipt_v1", ref: REFS.receipt, sha: hashes.receipt, shaKind: "canonical:ScoringReceiptV1@1.0.0", parents: [REFS.handoff], componentId: "evaluation", componentRunId: EVALUATION_RUN_ID, stageId: "evaluation.preflight", createdEventId: eventState.createdEvents[REFS.receipt] });
    await add({ kind: "evaluation_workflow_settings_v1", ref: REFS.settings, sha: hashes.settings, parents: [REFS.handoff], componentId: "evaluation", componentRunId: EVALUATION_RUN_ID, stageId: "evaluation.preflight", createdEventId: eventState.createdEvents[REFS.settings] });
    await add({ kind: "full_run_report_v1", ref: REFS.report, sha: hashes.report, shaKind: "canonical:FullRunReportV1@1.0.0", parents: [REFS.receipt, REFS.settings], componentId: "evaluation", componentRunId: EVALUATION_RUN_ID, stageId: "evaluation.aggregation", createdEventId: eventState.createdEvents[REFS.report] });
    await add({ kind: "publication_docx_v1", ref: REFS.docx, sha: hashes.docx, parents: [REFS.overlay], componentId: "publication", componentRunId: PUBLICATION_RUN_ID, stageId: "publication.export", createdEventId: eventState.createdEvents[REFS.docx] });
    await add({ kind: "publication_pdf_v1", ref: REFS.pdf, sha: hashes.pdf, parents: [REFS.overlay], componentId: "publication", componentRunId: PUBLICATION_RUN_ID, stageId: "publication.export", createdEventId: eventState.createdEvents[REFS.pdf] });
    const index = {
      schema_id: "WorkflowArtifactIndexV1",
      schema_version: "1.0.0",
      workflow_run_id: WORKFLOW_RUN_ID,
      flow_kind: FLOW_KIND,
      artifacts: rows,
      integrity: {},
    };
    index.integrity.artifact_index_sha256 = await adapter.canonicalSha256(index);
    return index;
  }

  async function componentBinding(adapter, componentId, runId, lastSeq) {
    const sha = await adapter.canonicalSha256({ component_id: componentId, component_run_id: runId, last_component_seq: lastSeq });
    return {
      component_attempt_id: 1,
      component_attempt_index: 1,
      component_id: componentId,
      component_run_id: runId,
      last_component_seq: lastSeq,
      manifest: {
        artifact_kind: "component_manifest",
        artifact_ref: `components/${componentId}/${runId}/component_manifest.json`,
        schema_version: "1.0.0",
        sha256: sha,
        sha256_kind: "physical",
      },
      status: "succeeded",
      terminal: true,
      validator: {
        validation_receipt_sha256: sha,
        validator_id: `${componentId}.component.validator_v1`,
        validator_revision: "v1",
      },
    };
  }

  async function buildEvaluationScope(adapter, handoffSha) {
    const basis = {
      settings_option_id: "evaluation_settings_v1",
      selected_chapter_ids: [...CHAPTER_IDS],
      selected_arm_ids: ["s0", "s1", "community", "google_nmt", "llm_lc"],
      selected_scorer_ids: ["sf_qe", "sf_bt", "pj"],
      highlight_pair: { baseline_arm_id: "s0", candidate_arm_id: "s1" },
      registered_option_sha256: "4".repeat(64),
    };
    const selectionSha = await adapter.canonicalSha256(basis);
    return {
      schema_id: "EvaluationWorkflowScopeReadV1",
      schema_version: "1.0.0",
      ...basis,
      selection_sha256: selectionSha,
      scoring_handoff_status: "validated",
      settings_status: "materialized",
      settings_sha256: await adapter.canonicalSha256({
        selection_sha256: selectionSha,
        scoring_handoff_sha256: handoffSha,
        schema_id: "EvaluationWorkflowSettingsV1",
        schema_version: "1.1.0",
      }),
    };
  }

  async function build(adapter, basePackage) {
    const scoring = await prepareScoringArtifacts(adapter, basePackage.handoff, basePackage.receipt);
    const hashes = await buildArtifactHashes(adapter, scoring);
    const eventState = await buildEvents(adapter, hashes);
    const artifactIndex = await buildArtifactIndex(adapter, hashes, scoring, eventState);
    const allStages = [
      ...TRANSLATION_STAGES.map(definition => ["translation", definition]),
      ...EVALUATION_STAGES.map(definition => ["evaluation", definition]),
      ...PUBLICATION_STAGES.map(definition => ["publication", definition]),
    ].map(([componentId, definition], index) => stageRow(componentId, definition, index + 1));
    const manifest = clone(basePackage.manifest);
    manifest.workflow_run_id = WORKFLOW_RUN_ID;
    manifest.job_id = JOB_ID;
    manifest.status = "succeeded";
    manifest.started_at = new Date(BASE_TIME_MS).toISOString();
    manifest.updated_at = eventState.events.at(-1).accepted_at;
    manifest.active_stage_id = null;
    manifest.components = [
      await componentBinding(adapter, "translation", TRANSLATION_RUN_ID, eventState.componentSeq.get("translation")),
      await componentBinding(adapter, "evaluation", EVALUATION_RUN_ID, eventState.componentSeq.get("evaluation")),
      await componentBinding(adapter, "publication", PUBLICATION_RUN_ID, eventState.componentSeq.get("publication")),
    ];
    manifest.stages = allStages;
    manifest.resume = { available: false, component_id: null };
    manifest.reconstructed = true;
    manifest.timing_authority = "logical_order_only";
    manifest.latest_event_seq = eventState.events.length;
    manifest.artifact_index_sha256 = artifactIndex.integrity.artifact_index_sha256;
    manifest.integrity = {};
    manifest.integrity.manifest_sha256 = await adapter.canonicalSha256(manifest);

    const evaluationScope = await buildEvaluationScope(adapter, scoring.handoff.integrity.handoff_sha256);
    const cursor = {
      through_seq: eventState.events.length,
      event_chain_head_sha256: eventState.events.at(-1).integrity.event_sha256,
      package_revision_sha256: await adapter.canonicalSha256({
        manifest_sha256: manifest.integrity.manifest_sha256,
        artifact_index_sha256: artifactIndex.integrity.artifact_index_sha256,
        event_chain_head_sha256: eventState.events.at(-1).integrity.event_sha256,
      }),
    };
    return adapter.validatePackage({
      manifest,
      events: eventState.events,
      artifactIndex,
      artifacts: {
        [REFS.handoff]: scoring.handoff,
        [REFS.receipt]: scoring.receipt,
      },
      validatedArtifacts: {
        [REFS.report]: {
          valid: true,
          sha256: hashes.report,
          validator_id: "evaluation.report.validator_v1",
          validator_revision: "v1",
        },
      },
      usage: buildUsage(manifest, eventState.events.length),
      cursor,
      actions: {
        pause_allowed: false,
        resume_allowed: false,
        cancel_allowed: false,
        replay: { allowed: true },
        score: { allowed: false, blocking_reasons: ["already_scored"] },
      },
      evaluationScope,
      artifactLinks: {},
      sourceMode: "replay",
    });
  }

  global.WorkflowReplayFullDemo = Object.freeze({ build });
})(window);
