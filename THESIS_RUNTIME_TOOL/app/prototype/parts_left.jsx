/* ===== LEFT SIDEBAR: project, source status, filters, chapter -> block tree ===== */

function ProjectSelector({ docId, projects, onSelectProject, onOpenProjectSource }) {
  const [open, setOpen] = React.useState(false);
  function openProjectSource() {
    setOpen(false);
    if (onOpenProjectSource) onOpenProjectSource();
  }
  return (
    <div className="proj">
      <button className="proj-pick" onClick={() => setOpen(o => !o)}>
        <Ic.folder size={14} className="muted" />
        <span className="proj-name">{docId || "no_project"}</span>
        <Ic.chevUpDown size={12} className="faint" />
      </button>
      {open && (
        <>
          <div className="menu-scrim" onClick={() => setOpen(false)} />
          <div className="proj-menu">
            <div className="proj-menu-sec">Recent projects</div>
            {(projects || []).map(p => (
              <button key={p.doc_id} className={"proj-menu-item" + (p.doc_id === docId ? " cur" : "")}
                onClick={() => { setOpen(false); onSelectProject && onSelectProject(p.doc_id); }}>
                <Ic.doc size={13} className="faint" />
                <span className="pm-id mono">{p.doc_id}</span>
                <span className="pm-meta">{p.status}</span>
                {p.doc_id === docId && <Ic.check size={13} className="pm-cur-ic" />}
              </button>
            ))}
            <div className="divider" />
            <button className="proj-menu-item" onClick={openProjectSource}><Ic.folder size={13} className="faint" /><span>Open project folder...</span></button>
            <button className="proj-menu-item" onClick={openProjectSource}><Ic.plus size={13} className="faint" /><span>New project from source...</span></button>
          </div>
        </>
      )}
      <div className="proj-actions">
        <button className="btn sm tip" data-tip="Open source/project setup" onClick={openProjectSource}><Ic.folder size={12} />Open</button>
        <button className="btn sm tip" data-tip="State is saved through backend API"><Ic.save size={12} />Save</button>
        <span className="resume tip" data-tip="Session restored from working/review_state.json">
          <span className="resume-dot" />resumed
        </span>
      </div>
    </div>
  );
}

