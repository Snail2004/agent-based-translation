/* ============================================================
   parts_graph.jsx — Story Bible graph view
   Drop-in for the existing prototype folder. Loaded with:
     <script type="text/babel" src="parts_graph.jsx"></script>
   No import/export, no npm, no build step. React/ReactDOM are
   globals; the graph is hand-rolled inline SVG with a plain-JS
   layout. All copy goes through uiText(vi, en).
   ============================================================ */

/* The host app already defines uiText(). Guarded fallback only keeps
   isolated static previews readable; production locale is host-owned. */
if (typeof window.uiText !== "function") {
  window.BIBLE_LANG = "vi";
  window.uiText = function (vi, en) {
    return window.BIBLE_LANG === "en" ? en : vi;
  };
}

var BIBLE_KIN = {
  parent_of: 1, sibling_of: 1, spouse_of: 1,
  widow_of: 1, other_kin: 1, mother_of: 1, father_of: 1
};

var BIBLE_W = 980;
var BIBLE_H = 660;
var BIBLE_EMPTY_DATA = Object.freeze({ nodes: [], edges: [], pending: [] });

function biblePositiveInt(value) {
  return Number.isInteger(+value) && +value > 0;
}

function bibleChapterNumber(value) {
  if (biblePositiveInt(value)) return +value;
  var match = String(value || "").match(/(?:^|_)ch(\d+)(?:_|$)/i);
  return match && biblePositiveInt(match[1]) ? +match[1] : 0;
}

function bibleStringList(value) {
  var values = Array.isArray(value) ? value : (value == null ? [] : [value]);
  return values.map(function (item) { return String(item || "").trim(); })
    .filter(Boolean);
}

function bibleClaimMap(value) {
  var source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  var mapped = {};
  Object.keys(source).forEach(function (key) {
    var values = bibleStringList(source[key]);
    if (values.length) mapped[key] = values;
  });
  return mapped;
}

function mapLiteraryB4StoryGraph(value) {
  var allowedSchemas = {
    literary_b4_ui_story_graph_v1: true,
    literary_b4_ui_story_graph_v2: true
  };
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      !allowedSchemas[value.schema_version] ||
      !Array.isArray(value.nodes) || !Array.isArray(value.edges) ||
      !Array.isArray(value.pending)) {
    return { valid: false, code: "literary_b4_story_graph_shape_invalid", data: BIBLE_EMPTY_DATA };
  }

  var chapter = bibleChapterNumber(value.chapter_id || value.chapter_order);
  var ids = Object.create(null), labels = Object.create(null), mappedNodes = [];
  for (var i = 0; i < value.nodes.length; i++) {
    var sourceNode = value.nodes[i];
    var id = String(sourceNode && sourceNode.node_id || "").trim();
    var name = String(sourceNode && sourceNode.label || "").trim();
    var chapters = bibleStringList(sourceNode && sourceNode.member_chapters)
      .map(bibleChapterNumber).filter(biblePositiveInt);
    chapters = Array.from(new Set(chapters)).sort(function (a, b) { return a - b; });
    var first = bibleChapterNumber(sourceNode && sourceNode.established_in_chapter) ||
      (chapters.length ? chapters[0] : 0);
    if (!id || !name || ids[id] || !first || !chapters.length) {
      return { valid: false, code: "literary_b4_story_graph_node_invalid", data: BIBLE_EMPTY_DATA };
    }
    ids[id] = true;
    labels[id] = name;
    mappedNodes.push({
      id: id,
      name: name,
      kind: String(sourceNode.referent_kind || sourceNode.kind || "unknown"),
      rc: String(sourceNode.record_class || "unknown"),
      ch: chapters,
      first: first,
      surf: bibleStringList(sourceNode.surface_forms).length
        ? bibleStringList(sourceNode.surface_forms)
        : [name],
      cl: bibleClaimMap(sourceNode.claims),
      source_node_id: id,
      effective_entity_id: String(sourceNode.effective_entity_id || id)
    });
  }

  var mappedEdges = [];
  for (var edgeIndex = 0; edgeIndex < value.edges.length; edgeIndex++) {
    var sourceEdge = value.edges[edgeIndex];
    var sourceId = String(sourceEdge && sourceEdge.source_node_id || "").trim();
    var targetId = String(sourceEdge && sourceEdge.target_node_id || "").trim();
    var relation = String(sourceEdge && sourceEdge.relation || "").trim();
    var edgeChapter = bibleChapterNumber(
      sourceEdge && (sourceEdge.established_in_chapter || sourceEdge.chapter_id)
    );
    if (!sourceId || !targetId || sourceId === targetId || !ids[sourceId] ||
        !ids[targetId] || !relation || !edgeChapter) {
      return { valid: false, code: "literary_b4_story_graph_edge_invalid", data: BIBLE_EMPTY_DATA };
    }
    mappedEdges.push({
      id: String(sourceEdge.edge_id || ""),
      s: sourceId,
      t: targetId,
      r: relation,
      family: String(sourceEdge.relation_family || ""),
      note: String(sourceEdge.relation_note || sourceEdge.semantic_status || ""),
      ch: edgeChapter,
      c: !!sourceEdge.structurally_contested,
      contested_group_id: sourceEdge.contested_group_id || null,
      a: []
    });
  }

  var mappedPending = [];
  for (var pendingIndex = 0; pendingIndex < value.pending.length; pendingIndex++) {
    var sourcePending = value.pending[pendingIndex];
    var pendingChapter = bibleChapterNumber(
      sourcePending && (sourcePending.established_in_chapter || sourcePending.chapter_id)
    );
    var pendingKind = String(sourcePending && sourcePending.pending_kind || "").trim();
    var entityIds = bibleStringList(sourcePending && sourcePending.effective_entity_ids);
    if (!pendingChapter || !pendingKind ||
        entityIds.some(function (entityId) { return !ids[entityId]; })) {
      return { valid: false, code: "literary_b4_story_graph_pending_invalid", data: BIBLE_EMPTY_DATA };
    }
    mappedPending.push({
      id: String(sourcePending.pending_id || ""),
      ch: pendingChapter,
      q: pendingKind,
      cond: entityIds.map(function (entityId) { return labels[entityId]; }).join(" · "),
      entity_ids: entityIds
    });
  }

  return {
    valid: true,
    code: "",
    data: { nodes: mappedNodes, edges: mappedEdges, pending: mappedPending },
    meta: {
      schema_version: value.schema_version,
      book_id: String(value.book_id || ""),
      chapter_id: String(value.chapter_id || ""),
      chapter: chapter,
      artifact_hash: String(value.artifact_hash || ""),
      story_bible_artifact_hash: String(value.story_bible_artifact_hash || ""),
      provider_calls: value.provider_calls
    }
  };
}

