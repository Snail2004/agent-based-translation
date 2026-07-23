# Workflow replay UI fixture

These files are an exact copy of the accepted 0-API neutral-relay package from:

`C:\work\pytest-eval-integration-relay\test_d2l_fragment_plus_three_o0\workflow`

The package is deliberately partial: Translation is complete, Evaluation and
Publication are pending, and no Evaluation report is indexed. It proves the
parent manifest/event/artifact/handoff/receipt wiring only. It is not evidence
of a live five-chapter D2L run and must never be used by the production app.

`workflow_replay_dev.html` may derive negative tamper scenarios in memory to
exercise fail-closed UI states. It does not write those mutations back here.
