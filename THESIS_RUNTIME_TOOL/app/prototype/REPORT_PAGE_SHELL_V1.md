# Report Page Shell V1

Status: UI shell implemented; full report contract integration intentionally deferred.

## 1. Product role

`Report / Báo cáo` is a run surface, not a document workspace mode.

- Routes: `#console` and `#report` are siblings and share the selected run.
- Console answers: “The run is doing what right now?”
- Report answers: “What persisted result and evidence did the run publish?”
- Report is a final persisted snapshot. It does not move with the Console replay cursor.
- The former prompt/context/cache page is no longer exposed as a run destination.

The page name is `Report / Báo cáo`; the document title inside the page is `Full Run Report`.

## 2. Hard UI boundary

The shell is read-only and fail-closed.

- Do not calculate metrics, coverage ratios, deltas, composite scores, gates, verdicts, costs, or cache rates in App UI.
- Do not infer a missing S0/S1 arm from event order or translated previews.
- Do not turn a run terminal status into a quality verdict.
- Do not treat live Console events as a final report artifact.
- Do not write pipeline state, SQLite, frozen memory, checkpoints, or artifacts.
- Render only values and evidence paths explicitly present in an accepted producer payload.
- Missing or invalid data remains `PENDING`, `UNAVAILABLE`, `PARTIAL`, `ONE ARM`, or `INVALID` with a named contract owner.

Production V1 consumes only the already-wired `report-summary` read model. It is always labeled `SUMMARY-ONLY`; it does not claim to be the future full-report contract. No `report-full` endpoint is called by this milestone.

## 3. Route and navigation model

```text
Workspace
  ├─ document views: Block / Chapter / Book / Memory / Preview
  └─ run views
       ├─ #console  — live state, events, replay, ledger
       └─ #report   — persisted result, evidence, provenance
```

The run picker is shared through App state. Moving between Console and Report preserves the current run selection. Direct navigation to either route selects the newest run only when no run is already selected.

## 4. Visual system

Report belongs to the Console design family:

- compact monospace run header;
- amber accent, square controls, explicit state chips;
- shared `Console | Report` sibling navigation;
- shared `ailab.console_theme` preference;
- identical dark-family palette.

Report light mode is intentionally different from Console paper mode:

- page background: white;
- reading surfaces: white;
- pale neutral gray is reserved for secondary cards and navigation states;
- no cream/paper canvas.

The content layout is optimized for reading instead of log density: sticky section navigation, wide report column, metric cards, comparison table, and finding detail drawer. At narrow widths the section navigation becomes horizontal and the finding drawer stacks below the list. Page-level horizontal overflow is forbidden.

## 5. Section shell

| Section | Purpose | Required producer facts | Fail-closed fallback |
|---|---|---|---|
| Summary | Run identity, persisted verdict, reasons, gate facts | explicit verdict state/source and summary facts | `CHƯA CÓ PHÁN QUYẾT` |
| Coverage | admitted/translated/excluded scope | named counts, units, scope, source | wait for Input Normalization contract |
| Quality | separate metrics and definitions | key, value, unit, definition, scope, direction, source | no value or definition synthesis |
| Comparison | S0/S1 or other explicit arms | baseline, candidate, reported values and reported delta | `ONE ARM` or unavailable; never calculate delta |
| Findings | terminology/literary issues and evidence | stable finding id, category, severity if issued, location, evidence, artifact | no issue inference from logs |
| Execution Evidence | audit facts about how the run executed | persisted calls/cache/cost/stages only when producer reports them | point users to Console; no event aggregation |
| Provenance | source/config/report identity and lineage | digests, versions, producer/source paths | show only confirmed identity fields |
| Artifacts | navigable artifact manifest | label, type, persisted path, status, optional digest | no path guessing |

No section may block rendering of the other sections. A partial report is a valid UI state.

## 6. Integration matrix

| Workstream owner | Future contract contribution | Report destinations | App UI responsibility |
|---|---|---|---|
| Input Normalization | source identity, admission state, source/chapter/block scope, content digests | Summary, Coverage, Provenance | map named facts; never calculate admission or coverage |
| Evaluation / scoring | metric definitions and values, gates, claims, verdict, S0/S1 comparison | Summary, Quality, Comparison | preserve units/scope/source; never create a composite or delta |
| Terminology | TC/TA family results, term findings, occurrence evidence, artifact paths | Quality, Findings | render producer terminology vocabulary and evidence |
| Literary | literary dimensions, findings, evidence state, artifact paths | Quality, Findings | keep literary evidence separate from terminology scores |
| Coordinator | accepted schema version, validation state, producer manifest, execution and integration identity | Status banner, Execution, Provenance, Artifacts | reject/flag invalid payloads and expose missing owner |
| App UI | route, theme, state presentation, responsive behavior, adapters | all UI surfaces | read-only mapping and fail-closed states only |

Full-contract wiring requires a separate Coordinator reservation and an accepted schema. The adapter boundary is the `report` prop of `AgentReportView`; replacing the temporary `report-summary` source must not require redesigning the page.

## 7. Dev harness policy

`report_dev.html` loads `fixtures/report_shell_v1/scenarios.js`, which is never loaded by `index.html`.

Every harness view displays a persistent `FIXTURE ONLY` banner. Fixture values exist only for visual and interaction QA and must not be cited as pipeline output.

Scenarios:

1. `empty` — running/no report;
2. `partial` — some producers ready;
3. `one-arm` — valid report with no comparison;
4. `comparison` — all shell sections populated;
5. `invalid` — contract errors and fail-closed rendering.

## 8. Known gaps

- No accepted full-report client method is wired.
- Coverage, literary findings, execution evidence, full provenance, and artifact manifests remain unavailable in production unless explicitly included in the current read model.
- The existing report-summary schema does not provide verified definitions for every metric; the UI labels those definitions as unavailable.
- Existing observability read-model code may remain elsewhere in the prototype, but it is not a Report data source and is no longer a run-view destination.

## 9. Verification checklist

- Production: `/index.html#report` and Console/Report sibling navigation.
- Harness: `/report_dev.html` across all five scenarios.
- Light theme visibly white; dark theme in the Console family.
- Desktop and narrow viewport; no page-level horizontal overflow.
- Native keyboard access for run picker, sibling tabs, section navigation, finding selection, refresh, and theme.
- No browser console errors.
- `git diff --check` clean.
- Key scan confirms no provider/model API additions, no backend/pipeline changes, and no full-report endpoint wiring.
