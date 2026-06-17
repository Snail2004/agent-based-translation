from __future__ import annotations

import json

from pipeline.eval.term_policy import (
    apply_glossary_fixes,
    classify_term,
    load_term_policy_assets,
)


def test_classify_buckets(tmp_path):
    assets_root = tmp_path / "eval"
    assets_root.mkdir()
    (assets_root / "d2l_term_stoplist.txt").write_text("class\nset\n", encoding="utf-8")
    (assets_root / "d2l_term_hard_allowlist.txt").write_text("tensor\n", encoding="utf-8")
    (assets_root / "d2l_term_policy_overrides.csv").write_text(
        "source_term,constraint_strength,justification\nAI,soft,acronym preserved\n",
        encoding="utf-8",
    )
    (assets_root / "d2l_glossary_fixes.csv").write_text(
        "source_term,op,value,justification\n",
        encoding="utf-8",
    )
    assets = load_term_policy_assets(assets_root)

    def cls(**row):
        return classify_term(row, stoplist=assets.stoplist, hard_allowlist=assets.hard_allowlist, overrides=assets.overrides)

    assert cls(source_term=".shape", term_type="term", do_not_translate=1) == "preserve"
    assert cls(source_term=".shape", term_type="code_api", do_not_translate=0) == "preserve"
    assert cls(source_term="PyTorch", term_type="proper_noun", do_not_translate=0) == "entity"
    assert cls(source_term="AI", term_type="abbreviation", do_not_translate=0) == "soft"
    assert cls(source_term="linear regression", term_type="term", do_not_translate=0) == "hard"
    assert cls(source_term="class", term_type="term", do_not_translate=0) == "ignore_for_consistency"
    assert cls(source_term="tensor", term_type="term", do_not_translate=0) == "hard"
    assert cls(source_term="example", term_type="term", do_not_translate=0) == "soft"


def test_apply_glossary_fixes_on_copy_only():
    rows = [
        {
            "source_term": "calculus",
            "target_term": "giải tích",
            "allowed_variants_json": json.dumps(["giải tích", "phép tính"], ensure_ascii=False),
        },
        {
            "source_term": "rules",
            "target_term": "luật",
            "allowed_variants_json": json.dumps(["luật", "quy tắc"], ensure_ascii=False),
        },
    ]
    fixed = apply_glossary_fixes(
        rows,
        [
            {"source_term": "calculus", "op": "remove_variant", "value": "phép tính", "justification": "linguistic"},
            {"source_term": "rules", "op": "set_canonical", "value": "quy tắc", "justification": "linguistic"},
        ],
    )

    assert rows[0]["allowed_variants_json"] == json.dumps(["giải tích", "phép tính"], ensure_ascii=False)
    assert rows[1]["target_term"] == "luật"
    assert json.loads(fixed[0]["allowed_variants_json"]) == ["giải tích"]
    assert fixed[1]["target_term"] == "quy tắc"
    assert json.loads(fixed[1]["allowed_variants_json"]) == ["luật", "quy tắc"]
