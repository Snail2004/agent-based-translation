from __future__ import annotations

import copy
import json

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.live_pilot_sf_qe_v1 import (
    prepare_evaluation_live_pilot_sf_qe_v1,
    validate_pilot_local_sf_qe_binding_v1,
)
from pipeline.eval.offline_orchestrator_v1 import build_evaluation_plan
from pipeline.eval.scorer_input_packets_v1 import build_scorer_input_packet
from pipeline.tests.test_evaluation_live_pilot_preflight_v1 import (
    _build,
    _common,
    _config,
)


class _Predictor:
    checkpoint_sha256 = "6" * 64

    def __init__(self, scores=None, *, runtime=None) -> None:
        self.calls = 0
        self.rows = None
        self.batch_size = None
        self.scores = scores
        self.runtime = runtime

    def describe_runtime(self):
        if self.runtime is not None:
            return copy.deepcopy(self.runtime)
        return {
            "schema_id": "CometKiwiRuntimeDescriptionV1",
            "package_name": "unbabel-comet",
            "package_version": "2.2.7",
            "python_version": "3.11.9",
            "device": "cpu",
            "checkpoint_sha256": self.checkpoint_sha256,
        }

    def __call__(self, rows, batch_size):
        self.calls += 1
        self.rows = copy.deepcopy(rows)
        self.batch_size = batch_size
        if self.scores is not None:
            return copy.deepcopy(self.scores)
        return [0.5 + index / 100 for index, _row in enumerate(rows)]


def _prepared(predictor=None):
    common = _common()
    config = _config(common)
    preflight = _build(common)
    active_predictor = predictor or _Predictor()
    prepared = prepare_evaluation_live_pilot_sf_qe_v1(
        common,
        config,
        preflight,
        active_predictor,
        batch_size=8,
    )
    return common, config, preflight, active_predictor, prepared


def _sf_qe_packets(common, config, preflight):
    plan = build_evaluation_plan(common, config)
    jobs = {row.job_id: row for row in plan.jobs}
    packets = []
    for row in preflight["jobs"]:
        if row["method_id"] != "sf_qe":
            continue
        packets.append(
            build_scorer_input_packet(
                common,
                plan,
                jobs[row["job_id"]].job_id,
                created_at=preflight["created_at"],
                producer_code_commit=preflight["producer"]["code_commit"],
            )
        )
    return packets


def test_prepares_one_closed_batch_for_exact_selected_sf_qe_cover():
    common, config, preflight, predictor, prepared = _prepared()

    assert predictor.calls == 1
    assert predictor.batch_size == 8
    assert len(predictor.rows) == 16
    assert all(set(row) == {"src", "mt"} for row in predictor.rows)
    rendered = json.dumps(predictor.rows)
    assert "arm_id" not in rendered
    assert "gold" not in rendered
    assert "reference" not in rendered
    binding = prepared.execution_binding
    assert binding["selected_job_count"] == 16
    assert binding["checkpoint_sha256"] == predictor.checkpoint_sha256
    assert binding["report_transform_id"] == "comet_native_0_1_times_100_v1"
    assert validate_pilot_local_sf_qe_binding_v1(binding) == binding

    packets = _sf_qe_packets(common, config, preflight)
    observed = []
    for packet in packets:
        observed.append(
            prepared(
                packet["source"]["blocks"][0]["text"],
                packet["candidates"][0]["blocks"][0]["text"],
            )
        )
    prepared.assert_exact_cover()
    assert observed[0] == 50.0
    assert observed[-1] == 65.0


def test_complete_batch_can_restart_without_reinvoking_predictor():
    common, config, preflight, predictor, prepared = _prepared()
    packets = _sf_qe_packets(common, config, preflight)
    for _round in range(2):
        prepared.begin_execution()
        for packet in packets:
            prepared(
                packet["source"]["blocks"][0]["text"],
                packet["candidates"][0]["blocks"][0]["text"],
            )
        prepared.assert_exact_cover()
    assert predictor.calls == 1


def test_partial_batch_cannot_restart_or_hide_missing_cover():
    common, config, preflight, _, prepared = _prepared()
    packet = _sf_qe_packets(common, config, preflight)[0]
    prepared(
        packet["source"]["blocks"][0]["text"],
        packet["candidates"][0]["blocks"][0]["text"],
    )
    with pytest.raises(ContractValidationError, match="partially consumed"):
        prepared.begin_execution()
    with pytest.raises(ContractValidationError, match="did not consume"):
        prepared.assert_exact_cover()


def test_request_order_and_extra_request_fail_closed():
    common, config, preflight, _, prepared = _prepared()
    packets = _sf_qe_packets(common, config, preflight)
    with pytest.raises(ContractValidationError, match="sealed batch order"):
        prepared("foreign source", "foreign target")

    prepared.begin_execution()
    for packet in packets:
        prepared(
            packet["source"]["blocks"][0]["text"],
            packet["candidates"][0]["blocks"][0]["text"],
        )
    with pytest.raises(ContractValidationError, match="extra row"):
        prepared("foreign source", "foreign target")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("checkpoint_sha256", "7" * 64, "different checkpoint"),
        ("package_name", "foreign-comet", "package is not approved"),
        ("device", "cuda", "must be CPU-bound"),
    ],
)
def test_runtime_substitution_fails_before_scoring(field, value, message):
    predictor = _Predictor()
    runtime = predictor.describe_runtime()
    runtime[field] = value
    predictor.runtime = runtime
    with pytest.raises(ContractValidationError, match=message):
        _prepared(predictor)
    assert predictor.calls == 0


@pytest.mark.parametrize(
    "scores",
    [
        [0.5],
        [float("nan") for _ in range(16)],
        [1.1 for _ in range(16)],
    ],
)
def test_invalid_predictor_score_count_or_value_fails(scores):
    with pytest.raises(ContractValidationError):
        _prepared(_Predictor(scores=scores))


def test_inputs_are_not_mutated_by_preparation():
    common = _common()
    config = _config(common)
    preflight = _build(common)
    before = (copy.deepcopy(common), copy.deepcopy(config), copy.deepcopy(preflight))

    prepare_evaluation_live_pilot_sf_qe_v1(
        common,
        config,
        preflight,
        _Predictor(),
        batch_size=8,
    )

    assert (common, config, preflight) == before
