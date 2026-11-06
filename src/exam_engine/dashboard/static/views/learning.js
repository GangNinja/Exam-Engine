import { h, icon, svg } from "../dom.js";
import { barChart } from "../charts.js";
import { formatInteger, formatNumber, formatPercent, humaniseCode, safeArray } from "../ui-state.js";
import {
  dataTable,
  emptyState,
  errorState,
  illustration,
  inlineLoading,
  jobControl,
  linkButton,
  loadingPanel,
  metrics,
  notice,
  pageHeader,
  panel,
  selectedSyllabus,
  selectControl,
  statusBadge,
} from "./shared.js";

export function renderLearning(ctx, mode = "graph") {
  const pathMode = mode === "paths";
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    pageHeader({
      eyebrow: `${ctx.course.course_code} · intelligence workspace`,
      title: pathMode ? "Learning path studio" : "Knowledge graph",
      description: pathMode
        ? "Build an ordered path from the stored prerequisite graph. Start from a target concept, mark what you already know, and keep review candidates visible."
        : "Explore only relationships present in this course's stored graph. Candidate prerequisites are reviewable evidence, not facts.",
      actions: [
        linkButton("Syllabus explorer", `#/c/${encodeURIComponent(ctx.courseId)}/syllabus`, {
          iconName: "course",
        }),
        linkButton("Run analysis", `#/c/${encodeURIComponent(ctx.courseId)}/course`, {
          iconName: "analysis",
        }),
      ],
    }),
    learningTabs(ctx, pathMode ? "paths" : "graph"),
  );
  const host = h("div", {}, loadingPanel("Reading the course knowledge graph", 4));
  wrap.append(host);

  ctx.api
    .graph(ctx.courseId, { limit: 1200 }, { signal: ctx.signal })
    .then((graph) => host.replaceChildren(pathMode ? renderPathStudio(ctx, graph) : renderLearningGraph(ctx, graph)))
    .catch((error) => {
      if (isAbort(error)) return;
      host.replaceChildren(
        errorState(error, {
          retry: () => ctx.reload(),
          context: "A graph failure does not affect stored questions or analyses.",
        }),
      );
    });
  return wrap;
}

function learningTabs(ctx, active) {
  const base = `#/c/${encodeURIComponent(ctx.courseId)}/learning`;
  const items = [
    { key: "graph", label: "Knowledge graph", href: base },
    { key: "paths", label: "Learning paths", href: `${base}/paths` },
  ];
  return h(
    "nav",
    { class: "workspace-tabs", "aria-label": "Learning views" },
    ...items.map((item) =>
      h(
        "a",
        {
          class: `workspace-tab ${active === item.key ? "is-active" : ""}`.trim(),
          href: item.href,
          "aria-current": active === item.key ? "page" : null,
        },
        item.label,
      ),
    ),
  );
}

