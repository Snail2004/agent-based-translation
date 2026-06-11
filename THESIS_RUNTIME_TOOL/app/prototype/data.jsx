/* ============================================================
   Sample dataset — doc_id: austen_pp_en
   Pride and Prejudice (Jane Austen, 1813, public domain)
   Demonstrates EN source -> VI annotation pipeline state.
   Pipeline-generated fields are read-only; annotator fields editable.
   ============================================================ */

const DOC = {
  doc_id: "austen_pp_en",
  metadata: {
    title: "Pride and Prejudice",
    author: "Jane Austen",
    genre: "novel",
    domain: "literary_fiction",
    source_format: "epub",
    license: "public-domain",
    source_url: "https://www.gutenberg.org/ebooks/1342",
    contamination_risk: "low",
  },
  provenance: {
    raw_sha256: "9f2c…b71a",
    extraction_tool: "ailab-extract",
    pipeline_version: "1.4.0",
    retrieved_at: "2026-05-28",
  },
};

/* block.review handled in REVIEW state; here are canonical-ish fields */
const BLOCKS = [
  {
    block_id: "b0001", chapter_id: "ch01", order_index: 1,
    block_type: "heading", is_chapter_opening: true,
    quality_flags: ["ok"],
    source_text: "Chapter 1",
    clean_text: "Chapter 1",
  },
  {
    block_id: "b0002", chapter_id: "ch01", order_index: 2,
    block_type: "paragraph", is_chapter_opening: false,
    quality_flags: ["ok"],
    source_text: "It is a truth universally acknowledged, that a single man in possession of a good fortune, must be in want of a wife.",
    clean_text: "It is a truth universally acknowledged, that a single man in possession of a good fortune, must be in want of a wife.",
  },
  {
    block_id: "b0003", chapter_id: "ch01", order_index: 3,
    block_type: "paragraph", is_chapter_opening: false,
    quality_flags: ["ok"],
    source_text: "However little known the feelings or views of such a man may be on his first entering a neighbourhood, this truth is so well fixed in the minds of the surrounding families, that he is considered the rightful property of some one or other of their daughters.",
    clean_text: "However little known the feelings or views of such a man may be on his first entering a neighbourhood, this truth is so well fixed in the minds of the surrounding families, that he is considered the rightful property of some one or other of their daughters.",
  },
  {
    block_id: "b0004", chapter_id: "ch01", order_index: 4,
    block_type: "dialogue", is_chapter_opening: false,
    quality_flags: ["ok"],
    source_text: "\u201CMy dear Mr. Bennet,\u201D said his lady to him one day, \u201Chave you heard that Netherfield Park is let at last?\u201D",
    clean_text: "\u201CMy dear Mr. Bennet,\u201D said his lady to him one day, \u201Chave you heard that Netherfield Park is let at last?\u201D",
    discourse: { speaker_entity_id: "e_mrs_bennet", addressee_entity_id: "e_mr_bennet" },
  },
  {
    block_id: "b0005", chapter_id: "ch01", order_index: 5,
    block_type: "paragraph", is_chapter_opening: false,
    quality_flags: ["needs_review"],
    source_text: "Mr. Bennet replied that he had not.",
    clean_text: "Mr. Bennet replied that he had not.",
  },
  {
    block_id: "b0006", chapter_id: "ch01", order_index: 6,
    block_type: "dialogue", is_chapter_opening: false,
    quality_flags: ["unclear_dialogue"],
    source_text: "\u201CBut it is,\u201D returned she; \u201Cfor Mrs. Long has just been here, and she told me all about it.\u201D",
    clean_text: "\u201CBut it is,\u201D returned she; \u201Cfor Mrs. Long has just been here, and she told me all about it.\u201D",
    discourse: { speaker_entity_id: "e_mrs_bennet", addressee_entity_id: "e_mr_bennet" },
  },
  {
    block_id: "b0007", chapter_id: "ch01", order_index: 7,
    block_type: "paragraph", is_chapter_opening: false,
    quality_flags: ["ok"],
    source_text: "Mr. Bennet made no answer.",
    clean_text: "Mr. Bennet made no answer.",
  },
  {
    block_id: "b0008", chapter_id: "ch01", order_index: 8,
    block_type: "dialogue", is_chapter_opening: false,
    quality_flags: ["ok"],
    source_text: "\u201CDo you not want to know who has taken it?\u201D cried his wife impatiently.",
    clean_text: "\u201CDo you not want to know who has taken it?\u201D cried his wife impatiently.",
    discourse: { speaker_entity_id: "e_mrs_bennet", addressee_entity_id: "e_mr_bennet" },
  },
  {
    block_id: "b0009", chapter_id: "ch01", order_index: 9,
    block_type: "dialogue", is_chapter_opening: false,
    quality_flags: ["ok"],
    source_text: "\u201C\u2018You\u2019 want to tell me, and I have no objection to hearing it.\u201D",
    clean_text: "\u201CYou want to tell me, and I have no objection to hearing it.\u201D",
  },
  {
    block_id: "b0010", chapter_id: "ch01", order_index: 10,
    block_type: "paragraph", is_chapter_opening: false,
    quality_flags: ["broken_paragraph"],
    source_text: "This was invitation enough.  \u201CWhy, my dear, you must know, Mrs. Long says that Netherfield is taken by a young man of large fortune from the north of England; that he came down on Monday in a chaise and four to see the place.",
    clean_text: "This was invitation enough.\n\n\u201CWhy, my dear, you must know, Mrs. Long says that Netherfield is taken by a young man of large fortune from the north of England; that he came down on Monday in a chaise and four to see the place.",
  },
  /* chapter 2 */
  {
    block_id: "b0011", chapter_id: "ch02", order_index: 11,
    block_type: "heading", is_chapter_opening: true,
    quality_flags: ["ok"],
    source_text: "Chapter 2",
    clean_text: "Chapter 2",
  },
  {
    block_id: "b0012", chapter_id: "ch02", order_index: 12,
    block_type: "paragraph", is_chapter_opening: false,
    quality_flags: ["ok"],
    source_text: "Mr. Bennet was among the earliest of those who waited on Mr. Bingley. He had always intended to visit him, though to the last always assuring his wife that he should not go.",
    clean_text: "Mr. Bennet was among the earliest of those who waited on Mr. Bingley. He had always intended to visit him, though to the last always assuring his wife that he should not go.",
  },
  {
    block_id: "b0013", chapter_id: "ch02", order_index: 13,
    block_type: "dialogue", is_chapter_opening: false,
    quality_flags: ["ok"],
    source_text: "\u201CI hope Mr. Bingley will like it, Lizzy.\u201D",
    clean_text: "\u201CI hope Mr. Bingley will like it, Lizzy.\u201D",
    discourse: { speaker_entity_id: "e_mrs_bennet", addressee_entity_id: "" },
  },
  {
    block_id: "b0014", chapter_id: "ch02", order_index: 14,
    block_type: "footnote", is_chapter_opening: false,
    quality_flags: ["license_check_needed"],
    source_text: "[Editor's note: Michaelmas fell on 29 September.]",
    clean_text: "[Editor's note: Michaelmas fell on 29 September.]",
  },
];

