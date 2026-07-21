/* ===== RIGHT PANEL: accordion of 6 tabs, multiple sections may stay open ===== */

function StatusPill({ status }) {
  const map = {
    locked: [uiText("Đã khóa", "Locked"), "pill-lock"],
    proposed: [uiText("Đề xuất", "Proposed"), "pill-amber"],
    candidate: [uiText("Ứng viên", "Candidate"), "pill-amber"],
    verified: [uiText("Đã xác minh", "Verified"), "pill-green"],
    human_verified: [uiText("Đã xác minh", "Verified"), "pill-green"],
    reviewed: [uiText("Đã duyệt", "Reviewed"), "pill-green"],
    draft: [uiText("Bản nháp", "Draft"), "pill-amber"],
  };
  const [label, cls] = map[status] || [status || uiText("Chưa đặt", "Unset"), "pill-grey"];
  return <span className={"pill " + cls}>{label}</span>;
}

function MiniField({ label, children, locked }) {
  return (
    <div className="mf">
      <div className="mf-label">{locked && <Ic.lock size={9} />}{label}</div>
      <div className="mf-val">{children}</div>
    </div>
  );
}

function FormField({ label, children }) {
  return (
    <label className="form-field">
      <span className="form-label">{label}</span>
      {children}
    </label>
  );
}

function csvToArray(value) {
  return value.split(",").map(x => x.trim()).filter(Boolean);
}

function arrayToCsv(value) {
  return (value || []).join(", ");
}

function linesToArray(value) {
  return value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
}

function arrayToLines(value) {
  return (value || []).join("\n");
}

function confidenceValue(value) {
  if (value === "" || value == null) return "";
  const n = Number(value);
  return Number.isFinite(n) ? String(n) : "";
}

