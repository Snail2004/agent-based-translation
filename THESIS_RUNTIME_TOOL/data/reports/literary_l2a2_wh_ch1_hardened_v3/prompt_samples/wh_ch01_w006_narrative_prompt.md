# Actual Builder Prompt Sample

- Source artifact: `data/reports/literary_l2a2_wh_ch1_hardened_v3/narrative/wb_wh_ch01_006.json`
- Mode: `literary_narrative_v1`
- Window: `wb_wh_ch01_006`
- Active block ids: `wh_ch01_b019, wh_ch01_b020, wh_ch01_b021, wh_ch01_b022`
- Prompt tokens: `2744`
- Completion tokens: `1030`
- Cost USD: `0.002746`

## SYSTEM MESSAGE

```text
- Prompt version: literary_narrative_v1.
- Return only valid JSON matching the Required JSON shape. No text outside JSON.
- CRITICAL — do NOT infer relationships, alliances, feelings, trust, or story phases. Those are decided later from the whole timeline. Report only single, locally observable acts and utterances.
- event_type MUST be a concrete observable action verb in lower_snake_case (examples: addresses, greets, mocks, strikes, protects, serves, refuses, threatens, embraces, orders, weeps_over, curses). It MUST NOT be a relationship or phase label. Forbidden event_type values: ally, enemy, friend, rival, love, hatred, betrayal, trust, alliance, reconciliation, phase, any "*_phase".
- Do NOT output state_label, valid_from_block, valid_to_block, address policy, or any book-level conclusion in this pass.
- speaker_turns: one row per direct quoted utterance. Give each a turn_id (t_<block>_<n>). "speaker" and "addressee" are each an OBJECT: {surface, reference_kind, resolution_status, candidate_entity_ids, attribution_method, confidence}.
  - reference_kind is one of: person, group, narrator, reader, unknown. Only person may become a character entity later; a group ("the household"), the narrator, or the reader MUST NOT be minted as a person.
  - resolution_status is one of: named (an explicit name or tag identifies them, e.g. "said Alden"), candidate (you map a pronoun/descriptor to a roster entity but are not certain), unknown (you cannot tell — never force a guess).
  - candidate_entity_ids: when resolution_status is candidate it MUST be non-empty — copy the id(s) verbatim from CHAPTER_ROSTER_ON_STAGE. If the roster has no id for the referent (including the first-person narrator before they are named on the page), use resolution_status=unknown with [] instead of candidate; never invent an id. There is NO placeholder id: ids such as ent_unknown, ent_unnamed, or ent_narrator do not exist — any id not literally listed in CHAPTER_ROSTER_ON_STAGE or REGISTRY_CONTEXT_PACK is an error; "I do not know who this is" is expressed ONLY as resolution_status=unknown with []. For named/unknown leave [].
  - attribution_method is one of: explicit_tag, turn_alternation, nearby_context, narrator_inference. This is HOW you attributed the reference — it is the real trust signal, more than confidence. It is NEVER a resolution_status value: do NOT put named/candidate/unknown in attribution_method.
  - confidence is one of: high, medium, low. Do NOT output a number.
- If the utterance contains a vocative (address_term_used is not null), the addressee MUST be the specific person that vocative names, not a group. Use a group addressee (reference_kind group) only when there is no specific vocative.
- A GENERIC honorific used as a vocative (sir, madam, ma'am, my lord, my lady, master, mistress, with no personal name attached) does NOT by itself name a specific person. Resolve it by turn-taking using CHAPTER_BRIEF: if the current scene in scenes_party_size has co_present_count == 2, the addressee is the OTHER co-present person (the one who is not the speaker); set resolution_status=candidate, candidate_entity_ids to that person, attribution_method=turn_alternation, confidence=medium. If co_present_count >= 3 and no other cue points to exactly one person, leave the addressee unknown — never force a guess.
- The addressee is the person the words are spoken TO, not a person or thing the words are ABOUT. In "You had better let the dog alone," the addressee is the listener being warned, not "the dog". Never record a non-person (animal, object) as an addressee: if the only surface available is such a thing, resolve the addressee from turn-taking / scene participants instead, or leave it unknown.
- utterance_quote = a short verbatim snippet of the spoken words (<=20 words; the whole utterance if short). This verbatim text — not the gist — is the evidence for later address/register scoring.
- address_term_used = the literal vocative the speaker uses for the addressee in the utterance ("Mira", "Mr. Alden", "master", "my girl"), or null. Copy verbatim; do not translate here.
- register_cue = one short lowercase tone word if visible (neutral, intimate, deferential, paternal, hostile, mocking), else "neutral".
- relation_events: give each an event_id (e_<block>_<n>). "actor" and "target" are the SAME object shape as speaker/addressee. evidence_quote is a short literal snippet (<=12 words) copied from the window that shows the act. actor and target should be PERSONS or the narrator — do NOT create a relation_event whose actor or target is an object or an animal (a dog, a room, furniture).
- Do NOT record the narrator's storytelling voice as a speaker_turn. Only record quoted speech by a character in-scene. (A first-person narrator counts as a speaker only when quoted speaking to someone in the scene.)
- A pronoun or zero-subject MAY be the surface when that is all the text gives: set resolution_status to candidate (if roster + turn order point to someone) or unknown, and record attribution_method. Do NOT drop a turn or event just because its subject is a pronoun.
- Every block_id MUST be a marker that literally appears in this window.

Required JSON shape:
{
  "chapter_id": "...",
  "window_block_ids": ["wh_ch01_b006", "wh_ch01_b007"],
  "context_only_used": false,
  "speaker_turns": [
    {"turn_id": "t_wh_ch01_b006_01",
     "speaker": {"surface": "the innkeeper", "reference_kind": "person", "resolution_status": "named", "candidate_entity_ids": [], "attribution_method": "explicit_tag", "confidence": "high"},
     "addressee": {"surface": "sir", "reference_kind": "person", "resolution_status": "candidate", "candidate_entity_ids": ["ent_ravel"], "attribution_method": "turn_alternation", "confidence": "medium"},
     "utterance_quote": "And what brings you so far north, sir?",
     "address_term_used": "sir", "register_cue": "neutral", "utterance_gist": "asks the traveller his business", "block_id": "wh_ch01_b006"}
  ],
  "relation_events": [
    {"event_id": "e_wh_ch01_b006_01",
     "actor": {"surface": "the innkeeper", "reference_kind": "person", "resolution_status": "named", "candidate_entity_ids": [], "attribution_method": "explicit_tag", "confidence": "high"},
     "target": {"surface": "sir", "reference_kind": "person", "resolution_status": "candidate", "candidate_entity_ids": ["ent_ravel"], "attribution_method": "turn_alternation", "confidence": "medium"},
     "event_type": "questions", "evidence_quote": "what brings you so far north", "block_id": "wh_ch01_b006"}
  ]
}
```