function validateStoryBibleGraphData(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { valid: false, code: "story_bible_graph_missing", data: BIBLE_EMPTY_DATA, maxChapter: 1 };
  }
  if (!Array.isArray(value.nodes) || !Array.isArray(value.edges) || !Array.isArray(value.pending)) {
    return { valid: false, code: "story_bible_graph_shape_invalid", data: BIBLE_EMPTY_DATA, maxChapter: 1 };
  }

  var ids = Object.create(null), maxChapter = 1, i, row, chapter;
  for (i = 0; i < value.nodes.length; i++) {
    row = value.nodes[i];
    if (!row || typeof row !== "object" || !String(row.id || "").trim() ||
        !String(row.name || "").trim() || ids[row.id] || !biblePositiveInt(row.first) ||
        !Array.isArray(row.ch) || row.ch.some(function (item) { return !biblePositiveInt(item); })) {
      return { valid: false, code: "story_bible_graph_node_invalid", data: BIBLE_EMPTY_DATA, maxChapter: 1 };
    }
    ids[row.id] = true;
    maxChapter = Math.max(maxChapter, +row.first);
    for (chapter = 0; chapter < row.ch.length; chapter++) {
      maxChapter = Math.max(maxChapter, +row.ch[chapter]);
    }
  }

  for (i = 0; i < value.edges.length; i++) {
    row = value.edges[i];
    if (!row || typeof row !== "object" || !ids[row.s] || !ids[row.t] || row.s === row.t ||
        !String(row.r || "").trim() || !biblePositiveInt(row.ch) ||
        !(row.c === 0 || row.c === 1 || row.c === false || row.c === true) ||
        (row.a != null && !Array.isArray(row.a))) {
      return { valid: false, code: "story_bible_graph_edge_invalid", data: BIBLE_EMPTY_DATA, maxChapter: 1 };
    }
    maxChapter = Math.max(maxChapter, +row.ch);
  }

  for (i = 0; i < value.pending.length; i++) {
    row = value.pending[i];
    if (!row || typeof row !== "object" || !biblePositiveInt(row.ch) ||
        !String(row.q || "").trim() ||
        (row.cond != null && typeof row.cond !== "string") ||
        (row.entity_ids != null && !Array.isArray(row.entity_ids))) {
      return { valid: false, code: "story_bible_graph_pending_invalid", data: BIBLE_EMPTY_DATA, maxChapter: 1 };
    }
    maxChapter = Math.max(maxChapter, +row.ch);
  }

  return {
    valid: true,
    code: "",
    data: { nodes: value.nodes, edges: value.edges, pending: value.pending },
    maxChapter: maxChapter
  };
}

function validateStoryBibleChapterSnapshots(value) {
  if (value == null) return { valid: true, code: "", data: null, maxChapter: 0 };
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { valid: false, code: "story_bible_snapshot_set_invalid", data: null, maxChapter: 0 };
  }
  var chapters = Object.keys(value).map(function (key) { return +key; })
    .filter(biblePositiveInt).sort(function (a, b) { return a - b; });
  if (!chapters.length || chapters.length !== Object.keys(value).length) {
    return { valid: false, code: "story_bible_snapshot_set_invalid", data: null, maxChapter: 0 };
  }
  var maxChapter = chapters[chapters.length - 1], checked = {};
  for (var chapter = 1; chapter <= maxChapter; chapter++) {
    if (chapters[chapter - 1] !== chapter) {
      return { valid: false, code: "story_bible_snapshot_gap", data: null, maxChapter: 0 };
    }
    var result = validateStoryBibleGraphData(value[chapter]);
    if (!result.valid || result.maxChapter > chapter) {
      return {
        valid: false,
        code: result.valid ? "story_bible_snapshot_future_data" : result.code,
        data: null,
        maxChapter: 0
      };
    }
    checked[chapter] = result.data;
  }
  var layoutIds = Object.create(null);
  checked[maxChapter].nodes.forEach(function (node) { layoutIds[node.id] = true; });
  for (var snapshotChapter = 1; snapshotChapter <= maxChapter; snapshotChapter++) {
    if (checked[snapshotChapter].nodes.some(function (node) { return !layoutIds[node.id]; })) {
      return { valid: false, code: "story_bible_snapshot_layout_drift", data: null, maxChapter: 0 };
    }
  }
  return { valid: true, code: "", data: checked, maxChapter: maxChapter };
}

function bibleDegree(edges) {
  var deg = {}, i;
  for (i = 0; i < edges.length; i++) {
    deg[edges[i].s] = (deg[edges[i].s] || 0) + 1;
    deg[edges[i].t] = (deg[edges[i].t] || 0) + 1;
  }
  return deg;
}

function bibleNodeClass(kind) {
  if (kind === "person") return "is-person";
  if (kind === "place") return "is-place";
  return "is-other";
}

function biblePendingLabel(code) {
  var labels = {
    contested_relations: ["Quan hệ tranh chấp", "Contested relations"],
    pending_identity_cases: ["Danh tính cần đối chiếu", "Identity review"],
    pending_states: ["Trạng thái đang chờ", "Pending states"],
    unknowable_windows: ["Khoảng thời gian chưa thể biết", "Unknowable windows"],
    unresolved_address: ["Người được gọi chưa xác định", "Unresolved address"]
  };
  var pair = labels[code];
  return pair ? uiText(pair[0], pair[1]) : String(code || "").replace(/_/g, " ");
}

/* ------------------------------------------------------------------
   Layout. Deterministic spring/repulsion relaxation, run ONCE on the
   full graph. Positions are then frozen, so scrubbing the chapter
   only shows/hides nodes in place — it never re-shuffles them.
   ------------------------------------------------------------------ */
