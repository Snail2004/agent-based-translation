const MEMORY_PAGE_SIZE = 200;

function memoryArray(value) {
  return Array.isArray(value) ? value : [];
}

function memoryBlockRef(item) {
  return item?.block_id || item?.trigger_block_id || item?.source_block_id || "";
}

function memoryUnique(values) {
  return [...new Set(values.filter(Boolean))];
}

function memoryEvidenceBlockIds(kind, item) {
  if (!item) return [];
  if (kind === "glossary") {
    return memoryUnique(memoryArray(item.occurrences).map(memoryBlockRef));
  }
  if (kind === "entities") {
    return memoryUnique([
      ...memoryArray(item.mentions).map(memoryBlockRef),
      item.first_block_id,
      item.latest_block_id,
    ]);
  }
  if (kind === "relations") {
    return memoryUnique([
      ...memoryArray(item.evidence).map(memoryBlockRef),
      item.valid_from_block_id,
      item.valid_to_block_id,
    ]);
  }
  return memoryUnique([
    ...memoryArray(item.source_block_ids),
    ...memoryArray(item.evidence).map(memoryBlockRef),
    item.block_id,
  ]);
}

function memoryStatus(kind, item) {
  if (kind === "relations") return item.state_label || item.relation_type || "relation";
  if (kind === "summaries") return item.status || (item.source || item.summary_source || item.summary ? "available" : "empty");
  return item.status || item.visibility || item.scope || "unclassified";
}

function memoryConfidence(item) {
  const value = Number(item?.confidence);
  return Number.isFinite(value) ? value.toFixed(2) : "";
}

function memoryEntityLabel(entityId, entityMap) {
  const entity = entityMap.get(entityId);
  return entity?.canonical_target || entity?.canonical_source || entityId || "unknown entity";
}

function memoryItemTitle(kind, item, entityMap, chapterMap) {
  if (kind === "glossary") return item.source_term || item.term || item.glossary_id || "Untitled term";
  if (kind === "entities") return item.canonical_target || item.canonical_source || item.entity_id || "Untitled entity";
  if (kind === "relations") {
    return `${memoryEntityLabel(item.source_entity_id, entityMap)} -> ${memoryEntityLabel(item.target_entity_id, entityMap)}`;
  }
  const chapter = chapterMap.get(item.chapter_id);
  return chapter?.title || chapter?.chapter_title || item.chapter_id || "Untitled summary";
}

function memoryItemSubtitle(kind, item) {
  if (kind === "glossary") {
    if (item.expected_target || item.canonical_target) return item.expected_target || item.canonical_target;
    const directives = memoryArray(item.directives);
    if (directives.length === 1) return directives[0].instruction || directives[0].target_term || "One run directive";
    if (directives.length > 1) return `${directives.length} persisted run directives`;
    return "No target form";
  }
  if (kind === "entities") {
    const source = item.canonical_source || "";
    const type = item.entity_type || item.referent_kind || "entity";
    return source ? `${source} | ${type}` : type;
  }
  if (kind === "relations") return item.relation_type || item.state_label || "Relation";
  return item.summary_source || item.summary || item.source || "No summary text";
}

function memorySearchText(kind, item, entityMap, chapterMap) {
  const values = [
    memoryItemTitle(kind, item, entityMap, chapterMap),
    memoryItemSubtitle(kind, item),
    memoryStatus(kind, item),
    item.source_term,
    item.expected_target,
    item.canonical_source,
    item.canonical_target,
    item.entity_type,
    item.relation_type,
    item.state_label,
    ...memoryArray(item.aliases_source),
    ...memoryArray(item.aliases_target),
    ...memoryArray(item.allowed_variants),
    ...memoryArray(item.forbidden_variants),
  ];
  return values.filter(Boolean).join(" ").toLocaleLowerCase();
}

function memoryRecordKey(kind, item, index) {
  return item.glossary_id || item.term_id || item.entity_id || item.relation_id || item.summary_id
    || item.chapter_id || `${kind}:${index}`;
}

function memoryChapterIds(kind, item, blockMap, entityChapterMap) {
  const ids = new Set();
  memoryEvidenceBlockIds(kind, item).forEach(blockId => {
    const chapterId = blockMap.get(blockId)?.chapter_id;
    if (chapterId) ids.add(chapterId);
  });
  if (kind === "summaries" && item.chapter_id) ids.add(item.chapter_id);
  if (kind === "relations") {
    const sourceChapters = entityChapterMap.get(item.source_entity_id) || new Set();
    const targetChapters = entityChapterMap.get(item.target_entity_id) || new Set();
    sourceChapters.forEach(chapterId => {
      if (targetChapters.has(chapterId)) ids.add(chapterId);
    });
  }
  return ids;
}

