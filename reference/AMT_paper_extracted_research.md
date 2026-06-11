# AMT: Agentic Machine Translation for Long-form Volumes

Phuc H. Duong^1[0000-0001-8746-4606]^, Ngoc-Tu Huynh^1[0000-0001-9402-8001]^, Huy T. Phan^1[0009-0007-8021-1295]^, and Hien T. Nguyen^2[0000-0002-1905-8697]^

^1^ Artificial Intelligence Laboratory, Faculty of Information Technology,  
Ton Duc Thang University, Ho Chi Minh City, Vietnam  
{duonghuuphuc, huynhngoctu, phanthanhhuy.st}@tdtu.edu.vn

^2^ Faculty of Data Science in Business, Ho Chi Minh University of Banking,  
Ho Chi Minh City, Vietnam  
nthien@hub.edu.vn

## Abstract

Translating long-form technical volumes remains a persistent challenge in machine translation, requiring not only semantic fidelity but also discourse coherence, consistent terminology, and preservation of structural elements such as figures and equations. This paper introduces Agentic Machine Translation (AMT), a framework that treats long-form volume translation as a coordinated multi-stage process rather than a single-pass task. AMT integrates layout-aware preprocessing with OCR, segment-wise translation, dual-signal quality evaluation combining embeddings and LLM-based scoring, and document reconstruction with multimodal reintegration. To evaluate the approach, we curate a domain-diverse dataset and conduct both automatic and human assessments. Results show that AMT achieves consistently higher similarity scores and human ratings than strong baselines, with clear advantages in structurally complex and extensive technical volumes. While challenges remain in handling specialized terminology and reducing computational overhead, the findings demonstrate the promise of agentic strategies for advancing the translation of extensive technical volumes.

**Keywords:** Agentic AI · Large language models · Machine translation.

## 1 Introduction

The ability to translate long-form volumes with high fidelity is increasingly central to global knowledge exchange. Scientific articles, technical manuals, legal contracts, and policy reports contain complex discourse structures, specialized terminology, and cross-references that span many pages. Unlike short sentences or isolated paragraphs, these materials demand translation systems that preserve coherence across distant context, maintain consistent terminology and style, and align textual content with non-textual elements such as figures, tables, and equations. As cross-lingual dissemination of research and professional content accelerates, reliable long-form volume translation is no longer a convenience but a prerequisite for equitable participation in science, education, and industry.

Machine translation (MT) has advanced considerably, evolving from phrase-based statistical approaches to neural architectures. Statistical systems provided robust local correspondences but struggled with discourse phenomena. Neural MT introduced sequence-to-sequence learning and attention mechanisms, culminating in Transformer-based models that set new benchmarks in performance and efficiency. More recently, document-level MT has targeted issues such as coherence, coreference, and lexical consistency. Yet translating long and structurally complex volumes remains challenging: current systems typically rely on fixed context windows, leading to truncation or ad hoc chunking that risks omissions, inconsistencies, and degraded discourse links. Moreover, most pipelines treat documents as homogeneous text, underemphasizing layout, structural hierarchy, and visual elements that are essential for readability and trustworthiness.

The emergence of large language models (LLMs) has expanded the design space for translation. Modern LLMs not only map text between languages but also plan, reason, invoke tools, and refine outputs iteratively. Prompting strategies such as chain-of-thought [12] and tool-augmented reasoning [13] demonstrate their potential to orchestrate multi-step workflows rather than perform a single-pass transformation. However, naïve document-level prompting inherits key limitations, including dependence on long context windows, susceptibility to omissions in ultra-long inputs, and the absence of explicit mechanisms for global consistency, structural alignment, or systematic quality assurance. Strong local fluency does not necessarily guarantee faithful document-level fidelity.

In this paper, we propose Agentic Machine Translation (AMT), a framework that reconceptualizes long-form volume translation as a coordinated process rather than a monolithic task. AMT integrates four stages: (i) document preprocessing and structural analysis with layout-aware OCR; (ii) segment-wise translation under a controller that enforces token constraints and dynamic segmentation; (iii) dual-signal quality evaluation via back-translation with embedding-based and LLM-based scoring; and (iv) document reconstruction that faithfully reintegrates visual and structural components. We also curate a diverse dataset of long-form volumes with human annotations to evaluate the framework. Experiments demonstrate that AMT achieves higher similarity scores and stronger human ratings than state-of-the-art baselines, particularly in structurally complex and technical volumes.

