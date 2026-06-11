# EXTRACTOR_EVAL v0.3.1

Generated at: `2026-06-02T02:13:47+07:00`
Pipeline version: `0.3.1`

## Overview
| book | source | format | toc_source | toc_items | extracted | expected | match_rate | low_confidence | fallback | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| jekyll_txt | Project Gutenberg | source.txt | text | 10 | 10 | 10 | 1.0 | False | False | PASS |
| jekyll_epub | Project Gutenberg | source.epub | ncx | 10 | 10 | 10 | 1.0 | False | False | FAIL |
| gatsby_txt | Project Gutenberg | source.txt | text | 12 | 9 | 9 | 0.75 | False | False | PASS |
| gatsby_epub | Project Gutenberg | source.epub | ncx | 7 | 4 | 9 | 1.0 | True | False | FLAGGED-OK |
| wizard_oz_epub | Standard Ebooks | source.epub | nav | 30 | 29 | 24 | 1.0 | False | False | FAIL |
| alice_epub | Global Grey | source.epub | ncx | 12 | 12 | 12 | 1.0 | False | False | PASS |
| time_machine_epub | Standard Ebooks | source.epub | nav | 18 | 17 | 13 | 1.0 | False | False | FAIL |
| frankenstein_epub | Standard Ebooks | source.epub | nav | 39 | 37 | 28 | 1.0 | True | False | FLAGGED-OK |
| call_wild_epub | Standard Ebooks | source.epub | nav | 13 | 12 | 7 | 1.0 | False | False | FAIL |

## Raw Corpus Verification
- Raw root: `C:\Users\nguye\OneDrive\Tài liệu\Baitap\DuAnCNTT\odl-pdf-demo\research\agent-based-translation\AILAB_SOURCES_RAW`
- Manifest present: `True`; manifest books: `7`
- Raw corpus OK: `True`

## Per-Book Results
### jekyll_txt — The Strange Case of Dr. Jekyll and Mr. Hyde

- Verdict: **PASS**
- Dataset load OK: `True`
- TOC report: `source=text`, `items=10`, `matched=10`, `match_rate=1.0`, `low_confidence=False`, `ambiguous=[]`

#### Chapter Diff
| # | Expected title | Extracted title | block_count | Match? |
| --- | --- | --- | --- | --- |
| 1 | STORY OF THE DOOR | STORY OF THE DOOR | 30 | match |
| 2 | SEARCH FOR MR. HYDE | SEARCH FOR MR. HYDE | 51 | match |
| 3 | DR. JEKYLL WAS QUITE AT EASE | DR. JEKYLL WAS QUITE AT EASE | 18 | match |
| 4 | THE CAREW MURDER CASE | THE CAREW MURDER CASE | 19 | match |
| 5 | INCIDENT OF THE LETTER | INCIDENT OF THE LETTER | 39 | match |
| 6 | REMARKABLE INCIDENT OF DR. LANYON | REMARKABLE INCIDENT OF DR. LANYON | 14 | match |
| 7 | INCIDENT AT THE WINDOW | INCIDENT AT THE WINDOW | 15 | match |
| 8 | THE LAST NIGHT | THE LAST NIGHT | 98 | match |
| 9 | DR. LANYON’S NARRATIVE | DR. LANYON’S NARRATIVE | 34 | match |
| 10 | HENRY JEKYLL’S FULL STATEMENT OF THE CASE | HENRY JEKYLL’S FULL STATEMENT OF THE CASE | 30 | match |

#### Defect Log
| severity | type | location | evidence | report flagged? |
| --- | --- | --- | --- | --- |
| minor | possible_dialogue_overclassification | jekyll_txt_ch01_b002 | Mr. Utterson the lawyer was a man of a rugged countenance, that was never lighted by a smile; cold, scanty and embarrassed in discourse; ba… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch01_b009 | “Well, it was this way,” returned Mr. Enfield: “I was coming home from some place at the end of the world, about three o’clock of a black w… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch01_b011 | “I see you feel as I do,” said Mr. Enfield. “Yes, it’s a bad story. For my man was a fellow that nobody could have to do with, a really dam… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch01_b015 | “No, sir: I had a delicacy,” was the reply. “I feel very strongly about putting questions; it partakes too much of the style of the day of … | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch01_b017 | “But I have studied the place for myself,” continued Mr. Enfield. “It seems scarcely a house. There is no other door, and nobody goes in or… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch01_b023 | “He is not easy to describe. There is something wrong with his appearance; something displeasing, something downright detestable. I never s… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch02_b002 | That evening Mr. Utterson came home to his bachelor house in sombre spirits and sat down to dinner without relish. It was his custom of a S… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch02_b038 | The lawyer stood awhile when Mr. Hyde had left him, the picture of disquietude. Then he began slowly to mount the street, pausing every ste… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch02_b042 | “Here, thank you,” said the lawyer, and he drew near and leaned on the tall fender. This hall, in which he was now left alone, was a pet fa… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch02_b051 | And the lawyer set out homeward with a very heavy heart. “Poor Harry Jekyll,” he thought, “my mind misgives me he is in deep waters! He was… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch03_b004 | A close observer might have gathered that the topic was distasteful; but the doctor carried it off gaily. “My poor Utterson,” said he, “you… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch03_b012 | “My good Utterson,” said the doctor, “this is very good of you, this is downright good of you, and I cannot find words to thank you in. I b… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch03_b015 | “Well, but since we have touched upon this business, and for the last time I hope,” continued the doctor, “there is one point I should like… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch04_b004 | This was brought to the lawyer the next morning, before he was out of bed; and he had no sooner seen it, and been told the circumstances, t… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch05_b012 | The letter was written in an odd, upright hand and signed “Edward Hyde”: and it signified, briefly enough, that the writer’s benefactor, Dr… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch05_b022 | This news sent off the visitor with his fears renewed. Plainly the letter had come by the laboratory door; possibly, indeed, it had been wr… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch06_b003 | On the 8th of January Utterson had dined at the doctor’s with a small party; Lanyon had been there; and the face of the host had looked fro… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch06_b004 | There at least he was not denied admittance; but when he came in, he was shocked at the change which had taken place in the doctor’s appear… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch06_b012 | As soon as he got home, Utterson sat down and wrote to Jekyll, complaining of his exclusion from the house, and asking the cause of this un… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch06_b013 | A week afterwards Dr. Lanyon took to his bed, and in something less than a fortnight he was dead. The night after the funeral, at which he … | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch07_b013 | “That is just what I was about to venture to propose,” returned the doctor with a smile. But the words were hardly uttered, before the smil… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b024 | “Hold your tongue!” Poole said to her, with a ferocity of accent that testified to his own jangled nerves; and indeed, when the girl had so… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b034 | “Well, Mr. Utterson, you are a hard man to satisfy, but I’ll do it yet,” said Poole. “All this last week (you must know) him, or it, or wha… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b036 | Poole felt in his pocket and handed out a crumpled note, which the lawyer, bending nearer to the candle, carefully examined. Its contents r… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b042 | “That’s it!” said Poole. “It was this way. I came suddenly into the theatre from the garden. It seems he had slipped out to look for this d… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b043 | “These are all very strange circumstances,” said Mr. Utterson, “but I think I begin to see daylight. Your master, Poole, is plainly seized … | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b044 | “Sir,” said the butler, turning to a sort of mottled pallor, “that thing was not my master, and there’s the truth. My master”—here he looke… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b054 | “Well, sir, it went so quick, and the creature was so doubled up, that I could hardly swear to that,” was the answer. “But if you mean, was… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b061 | “Pull yourself together, Bradshaw,” said the lawyer. “This suspense, I know, is telling upon all of you; but it is now our intention to mak… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b062 | As Bradshaw left, the lawyer looked at his watch. “And now, Poole, let us get to ours,” he said; and taking the poker under his arm, led th… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b088 | “You may say that!” said Poole. Next they turned to the business-table. On the desk among the neat array of papers, a large envelope was up… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch08_b090 | He caught up the next paper; it was a brief note in the doctor’s hand and dated at the top. “O Poole!” the lawyer cried, “he was alive and … | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch09_b009 | “P. S. I had already sealed this up when a fresh terror struck upon my soul. It is possible that the postoffice may fail me, and this lette… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch09_b011 | Here I proceeded to examine its contents. The powders were neatly enough made up, but not with the nicety of the dispensing chemist; so tha… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch09_b019 | I put him back, conscious at his touch of a certain icy pang along my blood. “Come, sir,” said I. “You forget that I have not yet the pleas… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch09_b020 | “I beg your pardon, Dr. Lanyon,” he replied civilly enough. “What you say is very well founded; and my impatience has shown its heels to my… | False |
| minor | possible_dialogue_overclassification | jekyll_txt_ch09_b028 | “And now,” said he, “to settle what remains. Will you be wise? will you be guided? will you suffer me to take this glass in my hand and to … | False |

#### Pass-Fake Audit
Extractor sai mà report vẫn high-confidence? **NO**. No serious silent high-confidence defect detected by automated audit.

### jekyll_epub — The Strange Case of Dr. Jekyll and Mr. Hyde

- Verdict: **FAIL**
- Dataset load OK: `True`
- TOC report: `source=ncx`, `items=10`, `matched=10`, `match_rate=1.0`, `low_confidence=False`, `ambiguous=[]`

#### Chapter Diff
| # | Expected title | Extracted title | block_count | Match? |
| --- | --- | --- | --- | --- |
| 1 | STORY OF THE DOOR | STORY OF THE DOOR | 30 | match |
| 2 | SEARCH FOR MR. HYDE | SEARCH FOR MR. HYDE | 51 | match |
| 3 | DR. JEKYLL WAS QUITE AT EASE | DR. JEKYLL WAS QUITE AT EASE | 18 | match |
| 4 | THE CAREW MURDER CASE | THE CAREW MURDER CASE | 19 | match |
| 5 | INCIDENT OF THE LETTER | INCIDENT OF THE LETTER | 39 | match |
| 6 | REMARKABLE INCIDENT OF DR. LANYON | REMARKABLE INCIDENT OF DR. LANYON | 14 | match |
| 7 | INCIDENT AT THE WINDOW | INCIDENT AT THE WINDOW | 15 | match |
| 8 | THE LAST NIGHT | THE LAST NIGHT | 98 | match |
| 9 | DR. LANYON’S NARRATIVE | DR. LANYON’S NARRATIVE | 34 | match |
| 10 | HENRY JEKYLL’S FULL STATEMENT OF THE CASE | HENRY JEKYLL’S FULL STATEMENT OF THE CASE | 36 | match |

