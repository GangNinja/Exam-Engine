import { h, icon } from "../dom.js";
import { formatInteger, humaniseCode, safeArray } from "../ui-state.js";
import {
  dataTable,
  dateOrDash,
  definitionList,
  disclaimer,
  emptyState,
  errorState,
  field,
  inputControl,
  jobControl,
  linkButton,
  metrics,
  notice,
  pageHeader,
  panel,
  selectControl,
  sourceSummary,
  statusBadge,
  textareaControl,
} from "./shared.js";

const DOCUMENT_KINDS = [
  { value: "question_paper", label: "Question paper" },
  { value: "syllabus", label: "Syllabus" },
  { value: "notes", label: "Lecture notes" },
  { value: "question_bank", label: "Question bank (CSV/JSON)" },
  { value: "other", label: "Other" },
];

const DATASET_TYPES = [
  { value: "permissioned", label: "Permissioned (you have the right to use it)" },
  { value: "official", label: "Official" },
  { value: "public", label: "Public" },
  { value: "openly_licensed", label: "Openly licensed" },
  { value: "synthetic", label: "Synthetic fixture" },
  { value: "unknown", label: "Unknown provenance" },
];

export function renderCourse(ctx) {
  const course = ctx.course;
  const wrap = h("div", { class: "view-stack" });
  wrap.append(
    pageHeader({
      eyebrow: `${course.course_code} · course workspace`,
      title: course.title,
      description: "Course metadata, syllabus versions, documents, provenance and analysis jobs.",
      actions: [
        linkButton("New course", "#/new", { iconName: "plus" }),
        linkButton("Overview", `#/c/${encodeURIComponent(ctx.courseId)}/overview`, {
          variant: "primary",
          iconName: "overview",
        }),
      ],
    }),
  );

  wrap.append(
    panel(
      "Course identity",
      { eyebrow: "Isolation boundary", description: "Documents, questions and syllabi are scoped to this course id." },
      definitionList([
        ["Course id", h("code", { text: course.course_id })],
        ["Institution", `${course.institution_name} (${course.institution_code})`],
        ["Department", course.department || "not recorded"],
        ["Programme", course.programme || "not recorded"],
        ["Academic year", course.academic_year || "not recorded"],
        ["Syllabus version", course.syllabus_version || "not recorded"],
      ]),
    ),
  );

  wrap.append(renderSyllabi(course));
  wrap.append(renderUpload(ctx));
  wrap.append(renderAnalysisJobs(ctx));
  wrap.append(renderDocuments(course));

  return wrap;
}

function renderSyllabi(course) {
  const syllabi = safeArray(course.syllabus_details);
  return panel(
    "Syllabus versions",
    {
      eyebrow: "Version management",
      description:
        "Multiple syllabus versions stay separate. Coverage, Exam DNA and generation always require an explicit selection.",
    },
    syllabi.length
      ? h(
          "div",
          { class: "disclosure-list" },
          ...syllabi.map((syllabus) =>
            h(
              "details",
              { class: "disclosure", open: syllabi.length === 1 },
              h(
                "summary",
                {},
                h("strong", { text: syllabus.title || syllabus.syllabus_id }),
                h("span", { class: "muted", text: `${syllabus.unit_count} units · confidence ${(syllabus.confidence * 100).toFixed(0)}%` }),
                syllabus.needs_review ? statusBadge("Needs attention", "caution") : statusBadge("Parsed", "positive"),
              ),
              h(
                "div",
                { class: "disclosure-body" },
                h("p", { class: "muted", text: `syllabus_id ${syllabus.syllabus_id}` }),
                dataTable({
                  caption: `Units in ${syllabus.title || syllabus.syllabus_id}`,
                  columns: [
                    { label: "Unit", key: "unit_label" },
                    { label: "Title", key: "title" },
                    { label: "Hours", key: "hours", className: "numeric" },
                    {
                      label: "Topics",
                      render: (row) => formatInteger(safeArray(row.topics).length),
                      className: "numeric",
                    },
                    { label: "Page", key: "page_number", className: "numeric" },
                    {
                      label: "Confidence",
                      render: (row) => `${(Number(row.confidence ?? 0) * 100).toFixed(0)}%`,
                      className: "numeric",
                    },
                  ],
                  rows: safeArray(syllabus.units),
                  empty: "No units were segmented from this syllabus document.",
                }),
                safeArray(syllabus.uncertainties).length
                  ? h(
                      "ul",
                      { class: "uncertainty-list" },
                      ...syllabus.uncertainties.map((item) =>
                        h("li", { text: `${humaniseCode(item.code)}: ${item.message}` }),
                      ),
                    )
                  : null,
              ),
            ),
          ),
        )
      : emptyState({
          title: "No parsed syllabus",
          body: "Question mapping, coverage and practice generation need a syllabus document. Add one below.",
          iconName: "course",
        }),
  );
}