The remainder of the paper is organized as follows. Section 2 reviews related work in document-level MT, LLM prompting, and agentic systems. Section 3 describes the AMT pipeline, including preprocessing, controller logic, translation, and evaluation. Section 4 presents the dataset, experimental setup, and results. Section 5 concludes the paper.

*Corresponding author: Phuc H. Duong, duonghuuphuc@tdtu.edu.vn*

## 2 Related Work

Machine translation research has progressed from statistical to neural paradigms over the past two decades. In the mid-2000s, Statistical Machine Translation (SMT), particularly phrase-based systems such as Moses [6], dominated the field by learning probabilistic mappings between text segments, though extensions incorporating syntax often provided only limited gains. By the 2010s, Neural Machine Translation (NMT) emerged, with sequence-to-sequence models [8] and attention mechanisms [1] enabling end-to-end learning and consistently outperforming SMT. The introduction of the Transformer architecture [9] further improved efficiency and quality, establishing the foundation of modern NMT. Advances in multilingual models [5] expanded translation coverage across hundreds of languages, while more recent research has emphasized document-level translation; however, extending these methods to long-form volumes introduces unique challenges in discourse coherence, pronoun resolution, and global consistency [2,10]. These developments highlight the persistent difficulty of achieving global consistency across long texts, motivating exploration of LLM-based agentic approaches.

Building on these advances, the rise of large language models (LLMs) has shifted attention toward more general, reasoning-driven approaches to language tasks. Rather than relying on task-specific MT systems, LLMs such as OpenAI's GPT and Google Gemini can be guided through prompting to perform translation alongside a wide range of complex tasks. Research in this direction has emphasized methods to enhance reasoning capabilities: Chain-of-Thought (CoT) prompting encourages step-by-step reasoning [12], ReAct integrates reasoning with external actions for tool use and knowledge retrieval [13], and systems such as AutoGPT^3^ demonstrate autonomous planning and recursive problem solving. These agentic strategies suggest that LLMs are capable not only of producing translations but also of coordinating multi-step workflows, paving the way for their application to long-form volume translation.

Recent studies have begun to apply such agent-based strategies directly to machine translation. A straightforward approach is to use powerful LLMs for document-level translation, yet these methods often struggle when scaled to the size of complete technical volumes. For example, [11] reported that GPT-4 can produce fluent and coherent translations that surpass commercial MT systems under human evaluation. However, one-shot Doc2Doc prompting faces critical limitations, including sensitivity to context-window constraints, the risk of omissions in ultra-long inputs, and the absence of mechanisms for ensuring consistency of terminology and style across an entire document. To address these challenges, multi-stage pipelines have been explored. In [3], the authors proposed a step-by-step framework where the model iteratively drafts, refines, and proofreads translations, achieving notable gains over single-pass methods. Other efforts combine prompting with parameter-efficient fine-tuning; for instance, [14] demonstrated that adapting a small subset of parameters via QLoRA yields significant BLEU improvements over zero-shot prompting, highlighting the value of targeted specialization. More recently, multi-agent frameworks such as GRAFT [4] have attempted to model discourse structure explicitly, segmenting documents into graph-based units and coordinating their translation to enhance coherence.

Despite these advances, current approaches to LLM-based translation still face important challenges. Systems often struggle to maintain coherence across long and complex documents, and their mechanisms for iterative quality control remain limited. Furthermore, structural alignment and the integration of multimodal elements such as figures and tables are rarely addressed in depth. These open issues motivate our proposed Agentic Machine Translation (AMT) framework, which seeks to advance long-form volume translation by combining structure-aware preprocessing, controlled agentic translation with dynamic segmentation, iterative evaluation through back-translation, and the reintegration of visual components. In this way, our work addresses key aspects of long-form fidelity while remaining extensible to future research directions in agentic translation.

^3^ https://github.com/Significant-Gravitas/AutoGPT

## 3 Proposed Method