#### Defect Log
| severity | type | location | evidence | report flagged? |
| --- | --- | --- | --- | --- |
| minor | possible_dialogue_overclassification | jekyll_epub_ch01_b002 | Mr. Utterson the lawyer was a man of a rugged countenance, that was never lighted by a smile; cold, scanty and embarrassed in discourse; ba… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch01_b009 | “Well, it was this way,” returned Mr. Enfield: “I was coming home from some place at the end of the world, about three o’clock of a black w… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch01_b011 | “I see you feel as I do,” said Mr. Enfield. “Yes, it’s a bad story. For my man was a fellow that nobody could have to do with, a really dam… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch01_b015 | “No, sir: I had a delicacy,” was the reply. “I feel very strongly about putting questions; it partakes too much of the style of the day of … | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch01_b017 | “But I have studied the place for myself,” continued Mr. Enfield. “It seems scarcely a house. There is no other door, and nobody goes in or… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch01_b023 | “He is not easy to describe. There is something wrong with his appearance; something displeasing, something downright detestable. I never s… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch02_b002 | That evening Mr. Utterson came home to his bachelor house in sombre spirits and sat down to dinner without relish. It was his custom of a S… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch02_b038 | The lawyer stood awhile when Mr. Hyde had left him, the picture of disquietude. Then he began slowly to mount the street, pausing every ste… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch02_b042 | “Here, thank you,” said the lawyer, and he drew near and leaned on the tall fender. This hall, in which he was now left alone, was a pet fa… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch02_b051 | And the lawyer set out homeward with a very heavy heart. “Poor Harry Jekyll,” he thought, “my mind misgives me he is in deep waters! He was… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch03_b004 | A close observer might have gathered that the topic was distasteful; but the doctor carried it off gaily. “My poor Utterson,” said he, “you… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch03_b012 | “My good Utterson,” said the doctor, “this is very good of you, this is downright good of you, and I cannot find words to thank you in. I b… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch03_b015 | “Well, but since we have touched upon this business, and for the last time I hope,” continued the doctor, “there is one point I should like… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch04_b004 | This was brought to the lawyer the next morning, before he was out of bed; and he had no sooner seen it, and been told the circumstances, t… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch05_b012 | The letter was written in an odd, upright hand and signed “Edward Hyde”: and it signified, briefly enough, that the writer’s benefactor, Dr… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch05_b022 | This news sent off the visitor with his fears renewed. Plainly the letter had come by the laboratory door; possibly, indeed, it had been wr… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch06_b003 | On the 8th of January Utterson had dined at the doctor’s with a small party; Lanyon had been there; and the face of the host had looked fro… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch06_b004 | There at least he was not denied admittance; but when he came in, he was shocked at the change which had taken place in the doctor’s appear… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch06_b012 | As soon as he got home, Utterson sat down and wrote to Jekyll, complaining of his exclusion from the house, and asking the cause of this un… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch06_b013 | A week afterwards Dr. Lanyon took to his bed, and in something less than a fortnight he was dead. The night after the funeral, at which he … | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch07_b013 | “That is just what I was about to venture to propose,” returned the doctor with a smile. But the words were hardly uttered, before the smil… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b024 | “Hold your tongue!” Poole said to her, with a ferocity of accent that testified to his own jangled nerves; and indeed, when the girl had so… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b034 | “Well, Mr. Utterson, you are a hard man to satisfy, but I’ll do it yet,” said Poole. “All this last week (you must know) him, or it, or wha… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b036 | Poole felt in his pocket and handed out a crumpled note, which the lawyer, bending nearer to the candle, carefully examined. Its contents r… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b042 | “That’s it!” said Poole. “It was this way. I came suddenly into the theatre from the garden. It seems he had slipped out to look for this d… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b043 | “These are all very strange circumstances,” said Mr. Utterson, “but I think I begin to see daylight. Your master, Poole, is plainly seized … | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b044 | “Sir,” said the butler, turning to a sort of mottled pallor, “that thing was not my master, and there’s the truth. My master”—here he looke… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b054 | “Well, sir, it went so quick, and the creature was so doubled up, that I could hardly swear to that,” was the answer. “But if you mean, was… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b061 | “Pull yourself together, Bradshaw,” said the lawyer. “This suspense, I know, is telling upon all of you; but it is now our intention to mak… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b062 | As Bradshaw left, the lawyer looked at his watch. “And now, Poole, let us get to ours,” he said; and taking the poker under his arm, led th… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b088 | “You may say that!” said Poole. Next they turned to the business-table. On the desk among the neat array of papers, a large envelope was up… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch08_b090 | He caught up the next paper; it was a brief note in the doctor’s hand and dated at the top. “O Poole!” the lawyer cried, “he was alive and … | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch09_b009 | “P. S. I had already sealed this up when a fresh terror struck upon my soul. It is possible that the postoffice may fail me, and this lette… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch09_b011 | Here I proceeded to examine its contents. The powders were neatly enough made up, but not with the nicety of the dispensing chemist; so tha… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch09_b019 | I put him back, conscious at his touch of a certain icy pang along my blood. “Come, sir,” said I. “You forget that I have not yet the pleas… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch09_b020 | “I beg your pardon, Dr. Lanyon,” he replied civilly enough. “What you say is very well founded; and my impatience has shown its heels to my… | False |
| minor | possible_dialogue_overclassification | jekyll_epub_ch09_b028 | “And now,” said he, “to settle what remains. Will you be wise? will you be guided? will you suffer me to take this glass in my hand and to … | False |
| major | boilerplate_block | jekyll_epub_ch10_b031 | THE FULL PROJECT GUTENBERG™ LICENSE | True |
| major | boilerplate_block | jekyll_epub_ch10_b032 | This eBook is for the use of anyone anywhere in the United States and most other parts of the world at no cost and with almost no restricti… | True |
| major | boilerplate_block | jekyll_epub_ch10_b033 | • You pay a royalty fee of 20% of the gross profits you derive from the use of Project Gutenberg works calculated using the method you alre… | True |
| minor | possible_dialogue_overclassification | jekyll_epub_ch10_b033 | • You pay a royalty fee of 20% of the gross profits you derive from the use of Project Gutenberg works calculated using the method you alre… | False |
| major | boilerplate_block | jekyll_epub_ch10_b034 | • You provide a full refund of any money paid by a user who notifies you in writing (or by e-mail) within 30 days of receipt that s/he does… | True |
| major | boilerplate_block | jekyll_epub_ch10_b036 | • You comply with all other terms of this agreement for free distribution of Project Gutenberg™ works. | True |

#### Pass-Fake Audit
Extractor sai mà report vẫn high-confidence? **YES**. Serious defects present while low_confidence=False and match_rate is high.

### gatsby_txt — The Great Gatsby

- Verdict: **PASS**
- Dataset load OK: `True`
- TOC report: `source=text`, `items=12`, `matched=9`, `match_rate=0.75`, `low_confidence=False`, `ambiguous=[]`

#### Chapter Diff
| # | Expected title | Extracted title | block_count | Match? |
| --- | --- | --- | --- | --- |
| 1 | I | I | 155 | match |
| 2 | II | II | 138 | match |
| 3 | III | III | 173 | match |
| 4 | IV | IV | 175 | match |
| 5 | V | V | 155 | match |
| 6 | VI | VI | 137 | match |
| 7 | VII | VII | 420 | match |
| 8 | VIII | VIII | 121 | match |
| 9 | IX | IX | 170 | match |

#### Defect Log
| severity | type | location | evidence | report flagged? |
| --- | --- | --- | --- | --- |
| minor | possible_dialogue_overclassification | gatsby_txt_ch01_b005 | And, after boasting this way of my tolerance, I come to the admission that it has a limit. Conduct may be founded on the hard rock or the w… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch01_b008 | I never saw this great-uncle, but I’m supposed to look like him—with special reference to the rather hard-boiled painting that hangs in fat… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch01_b014 | There was so much to read, for one thing, and so much fine health to be pulled down out of the young breath-giving air. I bought a dozen vo… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch01_b035 | I looked back at my cousin, who began to ask me questions in her low, thrilling voice. It was the kind of voice that the ear follows up and… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch01_b153 | Their interest rather touched me and made them less remotely rich—nevertheless, I was confused and a little disgusted as I drove away. It s… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch02_b056 | Mr. McKee was a pale, feminine man from the flat below. He had just shaved, for there was a white spot of lather on his cheekbone, and he w… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch02_b118 | “The only crazy I was was when I married him. I knew right away I made a mistake. He borrowed somebody’s best suit to get married in, and n… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch02_b120 | The bottle of whisky—a second one—was now in constant demand by all present, excepting Catherine, who “felt just as good on nothing at all.… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch02_b122 | “It was on the two little seats facing each other that are always the last ones left on the train. I was going up to New York to see my sis… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch02_b124 | “My dear,” she cried, “I’m going to give you this dress as soon as I’m through with it. I’ve got to get another one tomorrow. I’m going to … | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch03_b020 | “You’ve dyed your hair since then,” remarked Jordan, and I started, but the girls had moved casually on and her remark was addressed to the… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch03_b060 | There was dancing now on the canvas in the garden; old men pushing young girls backward in eternal graceless circles, superior couples hold… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch03_b094 | The nature of Mr. Tostoff’s composition eluded me, because just as it began my eyes fell on Gatsby, standing alone on the marble steps and … | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch03_b104 | I looked around. Most of the remaining women were now having fights with men said to be their husbands. Even Jordan’s party, the quartet fr… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch03_b172 | Her grey, sun-strained eyes stared straight ahead, but she had deliberately shifted our relations, and for a moment I thought I loved her. … | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch04_b007 | From West Egg came the Poles and the Mulreadys and Cecil Roebuck and Cecil Schoen and Gulick the State senator and Newton Orchid, who contr… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch04_b008 | A man named Klipspringer was there so often that he became known as “the boarder”—I doubt if he had any other home. Of theatrical people th… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch04_b035 | “Then came the war, old sport. It was a great relief, and I tried very hard to die, but I seemed to bear an enchanted life. I accepted a co… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch04_b076 | “The old Metropole,” brooded Mr. Wolfshiem gloomily. “Filled with faces dead and gone. Filled with friends gone now forever. I can’t forget… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch04_b149 | I saw them in Santa Barbara when they came back, and I thought I’d never seen a girl so mad about her husband. If he left the room for a mi… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch04_b151 | Well, about six weeks ago, she heard the name Gatsby for the first time in years. It was when I asked you—do you remember?—if you knew Gats… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch04_b171 | It was dark now, and as we dipped under a little bridge I put my arm around Jordan’s golden shoulder and drew her toward me and asked her t… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch05_b086 | I walked out the back way—just as Gatsby had when he had made his nervous circuit of the house half an hour before—and ran for a huge black… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch05_b111 | We went upstairs, through period bedrooms swathed in rose and lavender silk and vivid with new flowers, through dressing-rooms and poolroom… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch06_b006 | It was a random shot, and yet the reporter’s instinct was right. Gatsby’s notoriety, spread about by the hundreds who had accepted his hosp… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch06_b118 | Her glance left me and sought the lighted top of the steps, where “Three O’Clock in the Morning,” a neat, sad little waltz of that year, wa… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch07_b174 | The prolonged and tumultuous argument that ended by herding us into that room eludes me, though I have a sharp physical memory that, in the… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch07_b318 | The “death car” as the newspapers called it, didn’t stop; it came out of the gathering darkness, wavered tragically for a moment, and then … | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch07_b402 | “Yes,” he said after a moment, “but of course I’ll say I was. You see, when we left New York she was very nervous and she thought it would … | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch08_b011 | She was the first “nice” girl he had ever known. In various unrevealed capacities he had come in contact with such people, but always with … | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch08_b017 | “I can’t describe to you how surprised I was to find out I loved her, old sport. I even hoped for a while that she’d throw me over, but she… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch08_b021 | For Daisy was young and her artificial world was redolent of orchids and pleasant, cheerful snobbery and orchestras which set the rhythm of… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch08_b114 | His movements—he was on foot all the time—were afterward traced to Port Roosevelt and then to Gad’s Hill, where he bought a sandwich that h… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch09_b002 | After two years I remember the rest of that day, and that night and the next day, only as an endless drill of police and photographers and … | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch09_b003 | Most of those reports were a nightmare—grotesque, circumstantial, eager, and untrue. When Michaelis’s testimony at the inquest brought to l… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch09_b074 | The morning of the funeral I went up to New York to see Meyer Wolfshiem; I couldn’t seem to reach him any other way. The door that I pushed… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch09_b087 | “My memory goes back to when first I met him,” he said. “A young major just out of the army and covered over with medals he got in the war.… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch09_b091 | “I raised him up out of nothing, right out of the gutter. I saw right away he was a fine-appearing, gentlemanly young man, and when he told… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch09_b137 | One of my most vivid memories is of coming back West from prep school and later from college at Christmas time. Those who went farther than… | False |
| minor | possible_dialogue_overclassification | gatsby_txt_ch09_b158 | “I told him the truth,” he said. “He came to the door while we were getting ready to leave, and when I sent down word that we weren’t in he… | False |