function bibleLayout(core, edges, W, H) {
  var i, j, k, a, b, dx, dy, d, d2, f, p, N = core.length;
  var deg = bibleDegree(edges);
  var idx = {}, P = [];

  for (i = 0; i < N; i++) {
    idx[core[i].id] = i;
    var ang = i * 2.39996323;                       // golden angle
    var rad = 0.45 + Math.sqrt((i + 0.5) / N);      // even area spread
    P.push({
      x: Math.cos(ang) * rad * 215,
      y: Math.sin(ang) * rad * 152,
      d: deg[core[i].id] || 0
    });
  }

  var links = [];
  for (k = 0; k < edges.length; k++) {
    var si = idx[edges[k].s], ti = idx[edges[k].t];
    if (si === undefined || ti === undefined || si === ti) continue;
    links.push({ a: si, b: ti, len: BIBLE_KIN[edges[k].r] ? 94 : 132 });
  }

  var IT = 620;
  for (var it = 0; it < IT; it++) {
    var t = 0.14 + 0.86 * (1 - it / IT);            // cooling

    for (i = 0; i < N; i++) {
      for (j = i + 1; j < N; j++) {
        a = P[i]; b = P[j];
        dx = a.x - b.x; dy = a.y - b.y;
        d2 = dx * dx + dy * dy;
        if (d2 < 0.05) { dx = (i % 2 ? 0.3 : -0.3); dy = 0.2; d2 = 0.13; }
        d = Math.sqrt(d2);
        f = (4300 + (a.d + b.d) * 330) / d2 * t;
        if (f > 11) f = 11;
        a.x += dx / d * f; a.y += dy / d * f;
        b.x -= dx / d * f; b.y -= dy / d * f;
        var minD = 56 + (a.d + b.d) * 1.15;         // hard separation
        if (d < minD) {
          p = (minD - d) * 0.2;
          a.x += dx / d * p; a.y += dy / d * p;
          b.x -= dx / d * p; b.y -= dy / d * p;
        }
      }
    }

    for (k = 0; k < links.length; k++) {
      a = P[links[k].a]; b = P[links[k].b];
      dx = b.x - a.x; dy = b.y - a.y;
      d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      f = (d - links[k].len) * 0.055 * t;
      a.x += dx / d * f; a.y += dy / d * f;
      b.x -= dx / d * f; b.y -= dy / d * f;
    }

    for (i = 0; i < N; i++) { P[i].x *= 0.9964; P[i].y *= 0.9938; }
  }

  /* frame it: scale + centre the finished cloud into the viewBox.
     The bottom-left band is kept clear for the pinned stat strip. */
  var minX = 1e9, maxX = -1e9, minY = 1e9, maxY = -1e9;
  for (i = 0; i < N; i++) {
    if (P[i].x < minX) minX = P[i].x;
    if (P[i].x > maxX) maxX = P[i].x;
    if (P[i].y < minY) minY = P[i].y;
    if (P[i].y > maxY) maxY = P[i].y;
  }
  var mL = 74, mR = 74, mT = 46, mB = 88;
  var s = Math.min(
    (W - mL - mR) / Math.max(1, maxX - minX),
    (H - mT - mB) / Math.max(1, maxY - minY),
    1.6
  );
  var ox = mL + (W - mL - mR - (maxX - minX) * s) / 2 - minX * s;
  var oy = mT + (H - mT - mB - (maxY - minY) * s) / 2 - minY * s;

  var out = [];
  for (i = 0; i < N; i++) {
    var dg = deg[core[i].id] || 0;
    out.push({
      id: core[i].id,
      node: core[i],
      deg: dg,
      x: Math.round((P[i].x * s + ox) * 10) / 10,
      y: Math.round((P[i].y * s + oy) * 10) / 10,
      r: Math.min(17, 6.2 + Math.min(dg, 11) * 1.05)
    });
  }

  /* label below the disc, flipped above when a neighbour sits under it */
  for (i = 0; i < out.length; i++) {
    var above = false;
    for (j = 0; j < out.length; j++) {
      if (i === j) continue;
      if (Math.abs(out[i].x - out[j].x) < 78 &&
          out[j].y > out[i].y && out[j].y - out[i].y < 36) { above = true; break; }
    }
    out[i].ly = above ? -(out[i].r + 7) : out[i].r + 13.5;
  }
  return out;
}

/* Edge geometry, with perpendicular offsets so the repeated pairs
   (e.g. widow_of ch2 / other_kin ch3 / widow_of ch4) stay readable. */
function bibleEdgeGeom(edges, pos) {
  var by = {}, i, out = [], seen = {};
  for (i = 0; i < pos.length; i++) by[pos[i].id] = pos[i];
  for (i = 0; i < edges.length; i++) {
    var e = edges[i], a = by[e.s], b = by[e.t];
    if (!a || !b) continue;
    var key = e.s < e.t ? e.s + "|" + e.t : e.t + "|" + e.s;
    var n = seen[key] || 0;
    seen[key] = n + 1;
    var lift = n === 0 ? 0 : (n % 2 ? 1 : -1) * Math.ceil(n / 2) * 14;
    var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, dd;
    if (lift) {
      dd = Math.hypot(b.x - a.x, b.y - a.y) || 1;
      mx += -(b.y - a.y) / dd * lift;
      my += (b.x - a.x) / dd * lift;
    }
    out.push({
      e: e,
      i: i,
      cls: e.c ? "is-con" : (BIBLE_KIN[e.r] ? "is-kin" : ""),
      mx: lift ? (a.x + b.x) / 4 + mx / 2 : mx,
      my: lift ? (a.y + b.y) / 4 + my / 2 : my,
      d: lift
        ? "M" + a.x + " " + a.y + " Q" + mx + " " + my + " " + b.x + " " + b.y
        : "M" + a.x + " " + a.y + " L" + b.x + " " + b.y
    });
  }
  return out;
}

/* ------------------------------------------------------------------
   GraphPanel — the SVG canvas plus its pinned stat strip.
   ------------------------------------------------------------------ */