function SourceStatus({ docInfo, blocks, chapters, errors }) {
  const meta = docInfo?.metadata || {};
  const rows = [
    { k: "source", label: "Source", val: meta.source_format || "unknown", ok: !!meta.source_format, tip: meta.source_url || "Source URL not set" },
    { k: "extract", label: "Extracted", val: `${blocks.length} blocks · ${chapters.length} ch`, ok: blocks.length > 0, tip: `${meta.extraction_tool || "extractor pending"} · pipeline ${meta.pipeline_version || "pending"}` },
    { k: "valid", label: "Validation", val: errors.length ? `${errors.length} issue(s)` : "not run / clean", ok: errors.length === 0, tip: "Run Validate to see grouped issues" },
  ];
  return (
    <div className="srcstat">
      {rows.map(r => (
        <div key={r.k} className="srcstat-row tip" data-tip={r.tip}>
          <span className={"ss-dot " + (r.ok ? "ok" : "bad")} />
          <span className="ss-label">{r.label}</span>
          <span className={"ss-val mono" + (r.ok ? "" : " bad")}>{r.val}</span>
        </div>
      ))}
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
const STORAGE_LEFT_NAV_MODE = "ailab.left_nav_mode";

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

function SidebarModeTabs({ mode, onModeChange }) {
  return (
    <div className="left-mode-tabs" role="tablist" aria-label="Block navigation mode">
      <button
        className={"left-mode-btn" + (mode === "review" ? " on" : "")}
        onClick={() => onModeChange("review")}
        role="tab"
        aria-selected={mode === "review"}
      >
        Review
      </button>
      <button
        className={"left-mode-btn" + (mode === "preview" ? " on" : "")}
        onClick={() => onModeChange("preview")}
        role="tab"
        aria-selected={mode === "preview"}
      >
        Preview
      </button>
    </div>
  );
}

function RuntimeOverlayBadges({ block, compact = false }) {
  const counts = block.overlay_counts || {};
  const source = Number(counts.source || 0);
  const target = Number(counts.target || 0);
  const drift = Number(counts.drift || 0);
  if (!source && !target && !drift) return null;
  const cls = compact ? "previewrow-ic runtime" : "br-runtime";
  const tip = `Runtime overlay: source ${source}, target ${target}, drift ${drift}`;
  return (
    <>
      {source > 0 && <span className={cls + " tip"} data-tip={tip}><Ic.layers size={11} /></span>}
      {target > 0 && <span className={cls + " target tip"} data-tip={tip}><Ic.eye size={11} /></span>}
      {drift > 0 && <span className={cls + " drift tip"} data-tip={tip}><Ic.alert size={11} /></span>}
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

function SidebarPreviewRow({ block, reviewed, hasAnno, selected, onSelect }) {
  const flagged = (block.quality_flags || []).some(f => f !== "ok");
  const preview = String(block.clean_text || block.source_text || "").trim();
  return (
    <button
      className={"previewrow" + (selected ? " sel" : "")}
      onClick={() => onSelect(block.block_id)}
      title={block.block_id}
      aria-label={`Open block ${block.block_id}`}
    >
      <span className="previewrow-head">
        <span className="previewrow-id mono">{block.block_id}</span>
        <span className={"tag tag-" + block.block_type}>{block.block_type}</span>
        {reviewed && <span className="previewrow-ic ok tip" data-tip="Reviewed"><Ic.checkSmall size={11} /></span>}
        {hasAnno && <span className="previewrow-ic anno tip" data-tip="Has glossary / entity annotations"><Ic.tag size={11} /></span>}
        <RuntimeOverlayBadges block={block} compact />
        {flagged && <span className="previewrow-ic bad tip" data-tip={(block.quality_flags || []).join(", ")}><Ic.flag size={11} /></span>}
        {block.is_chapter_opening && <span className="previewrow-ic open tip" data-tip="Chapter opening"><Ic.bolt size={11} /></span>}
      </span>
      <span className="previewrow-text">{preview || "(empty block)"}</span>
    </button>
  );
}

function ChapterTree({ chapters, blocks, review, annoSet, selectedId, onSelect, mode }) {
  const [collapsed, setCollapsed] = React.useState({});
  return (
    <div className="tree">
      {chapters.map(ch => {
        const chBlocks = blocks.filter(b => b.chapter_id === ch.chapter_id);
        if (!chBlocks.length) return null;
        const isCol = collapsed[ch.chapter_id];
        const done = chBlocks.filter(b => review.blocks?.[b.block_id]?.reviewed).length;
        return (
          <div key={ch.chapter_id} className="tree-ch">
            <button className="ch-head" onClick={() => setCollapsed(c => ({ ...c, [ch.chapter_id]: !c[ch.chapter_id] }))}>
              <Ic.chevRight size={12} className="ch-caret" style={{ transform: isCol ? "none" : "rotate(90deg)" }} />
              <span className="ch-title">{ch.title}</span>
              <span className="ch-prog mono">{done}/{chBlocks.length}</span>
            </button>
            {!isCol && chBlocks.map(b => mode === "preview" ? (
              <SidebarPreviewRow key={b.block_id} block={b}
                reviewed={!!review.blocks?.[b.block_id]?.reviewed}
                hasAnno={annoSet.has(b.block_id)}
                selected={selectedId === b.block_id}
                onSelect={onSelect} />
            ) : (
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

function LeftSidebar({ docInfo, projects, blocks, chapters, review, annoSet, selectedId, onSelect, onSelectProject, filters, onToggleFilter, counts, total, errors, onOpenProjectSource }) {
  const [navMode, setNavModeState] = React.useState(() => {
    const saved = localStorage.getItem(STORAGE_LEFT_NAV_MODE);
    return saved === "preview" ? "preview" : "review";
  });
  function setNavMode(mode) {
    setNavModeState(mode);
    localStorage.setItem(STORAGE_LEFT_NAV_MODE, mode);
  }

  return (
    <div className="col col-left">
      <ProjectSelector docId={docInfo?.doc_id} projects={projects} onSelectProject={onSelectProject} onOpenProjectSource={onOpenProjectSource} />
      <div className="divider" />
      <SourceStatus docInfo={docInfo} blocks={blocks} chapters={chapters} errors={errors} />
      <div className="divider" />
      <div className="sec-head"><Ic.filter size={12} />Filters</div>
      <FilterChips active={filters} onToggle={onToggleFilter} counts={counts} />
      <div className="divider" />
      <div className="sec-head">
        <Ic.list size={12} />Blocks
        <span className="sec-count mono">{blocks.length}{blocks.length !== total ? `/${total}` : ""}</span>
      </div>
      <SidebarModeTabs mode={navMode} onModeChange={setNavMode} />
      <div className="tree-scroll">
        <ChapterTree chapters={chapters} blocks={blocks} review={review}
          annoSet={annoSet} selectedId={selectedId} onSelect={onSelect} mode={navMode} />
        {blocks.length === 0 && <div className="tree-empty">No blocks match the active filters.</div>}
      </div>
    </div>
  );
}

window.LeftSidebar = LeftSidebar;