#### Pass-Fake Audit
Extractor sai mà report vẫn high-confidence? **NO**. No serious silent high-confidence defect detected by automated audit.

### gatsby_epub — The Great Gatsby

- Verdict: **FLAGGED-OK**
- Dataset load OK: `True`
- TOC report: `source=ncx`, `items=7`, `matched=7`, `match_rate=1.0`, `low_confidence=True`, `ambiguous=[]`

#### Chapter Diff
| # | Expected title | Extracted title | block_count | Match? |
| --- | --- | --- | --- | --- |
| 1 | I | III | 343 | mismatch |
| 2 | II | V | 290 | mismatch |
| 3 | III | VII | 416 | mismatch |
| 4 | IV | VIII | 278 | mismatch |
| 5 | V |  |  | missing |
| 6 | VI |  |  | missing |
| 7 | VII |  |  | missing |
| 8 | VIII |  |  | missing |
| 9 | IX |  |  | missing |

#### Defect Log
| severity | type | location | evidence | report flagged? |
| --- | --- | --- | --- | --- |
| major | chapter_mismatch | gatsby_epub_ch01 | expected='I'; extracted='III' | True |
| major | chapter_mismatch | gatsby_epub_ch02 | expected='II'; extracted='V' | True |
| major | chapter_mismatch | gatsby_epub_ch03 | expected='III'; extracted='VII' | True |
| major | chapter_mismatch | gatsby_epub_ch04 | expected='IV'; extracted='VIII' | True |
| major | chapter_missing | index 5 | expected='V'; extracted=None | True |
| major | chapter_missing | index 6 | expected='VI'; extracted=None | True |
| major | chapter_missing | index 7 | expected='VII'; extracted=None | True |
| major | chapter_missing | index 8 | expected='VIII'; extracted=None | True |
| major | chapter_missing | index 9 | expected='IX'; extracted=None | True |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b020 | “You’ve dyed your hair since then,” remarked Jordan, and I started, but the girls had moved casually on and her remark was addressed to the… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b060 | There was dancing now on the canvas in the garden; old men pushing young girls backward in eternal graceless circles, superior couples hold… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b094 | The nature of Mr. Tostoff’s composition eluded me, because just as it began my eyes fell on Gatsby, standing alone on the marble steps and … | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b104 | I looked around. Most of the remaining women were now having fights with men said to be their husbands. Even Jordan’s party, the quartet fr… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b171 | Her grey, sun-strained eyes stared straight ahead, but she had deliberately shifted our relations, and for a moment I thought I loved her. … | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b179 | From West Egg came the Poles and the Mulreadys and Cecil Roebuck and Cecil Schoen and Gulick the State senator and Newton Orchid, who contr… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b180 | A man named Klipspringer was there so often that he became known as “the boarder”—I doubt if he had any other home. Of theatrical people th… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b206 | “Then came the war, old sport. It was a great relief, and I tried very hard to die, but I seemed to bear an enchanted life. I accepted a co… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b246 | “The old Metropole,” brooded Mr. Wolfshiem gloomily. “Filled with faces dead and gone. Filled with friends gone now forever. I can’t forget… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b318 | I saw them in Santa Barbara when they came back, and I thought I’d never seen a girl so mad about her husband. If he left the room for a mi… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b320 | Well, about six weeks ago, she heard the name Gatsby for the first time in years. It was when I asked you—do you remember?—if you knew Gats… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch01_b339 | It was dark now, and as we dipped under a little bridge I put my arm around Jordan’s golden shoulder and drew her toward me and asked her t… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch02_b086 | I walked out the back way—just as Gatsby had when he had made his nervous circuit of the house half an hour before—and ran for a huge black… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch02_b111 | We went upstairs, through period bedrooms swathed in rose and lavender silk and vivid with new flowers, through dressing-rooms and poolroom… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch02_b160 | It was a random shot, and yet the reporter’s instinct was right. Gatsby’s notoriety, spread about by the hundreds who had accepted his hosp… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch02_b271 | Her glance left me and sought the lighted top of the steps, where “Three O’Clock in the Morning,” a neat, sad little waltz of that year, wa… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch03_b173 | The prolonged and tumultuous argument that ended by herding us into that room eludes me, though I have a sharp physical memory that, in the… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch03_b316 | The “death car” as the newspapers called it, didn’t stop; it came out of the gathering darkness, wavered tragically for a moment, and then … | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch03_b398 | “Yes,” he said after a moment, “but of course I’ll say I was. You see, when we left New York she was very nervous and she thought it would … | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b011 | She was the first “nice” girl he had ever known. In various unrevealed capacities he had come in contact with such people, but always with … | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b016 | “I can’t describe to you how surprised I was to find out I loved her, old sport. I even hoped for a while that she’d throw me over, but she… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b019 | For Daisy was young and her artificial world was redolent of orchids and pleasant, cheerful snobbery and orchestras which set the rhythm of… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b108 | His movements—he was on foot all the time—were afterward traced to Port Roosevelt and then to Gad’s Hill, where he bought a sandwich that h… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b116 | After two years I remember the rest of that day, and that night and the next day, only as an endless drill of police and photographers and … | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b117 | Most of those reports were a nightmare—grotesque, circumstantial, eager, and untrue. When Michaelis’s testimony at the inquest brought to l… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b187 | The morning of the funeral I went up to New York to see Meyer Wolfshiem; I couldn’t seem to reach him any other way. The door that I pushed… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b200 | “My memory goes back to when first I met him,” he said. “A young major just out of the army and covered over with medals he got in the war.… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b204 | “I raised him up out of nothing, right out of the gutter. I saw right away he was a fine-appearing, gentlemanly young man, and when he told… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b247 | One of my most vivid memories is of coming back West from prep school and later from college at Christmas time. Those who went farther than… | False |
| minor | possible_dialogue_overclassification | gatsby_epub_ch04_b267 | “I told him the truth,” he said. “He came to the door while we were getting ready to leave, and when I sent down word that we weren’t in he… | False |

#### Pass-Fake Audit
Extractor sai mà report vẫn high-confidence? **NO**. No serious silent high-confidence defect detected by automated audit.

### wizard_oz_epub — The Wonderful Wizard of Oz

- Verdict: **FAIL**
- Dataset load OK: `True`
- TOC report: `source=nav`, `items=30`, `matched=30`, `match_rate=1.0`, `low_confidence=False`, `ambiguous=[]`

#### Chapter Diff
| # | Expected title | Extracted title | block_count | Match? |
| --- | --- | --- | --- | --- |
| 1 | I: The Cyclone | Imprint | 5 | mismatch |
| 2 | II: The Council with the Munchkins | Introduction | 6 | mismatch |
| 3 | III: How Dorothy Saved the Scarecrow | The Wonderful Wizard of Oz | 1 | mismatch |
| 4 | IV: The Road Through the Forest | I: The Cyclone | 22 | mismatch |
| 5 | V: The Rescue of the Tin Woodman | II: The Council with the Munchkins | 61 | mismatch |
| 6 | VI: The Cowardly Lion | III: How Dorothy Saved the Scarecrow | 58 | mismatch |
| 7 | VII: The Journey to the Great Oz | IV: The Road Through the Forest | 39 | mismatch |
| 8 | VIII: The Deadly Poppy Field | V: The Rescue of the Tin Woodman | 54 | mismatch |
| 9 | IX: The Queen of the Field Mice | VI: The Cowardly Lion | 49 | mismatch |
| 10 | X: The Guardian of the Gate | VII: The Journey to the Great Oz | 43 | mismatch |
| 11 | XI: The Wonderful City of Oz | VIII: The Deadly Poppy Field | 58 | mismatch |
| 12 | XII: The Search for the Wicked Witch | IX: The Queen of the Field Mice | 43 | mismatch |
| 13 | XIII: The Rescue | X: The Guardian of the Gate | 59 | mismatch |
| 14 | XIV: The Winged Monkeys | XI: The Wonderful City of Oz | 103 | mismatch |
| 15 | XV: The Discovery of Oz, the Terrible | XII: The Search for the Wicked Witch | 85 | mismatch |
| 16 | XVI: The Magic Art of the Great Humbug | XIII: The Rescue | 31 | mismatch |
| 17 | XVII: How the Balloon Was Launched | XIV: The Winged Monkeys | 45 | mismatch |
| 18 | XVIII: Away to the South | XV: The Discovery of Oz, the Terrible | 94 | mismatch |
| 19 | XIX: Attacked by the Fighting Trees | XVI: The Magic Art of the Great Humbug | 43 | mismatch |
| 20 | XX: The Dainty China Country | XVII: How the Balloon Was Launched | 35 | mismatch |
| 21 | XXI: The Lion Becomes the King of Beasts | XVIII: Away to the South | 49 | mismatch |
| 22 | XXII: The Country of the Quadlings | XIX: Attacked by the Fighting Trees | 29 | mismatch |
| 23 | XXIII: Glinda the Good Witch Grants Dorothy’s Wish | XX: The Dainty China Country | 47 | mismatch |
| 24 | XXIV: Home Again | XXI: The Lion Becomes the King of Beasts | 27 | mismatch |
| 25 |  | XXII: The Country of the Quadlings | 31 | extra |
| 26 |  | XXIII: Glinda the Good Witch Grants Dorothy’s Wish | 42 | extra |
| 27 |  | XXIV: Home Again | 5 | extra |
| 28 |  | Colophon | 6 | extra |
| 29 |  | Uncopyright | 5 | extra |