const CHAPTERS = [
  { chapter_id: "ch01", title: "Chapter 1", n: "01" },
  { chapter_id: "ch02", title: "Chapter 2", n: "02" },
];

/* ---- glossary: invariant terms (sidecar authoritative) ---- */
const GLOSSARY = [
  {
    term_id: "g_michaelmas", doc_id: "austen_pp_en",
    source_term: "Michaelmas", expected_target: "l\u1ec5 Th\u00e1nh Michael",
    allowed_variants: ["l\u1ec5 Michaelmas"], forbidden_variants: ["l\u1ec5 Gi\u00e1ng sinh"],
    chapter_scope: "global", status: "locked", confidence: 0.95,
    occurrences: [{ block_id: "b0014", span: [19, 29] }],
  },
  {
    term_id: "g_chaise_four", doc_id: "austen_pp_en",
    source_term: "chaise and four", expected_target: "c\u1ed7 xe t\u1ee9 m\u00e3",
    allowed_variants: ["xe ng\u1ef1a b\u1ed1n con"], forbidden_variants: [],
    chapter_scope: "global", status: "proposed", confidence: 0.8,
    occurrences: [{ block_id: "b0010", span: [180, 195] }],
  },
  {
    term_id: "g_fortune", doc_id: "austen_pp_en",
    source_term: "good fortune", expected_target: "gia s\u1ea3n l\u1edbn",
    allowed_variants: ["t\u00e0i s\u1ea3n l\u1edbn"], forbidden_variants: ["may m\u1eafn"],
    chapter_scope: "global", status: "human_verified", confidence: 0.9,
    occurrences: [{ block_id: "b0002", span: [54, 66] }],
  },
];