function renderUpload(ctx) {
  const status = h("div", { class: "form-status", role: "status", "aria-live": "polite" });
  const fileInput = inputControl({ name: "file", type: "file", required: true, id: "upload-file" });
  const titleInput = inputControl({ name: "title", id: "upload-title", placeholder: "Optional display title" });
  const kindSelect = selectControl(DOCUMENT_KINDS, { name: "kind", value: "question_paper", id: "upload-kind" });
  const sourceInput = inputControl({
    name: "source_id",
    id: "upload-source",
    value: `dashboard-${ctx.courseId}`,
  });
  const typeSelect = selectControl(DATASET_TYPES, {
    name: "dataset_type",
    value: "permissioned",
    id: "upload-type",
  });
  const licenseInput = inputControl({
    name: "license",
    id: "upload-license",
    placeholder: "e.g. Used with permission of the department",
  });
  const termsInput = inputControl({ name: "usage_terms", id: "upload-terms", placeholder: "Optional usage terms" });
  const duplicateSelect = selectControl(
    [
      { value: "skip", label: "Skip identical content" },
      { value: "replace", label: "Replace identical content" },
      { value: "fail", label: "Fail on identical content" },
    ],
    { name: "duplicate_policy", value: "skip", id: "upload-duplicate" },
  );

  const submit = h(
    "button",
    { class: "button button-primary", type: "submit" },
    icon("upload"),
    h("span", { text: "Ingest document" }),
  );

  const form = h(
    "form",
    { class: "form-grid", enctype: "multipart/form-data" },
    field("File", fileInput, "PDF, DOCX, TXT/MD, CSV/JSON banks or an image (images need OCR)."),
    field("Document kind", kindSelect),
    field("Title", titleInput, "Kept as the display title; the original filename is recorded too."),
    field("Dataset source id", sourceInput, "Provenance slug shared by documents from the same batch."),
    field("Dataset type", typeSelect),
    field("Licence", licenseInput, "Recorded verbatim with the document; never used to fetch anything."),
    field("Usage terms", termsInput),
    field("Duplicate policy", duplicateSelect),
    h("div", { class: "form-actions" }, submit, status),
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = fileInput.files?.[0];
    if (!file) {
      status.dataset.tone = "negative";
      status.textContent = "Choose a file first.";
      fileInput.focus();
      return;
    }
    const formData = new FormData();
    formData.set("file", file, file.name);
    formData.set("kind", kindSelect.value);
    formData.set("dataset_type", typeSelect.value);
    formData.set("duplicate_policy", duplicateSelect.value);
    if (titleInput.value) formData.set("title", titleInput.value);
    if (sourceInput.value) formData.set("source_id", sourceInput.value);
    if (licenseInput.value) formData.set("license", licenseInput.value);
    if (termsInput.value) formData.set("usage_terms", termsInput.value);

    submit.disabled = true;
    status.dataset.tone = "neutral";
    status.textContent = "Uploading and parsing…";
    try {
      const payload = await ctx.api.upload(ctx.courseId, formData, { signal: ctx.signal });
      const result = payload.result;
      status.dataset.tone = "positive";
      status.replaceChildren(renderUploadResult(result, () => ctx.reload()));
      ctx.toast(`Ingested ${result.source_path} (${humaniseCode(result.status)}).`, "positive");
      await ctx.refreshCourses({ silent: true });
    } catch (error) {
      if (error?.name === "AbortError") return;
      status.dataset.tone = "negative";
      status.replaceChildren(
        h("p", { text: error?.message || "The upload failed." }),
        error?.detail?.result?.errors
          ? h(
              "ul",
              { class: "uncertainty-list" },
              ...error.detail.result.errors.map((item) => h("li", { text: `${item.code}: ${item.message}` })),
            )
          : null,
      );
    } finally {
      submit.disabled = false;
    }
  });

  return panel(
    "Add a document",
    {
      eyebrow: "Document ingestion",
      description:
        "Files are validated, extracted and stored in the local SQLite database only. Nothing is uploaded anywhere else.",
    },
    form,
    disclaimer(
      "Do not add material you are not allowed to use. Videos of provenance are stored with the document and shown in the tables below.",
    ),
  );
}