#### Defect Log
| severity | type | location | evidence | report flagged? |
| --- | --- | --- | --- | --- |
| major | chapter_mismatch | wizard_oz_epub_ch01 | expected='I: The Cyclone'; extracted='Imprint' | False |
| major | chapter_mismatch | wizard_oz_epub_ch02 | expected='II: The Council with the Munchkins'; extracted='Introduction' | False |
| major | chapter_mismatch | wizard_oz_epub_ch03 | expected='III: How Dorothy Saved the Scarecrow'; extracted='The Wonderful Wizard of Oz' | False |
| major | chapter_mismatch | wizard_oz_epub_ch04 | expected='IV: The Road Through the Forest'; extracted='I: The Cyclone' | False |
| major | chapter_mismatch | wizard_oz_epub_ch05 | expected='V: The Rescue of the Tin Woodman'; extracted='II: The Council with the Munchkins' | False |
| major | chapter_mismatch | wizard_oz_epub_ch06 | expected='VI: The Cowardly Lion'; extracted='III: How Dorothy Saved the Scarecrow' | False |
| major | chapter_mismatch | wizard_oz_epub_ch07 | expected='VII: The Journey to the Great Oz'; extracted='IV: The Road Through the Forest' | False |
| major | chapter_mismatch | wizard_oz_epub_ch08 | expected='VIII: The Deadly Poppy Field'; extracted='V: The Rescue of the Tin Woodman' | False |
| major | chapter_mismatch | wizard_oz_epub_ch09 | expected='IX: The Queen of the Field Mice'; extracted='VI: The Cowardly Lion' | False |
| major | chapter_mismatch | wizard_oz_epub_ch10 | expected='X: The Guardian of the Gate'; extracted='VII: The Journey to the Great Oz' | False |
| major | chapter_mismatch | wizard_oz_epub_ch11 | expected='XI: The Wonderful City of Oz'; extracted='VIII: The Deadly Poppy Field' | False |
| major | chapter_mismatch | wizard_oz_epub_ch12 | expected='XII: The Search for the Wicked Witch'; extracted='IX: The Queen of the Field Mice' | False |
| major | chapter_mismatch | wizard_oz_epub_ch13 | expected='XIII: The Rescue'; extracted='X: The Guardian of the Gate' | False |
| major | chapter_mismatch | wizard_oz_epub_ch14 | expected='XIV: The Winged Monkeys'; extracted='XI: The Wonderful City of Oz' | False |
| major | chapter_mismatch | wizard_oz_epub_ch15 | expected='XV: The Discovery of Oz, the Terrible'; extracted='XII: The Search for the Wicked Witch' | False |
| major | chapter_mismatch | wizard_oz_epub_ch16 | expected='XVI: The Magic Art of the Great Humbug'; extracted='XIII: The Rescue' | False |
| major | chapter_mismatch | wizard_oz_epub_ch17 | expected='XVII: How the Balloon Was Launched'; extracted='XIV: The Winged Monkeys' | False |
| major | chapter_mismatch | wizard_oz_epub_ch18 | expected='XVIII: Away to the South'; extracted='XV: The Discovery of Oz, the Terrible' | False |
| major | chapter_mismatch | wizard_oz_epub_ch19 | expected='XIX: Attacked by the Fighting Trees'; extracted='XVI: The Magic Art of the Great Humbug' | False |
| major | chapter_mismatch | wizard_oz_epub_ch20 | expected='XX: The Dainty China Country'; extracted='XVII: How the Balloon Was Launched' | False |
| major | chapter_mismatch | wizard_oz_epub_ch21 | expected='XXI: The Lion Becomes the King of Beasts'; extracted='XVIII: Away to the South' | False |
| major | chapter_mismatch | wizard_oz_epub_ch22 | expected='XXII: The Country of the Quadlings'; extracted='XIX: Attacked by the Fighting Trees' | False |
| major | chapter_mismatch | wizard_oz_epub_ch23 | expected='XXIII: Glinda the Good Witch Grants Dorothy’s Wish'; extracted='XX: The Dainty China Country' | False |
| major | chapter_mismatch | wizard_oz_epub_ch24 | expected='XXIV: Home Again'; extracted='XXI: The Lion Becomes the King of Beasts' | False |
| major | chapter_extra | wizard_oz_epub_ch25 | expected=None; extracted='XXII: The Country of the Quadlings' | False |
| major | chapter_extra | wizard_oz_epub_ch26 | expected=None; extracted='XXIII: Glinda the Good Witch Grants Dorothy’s Wish' | False |
| major | chapter_extra | wizard_oz_epub_ch27 | expected=None; extracted='XXIV: Home Again' | False |
| major | chapter_extra | wizard_oz_epub_ch28 | expected=None; extracted='Colophon' | False |
| major | chapter_extra | wizard_oz_epub_ch29 | expected=None; extracted='Uncopyright' | False |
| major | front_matter_chapter | wizard_oz_epub_ch01 | Imprint | False |
| major | boilerplate_block | wizard_oz_epub_ch01_b001 | Imprint | True |
| major | boilerplate_block | wizard_oz_epub_ch01_b003 | This particular ebook is based on a transcription from Project Gutenberg and on digital scans from the Internet Archive . | True |
| major | front_matter_authorial_chapter | wizard_oz_epub_ch02 | Introduction (authorial front matter) | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch02_b003 | Yet the old time fairy tale, having served for generations, may now be classed as “historical” in the children’s library; for the time has … | False |
| minor | very_short_chapter | wizard_oz_epub_ch03 | title='The Wonderful Wizard of Oz'; block_count=1 | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch05_b025 | “Oh, no; that is a great mistake. There were only four witches in all the Land of Oz, and two of them, those who live in the North and the … | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch05_b045 | Dorothy began to sob at this, for she felt lonely among all these strange people. Her tears seemed to grieve the kindhearted Munchkins, for… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch08_b049 | “My body shone so brightly in the sun that I felt very proud of it and it did not matter now if my axe slipped, for it could not cut me. Th… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch09_b006 | “I cannot tell,” was the answer, “for I have never been to the Emerald City. But my father went there once, when I was a boy, and he said i… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch09_b026 | “It’s a mystery,” replied the Lion. “I suppose I was born that way. All the other animals in the forest naturally expect me to be brave, fo… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch10_b036 | “Wait a minute!” called the Scarecrow. He had been thinking what was best to be done, and now he asked the Woodman to chop away the end of … | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch13_b028 | “That is hard to tell,” said the man thoughtfully. “You see, Oz is a Great Wizard, and can take on any form he wishes. So that some say he … | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch14_b013 | “Oh, no,” returned the soldier; “I have never seen him. But I spoke to him as he sat behind his screen and gave him your message. He said h… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch14_b024 | “Oh, he will see you,” said the soldier who had taken her message to the Wizard, “although he does not like to have people ask to see him. … | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch14_b070 | So the Tin Woodman followed him and came to the great Throne Room. He did not know whether he would find Oz a lovely Lady or a Head, but he… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch14_b080 | “If he is a Beast when I go to see him, I shall roar my loudest, and so frighten him that he will grant all I ask. And if he is the lovely … | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch15_b060 | The Wicked Witch was both surprised and worried when she saw the mark on Dorothy’s forehead, for she knew well that neither the Winged Monk… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch17_b035 | “Once,” began the leader, “we were a free people, living happily in the great forest, flying from tree to tree, eating nuts and fruit, and … | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch17_b039 | “The princess was angry, and she knew, of course, who did it. She had all the Winged Monkeys brought before her, and she said at first that… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch18_b035 | The Lion thought it might be as well to frighten the Wizard, so he gave a large, loud roar, which was so fierce and dreadful that Toto jump… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch18_b060 | “Oh, I am a ventriloquist,” said the little man. “I can throw the sound of my voice wherever I wish, so that you thought it was coming out … | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch18_b074 | “No more than in any other city,” replied Oz; “but when you wear green spectacles, why of course everything you see looks green to you. The… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch18_b075 | “One of my greatest fears was the Witches, for while I had no magical powers at all I soon found out that the Witches were really able to d… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch18_b093 | “We shall have to think about that,” replied the little man. “Give me two or three days to consider the matter and I’ll try to find a way t… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch19_b043 | Oz, left to himself, smiled to think of his success in giving the Scarecrow and the Tin Woodman and the Lion exactly what they thought they… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch23_b007 | After a time the ladder was finished. It looked clumsy, but the Tin Woodman was sure it was strong and would answer their purpose. The Scar… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch23_b041 | “That would make me very unhappy,” answered the china Princess. “You see, here in our country we live contentedly, and can talk and move ar… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch24_b014 | “We are all threatened,” answered the tiger, “by a fierce enemy which has lately come into this forest. It is a most tremendous monster, li… | False |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch25_b010 | He was quite short and stout and had a big head, which was flat at the top and supported by a thick neck full of wrinkles. But he had no ar… | False |
| major | front_matter_chapter | wizard_oz_epub_ch28 | Colophon | False |
| major | boilerplate_block | wizard_oz_epub_ch28_b001 | Colophon | True |
| major | boilerplate_block | wizard_oz_epub_ch28_b003 | This ebook was produced for Standard Ebooks by Michael Atkinson , and is based on a transcription produced in 1993 by Distributed Proofread… | True |
| major | front_matter_chapter | wizard_oz_epub_ch29 | Uncopyright | False |
| major | boilerplate_block | wizard_oz_epub_ch29_b001 | Uncopyright | True |
| minor | possible_dialogue_overclassification | wizard_oz_epub_ch29_b005 | Non-authorship activities performed on items that are in the public domain﻿—so-called “sweat of the brow” work﻿—don’t create a new copyrigh… | False |

#### Pass-Fake Audit
Extractor sai mà report vẫn high-confidence? **YES**. Serious defects present while low_confidence=False and match_rate is high.

### alice_epub — Alice’s Adventures in Wonderland

- Verdict: **PASS**
- Dataset load OK: `True`
- TOC report: `source=ncx`, `items=12`, `matched=12`, `match_rate=1.0`, `low_confidence=False`, `ambiguous=[]`

#### Chapter Diff
| # | Expected title | Extracted title | block_count | Match? |
| --- | --- | --- | --- | --- |
| 1 | 1. Down The Rabbit-Hole | 1. Down The Rabbit-Hole | 25 | match |
| 2 | 2. The Pool Of Tears | 2. The Pool Of Tears | 35 | match |
| 3 | 3. A Caucus-Race And A Long Tale | 3. A Caucus-Race And A Long Tale | 98 | match |
| 4 | 4. The Rabbit Sends In A Little Bill | 4. The Rabbit Sends In A Little Bill | 45 | match |
| 5 | 5. Advice From A Caterpillar | 5. Advice From A Caterpillar | 100 | match |
| 6 | 6. Pig And Pepper | 6. Pig And Pepper | 88 | match |
| 7 | 7. A Mad Tea-Party | 7. A Mad Tea-Party | 109 | match |
| 8 | 8. The Queen’s Croquet-Ground | 8. The Queen’s Croquet-Ground | 73 | match |
| 9 | 9. The Mock Turtle’s Story | 9. The Mock Turtle’s Story | 93 | match |
| 10 | 10. The Lobster Quadrille | 10. The Lobster Quadrille | 128 | match |
| 11 | 11. Who Stole The Tarts? | 11. Who Stole The Tarts? | 79 | match |
| 12 | 12. Alice’s Evidence | 12. Alice’s Evidence | 91 | match |

