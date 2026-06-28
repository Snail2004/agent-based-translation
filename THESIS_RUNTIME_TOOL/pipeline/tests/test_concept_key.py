from __future__ import annotations

from pipeline.prepass.concept_key import concept_key, merge_reason, singularize_token


def test_regular_number_variants_merge_to_singular() -> None:
    assert concept_key("features") == "feature"
    assert concept_key("models") == "model"
    assert concept_key("classes") == "class"
    assert concept_key("biases") == "bias"
    assert concept_key("parties") == "party"


def test_dont_singularize_tokens_stay_stable() -> None:
    assert concept_key("loss") == "loss"
    assert concept_key("bias") == "bias"
    assert concept_key("axis") == "axis"
    assert concept_key("analysis") == "analysis"
    assert concept_key("statistics") == "statistics"
    assert concept_key("logits") == "logits"


def test_irregular_plurals_are_explicit_only() -> None:
    assert singularize_token("axes") == "axis"
    assert singularize_token("analyses") == "analysis"
    assert singularize_token("hypotheses") == "hypothesis"
    assert singularize_token("matrices") == "matrix"
    assert singularize_token("indices") == "index"
    assert singularize_token("vertices") == "vertex"


def test_phrase_overrides_prevent_bad_number_merge() -> None:
    assert concept_key("least squares") == "least squares"
    assert concept_key("ordinary least squares") == "ordinary least squares"
    assert concept_key("naive Bayes") == "naive bayes"


def test_derivational_forms_do_not_merge() -> None:
    assert concept_key("train") != concept_key("training")
    assert concept_key("general") != concept_key("generalization")
    assert concept_key("compute") != concept_key("computation")


def test_merge_reason_marks_plural_rewrites_only() -> None:
    assert merge_reason("feature") == "exact"
    assert merge_reason("features") == "regular_plural"
    assert merge_reason("matrices") == "irregular_plural"