function renderUploadResult(result, onReload) {
  const warnings = safeArray(result.warnings).filter((item) => item.severity !== "info");
  return h(
    "div",
    { class: "upload-result" },
    h("strong", { text: `${humaniseCode(result.status)} · ${result.source_path}` }),
    metrics([
      { label: "Pages", value: formatInteger(result.page_count) },
      { label: "Questions", value: formatInteger(result.question_count) },
      { label: "Syllabus units", value: formatInteger(result.syllabus_unit_count) },
      { label: "Duration", value: `${formatInteger(result.duration_ms)} ms` },
    ]),
    warnings.length
      ? h(
          "ul",
          { class: "uncertainty-list" },
          ...warnings.map((item) => h("li", { text: `${item.severity}: ${item.code}: ${item.message}` })),
        )
      : h("p", { class: "muted", text: "No extraction warnings were reported." }),
    h(
      "div",
      { class: "empty-actions" },
      h(
        "button",
        { class: "button button-ghost", type: "button", onClick: onReload },
        h("span", { text: "Reload workspace" }),
      ),
    ),
  );
}

function renderAnalysisJobs(ctx) {
  return panel(
    "Analysis pipeline",
    {
      eyebrow: "Analysis readiness",
      description:
        "Jobs run in the background on this machine. Submitting the same job twice while it runs returns the active job.",
    },
    h(
      "div",
      { class: "job-grid" },
      h(
        "div",
        {},
        h("h3", { text: "Text analysis" }),
        h("p", { text: "Normalised text, keyphrases, concepts, embeddings and topics." }),
        jobControl(ctx, "nlp", "Run text analysis"),
      ),
      h(
        "div",
        {},
        h("h3", { text: "Question understanding" }),
        h("p", { text: "Structure, syllabus mapping, type/Bloom estimates and similarity." }),
        jobControl(ctx, "understanding", "Run question understanding"),
      ),
      h(
        "div",
        {},
        h("h3", { text: "Knowledge graph" }),
        h("p", { text: "Rebuild the course-scoped graph; reviewed edges are preserved." }),
        jobControl(ctx, "graph", "Build knowledge graph"),
      ),
      h(
        "div",
        {},
        h("h3", { text: "Prerequisite discovery" }),
        h("p", { text: "Derive reviewable candidate prerequisites from real evidence." }),
        jobControl(ctx, "prerequisites", "Discover prerequisites"),
      ),
    ),
  );
}

function renderDocuments(course) {
  const documents = safeArray(course.documents);
  return panel(
    "Documents",
    {
      eyebrow: "Extraction state",
      description: `${documents.length} document(s) with page, quality and provenance detail.`,
    },
    documents.length
      ? h(
          "div",
          { class: "disclosure-list" },
          ...documents.map((document) =>
            h(
              "details",
              { class: "disclosure" },
              h(
                "summary",
                {},
                h("strong", { text: document.title }),
                h("span", { class: "muted", text: `${humaniseCode(document.kind)} · ${document.page_count} pages · ${document.question_count} questions` }),
                statusBadge(
                  humaniseCode(document.extraction_status),
                  document.extraction_status === "ok" ? "positive" : "warning",
                ),
              ),
              h(
                "div",
                { class: "disclosure-body" },
                definitionList([
                  ["document_id", h("code", { text: document.document_id })],
                  ["Format", humaniseCode(document.format)],
                  ["Original filename", document.original_filename],
                  ["Characters", formatInteger(document.char_count)],
                  ["Bytes", formatInteger(document.byte_size)],
                  ["Quality score", `${(Number(document.quality_score) * 100).toFixed(0)}%`],
                  ["Provenance", sourceSummary(document)],
                  ["Retrieved", dateOrDash(document.source?.retrieved_at)],
                  ["Ingested", dateOrDash(document.ingested_at, true)],
                ]),
                safeArray(document.warnings).length
                  ? h(
                      "ul",
                      { class: "uncertainty-list" },
                      ...document.warnings.map((warning) => h("li", { text: warning })),
                    )
                  : null,
              ),
            ),
          ),
        )
      : emptyState({
          title: "No documents",
          body: "Upload the material this course should be analysed from.",
          actions: [linkButton("Back to upload", "#", { iconName: "upload" })],
          iconName: "upload",
        }),
  );
}