#### Defect Log
| severity | type | location | evidence | report flagged? |
| --- | --- | --- | --- | --- |
| minor | possible_dialogue_overclassification | alice_epub_ch01_b017 | It was all very well to say ‘Drink me,’ but the wise little Alice was not going to do that in a hurry. ‘No, I’ll look first,’ she said, ‘an… | False |
| minor | possible_dialogue_overclassification | alice_epub_ch02_b014 | ‘I’m sure I’m not Ada,’ she said, ‘for her hair goes in such long ringlets, and mine doesn’t go in ringlets at all; and I’m sure I can’t be… | False |
| minor | possible_dialogue_overclassification | alice_epub_ch02_b023 | ‘I’m sure those are not the right words,’ said poor Alice, and her eyes filled with tears again as she went on, ‘I must be Mabel after all,… | False |
| minor | possible_dialogue_overclassification | alice_epub_ch04_b005 | ‘How queer it seems,’ Alice said to herself, ‘to be going messages for a rabbit! I suppose Dinah’ll be sending me on messages next!’ And sh… | False |

#### Pass-Fake Audit
Extractor sai mà report vẫn high-confidence? **NO**. No serious silent high-confidence defect detected by automated audit.

### time_machine_epub — The Time Machine

- Verdict: **FAIL**
- Dataset load OK: `True`
- TOC report: `source=nav`, `items=18`, `matched=18`, `match_rate=1.0`, `low_confidence=False`, `ambiguous=[]`

#### Chapter Diff
| # | Expected title | Extracted title | block_count | Match? |
| --- | --- | --- | --- | --- |
| 1 | I | Imprint | 5 | mismatch |
| 2 | II | I | 79 | mismatch |
| 3 | III | II | 24 | mismatch |
| 4 | IV | III | 15 | mismatch |
| 5 | V | IV | 33 | mismatch |
| 6 | VI | V | 42 | mismatch |
| 7 | VII | VI | 14 | mismatch |
| 8 | VIII | VII | 17 | mismatch |
| 9 | IX | VIII | 15 | mismatch |
| 10 | X | IX | 18 | mismatch |
| 11 | XI | X | 15 | mismatch |
| 12 | XII | XI | 13 | mismatch |
| 13 | Epilogue | XII | 34 | mismatch |
| 14 |  | Epilogue | 2 | extra |
| 15 |  | Endnotes | 2 | extra |
| 16 |  | Colophon | 6 | extra |
| 17 |  | Uncopyright | 5 | extra |

#### Defect Log
| severity | type | location | evidence | report flagged? |
| --- | --- | --- | --- | --- |
| major | chapter_mismatch | time_machine_epub_ch01 | expected='I'; extracted='Imprint' | False |
| major | chapter_mismatch | time_machine_epub_ch02 | expected='II'; extracted='I' | False |
| major | chapter_mismatch | time_machine_epub_ch03 | expected='III'; extracted='II' | False |
| major | chapter_mismatch | time_machine_epub_ch04 | expected='IV'; extracted='III' | False |
| major | chapter_mismatch | time_machine_epub_ch05 | expected='V'; extracted='IV' | False |
| major | chapter_mismatch | time_machine_epub_ch06 | expected='VI'; extracted='V' | False |
| major | chapter_mismatch | time_machine_epub_ch07 | expected='VII'; extracted='VI' | False |
| major | chapter_mismatch | time_machine_epub_ch08 | expected='VIII'; extracted='VII' | False |
| major | chapter_mismatch | time_machine_epub_ch09 | expected='IX'; extracted='VIII' | False |
| major | chapter_mismatch | time_machine_epub_ch10 | expected='X'; extracted='IX' | False |
| major | chapter_mismatch | time_machine_epub_ch11 | expected='XI'; extracted='X' | False |
| major | chapter_mismatch | time_machine_epub_ch12 | expected='XII'; extracted='XI' | False |
| major | chapter_mismatch | time_machine_epub_ch13 | expected='Epilogue'; extracted='XII' | False |
| major | chapter_extra | time_machine_epub_ch14 | expected=None; extracted='Epilogue' | False |
| major | chapter_extra | time_machine_epub_ch15 | expected=None; extracted='Endnotes' | False |
| major | chapter_extra | time_machine_epub_ch16 | expected=None; extracted='Colophon' | False |
| major | chapter_extra | time_machine_epub_ch17 | expected=None; extracted='Uncopyright' | False |
| major | front_matter_chapter | time_machine_epub_ch01 | Imprint | False |
| major | boilerplate_block | time_machine_epub_ch01_b001 | Imprint | True |
| major | boilerplate_block | time_machine_epub_ch01_b003 | This particular ebook is based on a transcription from Project Gutenberg and on digital scans from Google Books . | True |
| minor | possible_dialogue_overclassification | time_machine_epub_ch02_b012 | Filby became pensive. “Clearly,” the Time Traveller proceeded, “any real body must have extension in four directions: it must have Length, … | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch02_b014 | “Now, it is very remarkable that this is so extensively overlooked,” continued the Time Traveller, with a slight accession of cheerfulness.… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch02_b016 | “It is simply this. That Space, as our mathematicians have it, is spoken of as having three dimensions, which one may call Length, Breadth,… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch02_b019 | “Scientific people,” proceeded the Time Traveller, after the pause required for the proper assimilation of this, “know very well that Time … | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch02_b029 | “That is the germ of my great discovery. But you are wrong to say that we cannot move about in Time. For instance, if I am recalling an inc… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch02_b056 | “This little affair,” said the Time Traveller, resting his elbows upon the table and pressing his hands together above the apparatus, “is o… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch02_b058 | “It took two years to make,” retorted the Time Traveller. Then, when we had all imitated the action of the Medical Man, he said: “Now I wan… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch02_b059 | There was a minute’s pause perhaps. The Psychologist seemed about to speak to me, but changed his mind. Then the Time Traveller put forth h… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch02_b072 | “Of course,” said the Psychologist, and reassured us. “That’s a simple point of psychology. I should have thought of it. It’s plain enough,… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch02_b075 | “Would you like to see the Time Machine itself?” asked the Time Traveller. And therewith, taking the lamp in his hand, he led the way down … | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch03_b007 | The Psychologist was the only person besides the Doctor and myself who had attended the previous dinner. The other men were Blank, the Edit… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch03_b009 | He said not a word, but came painfully to the table, and made a motion towards the wine. The Editor filled a glass of champagne, and pushed… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch03_b011 | He put down his glass, and walked towards the staircase door. Again I remarked his lameness and the soft padding sound of his footfall, and… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch03_b013 | The first to recover completely from this surprise was the Medical Man, who rang the bell﻿—the Time Traveller hated to have servants waitin… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch03_b020 | “I’d give a shilling a line for a verbatim note,” said the Editor. The Time Traveller pushed his glass towards the Silent Man and rang it w… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch03_b023 | “I can’t argue tonight. I don’t mind telling you the story, but I can’t argue. I will,” he went on, “tell you the story of what has happene… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch03_b024 | “Agreed,” said the Editor, and the rest of us echoed “Agreed.” And with that the Time Traveller began his story as I have set it forth. He … | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch08_b006 | “Weena had been hugely delighted when I began to carry her, but after a while she desired me to let her down, and ran along by the side of … | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch09_b011 | “Then, going up a broad staircase, we came to what may once have been a gallery of technical chemistry. And here I had not a little hope of… | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch13_b022 | The Time Traveller put his hand to his head. He spoke like one who was trying to keep hold of an idea that eluded him. “They were put into … | False |
| minor | possible_dialogue_overclassification | time_machine_epub_ch13_b026 | I shared a cab with the Editor. He thought the tale a “gaudy lie.” For my own part I was unable to come to a conclusion. The story was so f… | False |
| major | front_matter_chapter | time_machine_epub_ch15 | Endnotes | False |
| major | front_matter_chapter | time_machine_epub_ch16 | Colophon | False |
| major | boilerplate_block | time_machine_epub_ch16_b001 | Colophon | True |
| major | boilerplate_block | time_machine_epub_ch16_b003 | This ebook was produced for Standard Ebooks by Alex Cabal , and is based on a transcription produced in 2004 by Distributed Proofreaders fo… | True |
| major | front_matter_chapter | time_machine_epub_ch17 | Uncopyright | False |
| major | boilerplate_block | time_machine_epub_ch17_b001 | Uncopyright | True |
| minor | possible_dialogue_overclassification | time_machine_epub_ch17_b005 | Non-authorship activities performed on items that are in the public domain﻿—so-called “sweat of the brow” work﻿—don’t create a new copyrigh… | False |

#### Pass-Fake Audit
Extractor sai mà report vẫn high-confidence? **YES**. Serious defects present while low_confidence=False and match_rate is high.

### frankenstein_epub — Frankenstein

- Verdict: **FLAGGED-OK**
- Dataset load OK: `True`
- TOC report: `source=nav`, `items=39`, `matched=39`, `match_rate=1.0`, `low_confidence=True`, `ambiguous=[]`
- Nested TOC anchors: Walton, in Continuation -> deferred nested anchor.

#### Nested TOC Anchor Audit
| title | parent_index | split_as_top_level | Level 1 expected | note |
| --- | --- | --- | --- | --- |
| Walton, in Continuation | 28 | False | deferred_nested_anchor | NCX/nav exposes this as a nested anchor in chapter-24.xhtml. Ground truth tracks it as a nested section, not a separate top-level chapter; extractor Level 1 ma… |