function renderPathStudio(ctx, graph) {
  const concepts = safeArray(graph.concepts);
  if (!concepts.length) {
    return emptyState({
      title: "No path can be built yet",
      body: "The path studio needs a stored knowledge graph. Build the graph after analysing syllabus and question records.",
      actions: [jobControl(ctx, "graph", "Build knowledge graph")],
      iconName: "learning",
    });
  }
  const edges = safeArray(graph.prerequisite_edges);
  const nodes = concepts.slice(0, 500);
  const status = graph.status || {};
  const labels = new Map(concepts.map((concept) => [concept.node_id, concept.label]));
  const targetSelect = selectControl(
    nodes.map((concept) => ({ value: concept.node_id, label: concept.label })),
    { id: "path-target" },
  );
  const setupHost = h("div", { class: "path-setup-host" });
  const resultHost = h("div", { class: "path-result-host" });
  const prerequisiteHost = h("div", { class: "path-prerequisite-host" });

  function drawSelection() {
    const concept = nodes.find((node) => node.node_id === targetSelect.value) || nodes[0];
    setupHost.replaceChildren(renderPathForm(ctx, concept, nodes, resultHost));
    resultHost.replaceChildren();
    const incoming = edges.filter((edge) => edge.target_node_id === concept.node_id);
    prerequisiteHost.replaceChildren(
      h(
        "div",
        { class: "path-target-summary" },
        h("p", { class: "eyebrow", text: "Selected target" }),
        h("h3", { text: concept.label }),
        h("p", {
          class: "muted",
          text: `${incoming.length} direct prerequisite${incoming.length === 1 ? "" : "s"} stored for this concept.`,
        }),
      ),
      incoming.length
        ? h(
            "ul",
            { class: "link-list path-prerequisites" },
            ...incoming.slice(0, 12).map((edge) =>
              h(
                "li",
                {},
                h("span", { text: labels.get(edge.source_node_id) || edge.source_node_id }),
                statusBadge(
                  humaniseCode(edge.status),
                  edge.status === "candidate" ? "caution" : "positive",
                ),
              ),
            ),
          )
        : h("p", { class: "muted", text: "No direct prerequisite edge is stored for this target. The generated path will state that limitation." }),
      h(
        "div",
        { class: "path-scope-links" },
        h(
          "a",
          { class: "text-link", href: `#/c/${encodeURIComponent(ctx.courseId)}/learning` },
          "Explore the full graph",
          icon("arrow"),
        ),
        h(
          "a",
          { class: "text-link", href: `#/c/${encodeURIComponent(ctx.courseId)}/syllabus` },
          "Check the syllabus outline",
          icon("arrow"),
        ),
      ),
    );
  }

  targetSelect.addEventListener("change", drawSelection);

  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    metrics([
      { label: "Concepts available", value: formatInteger(concepts.length) },
      { label: "Stored edges", value: formatInteger(status.edges) },
      { label: "Prerequisite edges", value: formatInteger(edges.length) },
      { label: "Candidate edges", value: formatInteger(status.candidate_edges) },
    ]),
    h(
      "div",
      { class: "path-layout" },
      panel(
        "Path setup",
        {
          eyebrow: "Choose a destination",
          description: "Ordering follows stored prerequisite evidence. You can mark existing knowledge to shorten the path.",
        },
        h(
          "label",
          { class: "field path-target-field" },
          h("span", { text: "Target concept" }),
          targetSelect,
        ),
        concepts.length > nodes.length
          ? h("p", { class: "muted", text: `The selector shows the first ${formatInteger(nodes.length)} concepts. Narrow the graph through the course scope for a complete export.` })
          : null,
        setupHost,
      ),
      panel(
        "Prerequisite evidence",
        {
          eyebrow: "Read the dependency",
          description: "Candidate edges are reviewable; they are not automatically facts.",
          actions: [h("a", { class: "text-link", href: `#/c/${encodeURIComponent(ctx.courseId)}/learning` }, "Open graph", icon("arrow"))],
        },
        prerequisiteHost,
      ),
    ),
    resultHost,
  );
  drawSelection();
  return wrap;
}