function GraphPanel(props) {
  var pos = props.pos;
  var geom = props.geom;
  var vis = props.vis;
  var ch = props.ch;
  var sel = props.sel;
  var near = props.near;
  var stats = props.stats;
  var conIds = props.conIds;

  var hovState = React.useState(null);
  var hov = hovState[0], setHov = hovState[1];

  var lit = sel || hov;

  var edgeEls = geom.map(function (g) {
    var on = g.e.ch <= ch && vis[g.e.s] && vis[g.e.t];
    if (!on) return null;
    var touches = lit ? (g.e.s === lit || g.e.t === lit) : false;
    var cls = "bg-edge " + g.cls +
      (lit ? (touches ? " is-lit" : " is-mute") : "");
    return React.createElement("path", { key: "e" + g.i, className: cls, d: g.d });
  });

  var conMarks = geom.map(function (g) {
    if (!g.e.c) return null;
    var on = g.e.ch <= ch && vis[g.e.s] && vis[g.e.t];
    if (!on) return null;
    if (lit && !(g.e.s === lit || g.e.t === lit)) return null;
    return React.createElement("text", {
      key: "m" + g.i, className: "bg-conmark",
      x: g.mx, y: g.my + 3.5
    }, "!");
  });

  var nodeEls = pos.map(function (p) {
    if (!vis[p.id]) return null;
    var isSel = sel === p.id;
    var mute = !!sel && !isSel && !near[p.id];
    var flagged = conIds[p.id];
    var name = p.node.name || "";
    var label = name.length > 21 ? name.slice(0, 20) + "…" : name;
    var kids = [
      React.createElement("circle", {
        key: "hit", className: "bg-node-hit", r: p.r + 11
      }),
      React.createElement("circle", {
        key: "disc", className: "bg-node-disc", r: p.r
      }),
      React.createElement("circle", {
        key: "ring", className: "bg-node-ring", r: p.r + 5
      }),
      React.createElement("text", {
        key: "lab", className: "bg-node-label", y: p.ly
      }, label)
    ];
    if (flagged) {
      kids.splice(2, 0, React.createElement("circle", {
        key: "flag", className: "bg-node-flag", r: p.r + 4.5
      }));
    }
    return React.createElement("g", {
      key: p.id,
      className: "bg-node " + bibleNodeClass(p.node.kind) +
        (isSel ? " is-sel" : "") + (mute ? " is-mute" : ""),
      transform: "translate(" + p.x + "," + p.y + ")",
      tabIndex: 0,
      role: "button",
      "aria-pressed": isSel,
      "aria-label": name + " · " + p.node.kind + " · " + p.deg,
      onClick: function () { props.onPick(p.id); },
      onMouseEnter: function () { setHov(p.id); },
      onMouseLeave: function () { setHov(null); },
      onFocus: function () { setHov(p.id); },
      onBlur: function () { setHov(null); },
      onKeyDown: function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          props.onPick(p.id);
        }
      }
    }, kids);
  });

  var statCells = [
    { k: uiText("Thực thể", "Entities"), v: stats.entities, cls: "is-accent" },
    { k: uiText("Quan hệ", "Relations"), v: stats.edges, cls: "" },
    { k: uiText("Còn ngỏ", "Open questions"), v: stats.pending, cls: "" },
    { k: uiText("Tranh chấp", "Contested"), v: stats.contested, cls: "is-bad" }
  ].map(function (s, i) {
    return React.createElement("div", {
      key: i,
      className: "bg-stat " + (s.v === 0 ? "is-zero" : s.cls)
    },
      React.createElement("b", null, s.v),
      React.createElement("span", null, s.k)
    );
  });

  return React.createElement("div", { className: "bg-canvas" },
    React.createElement("svg", {
      className: "bg-svg",
      viewBox: "0 0 " + BIBLE_W + " " + BIBLE_H,
      preserveAspectRatio: "xMidYMid meet",
      role: "group",
      "aria-label": uiText(
        "Đồ thị thực thể và quan hệ, tính tới chương " + ch,
        "Entity and relation graph, as of chapter " + ch
      )
    },
      React.createElement("rect", {
        x: 0, y: 0, width: BIBLE_W, height: BIBLE_H,
        fill: "transparent",
        onClick: function () { props.onPick(null); }
      }),
      React.createElement("g", null, edgeEls),
      React.createElement("g", null, conMarks),
      React.createElement("g", null, nodeEls)
    ),
    React.createElement("div", { className: "bg-asof" },
      uiText("ký ức tính tới chương", "memory as of chapter"), " ",
      React.createElement("b", null, ch),
      " · ", stats.onCanvas, " / ", pos.length, " ",
      uiText("nút", "nodes")
    ),
    stats.contested > 0 ? React.createElement("button", {
      className: "bg-alarm", type: "button", onClick: props.onJumpContested
    },
      React.createElement("span", { className: "bg-dot is-bad" }),
      stats.contested + " " + uiText("mâu thuẫn cấu trúc", "structural conflicts")
    ) : null,
    React.createElement("div", { className: "bg-stats" }, statCells)
  );
}

/* ------------------------------------------------------------------
   Rails
   ------------------------------------------------------------------ */
function BibleRow(props) {
  return React.createElement("div", { className: "bg-row" },
    React.createElement("span", { className: "bg-row-k" }, props.k),
    React.createElement("span", {
      className: "bg-row-v " + (props.cls || "")
    }, props.v)
  );
}

function BibleSection(props) {
  return React.createElement("div", {
    className: "bg-sec " + (props.bad ? "is-bad" : "")
  },
    React.createElement("h2", { className: "bg-h " + (props.bad ? "is-bad" : "") },
      props.title,
      props.count !== undefined
        ? React.createElement("span", { className: "bg-h-n" }, props.count)
        : null
    ),
    props.children
  );
}

