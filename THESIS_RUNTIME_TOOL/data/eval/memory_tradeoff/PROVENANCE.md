# PROVENANCE — memory_tradeoff judge files (HONEST labels)

ALL judges below are LLMs. There is NO independent human-expert annotation.
The thesis author is not a domain expert in technical VI ML terminology, so a
solo human anchor was not viable for this technical track. This is a stated
LIMITATION, reported as such. Do NOT describe any file here as "human".

| file | judge | mode | note |
|---|---|---|---|
| judgments_gemini.jsonl | Gemini-2.5-flash | API, blind worksheet, 2-orientation swap | our pipeline; conservative swap-resolve -> artifact-LOW harm 21% |
| judgments_human.json | Gemini-3.1-Pro (web) | screenshots + deep analysis, USER-CURATED | filename says "human" for pipeline compatibility but is LLM-assisted+curated; HIGH outlier 47%. RELABEL when reading. |
| judgments_gemini_3.1_pro.jsonl | Gemini-3.1-Pro | cold Antigravity session, read judge_pack_external.md only | controlled |
| judgments_gemini_3.5_flash.jsonl | Gemini-3.5-flash | cold Antigravity session | controlled |
| judgments_claude.json | Claude-Opus-4.6 | cold Antigravity session | CROSS-FAMILY confirmation |
| judgments_glm5.2.jsonl | GLM-5.2 (Zhipu) | cold Antigravity session | 4th FAMILY, pre-registered final judge; LENIENT end (harm 21%) |

Headline finding = majority vote of the 4 cold model-diverse blind judges
(3.1pro_cold, 3.5flash_cold, opus46_cold, glm52_cold) -> see data/reports/memory_tradeoff_panel.json.