function renderLearningGraph(ctx, graph) {
  const concepts = safeArray(graph.concepts);
  if (!concepts.length) {
    return h(
      "div",
      { class: "view-stack" },
      emptyState({
        title: "No knowledge graph yet",
        body: "The course-scoped graph is built from analysed questions and syllabi. Build it before exploring concepts or prerequisites.",
        actions: [jobControl(ctx, "graph", "Build knowledge graph")],
        iconName: "learning",
      }),
    );
  }

  const status = graph.status || {};
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    metrics([
      { label: "Concepts", value: formatInteger(status.concepts) },
      { label: "Graph nodes", value: formatInteger(status.nodes) },
      { label: "Edges", value: formatInteger(status.edges) },
      { label: "Candidate edges", value: formatInteger(status.candidate_edges) },
      { label: "Prerequisite edges", value: formatInteger(safeArray(graph.prerequisite_edges).length) },
    ]),
  );
  wrap.append(
    h(
      "div",
      { class: "graph-top" },
      illustration(ctx, "hub", "Hub illustration: relationships in the workspace. Decorative only."),
      panel(
        "Graph scope",
        {
          eyebrow: "Graph scope",
          description: "Adjacency storage with evidence on every edge; it is not a graph database.",
        },
        h(
          "p",
          { class: "muted" },
          `This view shows up to 1,200 concepts. ${graph.truncated ? "The list was truncated; narrow the graph through the CLI for full export." : "All course-scoped concepts are shown."}`,
        ),
        safeArray(graph.prerequisite_edges).length
          ? h(
              "p",
              { class: "muted" },
              `${safeArray(graph.prerequisite_edges).length} prerequisite relationship(s) are stored. Use the path builder after choosing a concept.`,
            )
          : h("p", { class: "muted", text: "No prerequisite relationship is stored yet. Candidate discovery is optional." }),
      ),
    ),
  );

  const explorerHost = h("div");
  const conceptButtons = h("div", {
    class: "concept-list",
    role: "listbox",
    "aria-label": "Concepts in this course",
    "aria-orientation": "vertical",
  });
  conceptButtons.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const options = [...conceptButtons.querySelectorAll(".concept-button")];
    if (!options.length) return;
    const current = options.indexOf(document.activeElement);
    let next = current;
    if (event.key === "ArrowDown") next = Math.min(options.length - 1, Math.max(0, current + 1));
    else if (event.key === "ArrowUp") next = Math.max(0, current <= 0 ? 0 : current - 1);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = options.length - 1;
    event.preventDefault();
    options[next].focus();
    options[next].click();
  });
  const search = h("input", {
    type: "search",
    placeholder: "Filter concepts…",
    "aria-label": "Filter concepts",
    autocomplete: "off",
  });
  const detailHost = h("div", { class: "concept-detail" }, h("p", { class: "muted", text: "Choose a concept to inspect its stored relationships." }));
  let selectedId = null;

  function drawList() {
    const query = search.value.trim().toLocaleLowerCase();
    const filtered = concepts.filter((concept) => !query || concept.label.toLocaleLowerCase().includes(query));
    conceptButtons.replaceChildren(
      ...filtered.slice(0, 300).map((concept) => {
        const button = h(
          "button",
          {
            type: "button",
            class: `concept-button ${concept.node_id === selectedId ? "is-active" : ""}`.trim(),
            role: "option",
            tabIndex: concept.node_id === selectedId ? 0 : -1,
            "aria-selected": concept.node_id === selectedId ? "true" : "false",
            dataset: { nodeId: concept.node_id },
          },
          h("span", { text: concept.label }),
          h("small", { text: humaniseCode(concept.metadata?.status || concept.node_type) }),
        );
        button.addEventListener("click", () => {
          selectedId = concept.node_id;
          drawList();
          loadConcept(concept);
          const next = conceptButtons.querySelector(
            `.concept-button[data-node-id="${concept.node_id}"]`,
          );
          next?.focus();
        });
        return button;
      }),
    );
    if (!filtered.length) conceptButtons.append(h("p", { class: "muted", text: "No concept matches the filter." }));
  }

  async function loadConcept(concept) {
    detailHost.replaceChildren(inlineLoading(`Reading neighbourhood of ${concept.label}…`));
    try {
      const neighborhood = await ctx.api.neighborhood(
        ctx.courseId,
        { node_id: concept.node_id, hops: 1 },
        { signal: ctx.signal },
      );
      detailHost.replaceChildren(renderConceptDetail(ctx, graph, concept, neighborhood));
    } catch (error) {
      if (isAbort(error)) return;
      detailHost.replaceChildren(
        errorState(error, {
          retry: () => loadConcept(concept),
          context: "The graph remains available for other concepts.",
        }),
      );
    }
  }

  drawList();
  wrap.append(
    panel(
      "Concept explorer",
      { eyebrow: "Course-scoped", description: "Only concepts belonging to this course are listed." },
      h(
        "div",
        { class: "explorer" },
        h(
          "div",
          { class: "explorer-list" },
          h("label", { class: "field field-inline" }, h("span", { text: "Find a concept" }), search),
          conceptButtons,
        ),
        detailHost,
      ),
    ),
  );

  const prereqRows = safeArray(graph.prerequisite_edges).slice(0, 100);
  const labels = new Map(concepts.map((concept) => [concept.node_id, concept.label]));
  wrap.append(
    panel(
      "Prerequisite relationships",
      {
        eyebrow: "Review-dependent ordering",
        description: "Syllabus order and co-occurrence are evidence, not proof of pedagogy.",
        actions: [jobControl(ctx, "prerequisites", "Discover candidates")],
      },
      h(
        "div",
        { class: "graph-top graph-top-reverse" },
        prereqRows.length
          ? dataTable({
              caption: "Stored prerequisite edges",
              columns: [
                { label: "Before", render: (row) => labels.get(row.source_node_id) || row.source_node_id },
                { label: "After", render: (row) => labels.get(row.target_node_id) || row.target_node_id },
                { label: "Status", render: (row) => statusBadge(humaniseCode(row.status), row.status === "candidate" ? "caution" : "positive") },
                { label: "Confidence", render: (row) => formatPercent(row.confidence, 0), className: "numeric" },
                { label: "Method", render: (row) => humaniseCode(row.method) },
                {
                  label: "Evidence",
                  render: (row) =>
                    safeArray(row.evidence).length
                      ? h("ul", { class: "compact-list" }, ...row.evidence.slice(0, 3).map((item) => h("li", { text: item })))
                      : "—",
                },
              ],
              rows: prereqRows,
              empty: "No prerequisite relationships are stored.",
            })
          : h("p", { class: "muted", text: "No stored prerequisite relationship. Discovery can only propose what the evidence supports." }),
        illustration(ctx, "branches", "Branching relationship illustration. Decorative only."),
      ),
    ),
  );

  // Select the first concept so the workspace is never an empty shell.
  selectedId = concepts[0].node_id;
  drawList();
  loadConcept(concepts[0]);
  return wrap;
}