function BibleLeftRail(props) {
  var stats = props.stats, ch = props.ch, growth = props.growth;
  var peak = 1, i;
  for (i = 0; i < growth.length; i++) if (growth[i].total > peak) peak = growth[i].total;

  var cols = growth.map(function (g, i) {
    var c = i + 1;
    return React.createElement("button", {
      key: c, type: "button",
      className: "bg-growth-col " + (c === ch ? "is-now" : (c < ch ? "is-past" : "")),
      onClick: function () { props.onCh(c); },
      title: "ch" + c + " · " + g.total + " / " + g.core,
      "aria-label": uiText("Đọc tới chương ", "Read through chapter ") + c
    },
      React.createElement("div", {
        className: "bg-growth-bar",
        style: { height: Math.round(g.total / peak * 100) + "%" }
      })
    );
  });

  return React.createElement("div", { className: "bg-rail" },
    React.createElement(BibleSection, { title: uiText("Tổng quan", "Overview") },
      React.createElement(BibleRow, {
        k: uiText("nguồn", "source"),
        v: props.sourceLabel || (uiText("chương 1–", "chapters 1–") + props.maxCh),
        cls: "is-dim"
      }),
      React.createElement(BibleRow, {
        k: uiText("đọc tới", "read through"), v: "ch" + ch, cls: "is-accent"
      }),
      React.createElement(BibleRow, {
        k: uiText("thực thể", "entities"), v: stats.entities
      }),
      React.createElement(BibleRow, {
        k: uiText("trên canvas", "on canvas"), v: stats.onCanvas
      }),
      React.createElement(BibleRow, {
        k: uiText("nhắc một lần", "mentioned once"), v: stats.isolated, cls: "is-dim"
      }),
      React.createElement(BibleRow, {
        k: uiText("quan hệ", "relations"), v: stats.edges
      }),
      React.createElement(BibleRow, {
        k: uiText("neo nguồn", "source anchors"), v: stats.anchors, cls: "is-dim"
      }),
      React.createElement(BibleRow, {
        k: uiText("tranh chấp", "contested"), v: stats.contested,
        cls: stats.contested ? "is-bad" : "is-dim"
      })
    ),
    React.createElement(BibleSection, {
      title: uiText("Ký ức theo chương", "Memory by chapter")
    },
      React.createElement("div", {
        className: "bg-growth",
        style: { gridTemplateColumns: "repeat(" + growth.length + ", minmax(0, 1fr))" }
      }, cols),
      React.createElement("div", {
        className: "bg-growth-axis",
        style: { gridTemplateColumns: "repeat(" + growth.length + ", minmax(0, 1fr))" }
      },
        growth.map(function (g, i) {
          return React.createElement("span", {
            key: i, className: i + 1 === ch ? "is-now" : ""
          }, i + 1);
        })
      ),
      React.createElement("p", { className: "bg-note" }, uiText(
        "Không suy diễn ngược. Mỗi mốc chỉ giữ điều văn bản đã nói tới chương đó.",
        "No back-filling. Each mark holds only what the text had said by that chapter."
      ))
    ),
    React.createElement(BibleSection, { title: uiText("Chú giải", "Legend") },
      React.createElement("div", { className: "bg-legend" },
        React.createElement("div", { className: "bg-legend-row" },
          React.createElement("span", { className: "bg-swatch-node" }),
          uiText("người", "person")
        ),
        React.createElement("div", { className: "bg-legend-row" },
          React.createElement("span", { className: "bg-swatch-node is-place" }),
          uiText("nơi chốn", "place")
        ),
        React.createElement("div", { className: "bg-legend-row" },
          React.createElement("span", { className: "bg-swatch-node is-other" }),
          uiText("khác / chưa rõ", "other / unresolved")
        ),
        React.createElement("div", { className: "bg-legend-row" },
          React.createElement("span", { className: "bg-swatch is-kin" }),
          uiText("Huyết thống", "Kinship")
        ),
        React.createElement("div", { className: "bg-legend-row" },
          React.createElement("span", { className: "bg-swatch" }),
          uiText("Nơi chốn / vai trò", "Place / role")
        ),
        React.createElement("div", { className: "bg-legend-row" },
          React.createElement("span", { className: "bg-swatch is-con" }),
          uiText("Mâu thuẫn cấu trúc", "Structural conflict")
        )
      ),
      React.createElement("p", { className: "bg-note" }, uiText(
        "Cỡ nút = số quan hệ đã lập.",
        "Node size = number of established relations."
      ))
    )
  );
}

function BibleProfile(props) {
  var n = props.node, ch = props.ch, rels = props.rels;
  if (!n) {
    return React.createElement("p", { className: "bg-empty" },
      uiText("Bấm vào một nút để xem hồ sơ", "Select a node to view its profile"));
  }
  var cl = n.cl || {};
  var attrs = Object.keys(cl).map(function (k) {
    return React.createElement("div", { key: k },
      React.createElement("p", { className: "bg-kv-k" }, k.replace(/_/g, " ")),
      React.createElement("p", { className: "bg-kv-v" }, cl[k].join(" / "))
    );
  });

  return React.createElement("div", null,
    React.createElement("p", { className: "bg-ent-name" }, n.name),
    React.createElement("p", { className: "bg-ent-kind" },
      n.kind, " · ", n.rc.replace(/_/g, " ")),
    React.createElement("div", { className: "bg-chips" },
      (n.ch || []).map(function (c) {
        return React.createElement("span", {
          key: c, className: "bg-chip " + (c <= ch ? "is-on" : "")
        }, "ch" + c);
      })
    ),
    React.createElement("div", { className: "bg-kv" },
      (n.surf && n.surf.length) ? React.createElement("div", null,
        React.createElement("p", { className: "bg-kv-k" },
          uiText("Tên gọi", "Surface forms")),
        React.createElement("p", { className: "bg-kv-v" }, n.surf.join(" · "))
      ) : null,
      attrs
    ),
    React.createElement("p", { className: "bg-kv-k", style: { marginTop: "13px" } },
      uiText("Quan hệ", "Relations"), " (", rels.length, ")"),
    rels.length === 0
      ? React.createElement("p", { className: "bg-empty" },
          uiText("Chưa có ở mốc này.", "None at this mark."))
      : rels.map(function (r, i) {
          return React.createElement("button", {
            key: i, type: "button",
            className: "bg-rel " + (r.con ? "is-con" : (r.kin ? "is-kin" : "")),
            onClick: function () { if (r.other) props.onPick(r.other.id); }
          },
            React.createElement("span", { className: "bg-rel-r" },
              r.rel,
              React.createElement("span", { className: "bg-rel-dir" }, r.dir),
              React.createElement("span", { className: "bg-rel-dir" }, "ch" + r.ch)
            ),
            React.createElement("span", { className: "bg-rel-o" },
              r.other ? r.other.name : "?"),
            r.con ? React.createElement("span", { className: "bg-rel-tag" },
              uiText("Đang tranh chấp", "Disputed")) : null,
            r.note ? React.createElement("span", { className: "bg-rel-note" },
              r.note) : null
          );
        })
  );
}

