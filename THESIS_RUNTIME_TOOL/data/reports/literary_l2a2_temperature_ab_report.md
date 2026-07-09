# L2A2e Builder Temperature A/B Report

Status: COMPLETE, NO ADOPTION

Scope: Wuthering Heights chapter 1, M1 (B0+B1+B2), 500 tokens / 8 blocks,
`gpt-5.4-mini`, reasoning `none`, seed `20260612`. Three fresh runs per arm,
interleaved as pre-registered. Every run used a separate local cache DB.

## Verdict

Keep the Builder default at `temperature=1.0`.

Temperature 0.2 reduced retries and median cost, but failed mandatory Gate 0:

- Critical cases were not correct in all three temp-0.2 runs:
  - `dog` at `wh_ch01_b018` failed in r1.
  - `sir` at `wh_ch01_b027` failed in r3.
- Temp-0.2 consensus retention relative to temp-1.0 consensus was:
  - mentions: 19/20 = 95.0%
  - turns: 15/19 = 78.9%
  - events: 7/13 = 53.8%

The pre-registered rule says Gate 0 failure stops adoption regardless of retry
or cost improvements.

## Per-run measurements

| Run | Calls | Retries | First-pass | Prompt tok | Provider cached tok | Completion tok | Cost |
|---|---:|---:|---:|---:|---:|---:|---:|
| temp10_r1 | 17 | 2 | 13/15 | 47,421 | 4,608 | 11,644 | $0.034106 |
| temp02_r1 | 15 | 0 | 15/15 | 41,029 | 3,328 | 9,255 | $0.028018 |
| temp10_r2 | 18 | 3 | 12/15 | 51,638 | 10,752 | 11,163 | $0.032816 |
| temp02_r2 | 16 | 1 | 14/15 | 44,963 | 6,144 | 9,850 | $0.029558 |
| temp10_r3 | 18 | 3 | 12/15 | 52,824 | 7,424 | 10,633 | $0.032802 |
| temp02_r3 | 16 | 1 | 14/15 | 43,688 | 5,120 | 10,701 | $0.031172 |

All local `cache_hits` were zero. Provider `cached_tokens` are reported
separately and do not invalidate freshness.

Arm totals:

| Arm | Calls | Retries | First-pass success | Prompt tok | Completion tok | Cost | Median cost |
|---|---:|---:|---:|---:|---:|---:|---:|
| temp 1.0 | 53 | 8 | 37/45 = 82.2% | 151,883 | 33,440 | $0.099724 | $0.032816 |
| temp 0.2 | 47 | 2 | 43/45 = 95.6% | 129,680 | 29,806 | $0.088749 | $0.029558 |

Total API cost: **$0.188473**. No run exceeded the $0.06 per-run stop cap.

## Reproducibility

Keys use the pre-registered NFC/casefold/whitespace/quote normalization and do
not remove words.

| Axis | temp 1.0 counts | temp 1.0 mean pairwise Jaccard | temp 0.2 counts | temp 0.2 mean pairwise Jaccard |
|---|---|---:|---|---:|
| mentions | 31 / 22 / 19 | 0.5011 | 22 / 22 / 22 | 0.7184 |
| turns | 21 / 21 / 19 | 0.7216 | 17 / 18 / 22 | 0.6806 |
| events | 23 / 19 / 13 | 0.2927 | 17 / 19 / 18 | 0.4055 |

Mean across the three axes improved from 0.5051 to 0.6015, but turn Jaccard
dropped by 0.0410. Therefore the scenario-(a) guard ("no axis down by more
than 0.02") also does not hold.

Pairwise resolution agreement on shared turns:

- temp 1.0: 0.7647 / 0.5625 / 0.4444 (mean 0.5905)
- temp 0.2: 0.3333 / 0.4000 / 0.6875 (mean 0.4736)

Lower temperature made output membership more stable on two axes, but did not
make speaker/addressee resolution more stable in this sample.

## Critical-case audit

| Run | sir b005 | sir b006 | sir b027 | dog b018 | narrator b009 | narrator b011 |
|---|---|---|---|---|---|---|
| temp10_r1 | pass | pass | pass | pass | present/unknown | present/unknown |
| temp10_r2 | pass | pass | pass | pass | present/unknown | absent |
| temp10_r3 | pass | pass | pass | pass | present/unknown | absent |
| temp02_r1 | pass | pass | pass | **fail** | present/candidate | absent |
| temp02_r2 | pass | pass | pass | pass | present/unknown | absent |
| temp02_r3 | pass | pass | **fail** | pass | present/unknown | present/unknown |

## Consensus-only evidence audit

Temp-1.0-only consensus contains real evidence, not only noise:

- Turns: `Walk in!`; Joseph is ordered to take Lockwood's horse and bring
  wine; `I would have made a few comments`; `You are flurried, Mr. Lockwood`.
- Mention: `the surly owner`.
- Events include Lockwood's introduction, the invitation to enter, Joseph
  being called, and the dogs not meddling with people.

Temp-0.2-only consensus includes both useful evidence and alternate quote
spans:

- Mentions: `a country squire`, `my landlord`.
- Turn: the shorter span `Joseph, take Mr. Lockwood's horse`.
- Events include alternate evidence spans for the introduction/Joseph call,
  plus `What the devil is the matter?`, the possessed-swine comparison, and
  `Your health, sir?`.

Some exact-quote Jaccard loss is therefore span-choice variation, but the
missing temp-1.0 turns and the two critical failures show that Gate 0 failure
is not a metric artifact.

## Provenance and safety

- Returned model in all artifacts: `gpt-5.4-mini`
- `system_fingerprint`: unavailable in all responses (allowed by spec)
- Request temperatures in the six cache DBs: exactly 1.0 or 0.2 as assigned
- Local cache rows: 17/15/18/16/18/16, matching actual calls
- Builder SHA256:
  `2cb7361af5cf2ddff5b44b3074824d1b583f9c78eb2549210f123803e892d6e8`
- Prompt design SHA256:
  `7913e857156ff476deb839c3783052d4d603866cec35c779c97bfb75769ac50b`
- Default config SHA256:
  `bf034516f40cd12a16e66eab5139422d6c381d0dcf528c08903beb0670b5c38d`
- Temp-0.2 config SHA256:
  `bfefe3ab8c007c74560a8e7b637ea208b5160e306f4c0a2919b705c5700fa4ba`
- Frozen D2L DB SHA256 after all runs:
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`
- Exact key scan over the six report dirs, task, and temp config: zero hits
- `OPENAI_API_KEY` removed from the process environment after each run
- Tests: `python -m pytest pipeline/tests -q` -> 335 passed

No Gatsby API run was performed for this task. No files were committed.