As illustrated in Figure 1, the proposed system consists of four main stages: document preprocessing and structural analysis, segment-wise machine translation, automatic evaluation of translation quality, and document reconstruction with multimodal output generation. Throughout this pipeline, we leverage large language models and layout-aware OCR to ensure both semantic fidelity and structural consistency. To demonstrate the effectiveness of our approach, we conduct experiments on English-to-Vietnamese document translation tasks.

### 3.1 Document Preprocessing and Structural Analysis

The preprocessing stage is designed to analyze the hierarchical structure of extensive volumes and accurately identify the placement of figures within them. In this study, we employ Mistral AI's OCR technology^4^, which not only provides high-precision text extraction but also generates detailed, layout-aware representations of document structure — including chapters, sections, tables, and figures. This marks a significant improvement over traditional OCR approaches, which typically output plain text and lack awareness of document hierarchy or spatial organization. By preserving the logical, hierarchical, and visual relationships within the source material, Mistral OCR enables the reliable detection of section boundaries and the precise localization of figures. This structural fidelity is essential for our methodology, as it facilitates a nuanced understanding of both content flow and embedded visual elements. As a result, downstream processing and knowledge extraction become significantly more accurate.

After OCR processing, the entire volume is divided into discrete segments, each corresponding to a specific structural unit (e.g., chapter or section). This segmentation enables a modular processing strategy, allowing the translation agent to process well-defined and contextually coherent portions of the document. Each segment is represented in JSON format, with keys corresponding to these structural units, along with metadata indicating the locations of detected figures.

For the purposes of this work, figures are defined broadly to include images, diagrams, tables, and equations. These elements are preserved in their original form and are not subjected to translation, ensuring the integrity of the document's visual information. The final output of the preprocessing stage is a collection of structured JSON files, each representing a segment of the source volume, ready for subsequent translation and analysis.

^4^ https://docs.mistral.ai/capabilities/document_ai/basic_ocr/

**Fig. 1.** Workflow of the proposed Agentic Machine Translation (AMT) system.

### 3.2 Segment-wise Machine Translation Agent

Once the data has been partitioned into discrete segments, the next stage is carried out by the TranslationAgent. The primary objective of this agent is to translate each segment from the source language to the target language by leveraging prompt-based techniques with large language models. Given a sequence of *n* segments, the agent processes and translates each segment sequentially.

To ensure compatibility with the LLM's input and output constraints, a Controller module is employed. Before submitting each segment for translation, the Controller evaluates its input token count. If a segment exceeds the LLM's maximum input token limit, it is further subdivided into smaller subsegments. Similarly, the Controller monitors the output token count and, if necessary, resplits segments to ensure all translations remain within the model's operational limits. This dynamic segmentation maintains both system reliability and efficiency.

To clarify the operational workflow for translating volume segments, we provide a detailed pseudocode in Algorithm 1. As illustrated, the Controller module systematically checks each document segment to ensure that the input and output token limits imposed by the LLM are not exceeded. If a segment surpasses these constraints, it is recursively divided into smaller subsegments. Each subsegment is then translated individually, and the translated outputs are collected to form the final set of translated segments. This dynamic segmentation and translation process ensures that all segments remain within the model's operational limits, thus maintaining both translation quality and system efficiency.

Two key parameters are emphasized during the translation process, i.e., `temperature` and `frequency_penalty`. The temperature parameter controls the stochasticity of token selection; lower values (typically between 0 and 0.3) promote more deterministic and consistent outputs, which is advantageous for translations requiring high fidelity to the original text. In contrast, higher values introduce greater variability, which may improve creativity but can reduce translation accuracy. The `frequency_penalty` parameter reduces repetition by penalizing the model for generating duplicate words or phrases, thereby improving the fluency and naturalness of the output.

### Algorithm 1. Segment-wise Translation with Controller

**Require:** List of document segments \(S = \{s_1, s_2, ..., s_n\}\)  
**Ensure:** List of translated segments \(T\)