#### Chapter Diff
| # | Expected title | Extracted title | block_count | Match? |
| --- | --- | --- | --- | --- |
| 1 | Letter I | Imprint | 5 | mismatch |
| 2 | Letter II | Introduction | 19 | mismatch |
| 3 | Letter III | Preface | 7 | mismatch |
| 4 | Letter IV | Dedication | 2 | mismatch |
| 5 | Chapter I | Epigraph | 2 | mismatch |
| 6 | Chapter II | Frankenstein | 2 | mismatch |
| 7 | Chapter III | Letter I | 14 | mismatch |
| 8 | Chapter IV | Letter II | 12 | mismatch |
| 9 | Chapter V | Letter III | 10 | mismatch |
| 10 | Chapter VI | Letter IV | 40 | mismatch |
| 11 | Chapter VII | Chapter I | 12 | mismatch |
| 12 | Chapter VIII | Chapter II | 17 | mismatch |
| 13 | Chapter IX | Chapter III | 22 | mismatch |
| 14 | Chapter X | Chapter IV | 15 | mismatch |
| 15 | Chapter XI | Chapter V | 28 | mismatch |
| 16 | Chapter XII | Chapter VI | 24 | mismatch |
| 17 | Chapter XIII | Chapter VII | 52 | mismatch |
| 18 | Chapter XIV | Chapter VIII | 34 | mismatch |
| 19 | Chapter XV | Chapter IX | 17 | mismatch |
| 20 | Chapter XVI | Chapter X | 18 | mismatch |
| 21 | Chapter XVII | Chapter XI | 20 | mismatch |
| 22 | Chapter XVIII | Chapter XII | 20 | mismatch |
| 23 | Chapter XIX | Chapter XIII | 23 | mismatch |
| 24 | Chapter XX | Chapter XIV | 21 | mismatch |
| 25 | Chapter XXI | Chapter XV | 39 | mismatch |
| 26 | Chapter XXII | Chapter XVI | 38 | mismatch |
| 27 | Chapter XXIII | Chapter XVII | 22 | mismatch |
| 28 | Chapter XXIV | Chapter XVIII | 26 | mismatch |
| 29 |  | Chapter XIX | 24 | extra |
| 30 |  | Chapter XX | 38 | extra |
| 31 |  | Chapter XXI | 50 | extra |
| 32 |  | Chapter XXII | 42 | extra |
| 33 |  | Chapter XXIII | 31 | extra |
| 34 |  | Chapter XXIV | 82 | extra |
| 35 |  | Endnotes | 4 | extra |
| 36 |  | Colophon | 6 | extra |
| 37 |  | Uncopyright | 5 | extra |

#### Defect Log
| severity | type | location | evidence | report flagged? |
| --- | --- | --- | --- | --- |
| major | chapter_mismatch | frankenstein_epub_ch01 | expected='Letter I'; extracted='Imprint' | True |
| major | chapter_mismatch | frankenstein_epub_ch02 | expected='Letter II'; extracted='Introduction' | True |
| major | chapter_mismatch | frankenstein_epub_ch03 | expected='Letter III'; extracted='Preface' | True |
| major | chapter_mismatch | frankenstein_epub_ch04 | expected='Letter IV'; extracted='Dedication' | True |
| major | chapter_mismatch | frankenstein_epub_ch05 | expected='Chapter I'; extracted='Epigraph' | True |
| major | chapter_mismatch | frankenstein_epub_ch06 | expected='Chapter II'; extracted='Frankenstein' | True |
| major | chapter_mismatch | frankenstein_epub_ch07 | expected='Chapter III'; extracted='Letter I' | True |
| major | chapter_mismatch | frankenstein_epub_ch08 | expected='Chapter IV'; extracted='Letter II' | True |
| major | chapter_mismatch | frankenstein_epub_ch09 | expected='Chapter V'; extracted='Letter III' | True |
| major | chapter_mismatch | frankenstein_epub_ch10 | expected='Chapter VI'; extracted='Letter IV' | True |
| major | chapter_mismatch | frankenstein_epub_ch11 | expected='Chapter VII'; extracted='Chapter I' | True |
| major | chapter_mismatch | frankenstein_epub_ch12 | expected='Chapter VIII'; extracted='Chapter II' | True |
| major | chapter_mismatch | frankenstein_epub_ch13 | expected='Chapter IX'; extracted='Chapter III' | True |
| major | chapter_mismatch | frankenstein_epub_ch14 | expected='Chapter X'; extracted='Chapter IV' | True |
| major | chapter_mismatch | frankenstein_epub_ch15 | expected='Chapter XI'; extracted='Chapter V' | True |
| major | chapter_mismatch | frankenstein_epub_ch16 | expected='Chapter XII'; extracted='Chapter VI' | True |
| major | chapter_mismatch | frankenstein_epub_ch17 | expected='Chapter XIII'; extracted='Chapter VII' | True |
| major | chapter_mismatch | frankenstein_epub_ch18 | expected='Chapter XIV'; extracted='Chapter VIII' | True |
| major | chapter_mismatch | frankenstein_epub_ch19 | expected='Chapter XV'; extracted='Chapter IX' | True |
| major | chapter_mismatch | frankenstein_epub_ch20 | expected='Chapter XVI'; extracted='Chapter X' | True |
| major | chapter_mismatch | frankenstein_epub_ch21 | expected='Chapter XVII'; extracted='Chapter XI' | True |
| major | chapter_mismatch | frankenstein_epub_ch22 | expected='Chapter XVIII'; extracted='Chapter XII' | True |
| major | chapter_mismatch | frankenstein_epub_ch23 | expected='Chapter XIX'; extracted='Chapter XIII' | True |
| major | chapter_mismatch | frankenstein_epub_ch24 | expected='Chapter XX'; extracted='Chapter XIV' | True |
| major | chapter_mismatch | frankenstein_epub_ch25 | expected='Chapter XXI'; extracted='Chapter XV' | True |
| major | chapter_mismatch | frankenstein_epub_ch26 | expected='Chapter XXII'; extracted='Chapter XVI' | True |
| major | chapter_mismatch | frankenstein_epub_ch27 | expected='Chapter XXIII'; extracted='Chapter XVII' | True |
| major | chapter_mismatch | frankenstein_epub_ch28 | expected='Chapter XXIV'; extracted='Chapter XVIII' | True |
| major | chapter_extra | frankenstein_epub_ch29 | expected=None; extracted='Chapter XIX' | True |
| major | chapter_extra | frankenstein_epub_ch30 | expected=None; extracted='Chapter XX' | True |
| major | chapter_extra | frankenstein_epub_ch31 | expected=None; extracted='Chapter XXI' | True |
| major | chapter_extra | frankenstein_epub_ch32 | expected=None; extracted='Chapter XXII' | True |
| major | chapter_extra | frankenstein_epub_ch33 | expected=None; extracted='Chapter XXIII' | True |
| major | chapter_extra | frankenstein_epub_ch34 | expected=None; extracted='Chapter XXIV' | True |
| major | chapter_extra | frankenstein_epub_ch35 | expected=None; extracted='Endnotes' | True |
| major | chapter_extra | frankenstein_epub_ch36 | expected=None; extracted='Colophon' | True |
| major | chapter_extra | frankenstein_epub_ch37 | expected=None; extracted='Uncopyright' | True |
| major | front_matter_chapter | frankenstein_epub_ch01 | Imprint | True |
| major | boilerplate_block | frankenstein_epub_ch01_b001 | Imprint | True |
| major | boilerplate_block | frankenstein_epub_ch01_b003 | This particular ebook is based on a transcription from Project Gutenberg and on digital scans from the Internet Archive . | True |
| major | front_matter_authorial_chapter | frankenstein_epub_ch02 | Introduction (authorial front matter) | True |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch02_b002 | The Publishers of the Standard Novels, in selecting Frankenstein for one of their series, expressed a wish that I should furnish them with … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch02_b003 | It is not singular that, as the daughter of two persons of distinguished literary celebrity, I should very early in life have thought of wr… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch02_b008 | “We will each write a ghost story,” said Lord Byron; and his proposition was acceded to. There were four of us. The noble author began a ta… | False |
| major | front_matter_authorial_chapter | frankenstein_epub_ch03 | Preface (authorial front matter) | True |
| major | front_matter_chapter | frankenstein_epub_ch04 | Dedication | True |
| major | front_matter_chapter | frankenstein_epub_ch05 | Epigraph | True |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch08_b007 | The master is a person of an excellent disposition, and is remarkable in the ship for his gentleness and the mildness of his discipline. Th… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch08_b009 | I cannot describe to you my sensations on the near prospect of my undertaking. It is impossible to communicate to you a conception of the t… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch10_b009 | In the morning, however, as soon as it was light, I went upon deck, and found all the sailors busy on one side of the vessel, apparently ta… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch10_b029 | He is now much recovered from his illness, and is continually on the deck, apparently watching for the sledge that preceded his own. Yet, a… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch10_b032 | “I agree with you,” replied the stranger; “we are unfashioned creatures, but half made up, if one wiser, better, dearer than ourselves﻿—suc… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch10_b037 | Yesterday the stranger said to me, “You may easily perceive, Captain Walton, that I have suffered great and unparalleled misfortunes. I had… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch11_b012 | Everyone loved Elizabeth. The passionate and almost reverential attachment with which all regarded her became, while I shared it, my pride … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch12_b008 | Natural philosophy is the genius that has regulated my fate; I desire, therefore, in this narration, to state those facts which led to my p… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch13_b003 | Elizabeth had caught the scarlet fever; her illness was severe, and she was in the greatest danger. During her illness, many arguments had … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch13_b008 | We sat late. We could not tear ourselves away from each other, nor persuade ourselves to say the word “Farewell!” It was said; and we retir… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch13_b009 | I threw myself into the chaise that was to convey me away, and indulged in the most melancholy reflections. I, who had ever been surrounded… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch13_b011 | The next morning I delivered my letters of introduction, and paid a visit to some of the principal professors. Chance﻿—or rather the evil i… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch13_b012 | I replied in the affirmative. “Every minute,” continued Mr. Krempe with warmth, “every instant that you have wasted on those books is utter… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch13_b017 | “The ancient teachers of this science,” said he, “promised impossibilities, and performed nothing. The modern masters promise very little; … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch13_b019 | I closed not my eyes that night. My internal being was in a state of insurrection and turmoil; I felt that order would thence arise, but I … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch13_b020 | “I am happy,” said M. Waldman, “to have gained a disciple; and if your application equals your ability, I have no doubt of your success. Ch… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch14_b011 | The summer months passed while I was thus engaged, heart and soul, in one pursuit. It was a most beautiful season; never did the fields bes… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch15_b010 | Continuing thus, I came at length opposite to the inn at which the various diligences and carriages usually stopped. Here I paused, I knew … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch15_b011 | Nothing could equal my delight on seeing Clerval; his presence brought back to my thoughts my father, Elizabeth, and all those scenes of ho… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch16_b017 | M. Krempe was not equally docile; and in my condition at that time, of almost insupportable sensitiveness, his harsh blunt encomiums gave m… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch17_b021 | During our walk, Clerval endeavoured to say a few words of consolation; he could only express his heartfelt sympathy. “Poor William!” said … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch17_b030 | While I watched the tempest, so beautiful yet terrific, I wandered on with a hasty step. This noble war in the sky elevated my spirits; I c… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch17_b035 | Six years had elapsed, passed as a dream but for one indelible trace, and I stood in the same place where I had last embraced my father bef… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch17_b049 | We were soon joined by Elizabeth. Time had altered her since I last beheld her; it had endowed her with loveliness surpassing the beauty of… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch18_b008 | “I know,” continued the unhappy victim, “how heavily and fatally this one circumstance weighs against me, but I have no power of explaining… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch18_b011 | “I am,” said she, “the cousin of the unhappy child who was murdered, or rather his sister, for I was educated by, and have lived with his p… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch18_b014 | I cannot pretend to describe what I then felt. I had before experienced sensations of horror; and I have endeavoured to bestow upon them ad… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch18_b023 | “I did confess; but I confessed a lie. I confessed, that I might obtain absolution; but now that falsehood lies heavier at my heart than al… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch18_b027 | During this conversation I had retired to a corner of the prison-room, where I could conceal the horrid anguish that possessed me. Despair!… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch18_b030 | Thus the poor sufferer tried to comfort others and herself. She indeed gained the resignation she desired. But I, the true murderer, felt t… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch19_b004 | My father observed with pain the alteration perceptible in my disposition and habits, and endeavoured by arguments deduced from the feeling… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch19_b009 | “When I reflect, my dear cousin,” said she, “on the miserable death of Justine Moritz, I no longer see the world and its works as they befo… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch19_b010 | I listened to this discourse with the extremest agony. I, not in deed, but in effect, was the true murderer. Elizabeth read my anguish in m… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch20_b006 | It was nearly noon when I arrived at the top of the ascent. For some time I sat upon the rock that overlooks the sea of ice. A mist covered… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch20_b009 | “I expected this reception,” said the daemon. “All men hate the wretched; how, then, must I be hated, who am miserable beyond all living th… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch20_b013 | “Be calm! I entreat you to hear me, before you give vent to your hatred on my devoted head. Have I not suffered enough, that you seek to in… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch20_b015 | “How can I move thee? Will no entreaties cause thee to turn a favourable eye upon thy creature, who implores thy goodness and compassion? B… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch20_b016 | “Why do you call to my remembrance,” I rejoined, “circumstances, of which I shudder to reflect, that I have been the miserable origin and a… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch20_b017 | “Thus I relieve thee, my creator,” he said, and placed his hated hands before my eyes, which I flung from me with violence; “thus I take fr… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch22_b020 | “The pleasant showers and genial warmth of spring greatly altered the aspect of the earth. Men, who before this change seemed to have been … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch24_b021 | “She arrived in safety at a town about twenty leagues from the cottage of De Lacey, when her attendant fell dangerously ill. Safie nursed h… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch25_b039 | “At that instant the cottage door was opened, and Felix, Safie, and Agatha entered. Who can describe their horror and consternation on beho… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch26_b038 | “For some days I haunted the spot where these scenes had taken place; sometimes wishing to see you, sometimes resolved to quit the world an… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch27_b006 | “You are in the wrong,” replied the fiend; “and, instead of threatening, I am content to reason with you. I am malicious because I am miser… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch27_b008 | “I intended to reason. This passion is detrimental to me; for you do not reflect that you are the cause of its excess. If any being felt em… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch27_b010 | “If you consent, neither you nor any other human being shall ever see us again: I will go to the vast wilds of South America. My food is no… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch27_b011 | “You propose,” replied I, “to fly from the habitations of man, to dwell in those wilds where the beasts of the field will be your only comp… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch27_b012 | “How inconstant are your feelings! but a moment ago you were moved by my representations, and why do you again harden yourself to my compla… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch27_b015 | “How is this? I must not be trifled with: and I demand an answer. If I have no ties and no affections, hatred and vice must be my portion; … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch27_b020 | His tale had occupied the whole day; and the sun was upon the verge of the horizon when he departed. I knew that I ought to hasten my desce… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch28_b004 | “I am happy to remark, my dear son, that you have resumed your former pleasures, and seem to be returning to yourself. And yet you are stil… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch28_b006 | “I confess, my son, that I have always looked forward to your marriage with our dear Elizabeth as the tie of our domestic comfort, and the … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch28_b008 | “The expression of your sentiments of this subject, my dear Victor, gives me more pleasure than I have for some time experienced. If you fe… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch28_b017 | After some days spent in listless indolence, during which I traversed many leagues, I arrived at Strasbourg, where I waited two days for Cl… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch28_b019 | We travelled at the time of the vintage, and heard the song of the labourers, as we glided down the stream. Even I, depressed in mind, and … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch28_b020 | Clerval! beloved friend! even now it delights me to record your words, and to dwell on the praise of which you are so eminently deserving. … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch29_b012 | From Derby, still journeying northward, we passed two months in Cumberland and Westmorland. I could now almost fancy myself among the Swiss… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch29_b016 | We left Edinburgh in a week, passing through Coupar, St. Andrew’s, and along the banks of the Tay, to Perth, where our friend expected us. … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch30_b010 | “You have destroyed the work which you began; what is it that you intend? Do you dare to break your promise? I have endured toil and misery… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch30_b014 | The monster saw my determination in my face, and gnashed his teeth in the impotence of anger. “Shall each man,” cried he, “find a wife for … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch30_b019 | All was again silent; but his words rung in my ears. I burned with rage to pursue the murderer of my peace, and precipitate him into the oc… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch30_b026 | I do not know how long I remained in this situation, but when I awoke I found that the sun had already mounted considerably. The wind was h… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch30_b030 | As I was occupied in fixing the boat and arranging the sails, several people crowded towards the spot. They seemed much surprised at my app… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch30_b034 | While this strange dialogue continued, I perceived the crowd rapidly increase. Their faces expressed a mixture of curiosity and anger, whic… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch31_b010 | I entered the room where the corpse lay, and was led up to the coffin. How can I describe my sensations on beholding it? I feel yet parched… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch31_b029 | “Immediately upon your being taken ill, all the papers that were on your person were brought me, and I examined them that I might discover … | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch32_b020 | This letter revived in my memory what I had before forgotten, the threat of the fiend﻿—“ I will be with you on your wedding night! ” Such w… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch32_b022 | In this state of mind I wrote to Elizabeth. My letter was calm and affectionate. “I fear, my beloved girl,” I said, “little happiness remai… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch32_b030 | Such were the lessons of my father. But to me the remembrance of the threat returned: nor can you wonder, that, omnipotent as the fiend had… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch32_b039 | “Be happy, my dear Victor,” replied Elizabeth; “there is, I hope, nothing to distress you; and be assured that if a lively joy is not paint… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch33_b022 | “I thank you,” replied I; “listen, therefore, to the deposition that I have to make. It is indeed a tale so strange, that I should fear you… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch33_b025 | This address caused a considerable change in the physiognomy of my own auditor. He had heard my story with that half kind of belief that is… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch33_b028 | “That cannot be; but all that I can say will be of little avail. My revenge is of no moment to you; yet, while I allow it to be a vice, I c… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b006 | The deep grief which this scene had at first excited quickly gave way to rage and despair. They were dead, and I lived; their murderer also… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b008 | I was answered through the stillness of the night by a loud and fiendish laugh. It rung on my ears long and heavily; the mountains reechoed… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b014 | What his feelings were whom I pursued I cannot know. Sometimes, indeed, he left marks in writing on the barks of the trees, or cut in stone… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b036 | “When younger,” said he, “I believed myself destined for some great enterprise. My feelings are profound; but I possessed a coolness of jud… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b038 | “I thank you, Walton,” he said, “for your kind intentions towards so miserable a wretch; but when you speak of new ties, and fresh affectio… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b049 | “What do you mean? What do you demand of your captain? Are you then so easily turned from your design? Did you not call this a glorious exp… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b057 | September 9th, the ice began to move, and roarings like thunder were heard at a distance, as the islands split and cracked in every directi… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b064 | “That he should live to be an instrument of mischief disturbs me; in other respects, this hour, when I momentarily expect my release, is th… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b072 | His voice seemed suffocated; and my first impulses, which had suggested to me the duty of obeying the dying request of my friend, in destro… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b073 | “And do you dream?” said the daemon; “do you think that I was then dead to agony and remorse?﻿—He,” he continued, pointing to the corpse, “… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b074 | “After the murder of Clerval, I returned to Switzerland, heartbroken and overcome. I pitied Frankenstein; my pity amounted to horror: I abh… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b075 | I was at first touched by the expressions of his misery; yet, when I called to mind what Frankenstein had said of his powers of eloquence a… | False |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch34_b076 | “Oh, it is not thus﻿—not thus,” interrupted the being; “yet such must be the impression conveyed to you by what appears to be the purport o… | False |
| major | front_matter_chapter | frankenstein_epub_ch35 | Endnotes | True |
| major | front_matter_chapter | frankenstein_epub_ch36 | Colophon | True |
| major | boilerplate_block | frankenstein_epub_ch36_b001 | Colophon | True |
| major | boilerplate_block | frankenstein_epub_ch36_b003 | This ebook was produced for Standard Ebooks by Alex Cabal , and is based on a transcription of the 1831 edition produced in 2013 by Greg We… | True |
| major | front_matter_chapter | frankenstein_epub_ch37 | Uncopyright | True |
| major | boilerplate_block | frankenstein_epub_ch37_b001 | Uncopyright | True |
| minor | possible_dialogue_overclassification | frankenstein_epub_ch37_b005 | Non-authorship activities performed on items that are in the public domain﻿—so-called “sweat of the brow” work﻿—don’t create a new copyrigh… | False |
| info | nested_toc_anchor_deferred | parent_index=28 | 'Walton, in Continuation' is a separate TOC anchor kept nested in ground truth; Level 1 does not split it (deferred Level 2). | True |

