/* ===== CENTER: block editor + continuous chapter stream ===== */

/* compute char offsets of current selection within a container element */
function selectionOffsets(container) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  if (!container.contains(range.commonAncestorContainer)) return null;
  const pre = range.cloneRange();
  pre.selectNodeContents(container);
  pre.setEnd(range.startContainer, range.startOffset);
  const start = pre.toString().length;
  const text = range.toString();
  if (!text.trim()) return null;
  return { start, end: start + text.length, text };
}

/* split text into segments by spans (non-overlapping; later spans skipped if overlapping) */
function segmentize(text, spans) {
  const sorted = [...spans].sort((a, b) => a.start - b.start);
  const segs = [];
  let cur = 0;
  for (const s of sorted) {
    if (s.start < cur || s.start >= text.length) continue;
    if (s.start > cur) segs.push({ text: text.slice(cur, s.start) });
    segs.push({ text: text.slice(s.start, Math.min(s.end, text.length)), span: s });
    cur = Math.min(s.end, text.length);
  }
  if (cur < text.length) segs.push({ text: text.slice(cur) });
  return segs;
}

function ModeToggle({ mode, onModeChange }) {
  const items = [
    { id: "block", label: "Block" },
    { id: "chapter", label: "Chapter" },
    { id: "book", label: "Book" },
    { id: "preview", label: "Preview" },
  ];
  return (
    <div className="mode-toggle" role="group" aria-label="Center view mode">
      {items.map(item => (
        <button
          key={item.id}
          className={"mode-btn" + (mode === item.id ? " on" : "")}
          onClick={() => onModeChange(item.id)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

function EditorToolbar({
  block, reviewed, mode, onModeChange, streamLabel, streamCount, onNextUnreviewed,
  onChangeType, onToggleOpening, onToggleFlag, onMarkReviewed
}) {
  const [typeOpen, setTypeOpen] = React.useState(false);
  const [flagOpen, setFlagOpen] = React.useState(false);
  const readOnlyPreview = mode === "preview";
  const qualityFlags = block.quality_flags || [];
  const flags = qualityFlags.filter(f => f !== "ok");
  return (
    <div className="ed-toolbar">
      <div className="ed-tb-left">
        <ModeToggle mode={mode} onModeChange={onModeChange} />

        <span className="toolbar-chapter-meta">
          {readOnlyPreview ? "Translation Preview · read-only" : mode === "block" ? block.block_id : `${streamLabel} · ${streamCount || 0} blocks`}
        </span>

        {/* block_type dropdown */}
        {!readOnlyPreview && <div className="dd">
          <button className="dd-btn" onClick={() => { setTypeOpen(o => !o); setFlagOpen(false); }}>
            <span className={"tag tag-" + block.block_type}>{block.block_type}</span>
            <Ic.chevDown size={11} className="faint" />
          </button>
          {typeOpen && (<>
            <div className="menu-scrim" onClick={() => setTypeOpen(false)} />
            <div className="dd-menu">
              {DATA.BLOCK_TYPES.map(t => (
                <button key={t} className={"dd-item" + (t === block.block_type ? " cur" : "")}
                  onClick={() => { onChangeType(t); setTypeOpen(false); }}>
                  <span className={"tag tag-" + t}>{t}</span>
                  {t === block.block_type && <Ic.check size={12} className="dd-cur" />}
                </button>
              ))}
            </div>
          </>)}
        </div>}

        {/* chapter opening toggle */}
        {!readOnlyPreview && <button className={"tog" + (block.is_chapter_opening ? " on" : "")} onClick={onToggleOpening}>
          <span className="tog-sw"><span className="tog-knob" /></span>
          <Ic.bolt size={12} />chapter opening
        </button>}

        {/* quality flags */}
        {!readOnlyPreview && <div className="dd">
          <button className="dd-btn flags-btn" onClick={() => { setFlagOpen(o => !o); setTypeOpen(false); }}>
            <Ic.flag size={12} className={flags.length ? "flag-on" : "faint"} />
            {flags.length === 0
              ? <span className="faint" style={{ fontSize: 12 }}>no flags</span>
              : flags.map(f => <span key={f} className="flag-chip">{f}</span>)}
            <Ic.chevDown size={11} className="faint" />
          </button>
          {flagOpen && (<>
            <div className="menu-scrim" onClick={() => setFlagOpen(false)} />
            <div className="dd-menu wide">
              <div className="dd-menu-head">quality_flags</div>
              {DATA.QUALITY_FLAGS.map(f => {
                const on = f === "ok" ? flags.length === 0 : qualityFlags.includes(f);
                return (
                  <button key={f} className={"dd-check" + (on ? " on" : "")} onClick={() => onToggleFlag(f)}>
                    <span className="dd-box">{on && <Ic.checkSmall size={10} />}</span>
                    <span className="mono">{f}</span>
                  </button>
                );
              })}
            </div>
          </>)}
        </div>}

        {readOnlyPreview && (
          <span className="mini-badge warn"><Ic.eye size={11} />preview only · not gold</span>
        )}
      </div>

      <div className="ed-tb-right">
        {readOnlyPreview ? (
          <span className="preview-toolbar-note"><Ic.lock size={12} />Preview export only. No save, review, or promote actions in this view.</span>
        ) : mode !== "block" && (
          <button className="btn sm" onClick={onNextUnreviewed}>
            <Ic.arrowRight size={13} />Next unreviewed
          </button>
        )}
        {!readOnlyPreview && <button className={"btn sm reviewed-btn" + (reviewed ? " is-on" : "")} onClick={onMarkReviewed}>
          <Ic.checkCircle size={13} />{reviewed ? "Reviewed" : "Mark reviewed"}
        </button>}
      </div>
    </div>
  );
}

function MetaBar({ block, docInfo }) {
  const schemaVersion = docInfo?.schema_version ? `schema ${docInfo.schema_version}` : "";
  const pipelineVersion = docInfo?.metadata?.pipeline_version ? `pipeline ${docInfo.metadata.pipeline_version}` : "";
  const items = [
    ["block_id", block.block_id],
    ["chapter_id", block.chapter_id],
    ["order_index", String(block.order_index)],
    ["provenance", [schemaVersion, pipelineVersion].filter(Boolean).join(" / ") || "extracted"],
  ];
  return (
    <div className="metabar">
      {items.map(([k, v]) => (
        <span key={k} className="lockfield tip" data-tip={"Read-only · set by extraction pipeline"}>
          <span className="lf-k"><Ic.lock size={10} />{k}</span>
          <span className="lf-v">{v}</span>
        </span>
      ))}
    </div>
  );
}

function SelectionPopover({ rect, onGlossary, onEntity }) {
  if (!rect) return null;
  return (
    <div
      className="sel-pop"
      style={{ top: rect.top, left: rect.left }}
      onMouseDown={e => {
        e.preventDefault();
        e.stopPropagation();
      }}
      onClick={e => e.stopPropagation()}
    >
      <button className="sel-pop-btn" onClick={onGlossary}><Ic.tag size={12} />Add glossary term</button>
      <div className="sel-pop-div" />
      <button className="sel-pop-btn" onClick={onEntity}><Ic.users size={12} />Add entity mention</button>
    </div>
  );
}

function compactList(value, empty = "none") {
  if (Array.isArray(value)) return value.length ? value.join(", ") : empty;
  if (typeof value === "object" && value !== null) {
    const rows = Object.entries(value).filter(([, v]) => v !== undefined && v !== null && v !== "");
    return rows.length ? rows.map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join("; ") : empty;
  }
  if (value === undefined || value === null || value === "") return empty;
  return String(value);
}

function limitItems(items, limit = 4) {
  const rows = (items || []).filter(Boolean);
  if (rows.length <= limit) return rows.join(", ");
  return rows.slice(0, limit).join(", ") + ` +${rows.length - limit}`;
}

function confidenceText(value) {
  if (value === undefined || value === null || value === "") return "n/a";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : String(value);
}

function hoverCardPosition(rect) {
  if (!rect) return { top: 0, left: 0, above: false };
  const width = 328;
  const left = Math.min(Math.max(12, rect.left), Math.max(12, window.innerWidth - width - 12));
  const above = rect.bottom > window.innerHeight - 250;
  return {
    left,
    top: above ? rect.top - 8 : rect.bottom + 8,
    above,
  };
}

function HighlightHoverCard({ hover, linkIndex }) {
  if (!hover?.span || !hover?.rect) return null;
  const { span, block } = hover;
  const pos = hoverCardPosition(hover.rect);

  if (span.kind === "entity") {
    const data = linkIndex?.entities?.[span.id];
    const entity = data?.item;
    if (!entity) return null;
    const currentMentions = data.mentionsByBlock?.[block.block_id] || [];
    const chapterLabels = (data.chapters || []).map(ch => ch.title || ch.chapter_id);
    const summaryLabels = (data.summaryChapters || []).map(ch => ch.title || ch.chapter_id);
    return (
      <div className={"hl-card" + (pos.above ? " above" : "")} style={{ top: pos.top, left: pos.left }}>
        <div className="hl-card-head">
          <span className="hl-card-kind entity"><Ic.users size={12} />Entity</span>
          <span className="hl-card-conf mono">conf {confidenceText(entity.confidence)}</span>
        </div>
        <div className="hl-card-title">
          <span>{entity.canonical_source || span.id}</span>
          <span className="hl-card-arrow">-</span>
          <span>{entity.canonical_target || "target needed"}</span>
        </div>
        <div className="hl-card-grid">
          <span>type</span><b>{compactList(entity.entity_type || entity.type)}</b>
          <span>gender</span><b>{compactList(entity.gender)}</b>
          <span>aliases</span><b>{compactList(entity.aliases_target)}</b>
          <span>pronoun</span><b>{compactList(entity.pronoun_policy)}</b>
          <span>annotator</span><b>{compactList(entity.annotated_by)}</b>
        </div>
        {span.stale && <div className="hl-card-warning"><Ic.alert size={12} />This span needs re-tag.</div>}
        <div className="hl-card-section">
          <div className="hl-card-section-title">Links</div>
          <div className="hl-card-links">
            <span>{currentMentions.length} mention{currentMentions.length === 1 ? "" : "s"} in this block</span>
            <span>{data.mentions.length} total mention{data.mentions.length === 1 ? "" : "s"}</span>
            <span>{data.blockIds.length} block{data.blockIds.length === 1 ? "" : "s"}</span>
            <span>{data.chapters.length} chapter{data.chapters.length === 1 ? "" : "s"}</span>
            <span>{data.speakerBlocks.length} speaker block{data.speakerBlocks.length === 1 ? "" : "s"}</span>
            <span>{data.addresseeBlocks.length} addressee block{data.addresseeBlocks.length === 1 ? "" : "s"}</span>
          </div>
          {chapterLabels.length > 0 && <div className="hl-card-small"><b>chapters</b> {limitItems(chapterLabels)}</div>}
          {summaryLabels.length > 0 && <div className="hl-card-small"><b>characters_present</b> {limitItems(summaryLabels)}</div>}
        </div>
      </div>
    );
  }

  if (span.kind === "glossary") {
    const data = linkIndex?.glossary?.[span.id];
    const term = data?.item;
    if (!term) return null;
    const currentOccurrences = data.occurrencesByBlock?.[block.block_id] || [];
    const chapterLabels = (data.chapters || []).map(ch => ch.title || ch.chapter_id);
    return (
      <div className={"hl-card" + (pos.above ? " above" : "")} style={{ top: pos.top, left: pos.left }}>
        <div className="hl-card-head">
          <span className="hl-card-kind glossary"><Ic.tag size={12} />Glossary</span>
          <span className="hl-card-status">{term.status || "candidate"}</span>
        </div>
        <div className="hl-card-title">
          <span>{term.source_term || span.id}</span>
          <span className="hl-card-arrow">-</span>
          <span>{term.expected_target || "target needed"}</span>
        </div>
        <div className="hl-card-grid">
          <span>allowed</span><b>{compactList(term.allowed_variants)}</b>
          <span>forbidden</span><b>{compactList(term.forbidden_variants)}</b>
          <span>domain</span><b>{compactList(term.domain)}</b>
          <span>scope</span><b>{compactList(term.chapter_scope)}</b>
          <span>annotator</span><b>{compactList(term.annotated_by)}</b>
          <span>confidence</span><b>{confidenceText(term.confidence)}</b>
        </div>
        {span.stale && <div className="hl-card-warning"><Ic.alert size={12} />This span needs re-tag.</div>}
        <div className="hl-card-section">
          <div className="hl-card-section-title">Links</div>
          <div className="hl-card-links">
            <span>{currentOccurrences.length} occurrence{currentOccurrences.length === 1 ? "" : "s"} in this block</span>
            <span>{data.occurrences.length} total occurrence{data.occurrences.length === 1 ? "" : "s"}</span>
            <span>{data.blockIds.length} block{data.blockIds.length === 1 ? "" : "s"}</span>
            <span>{data.chapters.length} chapter{data.chapters.length === 1 ? "" : "s"}</span>
          </div>
          {chapterLabels.length > 0 && <div className="hl-card-small"><b>chapters</b> {limitItems(chapterLabels)}</div>}
        </div>
      </div>
    );
  }

  return null;
}

function CleanTextSurface({
  block, spans = [], editing, draft, onDraft, onMouseUp, cleanRef, taRef, onAddGlossary, onAddEntity, selection,
  onHoverSpan, onLeaveSpan
}) {
  if (editing) {
    return (
      <textarea className="clean-edit mono" ref={taRef} value={draft}
        onChange={e => onDraft(e.target.value)} spellCheck={false} />
    );
  }
  return (
    <div className="clean-text" ref={cleanRef} onMouseUp={onMouseUp}>
      {segmentize(block.clean_text || "", spans).map((seg, i) =>
        seg.span
          ? <mark key={i} className={"hl hl-" + seg.span.kind + (seg.span.stale ? " hl-stale" : "")}
              aria-label={seg.span.label}
              onMouseEnter={e => onHoverSpan && onHoverSpan(seg.span, block, e.currentTarget.getBoundingClientRect())}
              onMouseLeave={() => onLeaveSpan && onLeaveSpan()}>{seg.text}</mark>
          : <span key={i}>{seg.text}</span>
      )}
      <SelectionPopover rect={selection?.rect}
        onGlossary={onAddGlossary}
        onEntity={onAddEntity} />
    </div>
  );
}

function ChapterBlockRow({
  block, spans, reviewed, active, onSelectBlock, onCommitClean,
  onMarkReviewed, onAddGlossary, onAddEntity, onHoverSpan, onLeaveSpan
}) {
  const cleanRef = React.useRef(null);
  const taRef = React.useRef(null);
  const [sel, setSel] = React.useState(null);
  const [editing, setEditing] = React.useState(false);
  const [sourceOpen, setSourceOpen] = React.useState(false);
  const [draft, setDraft] = React.useState(block.clean_text || "");

  React.useEffect(() => { setDraft(block.clean_text || ""); }, [block.block_id, block.clean_text]);
  React.useEffect(() => { if (editing && taRef.current) taRef.current.focus(); }, [editing]);

  function clearSelection() {
    setSel(null);
    const current = window.getSelection();
    if (current) current.removeAllRanges();
  }

  function handleMouseUp() {
    if (editing) return;
    const c = cleanRef.current;
    if (!c) return;
    const off = selectionOffsets(c);
    if (!off) { setSel(null); return; }
    onSelectBlock(block.block_id);
    if (onLeaveSpan) onLeaveSpan();
    const r = window.getSelection().getRangeAt(0).getBoundingClientRect();
    const host = c.getBoundingClientRect();
    const popoverWidth = 258;
    const top = r.bottom - host.top + 8;
    const left = Math.min(
      Math.max(8, r.left - host.left + r.width / 2 - popoverWidth / 2),
      Math.max(8, c.clientWidth - popoverWidth - 8)
    );
    setSel({ ...off, rect: { top, left } });
  }

  const staleCount = (spans || []).filter(s => s.stale).length;
  const flags = (block.quality_flags || []).filter(f => f !== "ok");

  return (
    <article
      className={"chapter-block-row" + (active ? " active" : "") + (reviewed ? " reviewed" : "")}
      data-block-id={block.block_id}
      onMouseDown={() => onSelectBlock(block.block_id)}
    >
      <div className="cbr-head">
        <div className="cbr-meta">
          <span className="mono cbr-id">{block.block_id}</span>
          <span className={"tag tag-" + block.block_type}>{block.block_type}</span>
          {reviewed && <span className="mini-badge good"><Ic.check size={10} />reviewed</span>}
          {flags.map(f => <span key={f} className="mini-badge bad"><Ic.flag size={10} />{f}</span>)}
          {staleCount > 0 && (
            <span className="stale-warn tip" data-tip="A glossary/entity span no longer matches this block text. Re-tag this row.">
              <Ic.alert size={11} />{staleCount} span{staleCount > 1 ? "s" : ""} need re-tag
            </span>
          )}
        </div>
        <div className="cbr-actions">
          <button className="btn sm ghost" onClick={e => { e.stopPropagation(); setSourceOpen(v => !v); }}>
            <Ic.eye size={12} />{sourceOpen ? "Hide source" : "Source"}
          </button>
          {!editing ? (
            <button className="btn sm" onClick={e => { e.stopPropagation(); clearSelection(); setEditing(true); }}>
              <Ic.pencil size={11} />Edit
            </button>
          ) : (
            <>
              <button className="btn sm" onClick={e => { e.stopPropagation(); setDraft(block.clean_text || ""); setEditing(false); }}>Cancel</button>
              <button className="btn sm primary" onClick={e => { e.stopPropagation(); setEditing(false); onCommitClean(block.block_id, draft); }}>
                <Ic.check size={11} />Save
              </button>
            </>
          )}
          <button className={"btn sm reviewed-btn" + (reviewed ? " is-on" : "")}
            onClick={e => { e.stopPropagation(); onMarkReviewed(block.block_id); }}>
            <Ic.checkCircle size={13} />{reviewed ? "Reviewed" : "Review"}
          </button>
        </div>
      </div>

      {sourceOpen && (
        <div className="chapter-source">
          <div className="field-head compact">
            <span className="fh-title"><Ic.lock size={11} />Source (EN)</span>
            <span className="fh-meta">read-only · source_text</span>
          </div>
          <div className="src-text compact">{block.source_text || ""}</div>
        </div>
      )}

      <CleanTextSurface
        block={block}
        spans={spans}
        editing={editing}
        draft={draft}
        onDraft={setDraft}
        cleanRef={cleanRef}
        taRef={taRef}
        selection={sel}
        onMouseUp={handleMouseUp}
        onHoverSpan={onHoverSpan}
        onLeaveSpan={onLeaveSpan}
        onAddGlossary={() => { onAddGlossary(block.block_id, sel); clearSelection(); }}
        onAddEntity={() => { onAddEntity(block.block_id, sel); clearSelection(); }}
      />
    </article>
  );
}

function SingleBlockView({
  block, docInfo, reviewed, spans, editing, onEdit, onCommitClean, onCancelEdit,
  onAddGlossary, onAddEntity, onHoverSpan, onLeaveSpan
}) {
  const cleanRef = React.useRef(null);
  const taRef = React.useRef(null);
  const [sel, setSel] = React.useState(null);
  const [draft, setDraft] = React.useState(block.clean_text || "");

  React.useEffect(() => { setDraft(block.clean_text || ""); }, [block.block_id, block.clean_text]);
  React.useEffect(() => { if (editing && taRef.current) { taRef.current.focus(); } }, [editing]);

  function clearSelection() {
    setSel(null);
    const current = window.getSelection();
    if (current) current.removeAllRanges();
  }

  function handleMouseUp() {
    if (editing) return;
    const c = cleanRef.current;
    if (!c) return;
    const off = selectionOffsets(c);
    if (!off) { setSel(null); return; }
    if (onLeaveSpan) onLeaveSpan();
    const r = window.getSelection().getRangeAt(0).getBoundingClientRect();
    const host = c.getBoundingClientRect();
    const popoverWidth = 258;
    const top = r.bottom - host.top + 8;
    const left = Math.min(
      Math.max(8, r.left - host.left + r.width / 2 - popoverWidth / 2),
      Math.max(8, c.clientWidth - popoverWidth - 8)
    );
    setSel({ ...off, rect: { top, left } });
  }

  const staleCount = (spans || []).filter(s => s.stale).length;

  return (
    <>
      <MetaBar block={block} docInfo={docInfo} />
      <div className="ed-scroll" onMouseDown={() => sel && setSel(null)}>
        <div className="ed-inner">
          <div className="field-block">
            <div className="field-head">
              <span className="fh-title"><Ic.lock size={11} />Source (EN)</span>
              <span className="fh-meta">read-only · source_text · extracted</span>
            </div>
            <div className="src-text">{block.source_text || ""}</div>
          </div>

          <div className="field-block">
            <div className="field-head">
              <span className="fh-title editable-title"><Ic.pencil size={11} />Clean text</span>
              <span className="fh-actions">
                {staleCount > 0 && !editing && (
                  <span className="stale-warn tip" data-tip="A glossary/entity span no longer matches the edited text. Re-tag from the right panel.">
                    <Ic.alert size={11} />{staleCount} span{staleCount > 1 ? "s" : ""} need re-tag
                  </span>
                )}
                {!editing
                  ? <button className="btn sm" onClick={() => { setSel(null); onEdit(); }}><Ic.pencil size={11} />Edit</button>
                  : <>
                      <button className="btn sm" onClick={() => { setDraft(block.clean_text || ""); onCancelEdit(); }}>Cancel</button>
                      <button className="btn sm primary" onClick={() => onCommitClean(block.block_id, draft)}><Ic.check size={11} />Save text</button>
                    </>}
              </span>
            </div>

            <CleanTextSurface
              block={block}
              spans={spans}
              editing={editing}
              draft={draft}
              onDraft={setDraft}
              cleanRef={cleanRef}
              taRef={taRef}
              selection={sel}
              onMouseUp={handleMouseUp}
              onHoverSpan={onHoverSpan}
              onLeaveSpan={onLeaveSpan}
              onAddGlossary={() => { onAddGlossary(block.block_id, sel); clearSelection(); }}
              onAddEntity={() => { onAddEntity(block.block_id, sel); clearSelection(); }}
            />

            {!editing && (
              <div className="clean-hint">
                <Ic.tag size={11} />Select text to add a glossary occurrence or entity mention.
                <span className="hint-keys"><span className="kbd">⌘</span><span className="kbd">↵</span> mark reviewed</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

function ChapterStream({
  blocks = [], chapters = [], selectedId, review, getSpansForBlock, onSelectBlock, onCommitClean,
  onMarkReviewed, onAddGlossary, onAddEntity, onHoverSpan, onLeaveSpan
}) {
  const rows = blocks || [];
  const chapterLookup = React.useMemo(() => {
    const map = {};
    (chapters || []).forEach(ch => { map[ch.chapter_id] = ch; });
    return map;
  }, [chapters]);
  const chapterCounts = React.useMemo(() => {
    const map = {};
    rows.forEach(row => { map[row.chapter_id] = (map[row.chapter_id] || 0) + 1; });
    return map;
  }, [rows]);
  const scrollRef = React.useRef(null);
  React.useEffect(() => {
    if (!selectedId || !scrollRef.current) return;
    const row = Array.from(scrollRef.current.querySelectorAll("[data-block-id]"))
      .find(el => el.dataset.blockId === selectedId);
    if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [selectedId, rows.length]);

  return (
    <div className="ed-scroll" ref={scrollRef}>
      <div className="chapter-stream">
        {rows.map((row, index) => {
          const prev = rows[index - 1];
          const startsChapter = index === 0 || prev?.chapter_id !== row.chapter_id;
          const chapter = chapterLookup[row.chapter_id] || {};
          const title = chapter.title || chapter.chapter_title || row.chapter_id;
          const count = chapterCounts[row.chapter_id] || 0;
          return (
            <React.Fragment key={row.block_id}>
              {startsChapter && (
                <div className="chapter-divider" data-chapter-id={row.chapter_id}>
                  <span className="chapter-divider-rule" />
                  <span className="chapter-divider-title">{title}</span>
                  <span className="chapter-divider-meta mono">{row.chapter_id} · {count} blocks</span>
                </div>
              )}
              <ChapterBlockRow
                block={row}
                spans={getSpansForBlock(row)}
                reviewed={!!review?.blocks?.[row.block_id]?.reviewed}
                active={row.block_id === selectedId}
                onSelectBlock={onSelectBlock}
                onCommitClean={onCommitClean}
                onMarkReviewed={onMarkReviewed}
                onAddGlossary={onAddGlossary}
                onAddEntity={onAddEntity}
                onHoverSpan={onHoverSpan}
                onLeaveSpan={onLeaveSpan}
              />
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
}

function previewRunLabel(run) {
  if (!run) return "preview run";
  const model = run.model ? ` · ${run.model}` : "";
  const warnings = run.warning_count ? ` · ${run.warning_count} warn` : "";
  return `${run.run_id || "run"} · ${run.block_count || 0} blocks${model}${warnings}`;
}

function contextChip(id, linkIndex) {
  const value = String(id || "");
  const entity = linkIndex?.entities?.[value]?.item;
  if (entity) return { kind: "entity", label: entity.canonical_source || value, title: value };
  const term = linkIndex?.glossary?.[value]?.item;
  if (term) return { kind: "term", label: term.source_term || value, title: value };
  const chapter = linkIndex?.chapterById?.[value];
  if (chapter) return { kind: "chapter", label: chapter.title || chapter.chapter_title || value, title: value };
  if (value.toLowerCase().startsWith("rel")) return { kind: "relation", label: value, title: value };
  return { kind: "unknown", label: value, title: value };
}

function addressSummary(address) {
  if (!address) return null;
  const selfTerm = address.self_term || address.self || "";
  const addressTerm = address.address_term || address.address || "";
  const pair = address.pair || "";
  const relationId = address.relation_id || "";
  const terms = [selfTerm, addressTerm].filter(Boolean).join(" / ");
  return {
    text: terms || pair || relationId || "address applied",
    sub: [pair, relationId].filter(Boolean).join(" · "),
  };
}

function previewMentionMeta(mention) {
  if (!mention || typeof mention !== "object") return null;
  if (mention.entity_id) return { kind: "entity", id: String(mention.entity_id) };
  if (mention.term_id) return { kind: "term", id: String(mention.term_id) };
  return null;
}

function rangesOverlap(aStart, aEnd, bStart, bEnd) {
  return aStart < bEnd && bStart < aEnd;
}

function findPreviewRanges(text, mentions = [], surfaceKey) {
  const value = String(text || "");
  if (!value || !Array.isArray(mentions) || !mentions.length) return [];
  const occupied = [];
  const candidates = mentions
    .map((mention, index) => {
      const meta = previewMentionMeta(mention);
      const surface = String(mention?.[surfaceKey] || "");
      if (!meta || !surface) return null;
      return {
        mention,
        index,
        kind: meta.kind,
        id: meta.id,
        surface,
        sourceSurface: String(mention.source_surface || ""),
        targetSurface: String(mention.target_surface || ""),
      };
    })
    .filter(Boolean)
    .sort((a, b) => b.surface.length - a.surface.length || a.index - b.index);

  const ranges = [];
  candidates.forEach(item => {
    let from = 0;
    while (from <= value.length) {
      const start = value.indexOf(item.surface, from);
      if (start === -1) break;
      const end = start + item.surface.length;
      if (!occupied.some(range => rangesOverlap(start, end, range.start, range.end))) {
        const kindLabel = item.kind === "entity" ? "entity" : "term";
        occupied.push({ start, end });
        ranges.push({
          start,
          end,
          ...item,
          title: `${kindLabel} ${item.id}: ${item.sourceSurface || "source?"} -> ${item.targetSurface || "target?"}`,
        });
        break;
      }
      from = start + Math.max(1, item.surface.length);
    }
  });

  return ranges.sort((a, b) => a.start - b.start || b.end - a.end);
}

function renderPreviewText(text, mentions, surfaceKey) {
  const value = String(text || "");
  const ranges = findPreviewRanges(value, mentions, surfaceKey);
  if (!ranges.length) return value;
  const pieces = [];
  let cursor = 0;
  ranges.forEach((range, index) => {
    if (range.start > cursor) pieces.push(<span key={`t-${index}`}>{value.slice(cursor, range.start)}</span>);
    pieces.push(
      <mark
        key={`h-${index}`}
        className={"tp-hl " + range.kind}
        title={range.title}
        aria-label={range.title}
      >
        {value.slice(range.start, range.end)}
      </mark>
    );
    cursor = range.end;
  });
  if (cursor < value.length) pieces.push(<span key="tail">{value.slice(cursor)}</span>);
  return pieces;
}

function prettyJson(value) {
  return JSON.stringify(value || {}, null, 2);
}

async function copyPlainText(text) {
  const value = String(text || "");
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const node = document.createElement("textarea");
  node.value = value;
  node.setAttribute("readonly", "");
  node.style.position = "fixed";
  node.style.opacity = "0";
  document.body.appendChild(node);
  node.select();
  document.execCommand("copy");
  document.body.removeChild(node);
}

function downloadJsonFile(filename, data) {
  const blob = new Blob([prettyJson(data)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function translationPreviewPaths(inputBundle, docId, chapterId) {
  const paths = inputBundle?.paths || {};
  const safeChapter = (chapterId || "").replace(/[^A-Za-z0-9_-]/g, "_") || "chapter";
  const fallbackRoot = docId ? `ailab_projects/${docId}` : "";
  return {
    project_root: paths.project_root || fallbackRoot,
    input: paths.input || (fallbackRoot ? `${fallbackRoot}/working/translation_preview/input/${safeChapter}_input.json` : ""),
    preview_output_suggested: paths.preview_output_suggested || (fallbackRoot ? `${fallbackRoot}/working/translation_preview/agent_outputs/${safeChapter}_preview.json` : ""),
    runs_dir: paths.runs_dir || (fallbackRoot ? `${fallbackRoot}/working/translation_preview/runs` : ""),
  };
}

function translationPreviewPrompt(docId, chapterId, inputBundle) {
  const paths = translationPreviewPaths(inputBundle, docId, chapterId);
  return [
    "You are the AI-LAB Translation Preview Agent.",
    "",
    "Read first:",
    "1. skills/dataset-translation-preview/SKILL.md",
    "2. skills/dataset-translation-preview/references/TRANSLATION_PREVIEW_CONTRACT.md",
    "",
    "Current source:",
    `- doc_id: ${docId || "<doc_id>"}`,
    `- chapter_id: ${chapterId || "<chapter_id>"}`,
    `- project folder: ${paths.project_root || ""}`,
    `- translation_input: ${paths.input || ""}`,
    `- preview_output_suggested: ${paths.preview_output_suggested || ""}`,
    "",
    "Requirements:",
    "- Read the input bundle JSON from translation_input above as UTF-8 (it contains Vietnamese; in Python use open(path, encoding=\"utf-8\")).",
    "- Translate only blocks included in the bundle.",
    "- Obey canonical_target / expected_target / forbidden_variants.",
    "- Apply address_policy when discourse speaker/addressee and relation match.",
    "- Do not create span/start/end/offset.",
    "- Do not write canonical/gold/manual_reference_subset.",
    "- Output exactly one JSON object following the contract. Do not wrap it in markdown.",
    "",
    "Output:",
    "- Preferred: return exactly one JSON object to paste back into the web tool (the paste path is always UTF-8 safe).",
    "- If you write the preview to a file (preview_output_suggested above), you MUST write it as UTF-8 WITHOUT BOM. Do NOT use the OS default encoding / ANSI / cp1252 — on Windows that replaces every Vietnamese diacritic with \"?\" and destroys the text. In Python: open(path, \"w\", encoding=\"utf-8\"). Never use Out-File/Set-Content without -Encoding utf8.",
    "- After writing a file, verify it still contains real Vietnamese letters (e.g. \"ông\", \"ngài\"), not \"?\".",
    "- The JSON is imported back with Load preview file or paste."
  ].join("\n");
}

async function decodeUploadedFile(file) {
  // Robust decode. PowerShell ">" / Out-File default to UTF-16LE with a BOM, and
  // some editors add a UTF-8 BOM. file.text() assumes UTF-8 and mangles those, so
  // sniff the byte-order mark and decode accordingly. This rescues Vietnamese from
  // UTF-16/BOM saves (their bytes are intact, only the decoding was wrong).
  const buf = new Uint8Array(await file.arrayBuffer());
  let encoding = "utf-8";
  let start = 0;
  if (buf.length >= 2 && buf[0] === 0xff && buf[1] === 0xfe) { encoding = "utf-16le"; start = 2; }
  else if (buf.length >= 2 && buf[0] === 0xfe && buf[1] === 0xff) { encoding = "utf-16be"; start = 2; }
  else if (buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) { encoding = "utf-8"; start = 3; }
  return new TextDecoder(encoding).decode(buf.subarray(start));
}

function countMojibakeMarks(text) {
  // A "?" glued to a letter (e.g. "?ng", "tr??ng") is almost never a real question
  // mark — it is the cp1252/ANSI-save signature where Vietnamese diacritics were
  // replaced by "?". A real "?" is followed by space/quote/end, not by a letter.
  return (String(text || "").match(/\?(?=[A-Za-z])/g) || []).length;
}

function TranslationPreviewView({
  docInfo, chapters = [], allBlocks = [], chapter, selectedId, onSelectBlock, linkIndex, onPreviewRunChange
}) {
  const [runs, setRuns] = React.useState([]);
  const [selectedChapterId, setSelectedChapterId] = React.useState(chapter?.chapter_id || chapters[0]?.chapter_id || "");
  const [selectedRunId, setSelectedRunId] = React.useState("");
  const [loadedRun, setLoadedRun] = React.useState(null);
  const [loadingRuns, setLoadingRuns] = React.useState(false);
  const [loadingRun, setLoadingRun] = React.useState(false);
  const [error, setError] = React.useState("");
  const [inputBundle, setInputBundle] = React.useState(null);
  const [inputText, setInputText] = React.useState("");
  const [inputLoading, setInputLoading] = React.useState(false);
  const [importText, setImportText] = React.useState("");
  const [importing, setImporting] = React.useState(false);
  const [loopNotice, setLoopNotice] = React.useState("");
  const [importWarnings, setImportWarnings] = React.useState([]);
  const importFileRef = React.useRef(null);
  const docId = docInfo?.doc_id || "";

  React.useEffect(() => {
    if (typeof onPreviewRunChange === "function") {
      onPreviewRunChange(loadedRun ? { run: loadedRun, chapter_id: selectedChapterId } : null);
    }
  }, [loadedRun, selectedChapterId, onPreviewRunChange]);

  React.useEffect(() => {
    const next = chapter?.chapter_id || chapters[0]?.chapter_id || "";
    setSelectedChapterId(next);
  }, [chapter?.chapter_id, chapters.length]);

  React.useEffect(() => {
    let alive = true;
    if (!docId) return;
    setLoadingRuns(true);
    setError("");
    setLoadedRun(null);
    window.AILAB_API.listTranslationPreviewRuns(docId)
      .then(data => {
        if (!alive) return;
        setRuns(data.runs || []);
      })
      .catch(err => {
        if (!alive) return;
        setRuns([]);
        setError(err?.message || "Cannot load translation preview runs.");
      })
      .finally(() => alive && setLoadingRuns(false));
    return () => { alive = false; };
  }, [docId]);

  const chapterRuns = React.useMemo(
    () => (runs || []).filter(run => run.chapter_id === selectedChapterId),
    [runs, selectedChapterId]
  );

  React.useEffect(() => {
    if (!chapterRuns.length) {
      setSelectedRunId("");
      setLoadedRun(null);
      return;
    }
    if (!chapterRuns.some(run => run.run_id === selectedRunId)) {
      setSelectedRunId(chapterRuns[chapterRuns.length - 1].run_id);
    }
  }, [chapterRuns, selectedRunId]);

  React.useEffect(() => {
    let alive = true;
    if (!docId || !selectedRunId) return;
    setLoadingRun(true);
    setError("");
    window.AILAB_API.loadTranslationPreviewRun(docId, selectedRunId)
      .then(data => {
        if (!alive) return;
        setLoadedRun(data.run || null);
      })
      .catch(err => {
        if (!alive) return;
        setLoadedRun(null);
        setError(err?.message || "Cannot load translation preview run.");
      })
      .finally(() => alive && setLoadingRun(false));
    return () => { alive = false; };
  }, [docId, selectedRunId]);

  const chapterRows = React.useMemo(
    () => (allBlocks || []).filter(block => block.chapter_id === selectedChapterId),
    [allBlocks, selectedChapterId]
  );

  const runByBlock = React.useMemo(() => {
    const map = {};
    (loadedRun?.blocks || []).forEach(row => { if (row.block_id) map[row.block_id] = row; });
    return map;
  }, [loadedRun]);

  const currentChapter = chapters.find(ch => ch.chapter_id === selectedChapterId) || {};
  const warnings = loadedRun?.warnings || [];
  const inputPaths = React.useMemo(
    () => translationPreviewPaths(inputBundle, docId, selectedChapterId),
    [inputBundle, docId, selectedChapterId]
  );

  React.useEffect(() => {
    let alive = true;
    if (!docId || !selectedChapterId) {
      setInputBundle(null);
      setInputText("");
      return () => { alive = false; };
    }
    setInputLoading(true);
    window.AILAB_API.getSavedTranslationPreviewInput(docId, selectedChapterId)
      .then(data => {
        if (!alive) return;
        setInputBundle(data);
        setInputText(prettyJson(data));
      })
      .catch(err => {
        if (!alive) return;
        if (err?.status !== 404) {
          setError(err?.message || "Cannot load saved translation preview input.");
        }
        setInputBundle(null);
        setInputText("");
      })
      .finally(() => alive && setInputLoading(false));
    return () => { alive = false; };
  }, [docId, selectedChapterId]);

  function changeChapter(chapterId) {
    setSelectedChapterId(chapterId);
    setInputBundle(null);
    setInputText("");
    setLoopNotice("");
    setImportWarnings([]);
    const first = (allBlocks || []).find(block => block.chapter_id === chapterId);
    if (first) onSelectBlock(first.block_id);
  }

  async function refreshRuns(selectRunId) {
    const data = await window.AILAB_API.listTranslationPreviewRuns(docId);
    setRuns(data.runs || []);
    if (selectRunId) setSelectedRunId(selectRunId);
  }

  async function buildInputBundle() {
    if (!docId || !selectedChapterId) return;
    setInputLoading(true);
    setError("");
    setLoopNotice("");
    setImportWarnings([]);
    try {
      const data = await window.AILAB_API.getTranslationPreviewInput(docId, selectedChapterId);
      setInputBundle(data);
      setInputText(prettyJson(data));
      setLoopNotice(`Input bundle built and saved: ${data.paths?.input || "working/translation_preview/input"}.`);
    } catch (err) {
      setInputBundle(null);
      setInputText("");
      setError(err?.message || "Cannot build translation preview input.");
    } finally {
      setInputLoading(false);
    }
  }

  async function copyInputJson() {
    if (!inputText) return;
    await copyPlainText(inputText);
    setLoopNotice("Input JSON copied.");
  }

  async function copyInputPath() {
    if (!inputPaths.input) return;
    await copyPlainText(inputPaths.input);
    setLoopNotice("Input path copied.");
  }

  async function copyPrompt() {
    await copyPlainText(translationPreviewPrompt(docId, selectedChapterId, inputBundle));
    setLoopNotice("Translation preview prompt copied.");
  }

  function downloadInputJson() {
    if (!inputBundle) return;
    downloadJsonFile(`${docId || "doc"}_${selectedChapterId || "chapter"}_translation_input.json`, inputBundle);
  }

  async function importPreviewRun() {
    setImporting(true);
    setError("");
    setLoopNotice("");
    setImportWarnings([]);
    try {
      const parsed = JSON.parse(importText || "{}");
      const data = await window.AILAB_API.importTranslationPreviewRun(docId, { preview: parsed });
      const warnings = data.warnings || [];
      setImportWarnings(warnings);
      await refreshRuns(data.run?.run_id);
      setLoadedRun(data.run || null);
      setLoopNotice(`Preview run imported: ${data.run?.run_id || "new run"}.`);
    } catch (err) {
      if (err instanceof SyntaxError) {
        setError("Preview JSON is invalid.");
      } else {
        setError(err?.message || "Cannot import translation preview run.");
      }
    } finally {
      setImporting(false);
    }
  }

  async function loadAgentPreviewRun() {
    if (!docId || !selectedChapterId) return;
    setImporting(true);
    setError("");
    setLoopNotice("");
    setImportWarnings([]);
    try {
      const data = await window.AILAB_API.importAgentTranslationPreviewRun(docId, selectedChapterId);
      const warnings = data.warnings || [];
      setImportWarnings(warnings);
      await refreshRuns(data.run?.run_id);
      setLoadedRun(data.run || null);
      setLoopNotice(`Agent preview imported: ${data.run?.run_id || "new run"}.`);
    } catch (err) {
      setError(err?.message || "Cannot load agent preview output.");
    } finally {
      setImporting(false);
    }
  }

  async function loadImportFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const text = await decodeUploadedFile(file);
      setImportText(text);
      const mojibake = countMojibakeMarks(text);
      if (mojibake >= 8) {
        setLoopNotice("");
        setError(
          `"${file.name}" looks corrupted: ${mojibake} Vietnamese marks were replaced by "?" ` +
          "(the file was saved as ANSI/cp1252, not UTF-8). Diacritics cannot be recovered from this " +
          "file — re-save the agent output as UTF-8 (VS Code: \"Save with Encoding -> UTF-8\", or paste " +
          "the JSON straight into the box above), then load again."
        );
      } else {
        setError("");
        setLoopNotice(`Loaded preview JSON file: ${file.name}.`);
      }
    } catch (err) {
      setError(err?.message || "Cannot read preview JSON file.");
    } finally {
      event.target.value = "";
    }
  }

  return (
    <div className="ed-scroll">
      <div className="translation-preview">
        <div className="tp-banner">
          <div className="tp-banner-title"><Ic.shield size={14} />Translation Preview</div>
          <div className="tp-banner-copy">Read-only preview. This is not gold data and cannot be saved, promoted, locked, or frozen from this view.</div>
        </div>

        <div className="tp-controls">
          <label className="tp-control">
            <span>Chapter</span>
            <select value={selectedChapterId} onChange={e => changeChapter(e.target.value)}>
              {chapters.map(ch => (
                <option key={ch.chapter_id} value={ch.chapter_id}>
                  {ch.title || ch.chapter_title || ch.chapter_id}
                </option>
              ))}
            </select>
          </label>
          <label className="tp-control wide">
            <span>Preview run</span>
            <select value={selectedRunId} onChange={e => setSelectedRunId(e.target.value)} disabled={!chapterRuns.length || loadingRuns}>
              {!chapterRuns.length && <option value="">No preview run for this chapter</option>}
              {chapterRuns.map(run => <option key={run.run_id} value={run.run_id}>{previewRunLabel(run)}</option>)}
            </select>
          </label>
          <div className="tp-run-meta mono">
            {loadedRun ? `${loadedRun.run_id} · ${loadedRun.skill_version || "unknown skill"}` : loadingRuns || loadingRun ? "loading..." : "no run loaded"}
          </div>
        </div>

        <div className="tp-loop-panel">
          <div className="tp-loop-head">
            <div>
              <b>Preview loop</b>
              <span>Build an input bundle, run the skill outside the app, then import the preview JSON here.</span>
            </div>
            <div className="tp-loop-actions">
              <button className="btn" onClick={buildInputBundle} disabled={!selectedChapterId || inputLoading}>
                <Ic.layers size={13} /> {inputLoading ? "Building..." : "Build input"}
              </button>
              <button className="btn" onClick={copyPrompt} disabled={!inputBundle}>
                <Ic.sparkle size={13} /> Copy prompt
              </button>
              <button className="btn" onClick={copyInputPath} disabled={!inputPaths.input || !inputBundle}>
                <Ic.folder size={13} /> Copy path
              </button>
              <button className="btn" onClick={copyInputJson} disabled={!inputText}>
                <Ic.doc size={13} /> Copy JSON
              </button>
              <button className="btn" onClick={downloadInputJson} disabled={!inputBundle}>
                <Ic.upload size={13} /> Download JSON
              </button>
            </div>
          </div>
          {inputText && (
            <textarea
              className="tp-json-textarea"
              value={inputText}
              readOnly
              aria-label="Translation preview input JSON"
            />
          )}
          <div className="tp-import-box">
            <textarea
              className="tp-json-textarea compact"
              value={importText}
              onChange={e => setImportText(e.target.value)}
              placeholder="Paste translation preview output JSON here..."
              aria-label="Translation preview output JSON"
            />
            <div className="tp-import-actions">
              <button className="btn" onClick={loadAgentPreviewRun} disabled={!selectedChapterId || importing}>
                <Ic.sparkle size={13} /> Load agent preview
              </button>
              <button className="btn" onClick={() => importFileRef.current?.click()}>
                <Ic.upload size={13} /> Load preview file
              </button>
              <input
                ref={importFileRef}
                type="file"
                accept="application/json,.json"
                className="hidden-file"
                onChange={loadImportFile}
              />
              <button className="btn primary" onClick={importPreviewRun} disabled={!importText.trim() || importing}>
                <Ic.checkCircle size={13} /> {importing ? "Importing..." : "Import preview"}
              </button>
            </div>
          </div>
          {loopNotice && <div className="tp-loop-note good"><Ic.checkSmall size={12} />{loopNotice}</div>}
          {importWarnings.length > 0 && (
            <div className="tp-loop-note warn">
              <Ic.alert size={12} />
              <span>{importWarnings.length} warning{importWarnings.length > 1 ? "s" : ""}: {importWarnings.slice(0, 3).map(item => item.code).join(", ")}</span>
            </div>
          )}
        </div>

        {error && <div className="tp-warning bad"><Ic.xCircle size={13} />{error}</div>}
        {warnings.length > 0 && (
          <div className="tp-warning">
            <Ic.alert size={13} />
            <div>
              <b>{warnings.length} import warning{warnings.length > 1 ? "s" : ""}</b>
              <div className="tp-warning-list">
                {warnings.slice(0, 4).map((warning, index) => (
                  <span key={index}>{warning.code}: {warning.context_id || warning.relation_id || warning.block_id || warning.message}</span>
                ))}
                {warnings.length > 4 && <span>+{warnings.length - 4} more</span>}
              </div>
            </div>
          </div>
        )}

        {!loadingRuns && !chapterRuns.length ? (
          <div className="tp-empty">
            <Ic.file size={22} />
            <div>No translation preview run for {currentChapter.title || selectedChapterId || "this chapter"}.</div>
            <p>Import a JSON run through the S2 API, then reload this view.</p>
          </div>
        ) : (
          <div className="tp-table">
            <div className="tp-table-head">
              <span>Source EN</span>
              <span>Preview VI</span>
            </div>
            {chapterRows.map(block => {
              const preview = runByBlock[block.block_id] || null;
              const address = addressSummary(preview?.address_applied);
              const usedContext = preview?.used_context || [];
              const mentions = preview?.mentions || [];
              return (
                <article
                  key={block.block_id}
                  className={"tp-row" + (selectedId === block.block_id ? " active" : "")}
                  data-block-id={block.block_id}
                  onMouseDown={() => onSelectBlock(block.block_id)}
                >
                  <div className="tp-cell tp-source">
                    <div className="tp-block-meta">
                      <span className="mono">{block.block_id}</span>
                      <span className={"tag tag-" + block.block_type}>{block.block_type}</span>
                    </div>
                    <div className="tp-text">{renderPreviewText(block.clean_text || "", mentions, "source_surface")}</div>
                  </div>
                  <div className={"tp-cell tp-target" + (!preview?.target_text ? " missing" : "")}>
                    <div className="tp-target-head">
                      <span className="mono">{preview ? "matched by block_id" : "missing preview"}</span>
                      {address && <span className="tp-address"><Ic.users size={12} />{address.text}{address.sub ? <em>{address.sub}</em> : null}</span>}
                    </div>
                    <div className="tp-text">{preview?.target_text ? renderPreviewText(preview.target_text, mentions, "target_surface") : "(not translated in this run)"}</div>
                    {(usedContext.length > 0 || preview?.notes) && (
                      <div className="tp-context">
                        {usedContext.map((id, idx) => {
                          const chip = contextChip(id, linkIndex);
                          return <span key={`${id}:${idx}`} className={"tp-chip " + chip.kind} title={chip.title}>{chip.label}</span>;
                        })}
                        {preview?.notes && <span className="tp-note">{preview.notes}</span>}
                      </div>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function CenterEditor({
  block, docInfo, reviewed, spans, editing, mode, onModeChange, chapter, chapters, chapterBlocks, allBlocks,
  review, selectedId, getSpansForBlock, linkIndex, onSelectBlock, onNextUnreviewed,
  onEdit, onCommitClean, onCancelEdit,
  onChangeType, onToggleOpening, onToggleFlag, onMarkReviewed,
  onAddGlossary, onAddEntity, onPreviewRunChange,
}) {
  const [hoverInfo, setHoverInfo] = React.useState(null);
  const chapterTitle = chapter?.title || chapter?.chapter_title || block.chapter_id;
  const streamBlocks = mode === "book" ? (allBlocks || []) : (chapterBlocks || []);
  const streamLabel = mode === "book" ? (docInfo?.metadata?.title || docInfo?.doc_id || "Full book") : chapterTitle;
  const streamCount = mode === "book" ? (allBlocks?.length || 0) : (chapterBlocks?.length || 0);
  const handleHoverSpan = React.useCallback((span, targetBlock, rect) => {
    setHoverInfo({ span, block: targetBlock, rect });
  }, []);
  const handleLeaveSpan = React.useCallback(() => setHoverInfo(null), []);

  return (
    <div className="col col-center">
      <EditorToolbar block={block} reviewed={reviewed} mode={mode} onModeChange={onModeChange}
        streamLabel={streamLabel} streamCount={streamCount} onNextUnreviewed={onNextUnreviewed}
        onChangeType={onChangeType} onToggleOpening={onToggleOpening}
        onToggleFlag={onToggleFlag} onMarkReviewed={() => onMarkReviewed(block.block_id)} />

      {mode === "preview" ? (
        <TranslationPreviewView
          docInfo={docInfo}
          chapters={chapters}
          allBlocks={allBlocks}
          chapter={chapter}
          selectedId={selectedId}
          onSelectBlock={onSelectBlock}
          linkIndex={linkIndex}
          onPreviewRunChange={onPreviewRunChange}
        />
      ) : mode !== "block" ? (
        <ChapterStream
          blocks={streamBlocks}
          chapters={chapters}
          selectedId={selectedId}
          review={review}
          getSpansForBlock={getSpansForBlock}
          onSelectBlock={onSelectBlock}
          onCommitClean={onCommitClean}
          onMarkReviewed={onMarkReviewed}
          onAddGlossary={onAddGlossary}
          onAddEntity={onAddEntity}
          onHoverSpan={handleHoverSpan}
          onLeaveSpan={handleLeaveSpan}
        />
      ) : (
        <SingleBlockView
          block={block}
          docInfo={docInfo}
          reviewed={reviewed}
          spans={spans}
          editing={editing}
          onEdit={onEdit}
          onCommitClean={onCommitClean}
          onCancelEdit={onCancelEdit}
          onAddGlossary={onAddGlossary}
          onAddEntity={onAddEntity}
          onHoverSpan={handleHoverSpan}
          onLeaveSpan={handleLeaveSpan}
        />
      )}
      <HighlightHoverCard hover={hoverInfo} linkIndex={linkIndex} />
    </div>
  );
}

window.CenterEditor = CenterEditor;