```text
1:  T <- empty set
2:  for each s in S do
3:      if InputTokenCount(s) > MaxInputTokens then
4:          S_sub <- SplitSegment(s)
5:          for each s_sub in S_sub do
6:              t_sub <- Translate(s_sub)
7:              if OutputTokenCount(t_sub) > MaxOutputTokens then
8:                  S_subsub <- FurtherSplit(s_sub)
9:                  for each s_subsub in S_subsub do
10:                     t_subsub <- Translate(s_subsub)
11:                     Append t_subsub to T
12:                 end for
13:             else
14:                 Append t_sub to T
15:             end if
16:         end for
17:     else
18:         t <- Translate(s)
19:         if OutputTokenCount(t) > MaxOutputTokens then
20:             S_sub <- SplitSegment(s)
21:             for each s_sub in S_sub do
22:                 t_sub <- Translate(s_sub)
23:                 Append t_sub to T
24:             end for
25:         else
26:             Append t to T
27:         end if
28:     end if
29: end for
30: return T
```

These mechanisms and hyperparameter configurations jointly ensure that translations remain semantically faithful and stylistically coherent. The final output of this stage is a set of translated segments, each corresponding to its respective portion of the original document.

### 3.3 Automatic Evaluation of Translation Quality

Evaluation plays a central role in the AMT pipeline, serving both to assess translation quality and to provide feedback for iterative refinement. Instead of relying on BLEU as the primary evaluation metric, this study adopts a back-translation approach. BLEU depends on reference translations for *n*-gram overlap, which are not available in our setting. Moreover, it primarily captures surface-level lexical overlap and is less effective at reflecting semantic adequacy, paraphrasing, or discourse-level fidelity — factors that are particularly critical in the translation of extensive technical volumes. To address these limitations, we combine back-translation with two complementary measures: embedding-based cosine similarity and LLM-based semantic scoring. Together, these signals provide a more direct and semantically oriented estimate of translation quality, which can subsequently be aggregated into a single similarity score.

To obtain the back-translation, we re-apply the TranslationAgent module, this time translating from the system output (Vietnamese) back into English. This yields three parallel texts: the original English document, the Vietnamese translation, and the English back-translation.

For the embedding-based similarity, both the original segment and its back-translated counterpart are encoded using the pretrained MPNet model [7]. Because MPNet restricts input length to 384 tokens, segments are evaluated at the paragraph level. The Controller ensures paragraph alignment across the volume's translations, allowing each paragraph in the original to be directly mapped to its counterpart in the back-translation. Given two aligned paragraphs, the resulting embeddings \(v_{orig}\) and \(v_{back} \in \mathbb{R}^{768}\) are compared using cosine similarity. The final similarity score for a segment, Score_cosine, is obtained by averaging across all paragraph pairs.

The second strategy employs an LLM-based evaluation using Google Gemini 2.5 Flash. Unlike MPNet, Gemini supports sequences of up to 1M tokens^5^, so segments do not require truncation. For each segment pair, the agent provides both texts together with a standardized prompt and returns a semantic similarity score on a normalized scale from 0 (completely dissimilar) to 1 (identical in meaning). To reduce variability, the evaluation is repeated twice and averaged, producing a stable score denoted as Score_LLM. This method leverages the contextual understanding of LLMs, capturing paraphrastic and discourse-level equivalence that may not be reflected in embedding similarity alone.

Let \(i\) denote the segment index, with \(s_i^{orig}\) and \(s_i^{back}\) representing the original and back-translated segments, respectively. To unify the two signals, the Controller integrates them into a single similarity score, formulated as a convex combination parameterized by \(\alpha \in [0, 1]\), ensuring the result remains within the normalized range [0, 1]. In this study, we set \(\alpha = 0.5\), giving equal weight to both measures. The overall similarity for segment \(i\) is defined as

\[
\mathrm{Sim}(s_i^{orig}, s_i^{back}) = \alpha \cdot \mathrm{Score}_{cosine} + (1-\alpha) \cdot \mathrm{Score}_{LLM}
\tag{1}
\]

This dual-signal approach balances the coarse-grained semantic proximity captured by embeddings with the fine-grained contextual judgment of LLMs, yielding a more reliable measure of translation quality. Importantly, the similarity score is not only used for evaluation but also serves as a feedback signal for optimizing the translation process. Specifically, the TranslationAgent module can be reinvoked iteratively until the similarity score converges or a predefined iteration limit is reached, thereby enhancing overall translation fidelity while preserving computational efficiency.