/* ---- entities (sidecar authoritative mentions) ---- */
const ENTITIES = [
  {
    entity_id: "e_mr_bennet", doc_id: "austen_pp_en",
    canonical_source: "Mr. Bennet", canonical_target: "\u00d4ng Bennet",
    entity_type: "person", gender: "male",
    aliases_source: ["Mr Bennet"], aliases_target: ["ng\u00e0i Bennet"],
    pronoun_policy: "\u00f4ng",
    mentions: [
      { block_id: "b0004", surface: "Mr. Bennet", span: [1, 11] },
      { block_id: "b0005", surface: "Mr. Bennet", span: [0, 10] },
    ],
  },
  {
    entity_id: "e_mrs_bennet", doc_id: "austen_pp_en",
    canonical_source: "Mrs. Bennet", canonical_target: "B\u00e0 Bennet",
    entity_type: "person", gender: "female",
    aliases_source: ["his lady", "his wife", "she"], aliases_target: ["b\u00e0 Bennet"],
    pronoun_policy: "b\u00e0",
    mentions: [
      { block_id: "b0004", surface: "his lady", span: [27, 35] },
    ],
  },
  {
    entity_id: "e_netherfield", doc_id: "austen_pp_en",
    canonical_source: "Netherfield Park", canonical_target: "Netherfield Park",
    entity_type: "place", gender: "n/a",
    aliases_source: ["Netherfield"], aliases_target: [],
    pronoun_policy: "n/a",
    mentions: [
      { block_id: "b0004", surface: "Netherfield Park", span: [60, 76] },
    ],
  },
  {
    entity_id: "e_bingley", doc_id: "austen_pp_en",
    canonical_source: "Mr. Bingley", canonical_target: "\u00d4ng Bingley",
    entity_type: "person", gender: "male",
    aliases_source: ["Bingley", "a young man of large fortune"], aliases_target: [],
    pronoun_policy: "\u00f4ng",
    mentions: [
      { block_id: "b0012", surface: "Mr. Bingley", span: [44, 55] },
    ],
  },
];

/* ---- chapter summaries ---- */
const SUMMARIES = [
  {
    doc_id: "austen_pp_en", chapter_id: "ch01",
    summary_source: "Mrs. Bennet presses her indifferent husband to call on Mr. Bingley, a wealthy newcomer who has taken Netherfield Park, hoping to match him with one of their daughters.",
    source: "human",
    characters_present: ["e_mr_bennet", "e_mrs_bennet", "e_bingley", "e_netherfield"],
    setting: "Longbourn, the Bennet household",
    emotional_tone: "ironic, comedic",
    confidence: 0.9,
  },
  {
    doc_id: "austen_pp_en", chapter_id: "ch02",
    summary_source: "",
    source: "",
    characters_present: [],
    setting: "",
    emotional_tone: "",
    confidence: 0,
  },
];

