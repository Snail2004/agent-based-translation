/* ===== LEFT SIDEBAR: project, source status, filters, chapter -> block tree ===== */

function SourceStatus({ docInfo, blocks, chapters, errors, total }) {
  const meta = docInfo?.metadata || {};
  const valid = errors.length === 0;
  const tip = [
    `Source: ${meta.source_format || "unknown"}`,
    `${total ?? blocks.length} blocks in ${chapters.length} chapters`,
    errors.length ? `${errors.length} validation issue(s)` : "No validation issues",
    meta.extraction_tool ? `Extractor: ${meta.extraction_tool}` : "",
  ].filter(Boolean).join(" · ");
  return (
    <div className="source-summary tip" data-tip={tip}>
      <span className={"ss-dot " + (valid ? "ok" : "bad")} />
      <b className="mono">{String(meta.source_format || "source").toUpperCase()}</b>
      <span>{Number(total ?? blocks.length).toLocaleString()} blocks</span>
      <span>{chapters.length} chapters</span>
      {errors.length > 0 && <span className="source-issues">{errors.length} issues</span>}
    </div>
  );
}

const FILTERS = [
  { id: "unreviewed", label: "Unreviewed" },
  { id: "dialogue", label: "Dialogue" },
  { id: "flag", label: "Has flag" },
  { id: "opening", label: "Chapter opening" },
  { id: "annotation", label: "Has annotation" },
];

function FilterChips({ active, onToggle, counts }) {
  return (
    <div className="filters">
      {FILTERS.map(f => (
        <button key={f.id}
          className={"chip" + (active.has(f.id) ? " active" : "")}
          onClick={() => onToggle(f.id)}>
          {f.label}
          <span className="count">{counts[f.id] ?? 0}</span>
        </button>
      ))}
    </div>
  );
}

function RuntimeOverlayBadges({ block, compact = false }) {
  const counts = block.overlay_counts || {};
  const source = Number(counts.source || 0);
  const target = Number(counts.target || 0);
  const drift = Number(counts.drift || 0);
  const mismatch = Number(counts.mismatch || 0);
  if (!source && !target && !drift && !mismatch) return null;
  const cls = compact ? "previewrow-ic runtime" : "br-runtime";
  const localizationOnly = block.overlay_mode === "localization";
  const tip = localizationOnly
    ? `Localization: EN ${source}, VI ${target}, lệch chuẩn ${mismatch}`
    : `Runtime overlay: source ${source}, target ${target}, drift ${drift}`;
  return (
    <>
      {source > 0 && <span className={cls + " tip"} data-tip={tip}><Ic.layers size={11} /></span>}
      {target > 0 && <span className={cls + " target tip"} data-tip={tip}><Ic.eye size={11} /></span>}
      {localizationOnly && mismatch > 0 && <span className={cls + " drift tip"} data-tip={tip}><Ic.alert size={11} /></span>}
      {!localizationOnly && drift > 0 && <span className={cls + " drift tip"} data-tip={tip}><Ic.alert size={11} /></span>}
    </>
  );
}

function SidebarBlockRow({ block, reviewed, hasAnno, selected, onSelect }) {
  const flagged = (block.quality_flags || []).some(f => f !== "ok");
  return (
    <button className={"blockrow" + (selected ? " sel" : "")} onClick={() => onSelect(block.block_id)}
      title={block.block_id} aria-label={`Open block ${block.block_id}`}>
      <span className={"br-check" + (reviewed ? " on" : "")}>
        {reviewed ? <Ic.checkSmall size={11} /> : null}
      </span>
      <span className="br-id mono">{block.block_id}</span>
      <span className={"tag tag-" + block.block_type}>{block.block_type}</span>
      <span className="br-spacer" />
      {hasAnno && <span className="br-anno tip" data-tip="Has glossary / entity annotations"><Ic.tag size={11} /></span>}
      <RuntimeOverlayBadges block={block} />
      {flagged && <span className="br-flag tip" data-tip={(block.quality_flags || []).join(", ")}><Ic.flag size={11} /></span>}
      {block.is_chapter_opening && <span className="br-open tip" data-tip="Chapter opening"><Ic.bolt size={11} /></span>}
    </button>
  );
}