^5^ https://cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/2-5-flash

### 3.4 Document Reconstruction and Output Generation

In the final stage, the Controller is responsible for assembling the finalized, multi-chapter volume from the individual evaluated segments. To ensure structural consistency, the Controller first verifies that the translated segments preserve the hierarchical organization and section boundaries present in the original source volume. This alignment is essential for maintaining the logical flow and readability of the final output.

Once structural integrity is confirmed, the Controller integrates the figures back into the translated text. Using the location metadata obtained from the Mistral OCR during the preprocessing stage, each figure is accurately positioned within its corresponding section. This precise reintegration process ensures that visual elements such as images, diagrams, tables, and equations remain aligned with their corresponding textual context.

The finalized document, now comprising the translated text and reinserted figures, is generated in Markdown format. This choice of format preserves both the content structure and visual layout, while facilitating downstream use cases such as publication, sharing, or further automated processing. The output of this finalization stage is a cohesive, translation-faithful document that mirrors the structure and multimodal elements of the original long-form volume.

## 4 Experiments

### 4.1 Dataset

To evaluate the proposed system, we curated a dataset consisting of seven long-form volumes drawn from diverse domains, including business management, education, economics, business strategy, and research articles. Unlike standard document-level MT datasets that typically focus on short-form content like news articles, our dataset consists of full-length technical books and comprehensive monographs. Table 1 summarizes their key statistics: the number of pages (\(P\)), chapters or sections (\(C\)), the word counts of the source and target documents (\(W_{EN}\) and \(W_{VI}\)), and the number of figures (\(F\)). It is important to note that word counting conventions differ between English and Vietnamese. In English, each lexical item in a sentence is counted as a separate word. In contrast, Vietnamese presents a distinction between single and compound words, i.e., while multi-syllable expressions such as "hôm nay" (today) are linguistically treated as a single lexical unit, they may consist of multiple space-separated tokens.

For each source document, we also report the corresponding target word counts in Vietnamese (\(W_{VI}\)). These values are derived from the translated outputs produced by our system, rather than from any pre-existing reference translations, as no gold-standard targets are available in this setting. Word counts are reported here solely for descriptive purposes, reflecting the size of the generated translations after iterative optimization.

The corpus spans a broad range of complexity, making it well-suited for benchmarking long-document translation. Documents 1-5 represent the low- to medium-complexity category, emphasizing fluency, stylistic consistency, and the handling of clause-heavy sentences with nuanced expressions in managerial and pedagogical contexts. By contrast, Documents 6 and 7 are highly challenging, containing formal statistical and algorithmic terminology together with symbol-dense prose. Collectively, these materials - which average approximately 90,000 words per document - evaluate the system's ability to balance stylistic fluency in narrative texts with semantic precision in technically demanding, structurally complex documents.

**Table 1.** Statistics of the experimental dataset consisting of seven long-form volumes, including the number of pages (\(P\)), chapters or sections (\(C\)), source and target word counts (\(W_{EN}\), \(W_{VI}\)), and the number of figures (\(F\)).

| No. | Title of Volume | P | C | W_EN | W_VI | F |
|---:|---|---:|---:|---:|---:|---:|
| 1 | The Cambridge Handbook of Responsible AI | 528 | 8 | 216,835 | 227,579 | 21 |
| 2 | AI: Future of Education Teaching | 253 | 12 | 44,963 | 57,991 | 41 |
| 3 | HBR Guide to AI Basics for Managers | 196 | 22 | 39,838 | 41,693 | 4 |
| 4 | Co-Intelligence: Living and Working With AI | 256 | 9 | 48,807 | 50,926 | 3 |
| 5 | Power and Prediction | 288 | 18 | 67,551 | 71,058 | 18 |
| 6 | Trustworthy Machine Learning | 266 | 18 | 94,209 | 103,048 | 180 |
| 7 | Advances and Challenges in Foundation Agents | 396 | 5 | 133,559 | 149,187 | 180 |

### 4.2 Experimental Setup

The TranslationAgent was implemented with Google Gemini 2.5 Pro via API, and the Controller module enforced input and output token constraints through dynamic segmentation. To balance fidelity and fluency, the decoding temperature was set to 0.3 and the `frequency_penalty` to 0.2. Token limits were restricted to 80% of the default values in Gemini 2.5 Pro to ensure stable outputs.