/* ---- reference subset (draft in working CSV; reviewed promoted to JSONL) ---- */
const REFERENCES = [
  {
    reference_id: "r0001", block_id: "b0002", stratum: "chapter_opening",
    reference_vi: "L\u00e0 m\u1ed9t s\u1ef1 th\u1eadt ai c\u0169ng c\u00f4ng nh\u1eadn, r\u1eb1ng m\u1ed9t ng\u01b0\u1eddi \u0111\u00e0n \u00f4ng \u0111\u1ed9c th\u00e2n s\u1edf h\u1eefu gia s\u1ea3n l\u1edbn h\u1eb3n ph\u1ea3i \u0111ang thi\u1ebfu m\u1ed9t ng\u01b0\u1eddi v\u1ee3.",
    status: "reviewed", translated_by: "U3 \u00b7 Lan", reviewed_by: "U2 \u00b7 Mai",
    source: "human", ai_model: "", canonical: true,
  },
  {
    reference_id: "r0002", block_id: "b0004", stratum: "dialogue",
    reference_vi: "\u201CB\u1ea3n th\u00e2n m\u00ecnh Bennet th\u00e2n m\u1ebfn,\u201D m\u1ed9t h\u00f4m b\u00e0 nh\u00e0 n\u00f3i v\u1edbi \u00f4ng, \u201C\u00f4ng \u0111\u00e3 nghe tin Netherfield Park r\u1ed1t cu\u1ed9c c\u0169ng c\u00f3 ng\u01b0\u1eddi thu\u00ea ch\u01b0a?\u201D",
    status: "draft", translated_by: "U3 \u00b7 Lan", reviewed_by: "",
    source: "", ai_model: "claude-3.5", canonical: false,
  },
];

/* ---- review state (working/review_state.json) ---- */
const REVIEW = {
  /* block_id -> reviewed */
  blocks: {
    b0001: { reviewed: true, reviewed_by: "U2 \u00b7 Mai" },
    b0002: { reviewed: true, reviewed_by: "U2 \u00b7 Mai" },
    b0003: { reviewed: true, reviewed_by: "U3 \u00b7 Lan" },
    b0004: { reviewed: true, reviewed_by: "U2 \u00b7 Mai" },
    b0005: { reviewed: false },
    b0006: { reviewed: false },
    b0007: { reviewed: true, reviewed_by: "U2 \u00b7 Mai" },
    b0008: { reviewed: false },
    b0009: { reviewed: false },
    b0010: { reviewed: false },
    b0011: { reviewed: true, reviewed_by: "U3 \u00b7 Lan" },
    b0012: { reviewed: false },
    b0013: { reviewed: false },
    b0014: { reviewed: false },
  },
};

/* ---- validation errors (validate.py --json) ---- */
const VALIDATION = [
  { file: "glossary.jsonl", block_id: "b0010", term_id: "g_chaise_four", location: "occurrences[0].span", severity: "error",
    message: "Span [180,195] does not match source substring after clean_text edit." },
  { file: "chapter_summaries.jsonl", chapter_id: "ch02", location: "summary_source", severity: "error",
    message: "Required field 'summary_source' is empty for reviewed chapter ch02." },
  { file: "document.json", block_id: "b0006", location: "quality_flags", severity: "warning",
    message: "Block flagged 'unclear_dialogue' but not yet reviewed." },
];

const QUALITY_FLAGS = ["ok","needs_review","extraction_error","ocr_suspect","broken_paragraph","unclear_dialogue","license_check_needed"];
const BLOCK_TYPES = ["heading","paragraph","dialogue","footnote"];

Object.assign(window, {
  DATA: { DOC, BLOCKS, CHAPTERS, GLOSSARY, ENTITIES, SUMMARIES, REFERENCES, REVIEW, VALIDATION, QUALITY_FLAGS, BLOCK_TYPES },
});