function renderConceptDetail(ctx, graph, concept, neighborhood) {
  const nodes = safeArray(neighborhood.nodes);
  const edges = safeArray(neighborhood.edges);
  const labels = new Map(nodes.map((node) => [node.node_id, node.label]));
  const relatedConcepts = nodes.filter(
    (node) => node.node_id !== concept.node_id && node.node_type === "concept",
  );
  const prerequisites = safeArray(graph.prerequisite_edges).filter(
    (edge) => edge.target_node_id === concept.node_id,
  );
  const pathHost = h("div", { class: "path-host" });
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    h(
      "div",
      { class: "concept-heading" },
      h("div", {}, h("p", { class: "eyebrow", text: "Selected concept" }), h("h3", { text: concept.label })),
      statusBadge(
        humaniseCode(concept.metadata?.status || "graph node"),
        concept.metadata?.status === "verified" ? "positive" : "neutral",
      ),
    ),
  );
  wrap.append(renderNeighborhood(concept, neighborhood));

  wrap.append(
    h(
      "div",
      { class: "two-col" },
      h(
        "div",
        {},
        h("h4", { text: "Related concepts" }),
        relatedConcepts.length
          ? h(
              "ul",
              { class: "link-list" },
              ...relatedConcepts.map((node) =>
                h(
                  "li",
                  {},
                  h("span", { text: node.label }),
                  h("small", { text: humaniseCode(node.node_type) }),
                ),
              ),
            )
          : h("p", { class: "muted", text: "No related concept is stored for this node." }),
        h("h4", { text: "Direct prerequisites" }),
        prerequisites.length
          ? h(
              "ul",
              { class: "link-list" },
              ...prerequisites.map((edge) =>
                h(
                  "li",
                  {},
                  h("span", { text: labels.get(edge.source_node_id) || edge.source_node_id }),
                  statusBadge(humaniseCode(edge.status), edge.status === "candidate" ? "caution" : "positive"),
                ),
              ),
            )
          : h("p", { class: "muted", text: "No direct prerequisite edge is stored." }),
      ),
      h(
        "div",
        {},
        h("h4", { text: "Build a learning path" }),
        h("p", { class: "muted", text: "Ordering reuses stored prerequisites. Candidate-only plans are explicitly partial." }),
        renderPathForm(ctx, concept, nodes, pathHost),
        pathHost,
      ),
    ),
  );
  return wrap;
}