Automatic evaluation employed a back-translation protocol with two complementary signals. First, embedding-based similarity was computed by encoding the original and back-translated segments using the `all-mpnet-base-v2` model from Sentence Transformers^6^. Second, an LLM-based similarity score was obtained with Gemini 2.5 Flash on a normalized [0, 1] scale; to reduce variability, the LLM-based evaluation was performed twice and the results were averaged. The final similarity for each segment was computed as a convex combination defined in Eq. 1, with \(\alpha = 0.5\), thereby balancing coarse-grained semantic proximity from embeddings with fine-grained contextual judgment from LLMs.

Guided by this similarity score, the Controller could iteratively reinvoke the TranslationAgent, with a maximum of four attempts or until convergence was reached, ensuring that low-quality outputs were systematically refined without excessive computational overhead.

For human evaluation, five bilingual annotators proficient in English and Vietnamese assessed translation quality based on a 5-point scale covering adequacy, fluency, terminology consistency, coherence, and structural integrity, with an additional overall score reflecting holistic quality. Annotators independently evaluated outputs from our system, Google Translate, and ChatGPT, with the latter two serving as baselines. Final document-level scores were obtained by averaging ratings across annotators. Google Translate outputs were generated via the Google Cloud Translation API^7^, while ChatGPT^8^ translations were produced using the ChatGPT-5 model via the official interface in single-pass translation mode, in which the full source document was uploaded and directly prompted for translation into the target language.

^6^ https://huggingface.co/sentence-transformers/all-mpnet-base-v2  
^7^ https://cloud.google.com/translate/  
^8^ https://chatgpt.com/

### 4.3 Experimental Results

Table 2 presents the results of both automatic and human evaluation. The proposed method achieves similarity scores ranging from 0.7962 to 0.8784 and overall human ratings above 3.8 across all long-form volumes. In contrast, Google Translate obtains considerably lower human ratings, between 0.9 and 2.1, reflecting its difficulty in capturing long-range dependencies and maintaining discourse-level consistency. ChatGPT yields higher scores than Google Translate, with human scores between 2.0 and 3.1, but its translations are frequently truncated due to context length constraints, leading to outputs that resemble summaries rather than full translations.

The results highlight that AMT consistently delivers higher fidelity and coherence in long-form volume translation. The improvement is particularly notable for narrative and managerial volumes (Documents 1-5), indicating strong fluency and adequacy. However, performance is relatively lower for highly technical volumes (Documents 6 and 7), where similarity scores remain below 0.83. This suggests that domain-specific terminology and symbol-dense content remain challenging.

To further ensure the credibility of the human evaluation, we examined the consistency of annotators' judgments across documents. The ratings showed stable patterns with no major discrepancies among annotators, indicating that the human evaluation results are reliable and provide a sound basis for comparing the systems.

While the dual-signal evaluation ensures robust quality assessment, it also introduces additional computational cost, as each segment requires repeated LLM-based evaluations before convergence. These findings indicate that although AMT achieves clear gains over existing baselines, future work should aim to enhance efficiency and strengthen handling of technical terminology.

**Table 2.** Experimental results of automatic (similarity scores) and human evaluation (5-point scale) on the dataset, comparing AMT with Google Translate and ChatGPT.

| Document | Similarity Score | Our method | Google Translate | ChatGPT |
|---:|---:|---:|---:|---:|
| 1 | 0.8107 | 3.8 | 1.4 | 2.6 |
| 2 | 0.8589 | 4.6 | 1.2 | 3.1 |
| 3 | 0.8449 | 4.1 | 2.1 | 2.8 |
| 4 | 0.8253 | 4.1 | 1.6 | 2.5 |
| 5 | 0.8784 | 4.2 | 1.9 | 2.5 |
| 6 | 0.7962 | 3.9 | 1.1 | 2.3 |
| 7 | 0.8043 | 4.0 | 0.9 | 2.0 |

## 5 Conclusion

