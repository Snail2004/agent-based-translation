"""Evidence-role-gated admission prompt for D2L Builder 2."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping

from pipeline.prepass import d2l_b2_consistency_contract_v3_3 as v3_3


PROMPT_VERSION = "d2l_b2_consistency_admission_v3_4"
RESPONSE_SCHEMA_VERSION = v3_3.RESPONSE_SCHEMA_VERSION
VALIDATOR_VERSION = v3_3.VALIDATOR_VERSION
RESPONSE_FORMAT = v3_3.RESPONSE_FORMAT
parse_response_json = v3_3.parse_response_json
schema_sha256 = v3_3.schema_sha256
user_payload_sha256 = v3_3.user_payload_sha256
validate_output = v3_3.validate_output


_ROLE_GATE = r"""EVIDENCE ROLE GATE - APPLY BEFORE THE TWO TESTS.
Classify the supplied evidence for this candidate by what the prose is doing:

- conceptual evidence defines, explains, contrasts, derives, or reasons about
  the item's technical properties, behavior, or implications;
- operational evidence merely names or uses the item while showing code,
  configuring infrastructure, transferring or validating files, navigating a
  platform, executing a workflow, or carrying out the local application.

If all supplied evidence is operational, reject the candidate. Repetition
inside the same implementation subsystem or workflow remains operational and
does not become book-level conceptual reuse. Do not infer unseen book-wide use
from the fact that an item is standard, reusable, or appears in book utility
code. The only exception is when the supplied source explicitly presents the
item as a section or chapter teaching target and explains its principles rather
than merely using it.

"""

_VERSION_MARKER_OLD = "Prompt version: d2l_b2_consistency_admission_v3_3."
_VERSION_MARKER_NEW = "Prompt version: d2l_b2_consistency_admission_v3_4."
_TEST_2_MARKER = "TEST 2 - MATERIAL BOOK-LEVEL CONSISTENCY VALUE."

if v3_3.SYSTEM_PROMPT.count(_VERSION_MARKER_OLD) != 1:
    raise RuntimeError("V3.3 prompt version marker drifted")
if v3_3.SYSTEM_PROMPT.count(_TEST_2_MARKER) != 1:
    raise RuntimeError("V3.3 Test 2 marker drifted")

SYSTEM_PROMPT = (
    v3_3.SYSTEM_PROMPT.replace(_VERSION_MARKER_OLD, _VERSION_MARKER_NEW)
    .replace(_TEST_2_MARKER, _ROLE_GATE + _TEST_2_MARKER)
)


def prompt_sha256() -> str:
    return sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest().upper()


def render_messages(packet: Mapping[str, Any]) -> list[dict[str, str]]:
    messages = v3_3.render_messages(packet)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *messages[1:],
    ]
