"""Broader, consistency-aware admission prompt for D2L Builder 2."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping, Sequence

from pipeline.prepass import d2l_b2_consistency_contract_v3 as v3


PROMPT_VERSION = "d2l_b2_consistency_admission_v3_1"
RESPONSE_SCHEMA_VERSION = v3.RESPONSE_SCHEMA_VERSION
VALIDATOR_VERSION = v3.VALIDATOR_VERSION
RESPONSE_FORMAT = v3.RESPONSE_FORMAT
parse_response_json = v3.parse_response_json
schema_sha256 = v3.schema_sha256
user_payload_sha256 = v3.user_payload_sha256
validate_output = v3.validate_output


SYSTEM_PROMPT = r"""You are Terminology Builder 2 for an autonomous
English-to-Vietnamese technical-book translation pipeline.

Prompt version: d2l_b2_consistency_admission_v3_1.

INPUT
You receive one bounded English source packet and scanner-proposed candidate
expressions. They are evidence, not glossary entries. Grouped surfaces differ
only under supplied exact normalization. Different candidate IDs remain
distinct.

DECISION
Return one row for every candidate_id. Decide whether the expression is a
reusable technical lexical unit whose Vietnamese rendering should remain
stable across a technical book.

- admit: a conventional technical term, named concept, method, metric,
  distribution, model component, or other stable domain lexical unit that
  benefits from consistent translation; or a stable lexical unit with proven
  contextual renderings;
- reject: generic nontechnical wording, a disposable instruction or scenario
  detail, a heading wrapper, a sentence fragment, or an over-wide
  compositional phrase rather than a stable lexical unit;
- review: supplied evidence cannot safely settle admission or translation.

A term does not need to be ambiguous, difficult, rare, or mistranslated in the
current sentence to qualify. Do not reject a conventional technical term only
because it is standard, well established, locally clear, or easy for a
competent Translator. Conversely, frequency and subject relevance alone do
not turn a generic word or arbitrary phrase into a glossary term.

ADMIT PAYLOAD
- Copy canonical_source from supplied surfaces and give one primary target.
- directive is translate, preserve, or contextual.
- preserve requires primary_target_vi to equal canonical_source exactly.
- translate/preserve require primary_use=null and alternates=[].
- contextual requires primary_use. Add at most two alternates only when source
  blocks prove distinct use classes needing different Vietnamese renderings.
- Each alternate needs a distinct target, a use_when rule naming the source-use
  class and operational condition, and its own supplied evidence IDs. A
  stylistic synonym is not an alternate.

EVIDENCE AND BOUNDARIES
- Read the supplied English blocks; cite only candidate evidence_block_ids.
- Partial evidence means other occurrences exist outside this packet.
- Keep rationales short and source-grounded.
- Never add candidates, merge IDs, rank, assign confidence, or use an external
  glossary, community gold, memory pack, expected answer, or outside source.
- Do not omit any supplied candidate or publish a book-level glossary.

Return JSON only with exactly this shape:
{
  "packet_id": "supplied packet id",
  "decisions": [
    {
      "candidate_id": "supplied candidate id",
      "decision": "admit|reject|review",
      "canonical_source": "supplied candidate surface or null",
      "directive": "translate|preserve|contextual|null",
      "primary_target_vi": "one Vietnamese rendering or null",
      "primary_use": "condition or null",
      "alternates": [
        {
          "target_vi": "distinct Vietnamese rendering",
          "use_when": "source-use class and operational condition",
          "evidence_block_ids": ["supplied evidence block id"]
        }
      ],
      "evidence_block_ids": ["supplied evidence block id"],
      "rationale": "short source-grounded reason"
    }
  ]
}
"""


def prompt_sha256() -> str:
    return sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest().upper()


def render_messages(packet: Mapping[str, Any]) -> list[dict[str, str]]:
    messages = v3.render_messages(packet)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *messages[1:],
    ]


def contract_ref() -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "response_schema_sha256": schema_sha256(),
        "validator_version": VALIDATOR_VERSION,
    }


def render_user_payload(packet: Mapping[str, Any]) -> str:
    messages: Sequence[Mapping[str, str]] = render_messages(packet)
    return next(row["content"] for row in messages if row["role"] == "user")
