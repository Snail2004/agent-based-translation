# Project Workspace Template

Mỗi nguồn/tác phẩm nên có một workspace theo `doc_id`.

```text
<doc_id>/
  project.json
  raw/
    source.<ext>
    provenance.json
  canonical/
    document.json
    glossary.jsonl
    entities.jsonl
    chapter_summaries.jsonl
    manual_reference_subset.jsonl
    entity_relations.jsonl
  working/
    review_state.json
    drafts.json
    translation_review_log.csv
    autosave/
  logs/
    extraction.log
    validation.log
    app_events.jsonl
    jobs.jsonl
  exports/
    dataset_<doc_id>_v0.1.0.zip
  CHANGELOG.md
  versions.json
```

`canonical/` chứa dataset sạch. `working/` chứa trạng thái làm việc và draft. `logs/` và `exports/` thường không commit.