#### Pass-Fake Audit
Extractor sai mà report vẫn high-confidence? **NO**. No serious silent high-confidence defect detected by automated audit.

### call_wild_epub — The Call of the Wild

- Verdict: **FAIL**
- Dataset load OK: `True`
- TOC report: `source=nav`, `items=13`, `matched=13`, `match_rate=1.0`, `low_confidence=False`, `ambiguous=[]`

#### Chapter Diff
| # | Expected title | Extracted title | block_count | Match? |
| --- | --- | --- | --- | --- |
| 1 | I: Into the Primitive | Imprint | 5 | mismatch |
| 2 | II: The Law of Club and Fang | Epigraph | 2 | mismatch |
| 3 | III: The Dominant Primordial Beast | The Call of the Wild | 1 | mismatch |
| 4 | IV: Who Has Won to Mastership | I: Into the Primitive | 51 | mismatch |
| 5 | V: The Toil of Trace and Trail | II: The Law of Club and Fang | 28 | mismatch |
| 6 | VI: For the Love of a Man | III: The Dominant Primordial Beast | 44 | mismatch |
| 7 | VII: The Sounding of the Call | IV: Who Has Won to Mastership | 38 | mismatch |
| 8 |  | V: The Toil of Trace and Trail | 68 | extra |
| 9 |  | VI: For the Love of a Man | 64 | extra |
| 10 |  | VII: The Sounding of the Call | 52 | extra |
| 11 |  | Colophon | 6 | extra |
| 12 |  | Uncopyright | 5 | extra |