## USER MESSAGE

```text
CHAPTER_BRIEF
setting | Wuthering Heights | frame_present | single_scene_one_location
neutral_premise | A visitor named Lockwood calls on his landlord Heathcliff at Wuthering Heights, is shown inside, and endures a disturbed meeting with the household dogs before leaving after conversation and wine.
cast | I | traveller | wh_ch01_b002
cast | Mr. Heathcliff | landlord | wh_ch01_b002
cast | Joseph | servant | wh_ch01_b008
cast | the canine mother | dog | wh_ch01_b017
cast | the lusty dame | servant | wh_ch01_b020
scene | wh_ch01_b002..wh_ch01_b008 | co_present_count=2 | I, Mr. Heathcliff
scene | wh_ch01_b008..wh_ch01_b020 | co_present_count=3 | I, Mr. Heathcliff, Joseph
scene | wh_ch01_b020..wh_ch01_b028 | co_present_count=4 | I, Mr. Heathcliff, Joseph, the lusty dame

NEIGHBOR_SUMMARIES_GIST_ONLY
(none)

ACTIVE_NARRATOR_HINTS_BY_BLOCK_RANGE
wh_ch01_b019..wh_ch01_b022 | unknown

CHAPTER_ROSTER_ON_STAGE
ent_hareton_earnshaw | Hareton Earnshaw | Hareton Earnshaw
ent_joseph | Joseph | Joseph
ent_mr_heathcliff | Mr. Heathcliff | Mr. Heathcliff
ent_mr_lockwood | Mr. Lockwood | Mr. Lockwood

WINDOW_MENTIONS_FROM_LEXICON_PASS
m_wh_ch01_b019_01 | Joseph | name | named | 
m_wh_ch01_b019_02 | his master | descriptor | candidate | ent_mr_heathcliff
m_wh_ch01_b020_01 | Mr. Heathcliff | name | named | 
m_wh_ch01_b020_02 | his man | descriptor | candidate | ent_joseph
m_wh_ch01_b020_03 | a lusty dame | descriptor | unknown | 

CHAPTER_ID
wh_ch01

PREVIOUS_WINDOW_TAIL_CONTEXT_ONLY
[wh_ch01_b017] I took a seat at the end of the hearthstone opposite that towards which my landlord advanced, and filled up an interval of silence by attempting to caress the canine mother, who had left her nursery, and was sneaking wolfishly to the back of my legs, her lip curled up, and her white teeth watering for a snatch. My caress provoked a long, guttural gnarl.
[wh_ch01_b018] “You’d better let the dog alone,” growled Mr. Heathcliff in unison, checking fiercer demonstrations with a punch of his foot. “She’s not accustomed to be spoiled—not kept for a pet.” Then, striding to a side door, he shouted again, “Joseph!”

NEXT_WINDOW_TAIL_CONTEXT_ONLY
[wh_ch01_b023] “They won’t meddle with persons who touch nothing,” he remarked, putting the bottle before me, and restoring the displaced table. “The dogs do right to be vigilant. Take a glass of wine?”
[wh_ch01_b024] “No, thank you.”

ENGLISH_SOURCE_WINDOW_WITH_BLOCK_MARKERS
[wh_ch01_b019] Joseph mumbled indistinctly in the depths of the cellar, but gave no intimation of ascending; so his master dived down to him, leaving me vis-à-vis the ruffianly bitch and a pair of grim shaggy sheep-dogs, who shared with her a jealous guardianship over all my movements. Not anxious to come in contact with their fangs, I sat still; but, imagining they would scarcely understand tacit insults, I unfortunately indulged in winking and making faces at the trio, and some turn of my physiognomy so irritated madam, that she suddenly broke into a fury and leapt on my knees. I flung her back, and hastened to interpose the table between us. This proceeding aroused the whole hive: half-a-dozen four-footed fiends, of various sizes and ages, issued from hidden dens to the common centre. I felt my heels and coat-laps peculiar subjects of assault; and parrying off the larger combatants as effectually as I could with the poker, I was constrained to demand, aloud, assistance from some of the household in re-establishing peace.
[wh_ch01_b020] Mr. Heathcliff and his man climbed the cellar steps with vexatious phlegm: I don’t think they moved one second faster than usual, though the hearth was an absolute tempest of worrying and yelping. Happily, an inhabitant of the kitchen made more dispatch; a lusty dame, with tucked-up gown, bare arms, and fire-flushed cheeks, rushed into the midst of us flourishing a frying-pan: and used that weapon, and her tongue, to such purpose, that the storm subsided magically, and she only remained, heaving like a sea after a high wind, when her master entered on the scene.
[wh_ch01_b021] “What the devil is the matter?” he asked, eyeing me in a manner that I could ill endure after this inhospitable treatment.
[wh_ch01_b022] “What the devil, indeed!” I muttered. “The herd of possessed swine could have had no worse spirits in them than those animals of yours, sir. You might as well leave a stranger with a brood of tigers!”
```