export function renderNewCourse(ctx) {
  const wrap = h("div", { class: "view-stack" });
  const status = h("div", { class: "form-status", role: "status", "aria-live": "polite" });

  const inputs = {
    course_id: inputControl({ name: "course_id", required: true, id: "new-course-id", placeholder: "my-uni-cs201-2024-v1" }),
    course_code: inputControl({ name: "course_code", required: true, id: "new-course-code", placeholder: "CS201" }),
    title: inputControl({ name: "title", required: true, id: "new-course-title", placeholder: "Data Structures and Algorithms" }),
    institution_code: inputControl({ name: "institution_code", required: true, id: "new-course-inst-code", placeholder: "my-uni" }),
    institution_name: inputControl({ name: "institution_name", required: true, id: "new-course-inst-name", placeholder: "My University" }),
    department: inputControl({ name: "department", id: "new-course-department" }),
    programme: inputControl({ name: "programme", id: "new-course-programme", placeholder: "B.Tech" }),
    academic_year: inputControl({ name: "academic_year", id: "new-course-year", placeholder: "2024-25" }),
    syllabus_version: inputControl({ name: "syllabus_version", id: "new-course-version", placeholder: "v1" }),
  };
  const description = textareaControl({ name: "description", id: "new-course-description", rows: 3 });

  const submit = h(
    "button",
    { class: "button button-primary", type: "submit" },
    icon("plus"),
    h("span", { text: "Create course" }),
  );
  const form = h(
    "form",
    { class: "form-grid form-grid-wide" },
    field("Course id", inputs.course_id, "Lowercase slug. Documents and questions are isolated by this id."),
    field("Course code", inputs.course_code),
    field("Title", inputs.title),
    field("Institution code", inputs.institution_code),
    field("Institution name", inputs.institution_name),
    field("Department", inputs.department),
    field("Programme", inputs.programme),
    field("Academic year", inputs.academic_year),
    field("Syllabus version", inputs.syllabus_version, "The course-level version label; per-syllabus versions are separate ids."),
    field("Description", description),
    h("div", { class: "form-actions" }, submit, status),
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {};
    for (const [name, control] of Object.entries({ ...inputs, description })) {
      if (control.value) payload[name] = control.value;
    }
    submit.disabled = true;
    status.dataset.tone = "neutral";
    status.textContent = "Creating course…";
    try {
      const response = await ctx.api.createCourse(payload, { signal: ctx.signal });
      status.dataset.tone = "positive";
      status.textContent = response.created ? "Course created." : "Existing course updated.";
      await ctx.refreshCourses({ silent: true });
      ctx.selectCourse(response.course.course_id);
      ctx.navigate(`#/c/${encodeURIComponent(response.course.course_id)}/course`);
    } catch (error) {
      if (error?.name === "AbortError") return;
      status.dataset.tone = "negative";
      status.replaceChildren(
        h("p", { text: error?.message || "The course could not be created." }),
        error?.detail?.conflicting_course_ids
          ? h("p", { class: "muted", text: `Conflicting course ids: ${error.detail.conflicting_course_ids.join(", ")}` })
          : null,
        safeArray(error?.detail?.errors).length
          ? h(
              "ul",
              { class: "uncertainty-list" },
              ...error.detail.errors.map((item) => h("li", { text: `${item.loc.join(".")}: ${item.msg}` })),
            )
          : null,
      );
    } finally {
      submit.disabled = false;
    }
  });

  wrap.append(
    pageHeader({
      eyebrow: "New course",
      title: "Create a course context",
      description:
        "A course is the isolation boundary: the same institution, code, academic year and syllabus version cannot be claimed twice.",
    }),
    panel("Course details", { eyebrow: "Document metadata" }, form),
    disclaimer(
      "Creating a course writes only to this local database. No account, telemetry or external service is involved.",
    ),
  );
  return wrap;
}

export function renderCourseNotFound(ctx, error) {
  return emptyState({
    title: "Course not found",
    body: error?.message || "The selected course does not exist in this database.",
    actions: [linkButton("Choose another course", "#/", { variant: "primary" }), linkButton("Create a course", "#/new", { iconName: "plus" })],
    iconName: "alert",
  });
}

export { errorState };