function ChapterTree({ chapters, blocks, review, annoSet, selectedId, onSelect, revealMatches }) {
  const [expanded, setExpanded] = React.useState({});
  const chapterKey = chapters.map(chapter => chapter.chapter_id).join("|");
  React.useEffect(() => setExpanded({}), [chapterKey]);
  return (
    <div className="tree">
      {chapters.map(ch => {
        const chBlocks = blocks.filter(b => b.chapter_id === ch.chapter_id);
        if (!chBlocks.length) return null;
        const isOpen = revealMatches || !!expanded[ch.chapter_id];
        const done = chBlocks.filter(b => review.blocks?.[b.block_id]?.reviewed).length;
        return (
          <div key={ch.chapter_id} className="tree-ch">
            <button className="ch-head" onClick={() => setExpanded(current => ({ ...current, [ch.chapter_id]: !current[ch.chapter_id] }))} aria-expanded={isOpen}>
              <Ic.chevRight size={12} className="ch-caret" style={{ transform: isOpen ? "rotate(90deg)" : "none" }} />
              <span className="ch-title">{ch.title}</span>
              <span className="ch-prog mono">{done}/{chBlocks.length}</span>
            </button>
            {isOpen && chBlocks.map(b => (
              <SidebarBlockRow key={b.block_id} block={b}
                reviewed={!!review.blocks?.[b.block_id]?.reviewed}
                hasAnno={annoSet.has(b.block_id)}
                selected={selectedId === b.block_id}
                onSelect={onSelect} />
            ))}
          </div>
        );
      })}
    </div>
  );
}

function LeftSidebar({ docInfo, blocks, chapters, review, annoSet, selectedId, onSelect, filters, onToggleFilter, counts, total, errors }) {
  const [query, setQuery] = React.useState("");
  const [filterOpen, setFilterOpen] = React.useState(false);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const chapterTitles = React.useMemo(() => Object.fromEntries(
    chapters.map(chapter => [chapter.chapter_id, String(chapter.title || chapter.chapter_id).toLocaleLowerCase()])
  ), [chapters]);
  const treeBlocks = React.useMemo(() => {
    if (!normalizedQuery) return blocks;
    return blocks.filter(block => [
      block.block_id,
      block.block_type,
      block.source_text,
      block.clean_text,
      chapterTitles[block.chapter_id],
    ].some(value => String(value || "").toLocaleLowerCase().includes(normalizedQuery)));
  }, [blocks, chapterTitles, normalizedQuery]);
  const activeFilterCount = filters.size;

  return (
    <div className="col col-left">
      <SourceStatus docInfo={docInfo} blocks={blocks} chapters={chapters} errors={errors} total={total} />
      <div className="divider" />
      <div className="left-tools">
        <label className="block-search">
          <Ic.search size={12} />
          <input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search chapters or blocks" />
          {query && <button type="button" aria-label="Clear block search" onClick={() => setQuery("")}><Ic.x size={11} /></button>}
        </label>
        <button className={"btn sm icon-only tip" + (filterOpen || activeFilterCount ? " is-on" : "")} type="button"
          data-tip="Block filters" aria-label="Toggle block filters" aria-expanded={filterOpen} onClick={() => setFilterOpen(open => !open)}>
          <Ic.filter size={12} />
          {activeFilterCount > 0 && <span className="filter-active-count">{activeFilterCount}</span>}
        </button>
      </div>
      {filterOpen && <FilterChips active={filters} onToggle={onToggleFilter} counts={counts} />}
      <div className="divider" />
      <div className="sec-head">
        <Ic.list size={12} />Blocks
        <span className="sec-count mono">{treeBlocks.length}{treeBlocks.length !== total ? `/${total}` : ""}</span>
      </div>
      <div className="tree-scroll">
        <ChapterTree chapters={chapters} blocks={treeBlocks} review={review}
          annoSet={annoSet} selectedId={selectedId} onSelect={onSelect} revealMatches={!!normalizedQuery} />
        {treeBlocks.length === 0 && <div className="tree-empty">No blocks match the current search or filters.</div>}
      </div>
    </div>
  );
}

window.LeftSidebar = LeftSidebar;