This paper presented Agentic Machine Translation (AMT), a framework for long-form volume translation that integrates structure-aware preprocessing, controlled segment-wise translation, dual-signal quality evaluation, and document reconstruction with multimodal integration. Experiments on diverse long-form volumes show that AMT achieves consistently higher performance than baseline systems in both automatic similarity metrics and human evaluation. These findings highlight the advantages of treating translation as an agentic process rather than a single-pass task, particularly for maintaining discourse coherence and structural fidelity. AMT operates in a reference-free, unsupervised evaluation setting, avoiding reliance on parallel corpora or gold-standard translations. To support further research, the resources from this study — including the curated dataset, annotation results, evaluation sheet, and system prompts — are available in our GitHub repository^9^. Remaining challenges in scaling agentic workflows to extensive volumes include domain-specific terminology and computational overhead, which future work will address through domain adaptation, efficiency gains, and broader language coverage.

^9^ https://github.com/duonghuuphuc/AMT-V1

## References

1. Bahdanau, D., Cho, K., Bengio, Y.: Neural machine translation by jointly learning to align and translate. arXiv preprint arXiv:1409.0473 (2014)

2. Bawden, R., Sennrich, R., Birch, A., Haddow, B.: Evaluating discourse phenomena in neural machine translation. arXiv preprint arXiv:1711.00513 (2017)

3. Briakou, E., Luo, J., Cherry, C., Freitag, M.: Translating step-by-step: Decomposing the translation process for improved translation quality of long-form texts. In: Proceedings of the Ninth Conference on Machine Translation. pp. 1301-1317 (2024)

4. Dutta, H., Manchanda, S., Bapat, P., Gurjar, M.R., Bhattacharyya, P.: GRAFT: A graph-based flow-aware agentic framework for document-level machine translation. arXiv preprint arXiv:2507.03311 (2025)

5. Johnson, M., Schuster, M., Le, Q.V., Krikun, M., Wu, Y., Chen, Z., Thorat, N., Viégas, F., Wattenberg, M., Corrado, G., et al.: Google's multilingual neural machine translation system: Enabling zero-shot translation. Transactions of the Association for Computational Linguistics 5, 339-351 (2017)

6. Koehn, P., Hoang, H., Birch, A., Callison-Burch, C., Federico, M., Bertoldi, N., Cowan, B., Shen, W., Moran, C., Zens, R., et al.: Moses: Open source toolkit for statistical machine translation. In: Proceedings of the 45th annual meeting of the association for computational linguistics companion volume proceedings of the demo and poster sessions. pp. 177-180. Association for Computational Linguistics (2007)

7. Song, K., Tan, X., Qin, T., Lu, J., Liu, T.Y.: MPNet: Masked and permuted pre-training for language understanding. Advances in neural information processing systems 33, 16857-16867 (2020)

8. Sutskever, I., Vinyals, O., Le, Q.V.: Sequence to sequence learning with neural networks. Advances in neural information processing systems 27 (2014)

9. Vaswani, A., Shazeer, N., Parmar, N., Uszkoreit, J., Jones, L., Gomez, A.N., Kaiser, Ł., Polosukhin, I.: Attention is all you need. Advances in neural information processing systems 30 (2017)

10. Voita, E., Sennrich, R., Titov, I.: Context-aware neural machine translation learns anaphora resolution. arXiv preprint arXiv:1805.10163 (2018)

11. Wang, L., Lyu, C., Ji, T., Zhang, Z., Yu, D., Shi, S., Tu, Z.: Document-level machine translation with large language models. arXiv preprint arXiv:2304.02210 (2023)

12. Wei, J., Wang, X., Schuurmans, D., Bosma, M., Xia, F., Chi, E., Le, Q.V., Zhou, D., et al.: Chain-of-thought prompting elicits reasoning in large language models. Advances in neural information processing systems 35, 24824-24837 (2022)

13. Yao, S., Zhao, J., Yu, D., Du, N., Shafran, I., Narasimhan, K., Cao, Y.: ReAct: Synergizing reasoning and acting in language models. In: International Conference on Learning Representations (ICLR) (2023)

14. Zhang, X., Rajabi, N., Duh, K., Koehn, P.: Machine translation with large language models: Prompting, few-shot learning, and fine-tuning with qlora. In: Proceedings of the Eighth Conference on Machine Translation. pp. 468-481 (2023)
