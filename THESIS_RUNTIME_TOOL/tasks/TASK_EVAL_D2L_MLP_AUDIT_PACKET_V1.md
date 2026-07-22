# TASK_EVAL_D2L_MLP_AUDIT_PACKET_V1

Status: COMPLETE - 0 API; SEALED D2L INPUT PENDING

Owner: Evaluation workstream

Depends on:

- `TASK_EVAL_D2L_MLP_ALIGNMENT_ACCEPTANCE_V1.md`;
- a D2L-produced, sealed `D2LEvaluationInputV1` package for exactly
  `d2l_multilayer_perceptrons`.

## 1. Objective

Build the content-addressed manual-review packet for the preregistered D2L MLP
structural alignment sample.

The packet displays exact selected source and community-target text. It does
not contain audit outcomes, scores, expected winners, translation-quality
claims, or auto-accept decisions.

## 2. Exact write set

1. `THESIS_RUNTIME_TOOL/pipeline/eval/d2l_alignment_audit_packet_v1.py`
2. `THESIS_RUNTIME_TOOL/pipeline/tests/test_evaluation_d2l_community_alignment_v1.py`
3. `THESIS_RUNTIME_TOOL/tasks/TASK_EVAL_D2L_MLP_AUDIT_PACKET_V1.md`

## 3. Packet contract

Internal schema: `D2LStructuralAlignmentAuditPacketV1` version `1.0.0`.

The packet binds:

- the review-held alignment manifest hash;
- exact source read-model hash;
- exact community target artifact and segment-set hashes;
- exact `_origin.md` file-set hash;
- deterministic audit selection hash and policy version;
- population and sample counts;
- each selected mapping, section, selection reason, exact source block, and
  exact target segment.

The packet has a deterministic self-hash. Text rows carry their own exact UTF-8
hashes. Loading the packet against source inputs must reproduce the complete
packet; a correctly resealed but foreign text row remains invalid.

## 4. Review boundary

This artifact only prepares evidence for a human/manual alignment check.

- no outcome field appears in packet items;
- no section becomes accepted when the packet is generated;
- audit dispositions remain a separate exact-cover artifact;
- a failed audited mapping routes its complete section to `review_required`;
- alignment acceptance never implies translation quality.

## 5. Hard exclusions

- no API, model, embedding, scorer, DB, checkpoint, or App change;
- no D2L producer simulation from Evaluation;
- no gold, oracle, human reference, score, verdict, recommendation, or winner;
- no fuzzy/semantic alignment fallback;
- no public Evaluation contract or `pipeline/eval/__init__.py` change;
- no real 48-row packet before the producer package is available.

## 6. Acceptance probes

1. Repeated construction over identical inputs is byte-equivalent.
2. The source manifest and caller inputs are not mutated.
3. Packet rows contain source and target text but no outcome/decision.
4. A forged deterministic plan is rejected.
5. A changed text row with a recomputed text hash and packet self-hash still
   fails exact input binding.
6. Forbidden score/result authority is rejected recursively.
7. Existing origin, structure, sample, and section-fallback probes remain green.
8. The complete applicable repository suite remains green.

## 7. Producer handoff

Evaluation sent D2L an exact-path request on 2026-07-18 for one committed,
gold-free MLP package containing the immutable source universe and S0/S1 arms.
The D2L session may finish its active milestone first. Evaluation will not
consume dirty cross-worktree output.

## 8. Verification record

Completed on 2026-07-18:

- focused alignment/audit-packet probes: `13 passed`;
- Evaluation contract/alignment/orchestrator group: `102 passed`;
- applicable repository suite: `994 passed, 1 skipped, 2 deselected`;
- the two deselections require the absent frozen SQLite fixture already
  recorded by earlier Evaluation milestones;
- packet generation and validation perform no API, DB, provider, scorer, or
  App action;
- no real packet was emitted because the D2L producer package is not yet
  available.