function MemoryPill({ children, tone = "" }) {
  return <span className={`memory-pill wb-status${tone ? ` ${tone}` : ""}`}>{children}</span>;
}

function MemoryField({ label, children, mono = false }) {
  if (children == null || children === "" || (Array.isArray(children) && !children.length)) return null;
  return (
    <div className="memory-field wb-kv-row">
      <span>{label}</span>
      <div className={mono ? "mono" : ""}>{children}</div>
    </div>
  );
}

function MemoryRecordDetail({ kind, item, entityMap, chapterMap, blockMap, evidenceLoading, onJumpToBlock }) {
  if (!item) {
    return (
      <div className="memory-detail-empty wb-empty">
        <Ic.layers size={22} />
        <b>Select a memory record</b>
        <span>Its complete fields and source evidence will appear here.</span>
      </div>
    );
  }
  const title = memoryItemTitle(kind, item, entityMap, chapterMap);
  const subtitle = memoryItemSubtitle(kind, item);
  const evidence = memoryEvidenceBlockIds(kind, item);
  const confidence = memoryConfidence(item);
  const isRunContext = item.provenance?.branch === "run_context";
  return (
    <section className="memory-detail wb-detail" aria-label="Memory record detail">
      <div className="memory-detail-head wb-section">
        <div>
          <span className="memory-kicker">{kind}</span>
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
        <MemoryPill>{memoryStatus(kind, item)}</MemoryPill>
      </div>

      {kind === "glossary" && <>
        <MemoryField label="Source term" mono>{item.source_term}</MemoryField>
        <MemoryField label="Expected target">{item.expected_target}</MemoryField>
        {isRunContext && <MemoryField label="Persisted directives">
          <div className="memory-directive-list">
            {memoryArray(item.directives).map((directive, index) => (
              <span key={`${directive.policy || "directive"}:${directive.instruction || index}`}>
                <b>{directive.policy || "directive"}</b>{directive.instruction || directive.target_term || "No rendered instruction"}
              </span>
            ))}
          </div>
        </MemoryField>}
        <MemoryField label="Allowed variants">{memoryArray(item.allowed_variants).join(", ")}</MemoryField>
        <MemoryField label="Forbidden variants">{memoryArray(item.forbidden_variants).join(", ")}</MemoryField>
        <MemoryField label="Scope">{item.scope}</MemoryField>
        {isRunContext && <MemoryField label="Applied in run" mono>{`${memoryArray(item.usage_block_ids).length} blocks`}</MemoryField>}
      </>}

      {kind === "entities" && <>
        <MemoryField label="Canonical source">{item.canonical_source}</MemoryField>
        <MemoryField label="Canonical target">{item.canonical_target}</MemoryField>
        <MemoryField label="Source aliases">{memoryArray(item.aliases_source).join(", ")}</MemoryField>
        <MemoryField label="Target aliases">{memoryArray(item.aliases_target).join(", ")}</MemoryField>
        <MemoryField label="Entity type">{item.entity_type || item.referent_kind}</MemoryField>
        <MemoryField label="Role">{item.role || item.social_role}</MemoryField>
        <MemoryField label="Pronoun policy">{item.pronoun_policy}</MemoryField>
      </>}

      {kind === "relations" && <>
        <MemoryField label="Source entity">{memoryEntityLabel(item.source_entity_id, entityMap)}</MemoryField>
        <MemoryField label="Target entity">{memoryEntityLabel(item.target_entity_id, entityMap)}</MemoryField>
        <MemoryField label="Relation type">{item.relation_type}</MemoryField>
        <MemoryField label="State">{item.state_label}</MemoryField>
        <MemoryField label="Valid interval" mono>{[item.valid_from_block_id, item.valid_to_block_id || "open"].filter(Boolean).join(" -> ")}</MemoryField>
        <MemoryField label="Address policy" mono>{item.address_policy ? JSON.stringify(item.address_policy) : ""}</MemoryField>
        <MemoryField label="Notes">{item.notes}</MemoryField>
      </>}

      {kind === "summaries" && <>
        <MemoryField label="Chapter" mono>{item.chapter_id}</MemoryField>
        <MemoryField label="Summary">{item.summary_source || item.summary || item.source}</MemoryField>
      </>}

      {confidence && <MemoryField label="Confidence" mono>{confidence}</MemoryField>}

      <div className="memory-evidence">
        <div className="memory-section-head">
          <b>{isRunContext ? "Context evidence / use" : "Source evidence"}</b>
          <span>{evidence.length} block{evidence.length === 1 ? "" : "s"}</span>
        </div>
        {evidence.length ? (
          <div className="memory-evidence-list">
            {evidence.map(blockId => {
              const sourceBlock = blockMap.get(blockId);
              return (
                <button key={blockId} type="button" disabled={!sourceBlock} onClick={() => onJumpToBlock(blockId)}>
                  <Ic.arrowRight size={11} />
                  <span className="mono">{blockId}</span>
                  <em>{sourceBlock?.chapter_id || "Block unavailable"}</em>
                </button>
              );
            })}
          </div>
        ) : evidenceLoading ? (
          <p className="memory-muted">Loading the full source-evidence index...</p>
        ) : <p className="memory-muted">No block-level evidence is available in this dataset.</p>}
      </div>

      <details className="memory-raw">
        <summary>Complete stored record</summary>
        <pre>{JSON.stringify(item, null, 2)}</pre>
      </details>
    </section>
  );
}