function renderPathForm(ctx, concept, nodes, host) {
  const knownSelect = h(
    "select",
    { multiple: true, size: "5", id: `known-${concept.node_id}`, "aria-label": "Concepts already known" },
    ...nodes
      .filter((node) => node.node_type === "concept" && node.node_id !== concept.node_id)
      .map((node) => h("option", { value: node.node_id, text: node.label })),
  );
  const maxDepth = h("input", { type: "number", min: "1", max: "32", value: "8", id: `depth-${concept.node_id}` });
  const includeCandidates = h("input", { type: "checkbox", checked: true, id: `candidates-${concept.node_id}` });
  const status = h("p", { class: "form-status", role: "status", "aria-live": "polite" });
  const submit = h(
    "button",
    { class: "button button-primary", type: "submit" },
    h("span", { text: "Generate path" }),
  );
  const form = h(
    "form",
    { class: "path-form" },
    h("label", { class: "field" }, h("span", { text: "Already known (optional, multi-select)" }), knownSelect),
    h(
      "div",
      { class: "inline-fields" },
      h("label", { class: "field" }, h("span", { text: "Maximum depth" }), maxDepth),
      h(
        "label",
        { class: "checkbox-field" },
        includeCandidates,
        h("span", { text: "Include review candidates" }),
      ),
    ),
    h("div", { class: "form-actions" }, submit),
    status,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    status.dataset.tone = "neutral";
    status.textContent = "Generating path…";
    host.replaceChildren();
    try {
      const plan = await ctx.api.learningPath(
        ctx.courseId,
        {
          target_node_id: concept.node_id,
          known_concept_ids: [...knownSelect.selectedOptions].map((option) => option.value),
          include_candidates: includeCandidates.checked,
          include_inferred: false,
          max_depth: Number(maxDepth.value) || 8,
        },
        { signal: ctx.signal },
      );
      status.dataset.tone = "positive";
      status.textContent = "Path generated from stored relationships.";
      host.replaceChildren(renderPathPlan(plan));
    } catch (error) {
      if (isAbort(error)) return;
      status.dataset.tone = "negative";
      status.textContent = error?.message || "The path could not be generated.";
      host.replaceChildren(
        notice(error?.message || "The path could not be generated.", "caution", "Path unavailable"),
      );
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

function renderPathPlan(plan) {
  const steps = safeArray(plan.steps);
  return panel(
    "Learning path",
    {
      eyebrow: `Status: ${humaniseCode(plan.status)}`,
      description: "Dependency order from the stored graph, not proven pedagogy or a study-time estimate.",
    },
    steps.length
      ? dataTable({
          caption: "Ordered learning path",
          columns: [
            { label: "#", key: "position", className: "numeric" },
            { label: "Concept", key: "label" },
            { label: "Role", render: (row) => humaniseCode(row.role) },
            { label: "Status", render: (row) => statusBadge(humaniseCode(row.status || "unknown"), row.status === "candidate" ? "caution" : "positive") },
            { label: "Confidence", render: (row) => formatPercent(row.confidence, 0), className: "numeric" },
            { label: "Review", render: (row) => (row.review_recommended ? statusBadge("Recommended", "caution") : "—") },
            { label: "Why", key: "rationale" },
          ],
          rows: steps,
        })
      : h("p", { class: "muted", text: "The path contains no step. No dependency evidence was found for this target." }),
    safeArray(plan.known_concepts_skipped).length
      ? h(
          "p",
          { class: "muted" },
          `Skipped as already known: ${plan.known_concepts_skipped.map((item) => item.label).join(", ")}.`,
        )
      : null,
    safeArray(plan.limitations).length
      ? h("ul", { class: "limitations" }, ...plan.limitations.map((item) => h("li", { text: item })))
      : null,
  );
}

function renderNeighborhood(concept, neighborhood) {
  const nodes = safeArray(neighborhood.nodes);
  const edges = safeArray(neighborhood.edges);
  if (!nodes.length) {
    return h("p", { class: "muted", text: "No neighbourhood is available for this concept." });
  }
  const width = 720;
  const height = 380;
  const cx = width / 2;
  const cy = height / 2;
  const others = nodes.filter((node) => node.node_id !== concept.node_id).slice(0, 10);
  const positions = new Map([[concept.node_id, { x: cx, y: cy }]]);
  others.forEach((node, index) => {
    const angle = (Math.PI * 2 * index) / Math.max(others.length, 1) - Math.PI / 2;
    positions.set(node.node_id, {
      x: cx + Math.cos(angle) * 150,
      y: cy + Math.sin(angle) * 120,
    });
  });
  const edgeNodes = edges.flatMap((edge) => {
    const source = positions.get(edge.source_node_id);
    const target = positions.get(edge.target_node_id);
    if (!source || !target) return [];
    return [
      svg("line", {
        x1: source.x,
        y1: source.y,
        x2: target.x,
        y2: target.y,
        class: `graph-edge edge-${edge.status || "observed"}`,
      }),
    ];
  });
  const nodeNodes = nodes
    .filter((node) => positions.has(node.node_id))
    .map((node) => {
      const position = positions.get(node.node_id);
      const isCenter = node.node_id === concept.node_id;
      const label = node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label;
      return svg(
        "g",
        { class: `graph-node ${isCenter ? "is-center" : ""}` },
        svg("circle", { cx: position.x, cy: position.y, r: isCenter ? 30 : 20 }),
        svg("text", { x: position.x, y: position.y + (isCenter ? 48 : 38), "text-anchor": "middle", text: label }),
        svg("title", { text: `${node.label} (${humaniseCode(node.node_type)})` }),
      );
    });
  const svgNode = svg(
    "svg",
    {
      class: "graph-svg",
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": `Neighbourhood of ${concept.label}. ${others.map((node) => node.label).join(", ")}.`,
    },
    ...edgeNodes,
    ...nodeNodes,
  );
  return h(
    "div",
    { class: "graph-visual" },
    svgNode,
    h(
      "details",
      { class: "chart-data" },
      h("summary", { text: "View relationships as data" }),
      h(
        "ul",
        { class: "compact-list" },
        ...edges.map((edge) =>
          h("li", {
            text: `${labelsFor(nodes, edge.source_node_id)} → ${labelsFor(nodes, edge.target_node_id)} (${humaniseCode(edge.relationship)}, ${humaniseCode(edge.status)})`,
          }),
        ),
      ),
    ),
  );
}

function labelsFor(nodes, nodeId) {
  return nodes.find((node) => node.node_id === nodeId)?.label || nodeId;
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "request_aborted";
}