/* ---------- GLOSSARY ---------- */
function GlossaryTab({ terms, onDeleteTerm, onUpdateTerm, onFocusTerm }) {
  const [expanded, setExpanded] = React.useState(terms[0]?.term_id);
  React.useEffect(() => {
    if (terms.length && !terms.some(t => t.term_id === expanded)) setExpanded(terms[0].term_id);
  }, [terms, expanded]);

  if (!terms.length) {
    return <Empty icon={Ic.tag} text={uiText("Block này không có thuật ngữ.", "No glossary terms for this block.")} sub={uiText("Chọn văn bản trong Văn bản sạch -> Thêm thuật ngữ.", "Select text in Clean text -> Add glossary term.")} />;
  }

  return (
    <div className="tab-body">
      {terms.map(t => {
        const open = expanded === t.term_id;
        return (
          <div key={t.term_id} className={"card" + (open ? " open" : "")}>
            <button className="card-head" onClick={() => {
              setExpanded(open ? null : t.term_id);
              if (onFocusTerm) onFocusTerm(t.term_id, null, { toggle: false });
            }}>
              <Ic.chevRight size={11} className="card-caret" style={{ transform: open ? "rotate(90deg)" : "none" }} />
              <span className="card-title mono">{t.source_term || uiText("(thuật ngữ mới)", "(new term)")}</span>
              <span className="card-arrow"><Ic.arrowRight size={11} /></span>
              <span className="card-target">{t.expected_target || uiText("cần bản đích", "target needed")}</span>
              <span className="card-spacer" />
              <StatusPill status={t.status} />
            </button>
            {open && (
              <div className="card-body">
                <div className="form-grid">
                  <FormField label="source_term">
                    <input value={t.source_term || ""} onChange={e => onUpdateTerm(t.term_id, { source_term: e.target.value })} />
                  </FormField>
                  <FormField label="expected_target">
                    <input value={t.expected_target || ""} onChange={e => onUpdateTerm(t.term_id, { expected_target: e.target.value })} />
                  </FormField>
                  <FormField label="allowed_variants">
                    <input value={arrayToCsv(t.allowed_variants)} onChange={e => onUpdateTerm(t.term_id, { allowed_variants: csvToArray(e.target.value) })} />
                  </FormField>
                  <FormField label="forbidden_variants">
                    <input value={arrayToCsv(t.forbidden_variants)} onChange={e => onUpdateTerm(t.term_id, { forbidden_variants: csvToArray(e.target.value) })} />
                  </FormField>
                  <FormField label="status">
                    <select value={t.status || "candidate"} onChange={e => onUpdateTerm(t.term_id, { status: e.target.value })}>
                      <option value="candidate">candidate</option>
                      <option value="verified">verified</option>
                      <option value="locked">locked</option>
                      <option value="human_verified">human_verified</option>
                    </select>
                  </FormField>
                </div>
                <div className="card-meta-row">
                  <span className="lockfield"><span className="lf-k"><Ic.lock size={9} />scope</span><span className="lf-v">{t.chapter_scope}</span></span>
                  <span className="lockfield"><span className="lf-k">conf</span><span className="lf-v">{Number(t.confidence || 0).toFixed(2)}</span></span>
                  <span className="lockfield"><span className="lf-k">occ</span><span className="lf-v">{(t.occurrences || []).length}</span></span>
                  <button className="card-del tip tip-left" data-tip={uiText("Xóa thuật ngữ", "Delete term")} onClick={() => onDeleteTerm(t)}><Ic.trash size={12} /></button>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ---------- ENTITIES ---------- */
function EntitiesTab({ entities, allEntities, block, onUpdateEntity, onUpdateDiscourse, onDeleteEntity }) {
  const [expanded, setExpanded] = React.useState(entities[0]?.entity_id);
  React.useEffect(() => {
    if (entities.length && !entities.some(e => e.entity_id === expanded)) setExpanded(entities[0].entity_id);
  }, [entities, expanded]);

  if (!entities.length && block.block_type !== "dialogue") {
    return <Empty icon={Ic.users} text={uiText("Block này không nhắc tới thực thể nào.", "No entities mentioned in this block.")} sub={uiText("Chọn văn bản trong Văn bản sạch -> Thêm lần nhắc thực thể.", "Select text in Clean text -> Add entity mention.")} />;
  }

  const isDialogue = block.block_type === "dialogue";
  return (
    <div className="tab-body">
      {isDialogue && (
        <div className="discourse">
          <div className="discourse-head"><Ic.quote size={11} />{uiText("hội thoại · người nói / người nghe", "dialogue · speaker / addressee")}</div>
          <div className="discourse-row">
            <DiscSelect label="speaker" entities={allEntities} value={block.discourse?.speaker_entity_id || ""}
              onChange={value => onUpdateDiscourse({ speaker_entity_id: value })} />
            <DiscSelect label="addressee" entities={allEntities} value={block.discourse?.addressee_entity_id || ""}
              onChange={value => onUpdateDiscourse({ addressee_entity_id: value })} />
          </div>
        </div>
      )}

      {!entities.length && <Empty icon={Ic.users} text={uiText("Block này chưa có lần nhắc thực thể.", "No entity mentions in this block yet.")} sub={uiText("Vẫn có thể đặt người nói/người nghe ở phía trên.", "Dialogue speaker/addressee can still be set above.")} />}

      {entities.map(e => {
        const open = expanded === e.entity_id;
        const mentions = (e.mentions || []).filter(m => m.block_id === block.block_id);
        return (
          <div key={e.entity_id} className={"card" + (open ? " open" : "")}>
            <button className="card-head" onClick={() => setExpanded(open ? null : e.entity_id)}>
              <Ic.chevRight size={11} className="card-caret" style={{ transform: open ? "rotate(90deg)" : "none" }} />
              <span className={"ent-type ent-" + e.entity_type}>{(e.entity_type || "?")[0]}</span>
              <span className="card-title">{e.canonical_source || uiText("(thực thể mới)", "(new entity)")}</span>
              <span className="card-arrow"><Ic.arrowRight size={11} /></span>
              <span className="card-target">{e.canonical_target || uiText("cần bản đích", "target needed")}</span>
              <span className="card-spacer" />
              {mentions.length > 0 && <span className="ment-count mono">{mentions.length}x</span>}
            </button>
            {open && (
              <div className="card-body">
                <div className="form-grid">
                  <FormField label="canonical_source">
                    <input value={e.canonical_source || ""} onChange={ev => onUpdateEntity(e.entity_id, { canonical_source: ev.target.value })} />
                  </FormField>
                  <FormField label="canonical_target">
                    <input value={e.canonical_target || ""} onChange={ev => onUpdateEntity(e.entity_id, { canonical_target: ev.target.value })} />
                  </FormField>
                  <FormField label="aliases_target">
                    <input value={arrayToCsv(e.aliases_target)} onChange={ev => onUpdateEntity(e.entity_id, { aliases_target: csvToArray(ev.target.value) })} />
                  </FormField>
                  <FormField label="pronoun_policy">
                    <input value={e.pronoun_policy || ""} onChange={ev => onUpdateEntity(e.entity_id, { pronoun_policy: ev.target.value })} />
                  </FormField>
                </div>
                {mentions.length > 0 && (
                  <MiniField label={uiText("lần nhắc trong block này", "mentions in this block")}>
                    {mentions.map((m, i) => <span key={i} className="var mono">"{m.surface}" [{m.span[0]},{m.span[1]}]</span>)}
                  </MiniField>
                )}
                <div className="card-meta-row">
                  <span className="lockfield"><span className="lf-k">type</span><span className="lf-v">{e.entity_type || "-"}</span></span>
                  <span className="lockfield"><span className="lf-k">conf</span><span className="lf-v">{Number(e.confidence || 0).toFixed(2)}</span></span>
                  <span className="lockfield"><span className="lf-k">mentions</span><span className="lf-v">{(e.mentions || []).length}</span></span>
                  <button className="card-del tip tip-left" data-tip={uiText("Xóa thực thể", "Delete entity")} onClick={() => onDeleteEntity(e)}><Ic.trash size={12} /></button>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function DiscSelect({ label, entities, value, onChange }) {
  return (
    <label className="disc-field">
      <span className="disc-label">{label}</span>
      <div className="disc-sel">
        <select value={value || ""} onChange={e => onChange(e.target.value)}>
          <option value="">-</option>
          {entities.map(e => <option key={e.entity_id} value={e.entity_id}>{e.canonical_source}</option>)}
        </select>
        <Ic.chevDown size={11} className="faint" />
      </div>
    </label>
  );
}

/* ---------- RELATIONS ---------- */
function relationEntityLabel(entityId, entityMap) {
  const entity = entityMap[entityId];
  if (!entity) return { label: entityId || "unknown", missing: true };
  return {
    label: entity.canonical_target || entity.canonical_source || entity.entity_id,
    missing: false,
  };
}

function termOrDash(value) {
  return value == null || value === "" ? "-" : value;
}

function AddressLine({ from, to, policy }) {
  return (
    <span className="var mono">
      {from} -&gt; {to}: self {termOrDash(policy?.self_term)} / address {termOrDash(policy?.address_term)}
    </span>
  );
}

function defaultRelationDraft(entities, block) {
  const speaker = block?.discourse?.speaker_entity_id || "";
  const addressee = block?.discourse?.addressee_entity_id || "";
  const first = entities?.[0]?.entity_id || "";
  const second = entities?.find(e => e.entity_id !== first)?.entity_id || "";
  return {
    source_entity_id: speaker || first,
    target_entity_id: addressee || second,
    relation_type: "",
    state_label: "",
    valid_from_block_id: "",
    valid_to_block_id: "",
    trigger_event_id: "",
    address_policy: {
      source_to_target: { self_term: "", address_term: "" },
      target_to_source: { self_term: "", address_term: "" },
    },
    evidence: block?.block_id ? [{ block_id: block.block_id, surface: "" }] : [],
    confidence: 0.8,
    notes: "",
  };
}

function evidenceToText(evidence) {
  return (evidence || []).map(item => `${item.block_id || ""}${item.surface ? "\t" + item.surface : ""}`).join("\n");
}

function textToEvidence(text) {
  return String(text || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(line => {
    const parts = line.split(/\t|\|/);
    const block_id = (parts.shift() || "").trim();
    const surface = parts.join("|").trim();
    return surface ? { block_id, surface } : { block_id };
  }).filter(item => item.block_id);
}

function RelationEditor({ relation, entities, block, onChange, onSave, onDelete, isNew }) {
  const safe = relation || {};
  const policy = safe.address_policy || {};
  const update = patch => onChange({ ...safe, ...patch });
  const updatePolicy = (direction, field, value) => {
    const current = safe.address_policy || {};
    update({
      address_policy: {
        ...current,
        [direction]: {
          ...(current[direction] || {}),
          [field]: value,
        },
      },
    });
  };
  const canSave = safe.source_entity_id && safe.target_entity_id && safe.source_entity_id !== safe.target_entity_id && safe.relation_type;
  return (
    <div className="relation-editor">
      <div className="form-grid">
        <FormField label="source_entity">
          <select value={safe.source_entity_id || ""} onChange={e => update({ source_entity_id: e.target.value })}>
            <option value="">-</option>
            {(entities || []).map(e => <option key={e.entity_id} value={e.entity_id}>{e.canonical_source || e.entity_id}</option>)}
          </select>
        </FormField>
        <FormField label="target_entity">
          <select value={safe.target_entity_id || ""} onChange={e => update({ target_entity_id: e.target.value })}>
            <option value="">-</option>
            {(entities || []).map(e => <option key={e.entity_id} value={e.entity_id}>{e.canonical_source || e.entity_id}</option>)}
          </select>
        </FormField>
        <FormField label="relation_type">
          <input value={safe.relation_type || ""} placeholder="friend / rival / parent / stranger..." onChange={e => update({ relation_type: e.target.value })} />
        </FormField>
        <FormField label={uiText("độ tin cậy", "confidence")}>
          <input type="number" min="0" max="1" step="0.01" value={confidenceValue(safe.confidence)}
            onChange={e => update({ confidence: e.target.value === "" ? 0 : Number(e.target.value) })} />
        </FormField>
      </div>

      <MiniField label={uiText("quy tắc xưng hô", "address policy")}>
        <div className="form-grid">
          <FormField label={uiText("nguồn tự xưng", "source self")}>
            <input value={policy.source_to_target?.self_term || ""} placeholder="toi / ta / ong..." onChange={e => updatePolicy("source_to_target", "self_term", e.target.value)} />
          </FormField>
          <FormField label={uiText("nguồn gọi đích", "source calls target")}>
            <input value={policy.source_to_target?.address_term || ""} placeholder="ban / chau / nguoi..." onChange={e => updatePolicy("source_to_target", "address_term", e.target.value)} />
          </FormField>
          <FormField label={uiText("đích tự xưng", "target self")}>
            <input value={policy.target_to_source?.self_term || ""} placeholder="toi / chau / em..." onChange={e => updatePolicy("target_to_source", "self_term", e.target.value)} />
          </FormField>
          <FormField label={uiText("đích gọi nguồn", "target calls source")}>
            <input value={policy.target_to_source?.address_term || ""} placeholder="ban / ong / anh..." onChange={e => updatePolicy("target_to_source", "address_term", e.target.value)} />
          </FormField>
        </div>
      </MiniField>

      <div className="form-grid">
        <FormField label="state_label">
          <input value={safe.state_label || ""} placeholder="before_betrayal / default" onChange={e => update({ state_label: e.target.value })} />
        </FormField>
        <FormField label="trigger_event_id">
          <input value={safe.trigger_event_id || ""} placeholder={uiText("nhãn sự kiện tùy chọn", "optional event label")} onChange={e => update({ trigger_event_id: e.target.value })} />
        </FormField>
        <FormField label="valid_from_block_id">
          <input value={safe.valid_from_block_id || ""} placeholder={block?.block_id || uiText("tùy chọn", "optional")} onChange={e => update({ valid_from_block_id: e.target.value })} />
        </FormField>
        <FormField label="valid_to_block_id">
          <input value={safe.valid_to_block_id || ""} placeholder={uiText("tùy chọn", "optional")} onChange={e => update({ valid_to_block_id: e.target.value })} />
        </FormField>
      </div>

      <FormField label="evidence (one per line: block_id | surface)">
        <textarea rows={3} value={evidenceToText(safe.evidence)} onChange={e => update({ evidence: textToEvidence(e.target.value) })} />
      </FormField>
      <FormField label="notes">
        <textarea rows={2} value={safe.notes || ""} onChange={e => update({ notes: e.target.value })} />
      </FormField>

      <div className="ref-actions">
        <button className="btn sm primary" disabled={!canSave} onClick={onSave}>
          {isNew ? uiText("Thêm quan hệ", "Add relation") : uiText("Lưu quan hệ", "Save relation")}
        </button>
        {!isNew && <button className="btn sm danger" onClick={onDelete}><Ic.trash size={12} />{uiText("Xóa", "Delete")}</button>}
      </div>
    </div>
  );
}

function RelationsTab({ relations, entities, block, onCreateRelation, onUpdateRelation, onDeleteRelation }) {
  const safeRelations = relations || [];
  const entityMap = {};
  (entities || []).forEach(entity => { entityMap[entity.entity_id] = entity; });
  const [expanded, setExpanded] = React.useState(null);
  const [showAdd, setShowAdd] = React.useState(!safeRelations.length);
  const [draft, setDraft] = React.useState(() => defaultRelationDraft(entities || [], block));
  React.useEffect(() => {
    setDraft(current => {
      if (current.source_entity_id || current.target_entity_id || current.relation_type || current.notes) return current;
      return defaultRelationDraft(entities || [], block);
    });
  }, [entities, block?.block_id]);
  React.useEffect(() => {
    if (expanded && !safeRelations.some(r => r.relation_id === expanded)) {
      setExpanded(null);
    }
  }, [safeRelations, expanded]);
  React.useEffect(() => {
    if (!safeRelations.length) setShowAdd(true);
  }, [safeRelations.length]);

  const speakerId = block?.discourse?.speaker_entity_id || "";
  const addresseeId = block?.discourse?.addressee_entity_id || "";

  return (
    <div className="tab-body">
      <div className="ref-explain">
        <Ic.users size={12} />
        <span><b>{uiText("Chế độ theo block.", "Block-scoped view.")}</b> {uiText("Quan hệ xuất hiện khi block này làm bằng chứng, ranh giới giai đoạn hoặc chứa cả hai bên. Bản ghi quan hệ gốc vẫn ở cấp tài liệu.", "Relations appear when this block grounds their evidence, phase boundary, or both participants. The underlying relation record remains document-level.")}</span>
      </div>

      {!safeRelations.length && <Empty icon={Ic.users} text={uiText("Block này không làm bằng chứng cho quan hệ thực thể nào.", "No entity relations are grounded in this block.")} sub={uiText("Chọn block khác hoặc để Builder điền bằng chứng quan hệ.", "Select another block or let Builder populate relation evidence.")} />}

      {safeRelations.map(relation => {
        const open = expanded === relation.relation_id;
        const source = relationEntityLabel(relation.source_entity_id, entityMap);
        const target = relationEntityLabel(relation.target_entity_id, entityMap);
        const policy = relation.address_policy || {};
        const evidence = relation.evidence || [];
        const activeForBlock = speakerId && addresseeId && (
          (speakerId === relation.source_entity_id && addresseeId === relation.target_entity_id) ||
          (speakerId === relation.target_entity_id && addresseeId === relation.source_entity_id)
        );
        const phaseItems = [
          ["state", relation.state_label],
          ["from", relation.valid_from_block_id],
          ["to", relation.valid_to_block_id],
          ["trigger", relation.trigger_event_id],
        ].filter(([, value]) => value);

        return (
          <div key={relation.relation_id} className={"card" + (open ? " open" : "")}>
            <button className="card-head" onClick={() => setExpanded(open ? null : relation.relation_id)}>
              <Ic.chevRight size={11} className="card-caret" style={{ transform: open ? "rotate(90deg)" : "none" }} />
              <span className="card-title">{source.label}</span>
              <span className="card-arrow"><Ic.arrowRight size={11} /></span>
              <span className="card-target">{target.label}</span>
              <span className="card-spacer" />
              {activeForBlock && <span className="pill pill-green">{uiText("hội thoại hiện tại", "current dialogue")}</span>}
              <span className="pill pill-grey">{relation.relation_type || "relation"}</span>
            </button>
            {open && (
              <div className="card-body">
                <div className="sum-meta">
                  <span className="lockfield"><span className="lf-k">relation</span><span className="lf-v">{relation.relation_id}</span></span>
                  <span className="lockfield"><span className="lf-k">conf</span><span className="lf-v">{Number(relation.confidence || 0).toFixed(2)}</span></span>
                </div>

                {(source.missing || target.missing) && (
                  <div className="ref-explain">
                    <Ic.alert size={12} />
                    <span>{uiText("Không phân giải được một phía từ entities.jsonl; đang hiển thị ID thực thể thô.", "One side could not be resolved from entities.jsonl; raw entity id is shown.")}</span>
                  </div>
                )}

                <MiniField label="address policy">
                  <AddressLine from={source.label} to={target.label} policy={policy.source_to_target} />
                  <AddressLine from={target.label} to={source.label} policy={policy.target_to_source} />
                </MiniField>

                {phaseItems.length > 0 && (
                  <MiniField label="phase">
                    {phaseItems.map(([label, value]) => <span key={label} className="var mono">{label}: {value}</span>)}
                  </MiniField>
                )}

                {evidence.length > 0 && (
                  <MiniField label="evidence">
                    {evidence.map((item, index) => (
                      <span key={index} className="var mono">
                        {item.block_id || item.trigger_block_id || item.source_block_id || "unresolved block"}{item.surface ? `: "${item.surface}"` : ""}
                      </span>
                    ))}
                  </MiniField>
                )}

                {relation.notes && (
                  <MiniField label="notes">
                    <span>{relation.notes}</span>
                  </MiniField>
                )}
                <RelationEditor relation={relation} entities={entities} block={block}
                  onChange={patch => onUpdateRelation(relation.relation_id, patch)}
                  onSave={() => onUpdateRelation(relation.relation_id, {})}
                  onDelete={() => onDeleteRelation(relation)} />
              </div>
            )}
          </div>
        );
      })}

      <div className={"card" + (showAdd ? " open" : "")}>
        <button className="card-head" onClick={() => setShowAdd(value => !value)}>
          <Ic.chevRight size={11} className="card-caret" style={{ transform: showAdd ? "rotate(90deg)" : "none" }} />
          <span className="card-title">{uiText("Thêm quan hệ", "Add relation")}</span>
          <span className="card-spacer" />
          <span className="pill pill-grey">{(entities || []).length} entities</span>
        </button>
        {showAdd && ((entities || []).length < 2 ? (
          <div className="card-body">
            <Empty icon={Ic.users} text="Need at least two entities." sub="Create entity mentions first, then add their relation here." />
          </div>
        ) : (
          <div className="card-body">
            <RelationEditor relation={draft} entities={entities} block={block} isNew
              onChange={setDraft}
              onSave={async () => {
                const result = await onCreateRelation(draft);
                if (result) {
                  setDraft(defaultRelationDraft(entities || [], block));
                  setShowAdd(false);
                }
              }} />
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------- SUMMARY ---------- */
function SummaryTab({ summary, entities, onUpdateSummary }) {
  const safe = summary || {};
  return (
    <div className="tab-body">
      <div className="ref-explain">
        <Ic.book size={12} />
        <span><b>{uiText("Cấp chương.", "Chapter-level.")}</b> {uiText("Tóm tắt này áp dụng cho mọi block trong chương; nó chỉ đổi khi block đang chọn chuyển sang chương khác.", "This summary applies to every block in this chapter; it only changes when the active block moves to another chapter.")}</span>
      </div>
      <div className="sum-meta">
        <span className="lockfield"><span className="lf-k">chapter</span><span className="lf-v">{safe.chapter_id || "-"}</span></span>
        <span className="lockfield"><span className="lf-k">conf</span><span className="lf-v">{Number(safe.confidence || 0).toFixed(2)}</span></span>
      </div>
      <FormField label="summary_source">
        <textarea rows={5} value={safe.summary_source || ""} onChange={e => onUpdateSummary(safe.chapter_id, { summary_source: e.target.value })} />
      </FormField>
      <div className="form-grid">
        <FormField label="source">
          <select value={safe.source || ""} onChange={e => onUpdateSummary(safe.chapter_id, { source: e.target.value })}>
            <option value="">not set</option>
            <option value="human">human</option>
            <option value="ai_assisted_verified">ai_assisted_verified</option>
          </select>
        </FormField>
        <FormField label="emotional_tone">
          <input value={safe.emotional_tone || ""} onChange={e => onUpdateSummary(safe.chapter_id, { emotional_tone: e.target.value })} />
        </FormField>
        <FormField label="setting">
          <input value={safe.setting || ""} onChange={e => onUpdateSummary(safe.chapter_id, { setting: e.target.value })} />
        </FormField>
        <FormField label="confidence">
          <input type="number" min="0" max="1" step="0.01" value={confidenceValue(safe.confidence)}
            onChange={e => onUpdateSummary(safe.chapter_id, { confidence: e.target.value === "" ? 0 : Number(e.target.value) })} />
        </FormField>
        <FormField label="motifs">
          <input value={arrayToCsv(safe.motifs)} onChange={e => onUpdateSummary(safe.chapter_id, { motifs: csvToArray(e.target.value) })} />
        </FormField>
      </div>
      <FormField label="key_events">
        <textarea rows={4} value={arrayToLines(safe.key_events)} onChange={e => onUpdateSummary(safe.chapter_id, { key_events: linesToArray(e.target.value) })} />
      </FormField>
      <FormField label="open_threads">
        <textarea rows={3} value={arrayToLines(safe.open_threads)} onChange={e => onUpdateSummary(safe.chapter_id, { open_threads: linesToArray(e.target.value) })} />
      </FormField>
      <FormField label="summary_target">
        <textarea rows={4} value={safe.summary_target || ""} onChange={e => onUpdateSummary(safe.chapter_id, { summary_target: e.target.value })} />
      </FormField>
      <FormField label="translation_notes">
        <textarea rows={3} value={safe.translation_notes || ""} onChange={e => onUpdateSummary(safe.chapter_id, { translation_notes: e.target.value })} />
      </FormField>
      <div className="entity-pick">
        <div className="form-label">characters_present</div>
        {!entities.length ? <span className="faint">{uiText("Chưa có thực thể nào.", "No entities available yet.")}</span> : entities.map(e => {
          const checked = (safe.characters_present || []).includes(e.entity_id);
          return (
            <label key={e.entity_id} className="check-row">
              <input type="checkbox" checked={checked} onChange={() => {
                const current = safe.characters_present || [];
                const next = checked ? current.filter(id => id !== e.entity_id) : [...current, e.entity_id];
                onUpdateSummary(safe.chapter_id, { characters_present: next });
              }} />
              <span>{e.canonical_source || e.entity_id}</span>
              <span className="mono faint">{e.entity_id}</span>
            </label>
          );
        })}
      </div>
    </div>
  );
}

/* ---------- NOTES ---------- */
function NotesTab({ block, onUpdateBlockNotes }) {
  const annotations = block.annotations || {};
  const update = patch => onUpdateBlockNotes(block.block_id, patch);
  return (
    <div className="tab-body">
      <div className="ref-explain">
        <Ic.doc size={12} />
        <span><b>{uiText("Ngữ cảnh mềm cấp block.", "Block-level soft context.")}</b> {uiText("Các ghi chú này hỗ trợ diễn giải và duyệt bản dịch, nhưng chỉ mang tính tư vấn, không phải liên kết cứng như thực thể hoặc thuật ngữ.", "These notes help interpretation and translation review, but they are advisory and not hard links like entities or glossary terms.")}</span>
      </div>
      <div className="sum-meta">
        <span className="lockfield"><span className="lf-k">block</span><span className="lf-v">{block.block_id}</span></span>
        <span className="lockfield"><span className="lf-k">type</span><span className="lf-v">{block.block_type}</span></span>
      </div>
      <div className="form-grid">
        <FormField label="tone">
          <input value={annotations.tone || ""} onChange={e => update({ tone: e.target.value || null })} />
        </FormField>
        <FormField label="motifs">
          <input value={arrayToCsv(annotations.motifs)} onChange={e => update({ motifs: csvToArray(e.target.value) })} />
        </FormField>
      </div>
      <FormField label="implicit_meaning">
        <textarea rows={5} value={annotations.implicit_meaning || ""} onChange={e => update({ implicit_meaning: e.target.value || null })} />
      </FormField>
      <FormField label="narrative_note">
        <textarea rows={5} value={annotations.narrative_note || ""} onChange={e => update({ narrative_note: e.target.value || null })} />
      </FormField>
    </div>
  );
}

/* ---------- REFERENCE ---------- */
function ReferenceTab({ refs, block, onUpdateReference, onCreateReference, onSaveDraft, onMarkReviewed, onLockReference }) {
  const blockRef = refs.find(r => r.block_id === block.block_id);
  const [newReference, setNewReference] = React.useState({ reference_vi: "", source: "human", ai_model: "" });
  React.useEffect(() => {
    setNewReference({ reference_vi: "", source: "human", ai_model: "" });
  }, [block.block_id]);
  return (
    <div className="tab-body">
      <div className="ref-explain">
        <Ic.layers size={12} />
        <span><b>{uiText("Cấp block.", "Block-level.")}</b> {uiText("Block hiện tại:", "Current block:")} <span className="mono">{block.block_id}</span>. {uiText("Bản nháp giữ ở trạng thái làm việc; chỉ reference", "Draft stays in working state; only")} <b>{uiText("Đã duyệt", "Reviewed")}</b> {uiText("hoặc", "or")} <b>{uiText("Đã khóa", "Locked")}</b> {uiText("mới đủ điều kiện đóng băng.", "references are freeze-eligible.")}</span>
      </div>
      {!blockRef ? (
        <div className="ref-card status-draft">
          <div className="ref-card-head">
            <span className="ref-stratum mono">{uiText("bản nháp mới", "new draft")}</span>
            <span className="card-spacer" />
            <StatusPill status="draft" />
          </div>
          <FormField label="reference_vi">
            <textarea className="ref-textarea" rows={6} value={newReference.reference_vi}
              onChange={e => setNewReference(r => ({ ...r, reference_vi: e.target.value }))} />
          </FormField>
          <div className="form-grid">
            <FormField label="source">
              <select value={newReference.source} onChange={e => setNewReference(r => ({ ...r, source: e.target.value }))}>
                <option value="human">human</option>
                <option value="ai_assisted_verified">ai_assisted_verified</option>
              </select>
            </FormField>
            <FormField label="ai_model">
              <input value={newReference.ai_model} onChange={e => setNewReference(r => ({ ...r, ai_model: e.target.value }))} />
            </FormField>
          </div>
          <div className="ref-actions">
            <button className="btn sm primary" onClick={() => onCreateReference(block.block_id, newReference)}><Ic.book size={12} />{uiText("Lưu bản nháp", "Save draft")}</button>
          </div>
        </div>
      ) : (
        <div className={"ref-card status-" + blockRef.status}>
          <div className="ref-card-head">
            <span className="ref-stratum mono">{blockRef.stratum}</span>
            <span className="card-spacer" />
            <StatusPill status={blockRef.status} />
            {blockRef.canonical && <span className="pill pill-lock"><Ic.lock size={9} />canonical</span>}
          </div>

          <FormField label="reference_vi">
            <textarea className="ref-textarea" rows={6} value={blockRef.reference_vi || ""}
              disabled={blockRef.status === "locked"}
              onChange={e => onUpdateReference(blockRef.reference_id, { reference_vi: e.target.value, canonical: false })} />
          </FormField>

          <div className="form-grid">
            <FormField label="source">
              <select value={blockRef.source || ""} disabled={blockRef.status === "locked"}
                onChange={e => onUpdateReference(blockRef.reference_id, { source: e.target.value, canonical: false })}>
                <option value="">not set</option>
                <option value="human">human</option>
                <option value="ai_assisted_verified">ai_assisted_verified</option>
              </select>
            </FormField>
            <FormField label="ai_model">
              <input value={blockRef.ai_model || ""} disabled={blockRef.status === "locked"}
                onChange={e => onUpdateReference(blockRef.reference_id, { ai_model: e.target.value, canonical: false })} />
            </FormField>
          </div>

          <div className="ref-foot">
            <span className="lockfield"><span className="lf-k">by</span><span className="lf-v">{blockRef.translated_by}</span></span>
            {blockRef.reviewed_by && <span className="lockfield"><span className="lf-k">reviewed</span><span className="lf-v">{blockRef.reviewed_by}</span></span>}
            {blockRef.ai_model && <span className="lockfield"><span className="lf-k"><Ic.sparkle size={9} />model</span><span className="lf-v">{blockRef.ai_model}</span></span>}
          </div>

          <div className="ref-actions">
            <button className="btn sm" disabled={blockRef.status === "locked"} onClick={() => onSaveDraft(blockRef.reference_id)}>{uiText("Lưu bản nháp", "Save draft")}</button>
            <button className="btn sm" disabled={blockRef.status === "locked"} onClick={() => onMarkReviewed(blockRef.reference_id)}><Ic.checkCircle size={12} />{uiText("Đánh dấu đã duyệt", "Mark reviewed")}</button>
            <button className="btn sm primary" disabled={blockRef.status !== "reviewed"} onClick={() => onLockReference(blockRef.reference_id)}><Ic.lock size={12} />{uiText("Khóa", "Lock")}</button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------- VALIDATE ---------- */
function needsSchemaMigration(errors, docInfo) {
  const version = docInfo?.schema_version || "";
  if (version && version !== "1.5.0") return true;
  return (errors || []).some(e => {
    const msg = String(e.message || "").toLowerCase();
    return (
      (e.file === "document.json" && String(e.location || "").includes("schema_version")) ||
      (e.file === "entity_relations.jsonl" && msg.includes("not present"))
    );
  });
}

function ValidateTab({ errors, onJump, docInfo, onMigrateSchema, schemaMigrating }) {
  const byFile = {};
  errors.forEach(e => { (byFile[e.file] = byFile[e.file] || []).push(e); });
  const files = Object.keys(byFile);
  const showMigration = needsSchemaMigration(errors, docInfo);
  if (!files.length && !showMigration) return <Empty icon={Ic.checkCircle} text="No validation errors." sub="All checks pass - freeze still requires review gates." good />;
  return (
    <div className="tab-body">
      {showMigration && (
        <div className="schema-migrate">
          <div className="schema-migrate-text">
            <div className="schema-migrate-title"><Ic.layers size={12} />{uiText("Có thể nâng cấp schema", "Schema upgrade available")}</div>
            <div className="schema-migrate-sub">
              {uiText("Dự án hiện tại là", "Current project is")} <span className="mono">{docInfo?.schema_version || uiText("không rõ", "unknown")}</span>. {uiText("Nâng cấp sẽ ghi", "Upgrade writes")} <span className="mono">schema_version=1.5.0</span> {uiText("và tạo file trống", "and creates empty")} <span className="mono">entity_relations.jsonl</span>; {uiText("không trích xuất lại hoặc chạm vào annotation/bản nháp.", "it does not re-extract or touch annotations/drafts.")}
            </div>
          </div>
          <button className="btn primary" disabled={schemaMigrating} onClick={onMigrateSchema}>
            <Ic.checkCircle size={13} />{schemaMigrating ? "Migrating..." : "Migrate to 1.5"}
          </button>
        </div>
      )}
      {files.map(f => (
        <div key={f} className="val-group">
          <div className="val-file"><Ic.file size={12} />{f}<span className="val-count mono">{byFile[f].length}</span></div>
          {byFile[f].map((e, i) => (
            <button key={i} className={"val-row sev-" + e.severity} onClick={() => onJump(e)}>
              <span className={"val-sev sev-" + e.severity}>{e.severity === "error" ? <Ic.xCircle size={12} /> : <Ic.alert size={12} />}</span>
              <span className="val-text">
                <span className="val-msg">{e.message}</span>
                <span className="val-loc mono">{e.block_id || e.chapter_id || "-"} · {e.location}</span>
              </span>
              {e.block_id && <span className="val-jump">{uiText("nhảy tới", "jump")} <Ic.arrowRight size={11} /></span>}
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}

/* ---------- PROGRESS ---------- */
function Bar({ label, done, total, tone }) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  return (
    <div className="prog-row">
      <div className="prog-top"><span className="prog-label">{label}</span><span className="prog-num mono">{done}/{total}</span></div>
      <div className="prog-track"><div className={"prog-fill " + (tone || "")} style={{ width: pct + "%" }} /></div>
    </div>
  );
}

function shortTarget(target) {
  if (!target) return "";
  return target.block_id || target.term_id || target.entity_id || target.chapter_id || target.reference_id || target.doc_id || "";
}

function HistoryList({ history }) {
  const recent = history?.recent || [];
  return (
    <div className="hist-panel">
      <div className="hist-head">
        <span><Ic.clock size={12} />{uiText("Lịch sử", "History")}</span>
        <span className="hist-actions mono">
          {history?.can_undo ? "undo ready" : "no undo"} · {history?.can_redo ? "redo ready" : "no redo"}
        </span>
      </div>
      {!recent.length ? (
        <div className="hist-empty">{uiText("Chưa có thay đổi nào có thể hoàn tác.", "No undoable changes yet.")}</div>
      ) : recent.map(event => (
        <div key={event.id} className="hist-row">
          <span className="hist-dot" />
          <span className="hist-main">
            <span className="hist-label">{event.label || event.action}</span>
            <span className="hist-meta mono">{event.user || "local"} · {shortTarget(event.target)}</span>
          </span>
          <span className="hist-time mono">{(event.ts || "").slice(11, 16)}</span>
        </div>
      ))}
    </div>
  );
}

function ProgressTab({ stats, freezeReasons, history }) {
  return (
    <div className="tab-body">
      <Bar label="Blocks reviewed" done={stats.reviewed} total={stats.totalBlocks} />
      <Bar label="Glossary terms" done={stats.glossaryDone} total={stats.glossary} tone="alt" />
      <Bar label="Entities resolved" done={stats.entitiesDone} total={stats.entities} tone="alt" />
      <Bar label="Chapter summaries" done={stats.summaries} total={stats.totalChapters} tone="alt" />
      <Bar label="Reference subset reviewed/locked" done={stats.refReviewed} total={stats.refs} tone="alt" />
      <Bar label="Validation clean" done={stats.valClean} total={stats.valTotal} tone={stats.valClean === stats.valTotal ? "good" : "bad"} />
      <div className="prog-note">
        <Ic.snow size={12} />
        <span>{freezeReasons.length ? "Freeze is blocked: " + freezeReasons.join("; ") : "Freeze gates are clear."}</span>
      </div>
      <HistoryList history={history} />
    </div>
  );
}

function Empty({ icon: I, text, sub, good }) {
  return (
    <div className={"empty" + (good ? " good" : "")}>
      <I size={20} className="empty-ic" />
      <div className="empty-text">{text}</div>
      {sub && <div className="empty-sub">{sub}</div>}
    </div>
  );
}

function InspectorField({ label, children, wide }) {
  if (children == null || children === "" || (Array.isArray(children) && !children.length)) return null;
  return (
    <div className={"ci-field" + (wide ? " wide" : "")}>
      <span>{label}</span>
      <div>{children}</div>
    </div>
  );
}

function InspectorChips({ values, limit = 16 }) {
  const rows = (values || []).filter(Boolean);
  if (!rows.length) return null;
  return (
    <div className="ci-chips">
      {rows.slice(0, limit).map((value, index) => <span className="ci-chip mono" key={`${value}:${index}`}>{value}</span>)}
      {rows.length > limit && <span className="ci-chip muted">+{rows.length - limit}</span>}
    </div>
  );
}

function inspectorKey(kind, row, index = 0) {
  if (kind === "glossary") return row.term_id || `${row.source_term || "term"}:${index}`;
  if (kind === "entities") return row.entity_id || `${row.canonical_source || "entity"}:${index}`;
  if (kind === "relations") return row.relation_id || `${row.source_entity_id || "source"}:${row.target_entity_id || "target"}:${index}`;
  return row.chapter_id || `summary:${index}`;
}

function inspectorEntityLabel(entityId, entityMap) {
  const entity = entityMap[entityId];
  return entity?.canonical_source || entity?.canonical_target || entityId || "Unknown";
}

function inspectorSearchText(kind, row, entityMap) {
  if (kind === "glossary") {
    return [
      row.source_term,
      row.expected_target,
      ...(row.allowed_variants || []),
      ...(row.forbidden_variants || []),
      row.status,
    ].filter(Boolean).join(" ");
  }
  if (kind === "entities") {
    return [
      row.canonical_source,
      row.canonical_target,
      row.entity_type,
      ...(row.aliases_source || []),
      ...(row.aliases_target || []),
    ].filter(Boolean).join(" ");
  }
  if (kind === "relations") {
    return [
      inspectorEntityLabel(row.source_entity_id, entityMap),
      inspectorEntityLabel(row.target_entity_id, entityMap),
      row.relation_type,
      row.state_label,
      row.notes,
    ].filter(Boolean).join(" ");
  }
  return [
    row.chapter_id,
    row.summary_source,
    row.summary_target,
    row.setting,
    row.emotional_tone,
    ...(row.motifs || []),
    ...(row.key_events || []),
  ].filter(Boolean).join(" ");
}

function InspectorRow({ kind, row, index, entityMap, onSelect }) {
  let title = "";
  let target = "";
  let meta = "";
  let status = null;
  let Icon = Ic.doc;

  if (kind === "glossary") {
    Icon = Ic.tag;
    title = row.source_term || "(unnamed term)";
    target = row.expected_target || "Target needed";
    meta = uiText(`${(row.occurrences || []).length} lần xuất hiện`, `${(row.occurrences || []).length} occurrence${(row.occurrences || []).length === 1 ? "" : "s"}`);
    status = row.status;
  } else if (kind === "entities") {
    Icon = Ic.users;
    title = row.canonical_source || uiText("(thực thể chưa đặt tên)", "(unnamed entity)");
    target = row.canonical_target || row.entity_type || uiText("Chưa phân giải", "Unresolved");
    meta = uiText(`${(row.mentions || []).length} lần nhắc`, `${(row.mentions || []).length} mention${(row.mentions || []).length === 1 ? "" : "s"}`);
  } else if (kind === "relations") {
    Icon = Ic.layers;
    title = inspectorEntityLabel(row.source_entity_id, entityMap);
    target = inspectorEntityLabel(row.target_entity_id, entityMap);
    meta = row.state_label || row.relation_type || "relation";
  } else {
    Icon = Ic.doc;
    title = row.chapter_title || row.title || row.chapter_id || uiText("Tóm tắt chương", "Chapter summary");
    target = row.summary_source || row.summary_target || uiText("Chưa viết tóm tắt", "Summary not written");
    meta = row.source || uiText("chương", "chapter");
  }

  return (
    <button className="ci-row wb-record-row" type="button" onClick={() => onSelect(inspectorKey(kind, row, index))}>
      <span className="ci-row-icon"><Icon size={13} /></span>
      <span className="ci-row-main">
        <b className={kind === "glossary" ? "mono" : ""}>{title}</b>
        <span>{kind === "relations" && <Ic.arrowRight size={10} />}{target}</span>
      </span>
      <span className="ci-row-side">
        {status ? <StatusPill status={status} /> : <em>{meta}</em>}
        {status && <em>{meta}</em>}
      </span>
      <Ic.chevRight size={11} className="ci-row-caret" />
    </button>
  );
}

function InspectorDetail({ kind, row, entityMap, onBack, onFocusTerm, canManage, onManage }) {
  if (!row) return null;
  const occurrenceBlocks = kind === "glossary"
    ? (row.occurrences || []).map(item => item.block_id)
    : kind === "entities"
      ? (row.mentions || []).map(item => item.block_id)
      : [];
  const evidenceBlocks = kind === "relations"
    ? (row.evidence || []).map(item => item.block_id || item.trigger_block_id || item.source_block_id)
    : [];

  return (
    <div className="ci-detail wb-detail">
      <div className="ci-detail-head wb-section-title">
        <button className="btn icon-only tip" type="button" data-tip={uiText("Về danh sách bản ghi", "Back to records")} aria-label={uiText("Về danh sách bản ghi", "Back to records")} onClick={onBack}>
          <Ic.chevRight size={13} style={{ transform: "rotate(180deg)" }} />
        </button>
        <div>
          <span>{kind === "glossary" ? uiText("Thuật ngữ", "Glossary term") : kind === "entities" ? uiText("Thực thể", "Entity") : kind === "relations" ? uiText("Quan hệ", "Relation") : uiText("Tóm tắt chương", "Chapter summary")}</span>
          <b>{kind === "glossary" ? row.source_term : kind === "entities" ? row.canonical_source : kind === "relations" ? row.relation_type || "Relation" : row.chapter_id}</b>
        </div>
        <span className="ci-detail-spacer" />
        {kind === "glossary" && (row.occurrences || []).length > 0 && (
          <button className="btn sm" type="button" onClick={() => onFocusTerm?.(row.term_id, null, { toggle: false })}>
            <Ic.search size={11} />{uiText("Định vị", "Locate")}
          </button>
        )}
        {canManage && <button className="btn sm" type="button" onClick={onManage}><Ic.pencil size={11} />{uiText("Quản lý", "Manage")}</button>}
      </div>

      <div className="ci-detail-body">
        {kind === "glossary" && (
          <>
            <div className="ci-title-pair">
              <strong className="mono">{row.source_term || "-"}</strong>
              <Ic.arrowRight size={13} />
              <strong>{row.expected_target || uiText("Cần bản đích", "Target needed")}</strong>
            </div>
            <div className="ci-field-grid">
              <InspectorField label={uiText("Trạng thái", "Status")}><StatusPill status={row.status} /></InspectorField>
              <InspectorField label={uiText("Phạm vi", "Scope")}>{row.chapter_scope || row.scope || "global"}</InspectorField>
              <InspectorField label={uiText("Độ tin cậy", "Confidence")}>{Number(row.confidence || 0).toFixed(2)}</InspectorField>
              <InspectorField label={uiText("Lần xuất hiện", "Occurrences")}>{(row.occurrences || []).length}</InspectorField>
              <InspectorField label={uiText("Biến thể được phép", "Allowed variants")} wide><InspectorChips values={row.allowed_variants} /></InspectorField>
              <InspectorField label={uiText("Biến thể bị cấm", "Forbidden variants")} wide><InspectorChips values={row.forbidden_variants} /></InspectorField>
              <InspectorField label={uiText("Block bằng chứng", "Evidence blocks")} wide><InspectorChips values={occurrenceBlocks} /></InspectorField>
              <InspectorField label={uiText("ID bản ghi", "Record id")} wide><span className="mono">{row.term_id}</span></InspectorField>
            </div>
          </>
        )}

        {kind === "entities" && (
          <>
            <div className="ci-title-pair">
              <strong>{row.canonical_source || "-"}</strong>
              <Ic.arrowRight size={13} />
              <strong>{row.canonical_target || uiText("Cần bản đích", "Target needed")}</strong>
            </div>
            <div className="ci-field-grid">
              <InspectorField label={uiText("Loại", "Type")}>{row.entity_type || uiText("không rõ", "unknown")}</InspectorField>
              <InspectorField label={uiText("Độ tin cậy", "Confidence")}>{Number(row.confidence || 0).toFixed(2)}</InspectorField>
              <InspectorField label={uiText("Lần nhắc", "Mentions")}>{(row.mentions || []).length}</InspectorField>
              <InspectorField label={uiText("Quy tắc đại từ", "Pronoun policy")}>{row.pronoun_policy || "-"}</InspectorField>
              <InspectorField label={uiText("Bí danh nguồn", "Source aliases")} wide><InspectorChips values={row.aliases_source} /></InspectorField>
              <InspectorField label={uiText("Bí danh đích", "Target aliases")} wide><InspectorChips values={row.aliases_target} /></InspectorField>
              <InspectorField label={uiText("Block có lần nhắc", "Mention blocks")} wide><InspectorChips values={occurrenceBlocks} /></InspectorField>
              <InspectorField label={uiText("ID bản ghi", "Record id")} wide><span className="mono">{row.entity_id}</span></InspectorField>
            </div>
          </>
        )}

        {kind === "relations" && (
          <>
            <div className="ci-title-pair">
              <strong>{inspectorEntityLabel(row.source_entity_id, entityMap)}</strong>
              <Ic.arrowRight size={13} />
              <strong>{inspectorEntityLabel(row.target_entity_id, entityMap)}</strong>
            </div>
            <div className="ci-field-grid">
              <InspectorField label={uiText("Quan hệ", "Relation")}>{row.relation_type || "-"}</InspectorField>
              <InspectorField label={uiText("Trạng thái", "State")}>{row.state_label || "-"}</InspectorField>
              <InspectorField label={uiText("Hiệu lực từ", "Valid from")}>{row.valid_from_block_id || "-"}</InspectorField>
              <InspectorField label={uiText("Hiệu lực đến", "Valid until")}>{row.valid_to_block_id || "-"}</InspectorField>
              <InspectorField label={uiText("Kích hoạt", "Trigger")}>{row.trigger_event_id || "-"}</InspectorField>
              <InspectorField label={uiText("Độ tin cậy", "Confidence")}>{Number(row.confidence || 0).toFixed(2)}</InspectorField>
              <InspectorField label={uiText("Block bằng chứng", "Evidence blocks")} wide><InspectorChips values={evidenceBlocks} /></InspectorField>
              <InspectorField label={uiText("Ghi chú", "Notes")} wide>{row.notes}</InspectorField>
              <InspectorField label={uiText("ID bản ghi", "Record id")} wide><span className="mono">{row.relation_id}</span></InspectorField>
            </div>
          </>
        )}

        {kind === "summaries" && (
          <>
            <div className="ci-summary-copy">{row.summary_source || uiText("Chưa viết tóm tắt.", "Summary not written.")}</div>
            <div className="ci-field-grid">
              <InspectorField label={uiText("Chương", "Chapter")}>{row.chapter_id || "-"}</InspectorField>
              <InspectorField label={uiText("Nguồn", "Source")}>{row.source || "-"}</InspectorField>
              <InspectorField label={uiText("Bối cảnh", "Setting")}>{row.setting || "-"}</InspectorField>
              <InspectorField label={uiText("Sắc thái", "Tone")}>{row.emotional_tone || "-"}</InspectorField>
              <InspectorField label={uiText("Mô-típ", "Motifs")} wide><InspectorChips values={row.motifs} /></InspectorField>
              <InspectorField label={uiText("Sự kiện chính", "Key events")} wide><InspectorChips values={row.key_events} limit={20} /></InspectorField>
              <InspectorField label={uiText("Mạch còn mở", "Open threads")} wide><InspectorChips values={row.open_threads} limit={20} /></InspectorField>
              <InspectorField label={uiText("Tóm tắt đích", "Target summary")} wide>{row.summary_target}</InspectorField>
              <InspectorField label={uiText("Ghi chú dịch", "Translation notes")} wide>{row.translation_notes}</InspectorField>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function EvalOnlyTab({ evalOnly }) {
  const gold = evalOnly?.gold_glossary || [];
  const refs = evalOnly?.references || [];
  if (!gold.length && !refs.length) {
    return <Empty icon={Ic.lock} text={uiText("DB này không có bản ghi eval-only.", "No eval-only records in this DB.")} sub={uiText("Đây là trạng thái bình thường với runtime memory của Treasure Island.", "This is normal for Treasure Island runtime memory.")} />;
  }
  return (
    <div className="tab-body">
      <div className="ref-explain">
        <Ic.lock size={12} />
        <span><b>Eval-only.</b> {uiText("Các hàng này có thể đọc để audit/chấm điểm nhưng tách biệt về cấu trúc khỏi runtime memory.", "These rows are readable for audit/scoring but are structurally separate from runtime memory.")}</span>
      </div>
      {!!gold.length && (
        <div className="eval-list">
          <div className="eval-title">{uiText("Thuật ngữ vàng", "Gold glossary")}</div>
          {gold.slice(0, 80).map(row => (
            <div className="eval-row" key={row.gold_id || `${row.source_term}:${row.target_term}`}>
              <span className="mono">{row.source_term}</span>
              <Ic.arrowRight size={11} />
              <b>{row.target_term}</b>
              <span className="pill pill-amber">eval-only</span>
            </div>
          ))}
          {gold.length > 80 && <div className="eval-more mono">+{gold.length - 80} {uiText("hàng nữa", "more rows")}</div>}
        </div>
      )}
      {!!refs.length && (
        <div className="eval-list">
          <div className="eval-title">{uiText("Reference thủ công", "Manual references")}</div>
          {refs.slice(0, 20).map(row => (
            <div className="eval-ref" key={row.reference_id}>
              <div className="mono">{row.block_id}</div>
              <div>{row.reference_vi || row.target_text || ""}</div>
              <span className="pill pill-amber">eval-only</span>
            </div>
          ))}
          {refs.length > 20 && <div className="eval-more mono">+{refs.length - 20} {uiText("hàng nữa", "more rows")}</div>}
        </div>
      )}
    </div>
  );
}

const CONTEXT_TABS = [
  { id: "glossary", vi: "Thuật ngữ", en: "Terms", icon: Ic.tag },
  { id: "entities", vi: "Thực thể", en: "Entities", icon: Ic.users },
  { id: "relations", vi: "Quan hệ", en: "Relations", icon: Ic.layers },
  { id: "summaries", vi: "Tóm tắt", en: "Summary", icon: Ic.doc },
];

const UTILITY_TABS = [
  { id: "notes", vi: "Ghi chú", en: "Notes", icon: Ic.doc },
  { id: "reference", vi: "Reference", en: "Reference", icon: Ic.book },
  { id: "eval_only", vi: "Chỉ eval", en: "Eval-only", icon: Ic.lock },
  { id: "validate", vi: "Kiểm tra", en: "Validate", icon: Ic.checkCircle },
  { id: "progress", vi: "Tiến độ", en: "Progress", icon: Ic.layers },
];

function inspectorTabLabel(tab) {
  return tab ? uiText(tab.vi, tab.en) : "";
}

function InspectorUtility({ id, ctx }) {
  if (id === "notes") return <NotesTab block={ctx.block} onUpdateBlockNotes={ctx.onUpdateBlockNotes} />;
  if (id === "reference") {
    return <ReferenceTab key={ctx.block.block_id} refs={ctx.references} block={ctx.block} onUpdateReference={ctx.onUpdateReference}
      onCreateReference={ctx.onCreateReference} onSaveDraft={ctx.onSaveDraft} onMarkReviewed={ctx.onMarkReviewedReference}
      onLockReference={ctx.onLockReference} />;
  }
  if (id === "eval_only") return <EvalOnlyTab evalOnly={ctx.evalOnly} />;
  if (id === "validate") {
    return <ValidateTab errors={ctx.errors} docInfo={ctx.docInfo} schemaMigrating={ctx.schemaMigrating}
      onMigrateSchema={ctx.onMigrateSchema} onJump={ctx.onJump} />;
  }
  return <ProgressTab stats={ctx.stats} freezeReasons={ctx.freezeReasons} history={ctx.history} />;
}

function InspectorManager({ kind, records, ctx, onBack }) {
  return (
    <div className="ci-manage">
      <div className="ci-subview-head">
        <button className="btn icon-only tip" type="button" data-tip={uiText("Về trình kiểm tra", "Back to inspector")} aria-label={uiText("Về trình kiểm tra", "Back to inspector")} onClick={onBack}>
          <Ic.chevRight size={13} style={{ transform: "rotate(180deg)" }} />
        </button>
        <div><span>{uiText("Quản lý block hiện tại", "Manage current block")}</span><b>{inspectorTabLabel(CONTEXT_TABS.find(tab => tab.id === kind))}</b></div>
      </div>
      <div className="ci-subview-body">
        {kind === "glossary" && <GlossaryTab terms={records} onDeleteTerm={ctx.onDeleteTerm} onUpdateTerm={ctx.onUpdateTerm} onFocusTerm={ctx.onFocusTerm} />}
        {kind === "entities" && <EntitiesTab entities={records} allEntities={ctx.allEntities} block={ctx.block} onUpdateEntity={ctx.onUpdateEntity}
          onUpdateDiscourse={ctx.onUpdateDiscourse} onDeleteEntity={ctx.onDeleteEntity} />}
        {kind === "relations" && <RelationsTab relations={records} entities={ctx.allEntities} block={ctx.block}
          onCreateRelation={ctx.onCreateRelation} onUpdateRelation={ctx.onUpdateRelation} onDeleteRelation={ctx.onDeleteRelation} />}
        {kind === "summaries" && <SummaryTab summary={records[0] || ctx.summary} entities={ctx.allEntities} onUpdateSummary={ctx.onUpdateSummary} />}
      </div>
    </div>
  );
}

function RightPanel({ openTabs, onToggleTab, counts, ctx, expanded, onToggleExpanded }) {
  const safeOpenTabs = openTabs || [];
  const requestedTab = safeOpenTabs[safeOpenTabs.length - 1] || "glossary";
  const normalizedRequested = requestedTab === "summary" ? "summaries" : requestedTab;
  const requestedIsContext = CONTEXT_TABS.some(tab => tab.id === normalizedRequested);
  const requestedIsUtility = UTILITY_TABS.some(tab => tab.id === normalizedRequested);
  const [scope, setScope] = React.useState("current");
  const [activeKind, setActiveKind] = React.useState(requestedIsContext ? normalizedRequested : "glossary");
  const [utility, setUtility] = React.useState(requestedIsUtility ? normalizedRequested : null);
  const [toolsOpen, setToolsOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [selectedKey, setSelectedKey] = React.useState(null);
  const [manage, setManage] = React.useState(false);
  const [visibleLimit, setVisibleLimit] = React.useState(100);

  React.useEffect(() => {
    if (requestedIsContext) {
      setActiveKind(normalizedRequested);
      setUtility(null);
      setManage(false);
    } else if (requestedIsUtility) {
      setUtility(normalizedRequested);
      setManage(false);
    }
  }, [normalizedRequested, requestedIsContext, requestedIsUtility]);

  React.useEffect(() => {
    setSelectedKey(null);
    setManage(false);
    setVisibleLimit(100);
  }, [scope, activeKind, ctx.block?.block_id, ctx.currentScopeKind]);

  React.useEffect(() => {
    setVisibleLimit(100);
  }, [query]);

  const currentMemory = ctx.currentMemory || {
    glossary: ctx.terms || [],
    entities: ctx.entities || [],
    relations: ctx.relations || [],
    summaries: ctx.summary ? [ctx.summary] : [],
  };
  const projectMemory = ctx.projectMemory || currentMemory;
  const memory = scope === "project" ? projectMemory : currentMemory;
  const records = memory[activeKind] || [];
  const entityMap = React.useMemo(() => {
    const map = {};
    (projectMemory.entities || []).forEach(entity => { map[entity.entity_id] = entity; });
    return map;
  }, [projectMemory.entities]);
  const filtered = React.useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return records;
    return records.filter(row => inspectorSearchText(activeKind, row, entityMap).toLocaleLowerCase().includes(needle));
  }, [records, query, activeKind, entityMap]);
  const selected = selectedKey == null
    ? null
    : records.find((row, index) => inspectorKey(activeKind, row, index) === selectedKey) || null;
  const canManage = !ctx.readOnly && scope === "current" && ctx.currentScopeKind === "block";
  const currentScopeTitle = ctx.currentScopeKind === "chapter"
    ? uiText("Chương hiện tại", "Current chapter")
    : ctx.currentScopeKind === "book"
      ? uiText("Sách hiện tại", "Current book")
      : uiText("Block hiện tại", "Current block");
  const totals = ctx.memoryTotals || {
    glossary: (projectMemory.glossary || []).length,
    entities: (projectMemory.entities || []).length,
    relations: (projectMemory.relations || []).length,
  };

  function selectKind(kind) {
    setActiveKind(kind);
    setUtility(null);
    setToolsOpen(false);
    setSelectedKey(null);
    setManage(false);
    onToggleTab?.(kind === "summaries" ? "summary" : kind);
  }

  function openUtility(id) {
    setUtility(id);
    setToolsOpen(false);
    setSelectedKey(null);
    setManage(false);
    onToggleTab?.(id);
  }

  return (
    <div className={"col col-right context-inspector wb-operational" + (expanded ? " is-expanded" : "")}>
      <div className="ci-header wb-toolbar">
        <div className="ci-heading">
          <span>{uiText("Trình kiểm tra ngữ cảnh", "Context inspector")}</span>
          <b title={scope === "project" ? (ctx.docInfo?.metadata?.title || ctx.docInfo?.doc_id) : ctx.currentScopeLabel}>
            {scope === "project" ? uiText("Toàn dự án", "Whole project") : (ctx.currentScopeLabel || currentScopeTitle)}
          </b>
        </div>
        <div className="ci-header-counts" aria-label={uiText("Tổng memory dự án", "Project memory totals")}>
          <span><b>{totals.glossary || 0}</b>T</span>
          <span><b>{totals.entities || 0}</b>E</span>
          <span><b>{totals.relations || 0}</b>R</span>
        </div>
        <button className={"btn icon-only tip tip-left" + (toolsOpen || utility ? " is-on" : "")} type="button"
          data-tip={uiText("Công cụ ngữ cảnh", "Context tools")} aria-label={uiText("Công cụ ngữ cảnh", "Context tools")} aria-expanded={toolsOpen} onClick={() => setToolsOpen(open => !open)}>
          <Ic.sliders size={13} />
        </button>
        <button className="btn icon-only tip tip-left" type="button" data-tip={uiText("Mở workspace Memory đầy đủ", "Open full Memory workspace")}
          aria-label={uiText("Mở workspace Memory đầy đủ", "Open full Memory workspace")} onClick={() => ctx.onOpenMemory?.(activeKind === "summaries" ? "summary" : activeKind)}>
          <Ic.book size={13} />
        </button>
        <button className={"btn icon-only tip tip-left" + (expanded ? " is-on" : "")} type="button"
          data-tip={expanded ? uiText("Khôi phục chiều rộng panel", "Restore panel width") : uiText("Mở rộng panel ngữ cảnh", "Expand context panel")} aria-label={expanded ? uiText("Khôi phục chiều rộng panel", "Restore panel width") : uiText("Mở rộng panel ngữ cảnh", "Expand context panel")}
          aria-pressed={!!expanded} onClick={onToggleExpanded}>
          <Ic.expand size={13} />
        </button>
      </div>

      {toolsOpen && (
        <div className="ci-tools wb-toolbar">
          {UTILITY_TABS.map(tab => {
            const Icon = tab.icon;
            const badge = counts?.[tab.id];
            return (
              <button key={tab.id} type="button" className={utility === tab.id ? "is-active" : ""} onClick={() => openUtility(tab.id)}>
                <Icon size={12} /><span>{inspectorTabLabel(tab)}</span>
                {badge?.text && <em className={badge.tone || ""}>{badge.text}</em>}
              </button>
            );
          })}
        </div>
      )}

      {utility ? (
        <div className="ci-subview">
          <div className="ci-subview-head wb-section-title">
            <button className="btn icon-only tip" type="button" data-tip={uiText("Về ngữ cảnh", "Back to context")} aria-label={uiText("Về ngữ cảnh", "Back to context")}
              onClick={() => { setUtility(null); onToggleTab?.(activeKind === "summaries" ? "summary" : activeKind); }}>
              <Ic.chevRight size={13} style={{ transform: "rotate(180deg)" }} />
            </button>
            <div>
              <span>{uiText("Công cụ ngữ cảnh", "Context tools")}</span>
              <b>{inspectorTabLabel(UTILITY_TABS.find(tab => tab.id === utility))}</b>
            </div>
          </div>
          <div className="ci-subview-body"><InspectorUtility id={utility} ctx={ctx} /></div>
        </div>
      ) : manage ? (
        <InspectorManager kind={activeKind} records={records} ctx={ctx} onBack={() => setManage(false)} />
      ) : (
        <>
          <div className="ci-scope wb-toolbar" role="group" aria-label={uiText("Phạm vi ngữ cảnh", "Context scope")}>
            <button className={scope === "current" ? "is-active" : ""} type="button" onClick={() => setScope("current")}>
              {currentScopeTitle}<span>{ctx.currentScopeLabel}</span>
            </button>
            <button className={scope === "project" ? "is-active" : ""} type="button" onClick={() => setScope("project")}>
              {uiText("Toàn dự án", "Whole project")}<span>{ctx.docInfo?.metadata?.title || ctx.docInfo?.doc_id}</span>
            </button>
          </div>

          <div className="ci-tabs wb-toolbar" role="tablist" aria-label={uiText("Loại bản ghi ngữ cảnh", "Context record type")}>
            {CONTEXT_TABS.map(tab => {
              const Icon = tab.icon;
              const tabCount = (memory[tab.id] || []).length;
              return (
                <button key={tab.id} type="button" role="tab" aria-selected={activeKind === tab.id}
                  className={activeKind === tab.id ? "is-active" : ""} onClick={() => selectKind(tab.id)}>
                  <Icon size={12} /><span>{inspectorTabLabel(tab)}</span><em>{tabCount}</em>
                </button>
              );
            })}
          </div>

          {selected ? (
            <InspectorDetail kind={activeKind} row={selected} entityMap={entityMap} onBack={() => setSelectedKey(null)}
              onFocusTerm={ctx.onFocusTerm} canManage={canManage} onManage={() => setManage(true)} />
          ) : (
            <>
              <div className="ci-toolbar wb-toolbar">
                <label className="ci-search">
                  <Ic.search size={12} />
                  <input value={query} onChange={event => setQuery(event.target.value)} placeholder={`${uiText("Tìm", "Search")} ${inspectorTabLabel(CONTEXT_TABS.find(tab => tab.id === activeKind)).toLocaleLowerCase() || uiText("ngữ cảnh", "context")}`} />
                  {query && <button type="button" aria-label={uiText("Xóa tìm kiếm", "Clear search")} onClick={() => setQuery("")}><Ic.x size={10} /></button>}
                </label>
                {canManage && (
                  <button className="btn sm" type="button" onClick={() => setManage(true)}><Ic.pencil size={11} />{uiText("Quản lý", "Manage")}</button>
                )}
              </div>
              <div className="ci-list-head wb-section-title">
                <span>{scope === "project" ? uiText("Toàn dự án", "Whole project") : currentScopeTitle}</span>
                <b>{uiText(`${filtered.length} bản ghi`, `${filtered.length} record${filtered.length === 1 ? "" : "s"}`)}</b>
              </div>
              <div className="ci-list wb-record-list">
                {!filtered.length ? (
                  <Empty icon={CONTEXT_TABS.find(tab => tab.id === activeKind)?.icon || Ic.doc}
                    text={uiText(`Không có bản ghi ${inspectorTabLabel(CONTEXT_TABS.find(tab => tab.id === activeKind)).toLocaleLowerCase() || "ngữ cảnh"} trong phạm vi này.`, `No ${inspectorTabLabel(CONTEXT_TABS.find(tab => tab.id === activeKind)).toLocaleLowerCase() || "context"} records in this scope.`)}
                    sub={scope === "current" ? uiText("Chuyển sang Toàn dự án để xem mọi bản ghi đã lưu.", "Switch to Whole project to inspect every stored record.") : uiText("Dự án này không có bản ghi đã lưu thuộc loại này.", "This project has no stored records of this type.")} />
                ) : (
                  <>
                    {filtered.slice(0, visibleLimit).map((row, index) => (
                      <InspectorRow key={inspectorKey(activeKind, row, index)} kind={activeKind} row={row} index={index}
                        entityMap={entityMap} onSelect={setSelectedKey} />
                    ))}
                    {filtered.length > visibleLimit && (
                      <button className="ci-more" type="button" onClick={() => setVisibleLimit(limit => limit + 100)}>
                        {uiText("Hiện thêm", "Show next")} {Math.min(100, filtered.length - visibleLimit)}<span>{visibleLimit} / {filtered.length}</span>
                      </button>
                    )}
                  </>
                )}
              </div>
            </>
          )}
        </>
      )}
      </div>
  );
}

window.RightPanel = RightPanel;
