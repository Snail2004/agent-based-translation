# Literary Temporal Claim Policy V1

Status: architecture decision accepted for later B2 implementation; no runtime
or prompt change is implied by this document alone.

## 1. Core rule

Only `entity_id` is treated as identity-stable. Names, referential gender,
titles, roles, residence, life status, relations, and similar facts are claims
with provenance, semantic status, and an optional validity interval.

An apparent contradiction must not automatically split one entity into two.
Identity continuity and claim transition are separate decisions.

## 2. Auditor responsibilities

The Identity/Surface Auditor decides whether two observations concern the same
referent. A `same_entity` decision must retain a concise reason and the source
blocks that support continuity. It does not itself rewrite gender, names, or
other stable claims.

When the same entity has a materially changed claim, the Stable-Claim Auditor
records a transition:

- close the earlier claim at `valid_to_block_id`;
- open the new claim at `valid_from_block_id`;
- retain the transition reason and supporting blocks;
- keep the decision append-only and auditable.

If evidence is insufficient, the field becomes pending/disputed. A disputed
old value must not continue to appear in `effective_claims` as authority.

## 3. B2 context contract

B2 normally receives only:

- `effective_claims_as_of`: current effective values at the active block/window;
- `relevant_claim_transitions`: bounded history only when the active source
  crosses a transition, uses an old name/pronoun, contains a flashback, or
  reopens a related ticket;
- `uncertainty_flags`: fields that remain pending or disputed.

B2 does not receive every historical state or the raw Auditor transcript. The
context renderer converts the audit ledger into compact semantic facts.

Example at a post-transition block:

```json
{
  "entity_id": "ent_A",
  "effective_claims_as_of": {
    "referential_gender": "masculine",
    "active_name": "Current Name"
  },
  "relevant_claim_transitions": [],
  "uncertainty_flags": []
}
```

When a flashback or old name makes history relevant, the renderer may add:

```json
{
  "claim_type": "referential_gender",
  "from_value": "feminine",
  "to_value": "masculine",
  "valid_from_block_id": "bk_ch05_b020",
  "source_block_ids": ["bk_ch05_b018", "bk_ch05_b020"],
  "status": "auditor_confirmed"
}
```

## 4. Names and address forms

A complete name change keeps one `entity_id`. The old and new names are stored
as scoped name-usage claims with validity intervals. The old name remains
available for flashbacks and historical references but is not automatically
active in later windows.

A surface used only by one speaker or in one chapter/block remains scoped to
that speaker/chapter/block. It is not promoted to a book-global alias merely
because it recurs.

Two genuinely different people with the same name remain separate entity ids;
the shared surface retrieves both as candidates and B2 resolves locally.

## 5. Local speech is not global identity

A character using an old name, an incorrect pronoun, an insult, or a mistaken
belief does not mutate the global entity claim. B2 records that as local
speaker/address evidence. Only supported narration or an Auditor-confirmed
transition changes the effective claim history.

## 6. Implementation sequence

1. Keep the current B1 entity registry contract unchanged.
2. Add append-only claim-history rows and effective-claim projection in code.
3. Extend Identity decisions with compact continuity reason/provenance.
4. Route same-entity claim changes to Stable-Claim review.
5. Render bounded `as_of` claim context for B2.
6. Add adversarial probes for name change, gender transition, same-name people,
   flashbacks, local misnaming/misgendering, and unresolved transitions.