function BibleRightRail(props) {
  var openState = React.useState({});
  var open = openState[0], setOpen = openState[1];

  var conBlock = props.contested.length === 0 ? null :
    React.createElement(BibleSection, {
      title: uiText("Tranh chấp", "Contested"),
      count: props.contested.length, bad: true
    },
      React.createElement("p", { className: "bg-note", style: { margin: "0 0 8px" } },
        uiText(
          "Hệ tự phát hiện các quan hệ này bất khả về cấu trúc và tự đánh dấu. Không cái nào bị sửa tự động.",
          "The system flagged these relations as structurally impossible itself. Nothing was corrected automatically."
        )),
      props.contested.map(function (c, i) {
        return React.createElement("button", {
          key: i, type: "button", className: "bg-con-item",
          onClick: function () { props.onPick(c.e.s); }
        },
          React.createElement("span", { className: "bg-con-pair" },
            c.sName, " → ", c.tName),
          React.createElement("span", { className: "bg-con-r" },
            c.e.r, " · ch", c.e.ch, " · ",
            uiText("đang tranh chấp", "disputed"))
        );
      })
    );

  return React.createElement("div", { className: "bg-rail" },
    conBlock,
    React.createElement(BibleSection, {
      title: uiText("Hồ sơ thực thể", "Entity profile")
    },
      React.createElement(BibleProfile, {
        node: props.selNode, ch: props.ch,
        rels: props.rels, onPick: props.onPick
      })
    ),
    React.createElement(BibleSection, {
      title: uiText("Câu hỏi chưa ngã ngũ", "Unsettled questions"),
      count: props.pending.length
    },
      props.pending.length === 0
        ? React.createElement("p", { className: "bg-empty" },
            uiText("Chưa có ca nào ở mốc này.", "No cases at this mark."))
        : props.pending.map(function (p, i) {
            var isOpen = !!open[i];
            var long = (p.cond || "").length > 118;
            return React.createElement("div", {
              key: i, className: "bg-q " + (isOpen ? "is-open" : "")
            },
              React.createElement("div", { className: "bg-q-head" },
                React.createElement("span", { className: "bg-q-ch" }, "ch" + p.ch),
                React.createElement("span", null, biblePendingLabel(p.q))
              ),
              React.createElement("p", { className: "bg-q-cond" },
                p.cond || uiText(
                  "Không có thực thể ràng buộc trong snapshot này.",
                  "No entity binding in this snapshot."
                )),
              long ? React.createElement("button", {
                className: "bg-more", type: "button",
                onClick: function () {
                  var next = {};
                  for (var kk in open) next[kk] = open[kk];
                  next[i] = !next[i];
                  setOpen(next);
                }
              }, isOpen ? uiText("thu lại", "less") : uiText("mở rộng", "more")) : null
            );
          })
    ),
    React.createElement(BibleSection, {
      title: uiText("Nhắc một lần, chưa nối", "Mentioned once, unconnected"),
      count: props.isolated.length
    },
      props.isolated.length === 0
        ? React.createElement("p", { className: "bg-empty" },
            uiText("Không có.", "None."))
        : React.createElement("p", { className: "bg-iso" },
            props.isolated.map(function (n, i) {
              return React.createElement("span", { key: n.id },
                i ? React.createElement("span", { className: "bg-iso-sep" }, " · ") : null,
                n.name
              );
            })
          )
    )
  );
}

/* ------------------------------------------------------------------
   StoryBiblePage — host-controlled run surface. Theme and locale are
   always supplied by the App; this page never owns competing switches.
   ------------------------------------------------------------------ */
function BibleTopBar(props) {
  var LocaleSwitch = typeof window !== "undefined" ? window.ThesisLocaleSwitch : null;
  return React.createElement("div", { className: "bg-topbar" },
    props.onBack ? React.createElement("button", {
      className: "bg-tab bg-back", type: "button", onClick: props.onBack
    }, "← ", uiText("WORKSPACE", "WORKSPACE")) : null,
    React.createElement("span", { className: "bg-crumb bg-crumb-strong" },
      React.createElement("span", { className: "bg-dot is-accent" }),
      "AGENT CONSOLE"),
    props.onOpenConsole ? React.createElement("button", {
      className: "bg-tab", type: "button", onClick: props.onOpenConsole
    }, "Console") : null,
    props.onOpenReport ? React.createElement("button", {
      className: "bg-tab", type: "button", onClick: props.onOpenReport
    }, uiText("Báo cáo", "Report")) : null,
    React.createElement("span", {
      className: "bg-tab is-on", "aria-current": "page"
    }, uiText("Bộ hồ sơ", "Story Bible")),
    props.runLabel ? React.createElement("span", { className: "bg-pill" },
      React.createElement("b", null, props.runLabel),
      props.statusLabel ? "· " + props.statusLabel : null
    ) : null,
    props.authorityLabel ? React.createElement("span", { className: "bg-pill" },
      React.createElement("span", {
        className: "bg-dot " + (props.authorityTone === "fixture" ? "is-accent" : "")
      }),
      props.authorityLabel
    ) : null,
    React.createElement("span", { className: "bg-spacer" }),
    LocaleSwitch && props.onLocaleChange ? React.createElement(LocaleSwitch, {
      compact: true, locale: props.lang, onChange: props.onLocaleChange
    }) : null,
    props.onToggleTheme ? React.createElement("button", {
      className: "bg-btn", type: "button", onClick: props.onToggleTheme
    }, "◐ ", uiText("GIAO DIỆN", "THEME")) : null,
    props.onExport ? React.createElement("button", {
      className: "bg-btn is-primary", type: "button", onClick: props.onExport
    }, uiText("XUẤT JSON", "EXPORT JSON")) : null
  );
}

function BibleSurfaceState(props) {
  return React.createElement("div", {
    className: "agentconsole console-theme-" + props.theme + " bg-page"
  }, React.createElement("div", { className: "bg-root bg-root-state" },
    React.createElement(BibleTopBar, props),
    React.createElement("section", {
      className: "bg-state", role: props.tone === "error" ? "alert" : "status"
    },
      React.createElement("span", { className: "bg-state-mark", "aria-hidden": "true" },
        props.tone === "loading" ? "◌" : props.tone === "error" ? "!" : "◇"),
      React.createElement("h1", null, props.title),
      React.createElement("p", null, props.message),
      props.code ? React.createElement("code", null, props.code) : null
    )
  ));
}