#### Defect Log
| severity | type | location | evidence | report flagged? |
| --- | --- | --- | --- | --- |
| major | chapter_mismatch | call_wild_epub_ch01 | expected='I: Into the Primitive'; extracted='Imprint' | False |
| major | chapter_mismatch | call_wild_epub_ch02 | expected='II: The Law of Club and Fang'; extracted='Epigraph' | False |
| major | chapter_mismatch | call_wild_epub_ch03 | expected='III: The Dominant Primordial Beast'; extracted='The Call of the Wild' | False |
| major | chapter_mismatch | call_wild_epub_ch04 | expected='IV: Who Has Won to Mastership'; extracted='I: Into the Primitive' | False |
| major | chapter_mismatch | call_wild_epub_ch05 | expected='V: The Toil of Trace and Trail'; extracted='II: The Law of Club and Fang' | False |
| major | chapter_mismatch | call_wild_epub_ch06 | expected='VI: For the Love of a Man'; extracted='III: The Dominant Primordial Beast' | False |
| major | chapter_mismatch | call_wild_epub_ch07 | expected='VII: The Sounding of the Call'; extracted='IV: Who Has Won to Mastership' | False |
| major | chapter_extra | call_wild_epub_ch08 | expected=None; extracted='V: The Toil of Trace and Trail' | False |
| major | chapter_extra | call_wild_epub_ch09 | expected=None; extracted='VI: For the Love of a Man' | False |
| major | chapter_extra | call_wild_epub_ch10 | expected=None; extracted='VII: The Sounding of the Call' | False |
| major | chapter_extra | call_wild_epub_ch11 | expected=None; extracted='Colophon' | False |
| major | chapter_extra | call_wild_epub_ch12 | expected=None; extracted='Uncopyright' | False |
| major | front_matter_chapter | call_wild_epub_ch01 | Imprint | False |
| major | boilerplate_block | call_wild_epub_ch01_b001 | Imprint | True |
| major | boilerplate_block | call_wild_epub_ch01_b003 | This particular ebook is based on a transcription from Project Gutenberg and on digital scans from the Internet Archive . | True |
| major | front_matter_chapter | call_wild_epub_ch02 | Epigraph | False |
| minor | very_short_chapter | call_wild_epub_ch03 | title='The Call of the Wild'; block_count=1 | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch04_b040 | “ ‘Answers to the name of Buck,’ ” the man soliloquized, quoting from the saloon-keeper’s letter which had announced the consignment of the… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch04_b050 | The other dog made no advances, nor received any; also, he did not attempt to steal from the newcomers. He was a gloomy, morose fellow, and… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch05_b007 | Before he had recovered from the shock caused by the tragic passing of Curly, he received another shock. François fastened upon him an arra… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch05_b009 | By afternoon, Perrault, who was in a hurry to be on the trail with his despatches, returned with two more dogs. “Billee” and “Joe” he calle… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch05_b010 | By evening Perrault secured another dog, an old husky, long and lean and gaunt, with a battle-scarred face and a single eye which flashed a… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch07_b029 | At other times this hairy man squatted by the fire with head between his legs and slept. On such occasions his elbows were on his knees, hi… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch08_b007 | Three days passed, by which time Buck and his mates found how really tired and weak they were. Then, on the morning of the fourth day, two … | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch08_b008 | Buck heard the chaffering, saw the money pass between the man and the Government agent, and knew that the Scotch halfbreed and the mail-tra… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch08_b033 | A third time the attempt was made, but this time, following the advice, Hal broke out the runners which had been frozen to the snow. The ov… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch08_b034 | Kindhearted citizens caught the dogs and gathered up the scattered belongings. Also, they gave advice. Half the load and twice the dogs, if… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch09_b006 | This man had saved his life, which was something; but, further, he was the ideal master. Other men saw to the welfare of their dogs from a … | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch09_b015 | For Thornton, however, his love seemed to grow and grow. He, alone among men, could put a pack upon Buck’s back in the summer travelling. N… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch09_b020 | It was at Circle City, ere the year was out, that Pete’s apprehensions were realized. “Black” Burton, a man evil-tempered and malicious, ha… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch09_b021 | Those who were looking on heard what was neither bark nor yelp, but a something which is best described as a roar, and they saw Buck’s body… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch09_b024 | Buck had sprung in on the instant; and at the end of three hundred yards, amid a mad swirl of water, he overhauled Thornton. When he felt h… | False |
| minor | possible_dialogue_overclassification | call_wild_epub_ch09_b041 | The Eldorado emptied its occupants into the street to see the test. The tables were deserted, and the dealers and gamekeepers came forth to… | False |
| major | front_matter_chapter | call_wild_epub_ch11 | Colophon | False |
| major | boilerplate_block | call_wild_epub_ch11_b001 | Colophon | True |
| major | boilerplate_block | call_wild_epub_ch11_b003 | This ebook was produced for Standard Ebooks by Alex Cabal , and is based on a transcription produced in 2008 by Ryan Trapp , Kirstin Trapp … | True |
| major | front_matter_chapter | call_wild_epub_ch12 | Uncopyright | False |
| major | boilerplate_block | call_wild_epub_ch12_b001 | Uncopyright | True |
| minor | possible_dialogue_overclassification | call_wild_epub_ch12_b005 | Non-authorship activities performed on items that are in the public domain﻿—so-called “sweat of the brow” work﻿—don’t create a new copyrigh… | False |

#### Pass-Fake Audit
Extractor sai mà report vẫn high-confidence? **YES**. Serious defects present while low_confidence=False and match_rate is high.

## Cross-Format Consistency
- **Jekyll TXT vs EPUB**: count `10` vs `10`, titles equivalent: `True`.
- **Gatsby TXT vs EPUB**: count `9` vs `4`, titles equivalent: `False`.

## Aggregate Defects
| defect_type | count |
| --- | --- |
| boilerplate_block | 25 |
| chapter_extra | 23 |
| chapter_mismatch | 76 |
| chapter_missing | 5 |
| front_matter_authorial_chapter | 3 |
| front_matter_chapter | 17 |
| nested_toc_anchor_deferred | 1 |
| possible_dialogue_overclassification | 304 |
| very_short_chapter | 2 |

## Phase 3 Proposals (Propose-Only)
| issue | likely cause | scope | regression risk | expected before/after |
| --- | --- | --- | --- | --- |
| boilerplate_block | review defect evidence before changing extractor | affects 25 observation(s) | risk of overfitting held-out corpus | deferred until user approves v0.3.2 |
| chapter_extra | review defect evidence before changing extractor | affects 23 observation(s) | risk of overfitting held-out corpus | deferred until user approves v0.3.2 |
| chapter_mismatch | review defect evidence before changing extractor | affects 76 observation(s) | risk of overfitting held-out corpus | deferred until user approves v0.3.2 |
| chapter_missing | review defect evidence before changing extractor | affects 5 observation(s) | risk of overfitting held-out corpus | deferred until user approves v0.3.2 |
| front_matter_authorial_chapter | review defect evidence before changing extractor | affects 3 observation(s) | risk of overfitting held-out corpus | deferred until user approves v0.3.2 |
| front_matter_chapter | review defect evidence before changing extractor | affects 17 observation(s) | risk of overfitting held-out corpus | deferred until user approves v0.3.2 |
| nested_toc_anchor_deferred | review defect evidence before changing extractor | affects 1 observation(s) | risk of overfitting held-out corpus | deferred until user approves v0.3.2 |
| possible_dialogue_overclassification | review defect evidence before changing extractor | affects 304 observation(s) | risk of overfitting held-out corpus | deferred until user approves v0.3.2 |
| very_short_chapter | review defect evidence before changing extractor | affects 2 observation(s) | risk of overfitting held-out corpus | deferred until user approves v0.3.2 |
| nested_toc_anchor_deferred | TOC exposes anchors below a top-level chapter | frankenstein_epub | splitting nested anchors can change chapter count and review workflow | deferred to Level 2; report surfaces anchors instead of silently ignoring them |

## Provenance
| doc_id | url | bytes | sha256 | license | license_url |
| --- | --- | --- | --- | --- | --- |
| jekyll_txt | https://www.gutenberg.org/cache/epub/42/pg42.txt | 163950 | 6f5ebfb79a24bb24e88c8f7e5f35515321e545bf4f137fce6e406c9da0bb2b59 | Project Gutenberg License; public domain in the United States unless otherwise noted | https://www.gutenberg.org/policy/license.html |
| jekyll_epub | https://www.gutenberg.org/ebooks/42.epub.images | 291888 | f5c4c2cc05b2c414b96c4811a4294d32c20a37274aabc003ad87dd4576472e31 | Project Gutenberg License; public domain in the United States unless otherwise noted | https://www.gutenberg.org/policy/license.html |
| gatsby_txt | https://www.gutenberg.org/cache/epub/64317/pg64317.txt | 306553 | ce760ec377accd352b41bb8f64504a72d7aa18ab3afb42ded2b56cecacf29e35 | Project Gutenberg License; public domain in the United States unless otherwise noted | https://www.gutenberg.org/policy/license.html |
| gatsby_epub | https://www.gutenberg.org/ebooks/64317.epub.images | 358098 | 118a24b44b35485364813709b511f087f575f9138e1f503cf35209fdf727c920 | Project Gutenberg License; public domain in the United States unless otherwise noted | https://www.gutenberg.org/policy/license.html |
| wizard_oz_epub | https://standardebooks.org/ebooks/l-frank-baum/the-wonderful-wizard-of-oz/downloads/l-frank-baum_the-wonderful-wizard-of-oz.epub?source=download | 599977 | 5b61bf17c376716690a4a6ffc1c033abc81fba7a393a703af5eb2a6b8bf8290b | CC0 1.0 Universal (Standard Ebooks edition) | https://creativecommons.org/publicdomain/zero/1.0/ |
| alice_epub | https://www.globalgreyebooks.com/ebooks/lewis-carroll_alices-adventures-in-wonderland.epub | 5130195 | 4dbf10d5a02a8fdfdcaa804b69132dd936323eea5eaf421cb29f9d98e7f27b35 | Global Grey public-domain ebook edition; verify source page before freeze | https://www.globalgreyebooks.com/alices-adventures-in-wonderland-ebook.html |
| time_machine_epub | https://standardebooks.org/ebooks/h-g-wells/the-time-machine/downloads/h-g-wells_the-time-machine.epub?source=download | 483334 | 400e8401f16d6b7b7b85759f383fb7b499493a99b62291ab838e43a2b2764a92 | CC0 1.0 Universal (Standard Ebooks edition) | https://creativecommons.org/publicdomain/zero/1.0/ |
| frankenstein_epub | https://standardebooks.org/ebooks/mary-shelley/frankenstein/downloads/mary-shelley_frankenstein.epub?source=download | 633853 | 499dfcc6ede350bec528a7f4f25e0331868197b3be1346a61ad6b04ee308e273 | CC0 1.0 Universal (Standard Ebooks edition) | https://creativecommons.org/publicdomain/zero/1.0/ |
| call_wild_epub | https://standardebooks.org/ebooks/jack-london/the-call-of-the-wild/downloads/jack-london_the-call-of-the-wild.epub?source=download | 418941 | 8e21b7ae4042a6789a9265646eaf88e319047f3225bbf2e54ceaccd8d23909f3 | CC0 1.0 Universal (Standard Ebooks edition) | https://creativecommons.org/publicdomain/zero/1.0/ |

## Re-Verify Commands
```powershell
cd app\reports
python eval_extractor_corpus.py --allow-draft-ground-truth
cd ..\..
python -m unittest discover app\backend\tests
```

## Conclusion
Verdict counts: `{'PASS': 3, 'FAIL': 4, 'FLAGGED-OK': 2}`.
Extractor readiness should be decided from the PASS/FLAGGED-OK/FAIL mix above. This report does not modify extractor code.
