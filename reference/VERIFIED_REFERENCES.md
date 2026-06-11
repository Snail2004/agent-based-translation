# Verified References

Last checked: 2026-06-01

Purpose: keep a verified reading list for the agent-based EN-VI long-document translation research. Do not cite `consensus.app` in the thesis. Prefer ACL Anthology, arXiv, publisher pages, official dataset pages, or official GitHub repositories.

## Core Papers To Read First

| Priority | Reference | Source | Use In Thesis | Note |
|---|---|---|---|---|
| High | Karpinska & Iyyer (2023), "LLMs Effectively Leverage Document-level Context for Literary Translation, but Critical Errors Persist" | https://aclanthology.org/2023.wmt-1.41/ | Motivation for document-level literary translation, error analysis, need for narrative/context-aware systems | Strong fit for motivation and D5-style error discussion. |
| High | He et al. (2024), "Exploring Human-Like Translation Strategy with Large Language Models" / MAPS | https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00642/119992/Exploring-Human-Like-Translation-Strategy-with | Supports source analysis before translation, close to Interpretation Brief / Narrative Understanding Agent | Often appears as arXiv 2023, final TACL 2024. |
| High | Jiang et al. (2023), "Discourse-Centric Evaluation with a Densely Annotated Parallel Corpus of Novels" / BWB | https://aclanthology.org/2023.acl-long.435/ | Dataset annotation design for entity, terminology, coreference, quotation; supports D4/ECS | Very relevant for AI-LAB dataset annotation design. |
| High | Wang et al. (2023), "Document-Level Machine Translation with Large Language Models" | https://aclanthology.org/2023.emnlp-main.1036/ | Background for DocMT with LLMs | Core background paper. |
| High | Cui et al. (2024), "CAP: Context-Aware Prompting for Document-Level Machine Translation" | https://aclanthology.org/2024.findings-acl.646/ | Supports context selection, summaries, relevant demonstrations, retrieval/context layer | Directly relevant to retrieval/context pack design. |
| High | Kocmi & Federmann (2023), GEMBA-MQM | https://arxiv.org/abs/2310.13988 | LLM-as-judge, MQM-style error span detection, CriticAgent Tier 2 | Use with caution: proprietary LLM evaluator dependence. |
| High | Fernandes et al. (2023), AutoMQM | https://arxiv.org/abs/2308.07286 | Automatic MQM error annotation and LLM-as-judge framing | Good support for CriticAgent design. |
| High | Guerreiro et al. (2023), xCOMET | https://arxiv.org/abs/2310.10482 | Open metric with error-span detection | Useful metric/evaluation baseline. |
| High | Domhan et al. (2025), "Same Evaluation, More Tokens: the Impact of Input Length on the Reasoning Performance of LLMs" | https://arxiv.org/abs/2505.01761 | Warning about long-input LLM judge length bias | Supports block/focus-sentence evaluation instead of huge prompts. |
| High | Cai et al. (2021), "Neural Machine Translation with Monolingual Translation Memory" | https://aclanthology.org/2021.acl-long.567/ | Retrieval / translation memory background | Not EN-VI specific, but important for memory-augmented MT. |
| High | Dinu et al. (2019), "Training Neural Machine Translation To Apply Terminology Constraints" | https://aclanthology.org/P19-1294/ | Terminology constraints, glossary hard rules | Seminal terminology-constraint paper. |
| High | Bogoychev & Chen (2023), "Terminology-Aware Translation with Constrained Decoding and Large Language Model Prompting" | https://aclanthology.org/2023.wmt-1.80/ | Terminology-aware translate-then-refine; supports Translator + Critic repair | Very relevant to glossary enforcement workflow. |
| High | Bane et al. (2023), "A Study of Three Approaches to Glossary Enforcement" | https://aclanthology.org/2023.eamt-1.34/ | Trade-off between glossary enforcement and fluency | Important caution for T1/glossary claims. |

## Retrieval, Translation Memory, And Context Selection

| Reference | Source | Use | Note |
|---|---|---|---|
| Bouthors et al. (2024), retrieval method comparison for RAMT | https://aclanthology.org/2024.findings-naacl.190/ | Justifies comparing retrieval channels/methods and D6 intrinsic evaluation | Good support for retrieval ablation. |
| Cheng et al. (2022), "Contrastive Translation Memories" | Search official paper before citing | Retrieval memory selection, diversity/info-gain | Verify final source before thesis citation. |
| Nguyen et al. (2023), Diversity-Enabled Translation Memory | https://dblp.org/rec/conf/aciids/NguyenDNB23 | Possible EN-VI / MMR translation-memory related work | Need to check PDF before claiming exact IWSLT En-Vi details. |

## Literary Translation, Multi-Agent, And Recent Work