function StoryBibleContent(props) {
  var data = props.data;
  var layoutData = props.layoutData || data;
  var chapterSnapshots = props.chapterSnapshots || null;
  var maxCh = props.maxChapter;
  var requestedInitial = biblePositiveInt(props.initialChapter) ? +props.initialChapter : maxCh;
  var chState = React.useState(Math.min(maxCh, Math.max(1, requestedInitial)));
  var ch = chState[0], setCh = chState[1];
  var selState = React.useState(null);
  var sel = selState[0], setSel = selState[1];

  React.useEffect(function () {
    var requested = biblePositiveInt(props.initialChapter) ? +props.initialChapter : maxCh;
    setCh(Math.min(maxCh, Math.max(1, requested)));
  }, [props.initialChapter, maxCh]);
  React.useEffect(function () {
    if (props.onChapterChange) props.onChapterChange(ch);
  }, [ch, props.onChapterChange]);

  var G = React.useMemo(function () {
    var layoutDeg = bibleDegree(layoutData.edges);
    var layoutCore = layoutData.nodes.filter(function (n) { return layoutDeg[n.id]; });
    var layoutPos = bibleLayout(layoutCore, layoutData.edges, BIBLE_W, BIBLE_H);
    var coordinateById = Object.create(null);
    layoutPos.forEach(function (position) { coordinateById[position.id] = position; });

    var currentDeg = bibleDegree(data.edges);
    var core = [], iso = [], pos = [], byId = Object.create(null);
    data.nodes.forEach(function (node) {
      byId[node.id] = node;
      var coordinate = coordinateById[node.id];
      if (!coordinate) {
        iso.push(node);
        return;
      }
      var degree = currentDeg[node.id] || 0;
      var radius = Math.min(17, 6.2 + Math.min(degree, 11) * 1.05);
      core.push(node);
      pos.push({
        id: node.id,
        node: node,
        deg: degree,
        x: coordinate.x,
        y: coordinate.y,
        r: radius,
        ly: coordinate.ly < 0 ? -(radius + 7) : radius + 13.5
      });
    });
    var conIds = {};
    data.edges.forEach(function (e) {
      if (e.c) { conIds[e.s] = 1; conIds[e.t] = 1; }
    });
    var growth = [];
    for (var c = 1; c <= maxCh; c++) {
      var snapshot = chapterSnapshots && chapterSnapshots[c]
        ? chapterSnapshots[c]
        : data;
      growth.push({
        total: snapshot.nodes.filter(function (n) { return n.first <= c; }).length,
        core: snapshot.nodes.filter(function (n) {
          return n.first <= c && !!coordinateById[n.id];
        }).length
      });
    }
    return {
      core: core, iso: iso, byId: byId, pos: pos,
      geom: bibleEdgeGeom(data.edges, pos),
      conIds: conIds, growth: growth
    };
  }, [data, layoutData, chapterSnapshots, maxCh]);

  var V = React.useMemo(function () {
    var vis = {}, i, e;
    G.core.forEach(function (n) { if (n.first <= ch) vis[n.id] = 1; });
    var edges = [], anchors = {};
    for (i = 0; i < data.edges.length; i++) {
      e = data.edges[i];
      if (e.ch <= ch && vis[e.s] && vis[e.t]) {
        edges.push(e);
        (e.a || []).forEach(function (anchor) { anchors[anchor] = 1; });
      }
    }
    var iso = G.iso.filter(function (n) { return n.first <= ch; });
    var pending = data.pending.filter(function (p) { return p.ch <= ch; });
    var contested = edges.filter(function (x) { return x.c; }).map(function (x) {
      return {
        e: x,
        sName: G.byId[x.s] ? G.byId[x.s].name : x.s,
        tName: G.byId[x.t] ? G.byId[x.t].name : x.t
      };
    });
    var onCanvas = Object.keys(vis).length;
    return {
      vis: vis, edges: edges, iso: iso, pending: pending, contested: contested,
      stats: {
        entities: onCanvas + iso.length,
        onCanvas: onCanvas,
        isolated: iso.length,
        edges: edges.length,
        pending: pending.length,
        contested: contested.length,
        anchors: Object.keys(anchors).length
      }
    };
  }, [G, data, ch]);

  var selNode = sel && V.vis[sel] ? G.byId[sel] : null;
  var selId = selNode ? sel : null;
  var rels = React.useMemo(function () {
    if (!selNode) return [];
    return V.edges.filter(function (e) {
      return e.s === sel || e.t === sel;
    }).map(function (e) {
      return {
        other: G.byId[e.s === sel ? e.t : e.s],
        rel: e.r, con: !!e.c, kin: !!BIBLE_KIN[e.r],
        dir: e.s === sel ? "→" : "←", ch: e.ch, note: e.note
      };
    });
  }, [V, sel, selNode, G]);
  var near = React.useMemo(function () {
    var values = {};
    rels.forEach(function (relation) {
      if (relation.other) values[relation.other.id] = 1;
    });
    return values;
  }, [rels]);

  function pick(id) {
    setSel(function (current) { return current === id || id === null ? null : id; });
  }
  function jumpContested() {
    if (V.contested.length) pick(V.contested[0].e.s);
  }

  var pct = maxCh > 1 ? (ch - 1) / (maxCh - 1) : 1;
  var ticks = [];
  for (var t = 0; t < maxCh; t++) {
    var tickPct = maxCh > 1 ? t / (maxCh - 1) : 1;
    ticks.push(React.createElement("span", {
      key: t, className: "bg-track-tick",
      style: { left: "calc(6px + " + tickPct * 100 + "% - " + tickPct * 12 + "px)" }
    }));
  }

  return React.createElement("div", {
    className: "agentconsole console-theme-" + props.theme + " bg-page"
  }, React.createElement("div", { className: "bg-root" },
    React.createElement(BibleTopBar, props),
    React.createElement("div", { className: "bg-scrub" },
      React.createElement("button", {
        className: "bg-step", type: "button", disabled: ch === 1,
        onClick: function () { setCh(1); },
        "aria-label": uiText("Về chương 1", "Back to chapter 1")
      }, "|<"),
      React.createElement("button", {
        className: "bg-step", type: "button", disabled: ch === 1,
        onClick: function () { setCh(Math.max(1, ch - 1)); },
        "aria-label": uiText("Chương trước", "Previous chapter")
      }, "‹"),
      React.createElement("span", { className: "bg-scrub-label" },
        uiText("Đọc tới chương", "Read through chapter")),
      React.createElement("div", { className: "bg-track" },
        React.createElement("span", { className: "bg-track-bed" }),
        React.createElement("span", {
          className: "bg-track-fill",
          style: { width: "calc(" + pct * 100 + "% - " + pct * 12 + "px)" }
        }),
        ticks,
        React.createElement("input", {
          className: "bg-range", type: "range",
          min: 1, max: maxCh, step: 1, value: ch,
          onChange: function (event) { setCh(+event.target.value); },
          "aria-label": uiText("Đọc tới chương", "Read through chapter"),
          "aria-valuetext": "ch" + ch
        })
      ),
      React.createElement("span", { className: "bg-readout" },
        React.createElement("b", null, ch),
        React.createElement("span", { className: "bg-of" }, " / " + maxCh),
        React.createElement("span", { className: "bg-of" },
          "  " + uiText("chương", "chapters"))
      ),
      React.createElement("button", {
        className: "bg-step", type: "button", disabled: ch === maxCh,
        onClick: function () { setCh(Math.min(maxCh, ch + 1)); },
        "aria-label": uiText("Chương sau", "Next chapter")
      }, "›"),
      React.createElement("button", {
        className: "bg-step", type: "button", disabled: ch === maxCh,
        onClick: function () { setCh(maxCh); },
        "aria-label": uiText("Tới chương cuối", "To last chapter")
      }, ">|")
    ),
    React.createElement("div", { className: "bg-body" },
      React.createElement(BibleLeftRail, {
        stats: V.stats, ch: ch, growth: G.growth, onCh: setCh,
        sourceLabel: props.sourceLabel, maxCh: maxCh
      }),
      React.createElement("div", { className: "bg-gutter" }),
      React.createElement(GraphPanel, {
        pos: G.pos, geom: G.geom, vis: V.vis, ch: ch, sel: selId, near: near,
        stats: V.stats, conIds: G.conIds, onPick: pick,
        onJumpContested: jumpContested
      }),
      React.createElement("div", { className: "bg-gutter" }),
      React.createElement(BibleRightRail, {
        ch: ch, selNode: selNode, rels: rels, pending: V.pending,
        isolated: V.iso, contested: V.contested, onPick: pick
      })
    )
  ));
}

