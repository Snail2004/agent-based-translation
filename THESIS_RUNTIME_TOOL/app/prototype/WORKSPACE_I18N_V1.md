# Workspace VI/EN UI contract v1

## Scope

The prototype uses one UI tree with complete Vietnamese and English interface labels. Vietnamese is the default. The language choice applies to the main workspace, Project / Source, Structure, reader and inspector surfaces, Memory, Console, Report, and their UI-only development harnesses.

The implementation is limited to `THESIS_RUNTIME_TOOL/app/prototype/**`. It does not change backend endpoints, payload schemas, lifecycle rules, persisted runtime data, or pipeline output.

## Locale state

- Canonical storage key: `thesis.workspace.locale.v1`.
- Supported values: `vi`, `en`.
- Invalid or unavailable values fall back to `vi`.
- The legacy Console key `thesis.agentconsole.locale.v1` is read and mirrored so an existing Console preference is preserved.
- `document.documentElement.lang` follows the active locale.
- A `thesis:workspace-locale-change` event and the browser `storage` event keep mounted surfaces and other tabs synchronized.

The compact `VI / EN` control is available in the main top bar and on standalone Project, Console, Report, loading, offline, and empty surfaces. The control is a keyboard-focusable button group and exposes the active option with `aria-pressed`.

## Translation boundary

UI-owned navigation, labels, actions, help text, empty/error states, confirmations, and notifications are localized. Producer-owned or contract-bearing values remain unchanged, including:

- document, chapter, block, run, event, publication, and artifact identifiers;
- hashes, schema versions, status codes, backend error details, and payload field names;
- source text, translation text, runtime events, report findings, and other content received from APIs;
- filenames, filesystem paths, model names, and provider names.

This boundary prevents the UI from rewriting authoritative data or implying metrics and lifecycle states that the backend did not provide.

## Verification matrix

- Compile every changed JSX entry and syntax-check `i18n.js`.
- Verify Vietnamese default, English switching, reload persistence, and cross-surface synchronization.
- Exercise production and dev surfaces at desktop, 900 px, and 390 px.
- Check keyboard focus, no overlap, no horizontal overflow, and zero browser console errors.
- Run `git diff --check`, secret/key scan, and an exact owned-path scan before the narrow CodeX commit.
