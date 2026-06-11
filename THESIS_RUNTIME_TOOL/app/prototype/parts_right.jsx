/* ===== RIGHT PANEL: accordion of 6 tabs, multiple sections may stay open ===== */

function StatusPill({ status }) {
  const map = {
    locked: ["Locked", "pill-lock"],
    proposed: ["Proposed", "pill-amber"],
    candidate: ["Candidate", "pill-amber"],
    verified: ["Verified", "pill-green"],
    human_verified: ["Verified", "pill-green"],
    reviewed: ["Reviewed", "pill-green"],
    draft: ["Draft", "pill-amber"],
  };
  const [label, cls] = map[status] || [status || "Unset", "pill-grey"];
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
function GlossaryTab({ terms, onDeleteTerm, onUpdateTerm }) {
  const [expanded, setExpanded] = React.useState(terms[0]?.term_id);
  React.useEffect(() => {
    if (terms.length && !terms.some(t => t.term_id === expanded)) setExpanded(terms[0].term_id);
  }, [terms, expanded]);

  if (!terms.length) {
    return <Empty icon={Ic.tag} text="No glossary terms for this block." sub="Select text in Clean text -> Add glossary term." />;
  }

  return (
    <div className="tab-body">
      {terms.map(t => {
        const open = expanded === t.term_id;
        return (
          <div key={t.term_id} className={"card" + (open ? " open" : "")}>
            <button className="card-head" onClick={() => setExpanded(open ? null : t.term_id)}>
              <Ic.chevRight size={11} className="card-caret" style={{ transform: open ? "rotate(90deg)" : "none" }} />
              <span className="card-title mono">{t.source_term || "(new term)"}</span>
              <span className="card-arrow"><Ic.arrowRight size={11} /></span>
              <span className="card-target">{t.expected_target || "target needed"}</span>
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
                  <button className="card-del tip tip-left" data-tip="Delete term" onClick={() => onDeleteTerm(t)}><Ic.trash size={12} /></button>
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
    return <Empty icon={Ic.users} text="No entities mentioned in this block." sub="Select text in Clean text -> Add entity mention." />;
  }

  const isDialogue = block.block_type === "dialogue";
  return (
    <div className="tab-body">
      {isDialogue && (
        <div className="discourse">
          <div className="discourse-head"><Ic.quote size={11} />dialogue · speaker / addressee</div>
          <div className="discourse-row">
            <DiscSelect label="speaker" entities={allEntities} value={block.discourse?.speaker_entity_id || ""}
              onChange={value => onUpdateDiscourse({ speaker_entity_id: value })} />
            <DiscSelect label="addressee" entities={allEntities} value={block.discourse?.addressee_entity_id || ""}
              onChange={value => onUpdateDiscourse({ addressee_entity_id: value })} />
          </div>
        </div>
      )}

      {!entities.length && <Empty icon={Ic.users} text="No entity mentions in this block yet." sub="Dialogue speaker/addressee can still be set above." />}

      {entities.map(e => {
        const open = expanded === e.entity_id;
        const mentions = (e.mentions || []).filter(m => m.block_id === block.block_id);
        return (
          <div key={e.entity_id} className={"card" + (open ? " open" : "")}>
            <button className="card-head" onClick={() => setExpanded(open ? null : e.entity_id)}>
              <Ic.chevRight size={11} className="card-caret" style={{ transform: open ? "rotate(90deg)" : "none" }} />
              <span className={"ent-type ent-" + e.entity_type}>{(e.entity_type || "?")[0]}</span>
              <span className="card-title">{e.canonical_source || "(new entity)"}</span>
              <span className="card-arrow"><Ic.arrowRight size={11} /></span>
              <span className="card-target">{e.canonical_target || "target needed"}</span>
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
                  <MiniField label="mentions in this block">
                    {mentions.map((m, i) => <span key={i} className="var mono">"{m.surface}" [{m.span[0]},{m.span[1]}]</span>)}
                  </MiniField>
                )}
                <div className="card-meta-row">
                  <span className="lockfield"><span className="lf-k">type</span><span className="lf-v">{e.entity_type || "-"}</span></span>
                  <span className="lockfield"><span className="lf-k">conf</span><span className="lf-v">{Number(e.confidence || 0).toFixed(2)}</span></span>
                  <span className="lockfield"><span className="lf-k">mentions</span><span className="lf-v">{(e.mentions || []).length}</span></span>
                  <button className="card-del tip tip-left" data-tip="Delete entity" onClick={() => onDeleteEntity(e)}><Ic.trash size={12} /></button>
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
        <FormField label="confidence">
          <input type="number" min="0" max="1" step="0.01" value={confidenceValue(safe.confidence)}
            onChange={e => update({ confidence: e.target.value === "" ? 0 : Number(e.target.value) })} />
        </FormField>
      </div>

      <MiniField label="address policy">
        <div className="form-grid">
          <FormField label="source self">
            <input value={policy.source_to_target?.self_term || ""} placeholder="toi / ta / ong..." onChange={e => updatePolicy("source_to_target", "self_term", e.target.value)} />
          </FormField>
          <FormField label="source calls target">
            <input value={policy.source_to_target?.address_term || ""} placeholder="ban / chau / nguoi..." onChange={e => updatePolicy("source_to_target", "address_term", e.target.value)} />
          </FormField>
          <FormField label="target self">
            <input value={policy.target_to_source?.self_term || ""} placeholder="toi / chau / em..." onChange={e => updatePolicy("target_to_source", "self_term", e.target.value)} />
          </FormField>
          <FormField label="target calls source">
            <input value={policy.target_to_source?.address_term || ""} placeholder="ban / ong / anh..." onChange={e => updatePolicy("target_to_source", "address_term", e.target.value)} />
          </FormField>
        </div>
      </MiniField>

      <div className="form-grid">
        <FormField label="state_label">
          <input value={safe.state_label || ""} placeholder="before_betrayal / default" onChange={e => update({ state_label: e.target.value })} />
        </FormField>
        <FormField label="trigger_event_id">
          <input value={safe.trigger_event_id || ""} placeholder="optional event label" onChange={e => update({ trigger_event_id: e.target.value })} />
        </FormField>
        <FormField label="valid_from_block_id">
          <input value={safe.valid_from_block_id || ""} placeholder={block?.block_id || "optional"} onChange={e => update({ valid_from_block_id: e.target.value })} />
        </FormField>
        <FormField label="valid_to_block_id">
          <input value={safe.valid_to_block_id || ""} placeholder="optional" onChange={e => update({ valid_to_block_id: e.target.value })} />
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
          {isNew ? "Add relation" : "Save relation"}
        </button>
        {!isNew && <button className="btn sm danger" onClick={onDelete}><Ic.trash size={12} />Delete</button>}
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
        <span><b>Document-level.</b> Relations connect two entities for Vietnamese address policy. They are not limited to the current block.</span>
      </div>

      {!safeRelations.length && <Empty icon={Ic.users} text="No entity relations in this document yet." sub="Use Add relation below or apply an annotation drafter candidate." />}

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
              {activeForBlock && <span className="pill pill-green">current dialogue</span>}
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
                    <span>One side could not be resolved from entities.jsonl; raw entity id is shown.</span>
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
                        {item.block_id}{item.surface ? `: "${item.surface}"` : ""}
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
          <span className="card-title">Add relation</span>
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
        <span><b>Chapter-level.</b> This summary applies to every block in this chapter; it only changes when the active block moves to another chapter.</span>
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
        {!entities.length ? <span className="faint">No entities available yet.</span> : entities.map(e => {
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
        <span><b>Block-level soft context.</b> These notes help interpretation and translation review, but they are advisory and not hard links like entities or glossary terms.</span>
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
        <span><b>Block-level.</b> Current block: <span className="mono">{block.block_id}</span>. Draft stays in working state; only <b>Reviewed</b> or <b>Locked</b> references are freeze-eligible.</span>
      </div>
      {!blockRef ? (
        <div className="ref-card status-draft">
          <div className="ref-card-head">
            <span className="ref-stratum mono">new draft</span>
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
            <button className="btn sm primary" onClick={() => onCreateReference(block.block_id, newReference)}><Ic.book size={12} />Save draft</button>
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
            <button className="btn sm" disabled={blockRef.status === "locked"} onClick={() => onSaveDraft(blockRef.reference_id)}>Save draft</button>
            <button className="btn sm" disabled={blockRef.status === "locked"} onClick={() => onMarkReviewed(blockRef.reference_id)}><Ic.checkCircle size={12} />Mark reviewed</button>
            <button className="btn sm primary" disabled={blockRef.status !== "reviewed"} onClick={() => onLockReference(blockRef.reference_id)}><Ic.lock size={12} />Lock</button>
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
            <div className="schema-migrate-title"><Ic.layers size={12} />Schema upgrade available</div>
            <div className="schema-migrate-sub">
              Current project is <span className="mono">{docInfo?.schema_version || "unknown"}</span>. Upgrade writes <span className="mono">schema_version=1.5.0</span> and creates empty <span className="mono">entity_relations.jsonl</span>; it does not re-extract or touch annotations/drafts.
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
              {e.block_id && <span className="val-jump">jump <Ic.arrowRight size={11} /></span>}
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
        <span><Ic.clock size={12} />History</span>
        <span className="hist-actions mono">
          {history?.can_undo ? "undo ready" : "no undo"} · {history?.can_redo ? "redo ready" : "no redo"}
        </span>
      </div>
      {!recent.length ? (
        <div className="hist-empty">No undoable changes yet.</div>
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

const TABS = [
  { id: "glossary", label: "Glossary", icon: Ic.tag },
  { id: "entities", label: "Entities", icon: Ic.users },
  { id: "relations", label: "Relations", icon: Ic.users },
  { id: "summary", label: "Summary", icon: Ic.doc },
  { id: "notes", label: "Notes", icon: Ic.doc },
  { id: "reference", label: "Reference", icon: Ic.book },
  { id: "validate", label: "Validate", icon: Ic.checkCircle },
  { id: "progress", label: "Progress", icon: Ic.layers },
];

function RightPanel({ openTabs, onToggleTab, counts, ctx }) {
  const openSet = new Set(openTabs || []);
  return (
    <div className="col col-right">
      <div className="rp-accordion">
        {TABS.map(t => {
          const open = openSet.has(t.id);
          const I = t.icon;
          const badge = counts[t.id];
          return (
            <div key={t.id} className={"rp-sec" + (open ? " open" : "")}>
              <button className="rp-head" onClick={() => onToggleTab(t.id)} aria-expanded={open}>
                <Ic.chevRight size={12} className="rp-caret" style={{ transform: open ? "rotate(90deg)" : "none" }} />
                <I size={13} className="rp-ic" />
                <span className="rp-label">{t.label}</span>
                <span className="rp-spacer" />
                {badge != null && badge.text && (
                  <span className={"rp-badge" + (badge.tone ? " " + badge.tone : "")}>{badge.text}</span>
                )}
              </button>
              {open && (
                <div className="rp-content">
                  {t.id === "glossary" && <GlossaryTab terms={ctx.terms} onDeleteTerm={ctx.onDeleteTerm} onUpdateTerm={ctx.onUpdateTerm} />}
                  {t.id === "entities" && <EntitiesTab entities={ctx.entities} allEntities={ctx.allEntities} block={ctx.block} onUpdateEntity={ctx.onUpdateEntity} onUpdateDiscourse={ctx.onUpdateDiscourse} onDeleteEntity={ctx.onDeleteEntity} />}
                  {t.id === "relations" && <RelationsTab relations={ctx.relations} entities={ctx.allEntities} block={ctx.block}
                    onCreateRelation={ctx.onCreateRelation} onUpdateRelation={ctx.onUpdateRelation} onDeleteRelation={ctx.onDeleteRelation} />}
                  {t.id === "summary" && <SummaryTab summary={ctx.summary} entities={ctx.allEntities} onUpdateSummary={ctx.onUpdateSummary} />}
                  {t.id === "notes" && <NotesTab block={ctx.block} onUpdateBlockNotes={ctx.onUpdateBlockNotes} />}
                  {t.id === "reference" && <ReferenceTab key={ctx.block.block_id} refs={ctx.references} block={ctx.block} onUpdateReference={ctx.onUpdateReference} onCreateReference={ctx.onCreateReference} onSaveDraft={ctx.onSaveDraft} onMarkReviewed={ctx.onMarkReviewedReference} onLockReference={ctx.onLockReference} />}
                  {t.id === "validate" && <ValidateTab errors={ctx.errors} docInfo={ctx.docInfo} schemaMigrating={ctx.schemaMigrating} onMigrateSchema={ctx.onMigrateSchema} onJump={ctx.onJump} />}
                  {t.id === "progress" && <ProgressTab stats={ctx.stats} freezeReasons={ctx.freezeReasons} history={ctx.history} />}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

window.RightPanel = RightPanel;