| Reference | Source | Use | Caution |
|---|---|---|---|
| TransAgents (Wu et al., 2025), TACL | https://aclanthology.org/2025.tacl-1.42/ | Main related work: multi-agent literary translation, global translation guidelines, MHP/BLP evaluation | Already central to V3. |
| TransAgents GitHub | https://github.com/minghao-wu/transagents | Implementation/reference prompts if available | Cite repo only for implementation details, not scientific claims. |
| DITING + AgentEval (2025) | https://arxiv.org/abs/2510.09116 | Recent web-novel evaluation dimensions; possible support for CriticAgent dimensions and D5 | Preprint/recent; do not make it a main pillar. |
| MAS-LitEval (2025) | https://arxiv.org/abs/2506.14199 | Multi-agent literary translation evaluation | Preprint/recent; cite as related trend. |
| SAMAS (2026) | https://arxiv.org/abs/2602.19840 | Agentic style-fidelity translation workflow | Very new; use only as recent related work if needed. |
| HiMATE (2025) | https://arxiv.org/abs/2505.16281 | Hierarchical multi-agent MT evaluation | Evaluation paper, not translation pipeline. |
| Multi-agent Classical Chinese translation | https://pmc.ncbi.nlm.nih.gov/articles/PMC12623723/ | Analogy for multi-agent + RAG + review loop | Different language/domain; use cautiously. |

## Discourse, Consistency, And Entity/Coreference

| Reference | Source | Use | Note |
|---|---|---|---|
| BlonDe (Jiang et al., 2021) | https://aclanthology.org/2021.acl-long.48/ | Document-level MT metric over discourse spans; supports discourse consistency discussion | Useful beside ECS. |
| DCoEM / discourse cohesion evaluation | Verify official source before citing | Cohesion evaluation for DocMT | Candidate only until verified. |
| Miculicich et al., discourse phenomena / entity coreference in MT | Verify exact paper before citing | Supports coreference/discourse memory | Candidate only until exact source is pinned. |

## Agent Memory And Narrative Consistency

| Reference | Source | Use | Caution |
|---|---|---|---|
| Zhang et al. (2024), "A Survey on the Memory Mechanism of LLM-based Agents" | https://arxiv.org/abs/2404.13501 | Background for agent memory, T1-T7 framing | Do not cite volatile citation counts. |
| DOME (2024), long-form story generation with dynamic hierarchical outlining and memory | https://arxiv.org/abs/2412.13575 | Related machinery for narrative memory consistency | Story generation, not MT; cite as analogy. |
| GAM (2026), graph-based agentic memory | https://arxiv.org/abs/2604.12285 | Recent agent memory architecture | Very new; optional. |
| MemAgent | Verify official source before citing | Generate-reflect-revise analogy for Translator/Critic loops | Candidate only until verified. |

## EN-VI And Dataset Background

| Reference / Dataset | Source | Use | Note |
|---|---|---|---|
| PhoMT | https://github.com/VinAIResearch/PhoMT | Shows large EN-VI sentence-level corpus exists, but not long-document literary dataset | Useful for data gap argument. |
| PhoMT paper | https://arxiv.org/abs/2110.12199 | Citation for PhoMT | Verify license/details before dataset reuse. |
| MedEV | https://huggingface.co/datasets/nhuvo/MedEV | EN-VI document/domain dataset comparison | Medical domain, not literary. |
| MedEV paper | https://aclanthology.org/2024.lrec-main.784/ | Citation for MedEV | Use to position document-level EN-VI gap. |
| IWSLT / TED EN-VI | Verify official source before citing | Sentence/speech-style baseline data | Not literary long document. |

## Dataset Source Candidates For AI-LAB

| Source | Link | Use | Caution |
|---|---|---|---|
| Standard Ebooks | https://standardebooks.org/ | Clean public-domain-oriented EPUB sources; good for AI-LAB extraction | Still record the exact book page and license/provenance. |
| Project Gutenberg Terms of Use | https://www.gutenberg.org/policy/terms_of_use.html | Large public-domain text source | Public-domain status may be US-specific; record caveat. |
| Wikisource Copyright Policy | https://wikisource.org/wiki/Wikisource:Copyright_policy | Public-domain/free-license text candidates | Check each page/license tag. |
| WMT 2023 Literary Translation Task | https://www2.statmt.org/wmt23/literary-translation-task.html | Reference for GuoFeng webnovel task used by TransAgents context | Mostly zh-en, not EN-VI. |
| WMT 2024 Literary Translation Task | https://www2.statmt.org/wmt24/literary-translation-task.html | Updated literary translation task context | Mostly not directly reusable for EN-VI AI-LAB. |

## Notes For Thesis Writing

- Cite primary sources, not aggregator pages.
- Separate peer-reviewed papers from arXiv/preprints.
- For AI-LAB dataset design, strongest references are BWB, Karpinska & Iyyer, PhoMT, MedEV, Standard Ebooks/Gutenberg provenance pages.
- For thesis architecture, strongest references are TransAgents, MAPS, Document-Level MT with LLMs, CAP, GEMBA-MQM/AutoMQM/xCOMET, Cai 2021, Dinu 2019, Bogoychev 2023, Bane 2023.
- Treat story-generation memory papers as analogy only, not as MT evidence.
