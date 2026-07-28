from __future__ import annotations


SELECTOR_SYSTEM_PROMPT = """You classify supplied D2L source contexts for one
fixed term sense. Return one annotation for every supplied context ID. Do not
select the final contexts, score a translation candidate, or invent IDs. Use
SAME_SENSE only when the source context supports the supplied sense and scope.
Use CONTRASTIVE for a materially different sense, and AMBIGUOUS when the text
cannot establish the sense. Return JSON matching the supplied schema only."""

TRIAL_SYSTEM_PROMPT = """Translate the supplied English source context fully
into Vietnamese. You must use the exact candidate_translation literal and must
not replace it with an alternative term. Preserve all source meaning, add no
explanation, and use an expansion only when candidate_policy permits it.
Report any expansion explicitly. Return JSON matching the supplied schema only."""

TRIAL_GATE_SYSTEM_PROMPT = """Audit a trial translation independently of the
candidate's semantic quality. Determine whether the required candidate literal
was used and whether the surrounding translation introduced an external error,
omitted content, added meaning, or relied on an ambiguous source. Do not score
the candidate and return JSON matching the supplied schema only."""

CONTEXT_JUDGE_SYSTEM_PROMPT = """Judge only how the fixed Vietnamese candidate
behaves in the supplied source context and trial translation. Return component
scores, flags, judgeability, short evidence spans, one variant observation,
and a concise reason. Never return a total, PASS/MINOR/FAIL label, final
decision, recommendation, confidence percentage, probability, or hidden
reasoning. JSON must match the supplied schema."""

CONTRASTIVE_SYSTEM_PROMPT = """Classify whether a contrastive source context
belongs to another sense and what that implies for the fixed candidate scope.
Do not add it to the same-sense score and do not return a final candidate
decision. Return JSON matching the supplied schema only."""