function StoryBiblePage(props) {
  var checked = React.useMemo(function () {
    return validateStoryBibleGraphData(props.data);
  }, [props.data]);
  var snapshotsChecked = React.useMemo(function () {
    return validateStoryBibleChapterSnapshots(props.chapterSnapshots);
  }, [props.chapterSnapshots]);
  var layoutSource = props.layoutData ||
    (snapshotsChecked.valid && snapshotsChecked.data
      ? snapshotsChecked.data[snapshotsChecked.maxChapter]
      : props.data);
  var layoutChecked = React.useMemo(function () {
    return validateStoryBibleGraphData(layoutSource);
  }, [layoutSource]);
  var shared = {
    theme: props.theme === "dark" ? "dark" : "paper",
    lang: props.lang === "en" ? "en" : "vi",
    onLocaleChange: props.onLocaleChange,
    onToggleTheme: props.onToggleTheme,
    onBack: props.onBack,
    onOpenConsole: props.onOpenConsole,
    onOpenReport: props.onOpenReport,
    onExport: props.onExport,
    runLabel: props.runLabel || props.runId || props.projectId || "",
    statusLabel: props.statusLabel || "",
    authorityLabel: props.authorityLabel || "",
    authorityTone: props.authorityTone || ""
  };

  if (props.loading) {
    return React.createElement(BibleSurfaceState, Object.assign({}, shared, {
      tone: "loading",
      title: uiText("Đang tải Bộ hồ sơ", "Loading Story Bible"),
      message: uiText(
        "Đang đọc projection đã được backend xác thực cho lần chạy đang chọn.",
        "Reading the backend-validated projection for the selected run."
      )
    }));
  }
  if (props.error) {
    return React.createElement(BibleSurfaceState, Object.assign({}, shared, {
      tone: "error",
      title: uiText("Không thể mở Bộ hồ sơ", "Story Bible unavailable"),
      message: String(props.error.message || props.error),
      code: props.error.code || ""
    }));
  }
  if (!props.data) {
    return React.createElement(BibleSurfaceState, Object.assign({}, shared, {
      tone: "empty",
      title: uiText("Chưa có Bộ hồ sơ đã xác thực", "No validated Story Bible yet"),
      message: uiText(
        "Trang chỉ xuất hiện khi pipeline Văn học đã tạo projection và backend quảng bá artifact hợp lệ.",
        "This surface becomes available after the Literary pipeline creates a projection and the backend advertises a valid artifact."
      )
    }));
  }
  if (!checked.valid || !snapshotsChecked.valid || !layoutChecked.valid) {
    var invalidCode = !checked.valid
      ? checked.code
      : !snapshotsChecked.valid
        ? snapshotsChecked.code
        : layoutChecked.code;
    return React.createElement(BibleSurfaceState, Object.assign({}, shared, {
      tone: "error",
      title: uiText("Dữ liệu Bộ hồ sơ không hợp lệ", "Invalid Story Bible data"),
      message: uiText(
        "UI đã ẩn graph để không ghép hoặc suy diễn dữ liệu sai contract.",
        "The UI hid the graph rather than splicing or inferring data outside the contract."
      ),
      code: invalidCode
    }));
  }

  return React.createElement(StoryBibleContent, Object.assign({}, shared, {
    data: checked.data,
    layoutData: layoutChecked.data,
    chapterSnapshots: snapshotsChecked.data,
    maxChapter: snapshotsChecked.data ? snapshotsChecked.maxChapter : checked.maxChapter,
    initialChapter: props.initialChapter,
    onChapterChange: props.onChapterChange,
    sourceLabel: props.sourceLabel
  }));
}

window.GraphPanel = GraphPanel;
window.mapLiteraryB4StoryGraph = mapLiteraryB4StoryGraph;
window.validateStoryBibleGraphData = validateStoryBibleGraphData;
window.validateStoryBibleChapterSnapshots = validateStoryBibleChapterSnapshots;
window.StoryBiblePage = StoryBiblePage;