function MemoryWorkspace({
  docInfo, profile, glossary, entities, relations, summaries, blocks, chapters, activeBlock,
  runMemory, initialKind, evidenceLoading, onSelectBlock, onModeChange,
}) {
  const runScope = runMemory?.scope || {};
  const runCollections = React.useMemo(() => ({
    glossary: memoryArray(runMemory?.glossary),
    entities: memoryArray(runMemory?.entities),
    relations: memoryArray(runMemory?.relations),
    summaries: memoryArray(runMemory?.summaries),
  }), [runMemory]);
  const runAvailable = !!runScope.available && Object.values(runCollections).some(rows => rows.length > 0);
  const runScopedProject = docInfo?.thesis?.memory_scope === "selected_run";
  const defaultScope = runScopedProject || runAvailable ? "run" : "book";
  const [scope, setScope] = React.useState(defaultScope);
  const [kind, setKind] = React.useState(initialKind || "glossary");
  const [query, setQuery] = React.useState("");
  const [statusFilter, setStatusFilter] = React.useState("all");
  const [chapterId, setChapterId] = React.useState(activeBlock?.chapter_id || chapters?.[0]?.chapter_id || "");
  const [selectedKey, setSelectedKey] = React.useState("");
  const [visibleCount, setVisibleCount] = React.useState(MEMORY_PAGE_SIZE);

  React.useEffect(() => {
    if (initialKind) setKind(initialKind);
  }, [initialKind]);
  React.useEffect(() => {
    setScope(defaultScope);
    setQuery("");
    setStatusFilter("all");
  }, [docInfo?.doc_id, defaultScope]);
  const runChapterIds = React.useMemo(() => new Set(memoryArray(runScope.chapter_ids)), [runScope.chapter_ids]);
  React.useEffect(() => {
    if (activeBlock?.chapter_id && (!runAvailable || runChapterIds.has(activeBlock.chapter_id))) setChapterId(activeBlock.chapter_id);
  }, [activeBlock?.chapter_id, runAvailable, runChapterIds]);
  React.useEffect(() => {
    setVisibleCount(MEMORY_PAGE_SIZE);
    setSelectedKey("");
  }, [kind, scope, chapterId, query, statusFilter]);

  const blockMap = React.useMemo(() => new Map(memoryArray(blocks).map(block => [block.block_id, block])), [blocks]);
  const chapterMap = React.useMemo(() => new Map(memoryArray(chapters).map(chapter => [chapter.chapter_id, chapter])), [chapters]);
  const registryCollections = React.useMemo(() => ({
    glossary: memoryArray(glossary),
    entities: memoryArray(entities),
    relations: memoryArray(relations),
    summaries: memoryArray(summaries),
  }), [glossary, entities, relations, summaries]);
  const baseCollections = scope === "book" || !runAvailable ? registryCollections : runCollections;
  const entityMap = React.useMemo(() => new Map(memoryArray(baseCollections.entities).map(entity => [entity.entity_id, entity])), [baseCollections.entities]);
  const entityChapterMap = React.useMemo(() => {
    const result = new Map();
    memoryArray(baseCollections.entities).forEach(entity => {
      const ids = new Set();
      memoryEvidenceBlockIds("entities", entity).forEach(blockId => {
        const sourceBlock = blockMap.get(blockId);
        if (sourceBlock?.chapter_id) ids.add(sourceBlock.chapter_id);
      });
      result.set(entity.entity_id, ids);
    });
    return result;
  }, [baseCollections.entities, blockMap]);

  const collections = React.useMemo(() => {
    if (scope !== "chapter") return baseCollections;
    return Object.fromEntries(Object.entries(baseCollections).map(([collectionKind, rows]) => [
      collectionKind,
      rows.filter(item => memoryChapterIds(collectionKind, item, blockMap, entityChapterMap).has(chapterId)),
    ]));
  }, [scope, baseCollections, blockMap, entityChapterMap, chapterId]);
  const descriptors = [
    { id: "glossary", label: "Glossary", icon: Ic.tag },
    { id: "entities", label: "Entities", icon: Ic.users },
    { id: "relations", label: "Relations", icon: Ic.layers },
    { id: "summaries", label: "Summaries", icon: Ic.doc },
  ];
  const selectedCollection = collections[kind] || [];
  const scopedRows = selectedCollection;
  const statuses = React.useMemo(() => memoryUnique(scopedRows.map(item => memoryStatus(kind, item))).sort(), [scopedRows, kind]);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filteredRows = React.useMemo(() => scopedRows
    .map((item, index) => ({ item, index, key: memoryRecordKey(kind, item, index) }))
    .filter(row => statusFilter === "all" || memoryStatus(kind, row.item) === statusFilter)
    .filter(row => !normalizedQuery || memorySearchText(kind, row.item, entityMap, chapterMap).includes(normalizedQuery))
    .sort((a, b) => memoryItemTitle(kind, a.item, entityMap, chapterMap).localeCompare(memoryItemTitle(kind, b.item, entityMap, chapterMap))),
  [scopedRows, statusFilter, normalizedQuery, kind, entityMap, chapterMap]);
  const visibleRows = filteredRows.slice(0, visibleCount);
  const selectedRow = filteredRows.find(row => row.key === selectedKey) || visibleRows[0] || null;
  const selectedChapter = chapterMap.get(chapterId);
  const inferredLiterary = /literary|literature|novel/i.test(profile || "") || entities.length > 0 || relations.length > 0;
  const scopedChapters = runAvailable
    ? memoryArray(chapters).filter(chapter => runChapterIds.has(chapter.chapter_id))
    : memoryArray(chapters);
  const projectLabel = docInfo?.thesis?.job_id || docInfo?.doc_id || docInfo?.metadata?.title || "Memory workspace";
  const experimentLabel = memoryArray(runScope.experiment_ids).join(", ");
  const scopeTitle = scope === "book"
    ? "Full inherited registry"
    : scope === "run"
      ? "Run context"
      : (selectedChapter?.title || selectedChapter?.chapter_title || chapterId);
  const scopeDescription = scope === "book"
    ? `${registryCollections.glossary.length} baseline terms stored in this work DB; this is not the selected run result.`
    : scope === "chapter"
      ? `${experimentLabel || "Selected run"}: ${collections.glossary.length} persisted context terms in this chapter; run total ${runScope.context_term_count || 0}.`
      : `${experimentLabel || "Selected run"}: ${runScope.context_term_count || 0} context terms across ${memoryArray(runScope.chapter_ids).length} chapter(s).`;

  function jumpToBlock(blockId) {
    if (!blockMap.has(blockId)) return;
    onSelectBlock(blockId);
    onModeChange("block");
  }

  return (
    <main className="col col-center memory-workspace wb-operational">
      <header className="memory-header wb-section">
        <div>
          <span className="memory-kicker">{scope === "book" ? "Registry baseline" : "Persisted run memory"}</span>
          <h1>{projectLabel}</h1>
          <p>{scopeDescription}</p>
        </div>
        <div className="memory-header-counts">
          <span><b>{collections.glossary.length}</b> terms</span>
          <span><b>{collections.entities.length}</b> entities</span>
          <span><b>{collections.relations.length}</b> relations</span>
          <span><b>{collections.summaries.length}</b> summaries</span>
        </div>
      </header>

      <div className="memory-controls wb-toolbar">
        <div className="memory-scope" role="group" aria-label="Memory scope">
          {(runScopedProject || runAvailable) && <button type="button" className={scope === "run" ? "on" : ""} onClick={() => setScope("run")}>Run context</button>}
          <button type="button" className={scope === "chapter" ? "on" : ""} disabled={evidenceLoading && !runAvailable}
            title={evidenceLoading && !runAvailable ? "Chapter scope becomes available after source evidence is loaded" : ""}
            onClick={() => {
              if (runAvailable && !runChapterIds.has(chapterId)) setChapterId(scopedChapters[0]?.chapter_id || "");
              setScope("chapter");
            }}>{runScopedProject || runAvailable ? "Run chapter" : "Current chapter"}</button>
          {!runScopedProject && <button type="button" className={scope === "book" ? "on" : ""} onClick={() => setScope("book")}>Project registry</button>}
        </div>
        {scope === "chapter" && (
          <select aria-label="Memory chapter" value={chapterId} onChange={event => setChapterId(event.target.value)}>
            {scopedChapters.map(chapter => <option key={chapter.chapter_id} value={chapter.chapter_id}>{chapter.title || chapter.chapter_title || chapter.chapter_id}</option>)}
          </select>
        )}
        <label className="memory-search">
          <Ic.search size={13} />
          <input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search memory records" />
        </label>
        <select aria-label="Memory status" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}>
          <option value="all">All statuses</option>
          {statuses.map(status => <option key={status} value={status}>{status}</option>)}
        </select>
        {evidenceLoading && <span className="memory-sync" role="status"><span />Loading source evidence</span>}
      </div>

      {scope === "book" && runAvailable && (
        <div className="memory-notice memory-notice--info">
          <Ic.alert size={13} />
          <span><b>Inherited baseline.</b> These rows were copied into the work database; switch to Run context to inspect what was actually sent to the selected translation run.</span>
        </div>
      )}

      {scope === "book" && inferredLiterary && (
        <div className="memory-notice">
          <Ic.alert size={13} />
          <span><b>Whole-book inspector.</b> This view can expose future-story information; it does not change the as-of context sent to Translator.</span>
        </div>
      )}

      <nav className="memory-tabs wb-toolbar" aria-label="Memory record types">
        {descriptors.map(descriptor => {
          const I = descriptor.icon;
          return (
            <button key={descriptor.id} type="button" className={kind === descriptor.id ? "on" : ""} onClick={() => setKind(descriptor.id)}>
              <I size={13} />
              <span>{descriptor.label}</span>
              <b>{collections[descriptor.id].length}</b>
            </button>
          );
        })}
      </nav>

      <div className="memory-body wb-split">
        <section className="memory-list wb-pane" aria-label={`${kind} memory records`}>
          <div className="memory-list-head wb-section-title">
            <div>
              <b>{scopeTitle}</b>
              <span>{filteredRows.length} matching record{filteredRows.length === 1 ? "" : "s"}</span>
            </div>
            <span className="mono">showing {Math.min(visibleRows.length, filteredRows.length)} / {filteredRows.length}</span>
          </div>
          <div className="memory-table-head"><span>Record</span><span>Status</span><span>Evidence</span></div>
          <div className="memory-rows wb-record-list">
            {visibleRows.map(row => {
              const evidence = memoryEvidenceBlockIds(kind, row.item);
              return (
                <button key={row.key} type="button" aria-selected={selectedRow?.key === row.key}
                  className={`memory-row wb-record-row${selectedRow?.key === row.key ? " selected" : ""}`} onClick={() => setSelectedKey(row.key)}>
                  <span className="memory-row-copy">
                    <b>{memoryItemTitle(kind, row.item, entityMap, chapterMap)}</b>
                    <em>{memoryItemSubtitle(kind, row.item)}</em>
                  </span>
                  <MemoryPill>{memoryStatus(kind, row.item)}</MemoryPill>
                  <span className="memory-row-evidence mono">{evidenceLoading && !evidence.length ? "..." : evidence.length}</span>
                </button>
              );
            })}
            {!visibleRows.length && (
              <div className="memory-empty wb-empty"><Ic.search size={20} /><b>No matching records</b><span>Change scope, status, or search text.</span></div>
            )}
          </div>
          {visibleRows.length < filteredRows.length && (
            <button className="memory-load-more" type="button" onClick={() => setVisibleCount(count => count + MEMORY_PAGE_SIZE)}>
              Show next {Math.min(MEMORY_PAGE_SIZE, filteredRows.length - visibleRows.length)}
            </button>
          )}
        </section>
        <MemoryRecordDetail kind={kind} item={selectedRow?.item || null} entityMap={entityMap} chapterMap={chapterMap}
          blockMap={blockMap} evidenceLoading={evidenceLoading} onJumpToBlock={jumpToBlock} />
      </div>
    </main>
  );
}

window.MemoryWorkspace = MemoryWorkspace;
