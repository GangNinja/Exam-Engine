import { h, icon } from "./dom.js";
import { createApi } from "./api.js";
import { errorState } from "./views/shared.js";
import { renderDashboard } from "./views/dashboard.js";
import { renderOverview } from "./views/overview.js";
import { renderCourse, renderNewCourse } from "./views/course.js";
import { renderAnalysis } from "./views/analysis.js";
import { renderDatasets } from "./views/datasets.js";
import { renderExams } from "./views/exams.js";
import { renderLearning } from "./views/learning.js";
import { renderPractice } from "./views/practice.js";
import { renderSyllabus } from "./views/syllabus.js";
import { renderTutor } from "./views/tutor.js";

const api = createApi();
const viewRoot = document.getElementById("view");
const main = document.getElementById("main");
const navRoot = document.getElementById("primary-nav");
const breadcrumbs = document.getElementById("breadcrumbs");
const courseSelect = document.getElementById("course-select");
const toastRegion = document.getElementById("toast-region");
const sidebar = document.getElementById("sidebar");
const sidebarToggle = document.getElementById("sidebar-toggle");
const sidebarScrim = document.getElementById("sidebar-scrim");
const railToggle = document.getElementById("sidebar-collapse");
const themeToggle = document.getElementById("theme-toggle");
const themeLabel = document.getElementById("theme-label");
const commandDialog = document.getElementById("command-dialog");
const commandTrigger = document.getElementById("command-trigger");
const commandInput = document.getElementById("command-input");
const commandResults = document.getElementById("command-results");
const commandClose = document.getElementById("command-close");
const commandShortcut = document.getElementById("command-shortcut");

const COURSE_VIEWS = ["overview", "course", "analysis", "learning", "practice", "tutor", "syllabus"];

const NAV_GROUPS = [
  {
    label: "Overview",
    items: [
      { key: "dashboard", label: "Dashboard", icon: "overview", href: () => "#/dashboard" },
      {
        key: "overview",
        label: "Course overview",
        icon: "course",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/overview`,
      },
    ],
  },
  {
    label: "Examinations",
    items: [
      { key: "exams", label: "Examination explorer", icon: "search", href: () => "#/exams" },
      { key: "questions", label: "Question bank", icon: "practice", href: () => "#/exams/questions" },
      {
        key: "analyzer",
        label: "Paper analyzer",
        icon: "analysis",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/analysis/distribution`,
      },
    ],
  },
  {
    label: "Intelligence",
    items: [
      {
        key: "graph",
        label: "Knowledge graph",
        icon: "learning",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/learning`,
      },
      {
        key: "paths",
        label: "Learning paths",
        icon: "arrow",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/learning/paths`,
      },
      {
        key: "coverage",
        label: "Coverage gaps",
        icon: "check",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/analysis/coverage`,
      },
      {
        key: "exam-dna",
        label: "Exam DNA",
        icon: "spark",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/analysis/exam-dna`,
      },
      {
        key: "evolution",
        label: "Concept evolution",
        icon: "refresh",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/analysis/evolution`,
      },
    ],
  },
  {
    label: "Assessment",
    items: [
      {
        key: "practice",
        label: "Practice studio",
        icon: "spark",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/practice`,
      },
      {
        key: "tutor",
        label: "AI tutor",
        icon: "tutor",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/tutor`,
      },
    ],
  },
  {
    label: "Resources",
    items: [
      {
        key: "syllabus",
        label: "Syllabus explorer",
        icon: "course",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/syllabus`,
      },
      { key: "datasets", label: "Dataset management", icon: "upload", href: () => "#/datasets" },
      {
        key: "course",
        label: "Course & documents",
        icon: "course",
        requiresCourse: true,
        href: (courseId) => `#/c/${encodeURIComponent(courseId)}/course`,
      },
      { key: "new", label: "New course", icon: "plus", href: () => "#/new" },
    ],
  },
];

const state = {
  courses: [],
  courseId: null,
  health: null,
  capabilities: null,
  route: null,
};

let controller = null;
let cleanups = [];
let renderSequence = 0;
let booted = false;
let chromeWired = false;
let commandEntries = [];
let commandNodes = [];
let commandIndex = 0;

boot();

async function boot() {
  wireChrome();
  applyInitialTheme();
  applyRailState(localStorage.getItem("exam-engine.rail") === "collapsed", { persist: false });
  renderLoading("Opening the local database");
  try {
    state.health = await api.health();
    document.getElementById("db-name").textContent = state.health.database?.name || "exam engine database";
    document.getElementById("build-info").textContent = `v${state.health.version || "—"} · local only`;
  } catch (error) {
    renderFatal(error, "The dashboard could not reach its local API. Check the terminal running `exam-engine dashboard`.");
    return;
  }
  api
    .capabilities()
    .then((capabilities) => {
      state.capabilities = capabilities;
      document.getElementById("build-info").textContent = `v${capabilities?.version || state.health.version || "—"} · local only`;
    })
    .catch(() => {
      /* Capabilities are informational; the workspace remains usable. */
    });

  try {
    await refreshCourses({ silent: true });
  } catch (error) {
    renderFatal(error, "Courses could not be listed. The database may be locked or unreadable.");
    return;
  }

  if (!location.hash || location.hash === "#/" || location.hash === "#") {
    history.replaceState(null, "", "#/dashboard");
  }
  booted = true;
  renderRoute({ focus: false });
}

function wireChrome() {
  if (chromeWired) return;
  chromeWired = true;

  courseSelect.addEventListener("change", () => {
    const courseId = courseSelect.value;
    if (!courseId) return;
    state.courseId = courseId;
    localStorage.setItem("exam-engine.course", courseId);
    const route = state.route;
    if (route?.name === "course") {
      const view = route.view === "learning" && route.tab ? `learning/${route.tab}` : route.view;
      const tab = route.view === "analysis" && route.tab ? `/${route.tab}` : "";
      navigate(`#/c/${encodeURIComponent(courseId)}/${view}${tab}`);
    } else {
      navigate(`#/c/${encodeURIComponent(courseId)}/overview`);
    }
  });

  themeToggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(next, { persist: true });
  });

  sidebarToggle.addEventListener("click", () => {
    const open = !document.body.classList.contains("sidebar-open");
    setSidebarOpen(open);
  });
  sidebarScrim.addEventListener("click", () => setSidebarOpen(false));

  railToggle.addEventListener("click", () => {
    const collapsed = !document.documentElement.classList.contains("rail-collapsed");
    applyRailState(collapsed, { persist: true });
  });

  sidebar.addEventListener("click", (event) => {
    if (event.target.closest("a")) setSidebarOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (commandDialog.open) commandDialog.close();
      else openCommandDialog();
      return;
    }
    if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
      setSidebarOpen(false);
      sidebarToggle.focus();
    }
  });

  commandTrigger.addEventListener("click", openCommandDialog);
  commandClose.addEventListener("click", () => commandDialog.close());
  commandInput.addEventListener("input", () => drawCommandResults(commandInput.value));
  commandInput.addEventListener("keydown", handleCommandKeys);
  commandDialog.addEventListener("click", (event) => {
    if (event.target === commandDialog) commandDialog.close();
  });
  commandDialog.addEventListener("close", () => commandTrigger.focus());

  const mac = /Mac|iPhone|iPad/.test(navigator.platform || "");
  commandShortcut.textContent = mac ? "⌘ K" : "Ctrl K";

  window.addEventListener("hashchange", () => {
    if (commandDialog.open) commandDialog.close();
    renderRoute({ focus: true });
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth > 900) setSidebarOpen(false);
  });
}

function applyInitialTheme() {
  const stored = localStorage.getItem("exam-engine.theme");
  if (stored === "light" || stored === "dark") {
    applyTheme(stored, { persist: false });
    return;
  }
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  applyTheme(prefersDark ? "dark" : "light", { persist: false });
}

function applyTheme(theme, { persist }) {
  document.documentElement.dataset.theme = theme;
  themeLabel.textContent = theme === "dark" ? "Dark" : "Light";
  themeToggle.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
  themeToggle.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} theme`);
  if (persist) localStorage.setItem("exam-engine.theme", theme);
}

function applyRailState(collapsed, { persist }) {
  document.documentElement.classList.toggle("rail-collapsed", collapsed);
  railToggle.setAttribute("aria-pressed", collapsed ? "true" : "false");
  railToggle.setAttribute("aria-label", collapsed ? "Expand navigation" : "Collapse navigation");
  railToggle.setAttribute("title", collapsed ? "Expand navigation" : "Collapse navigation");
  if (persist) localStorage.setItem("exam-engine.rail", collapsed ? "collapsed" : "expanded");
}

function setSidebarOpen(open) {
  document.body.classList.toggle("sidebar-open", open);
  sidebarToggle.setAttribute("aria-expanded", open ? "true" : "false");
  sidebarScrim.hidden = !open;
  if (open) {
    const first = sidebar.querySelector(".nav-item");
    if (first) window.setTimeout(() => first.focus({ preventScroll: true }), 80);
  }
}

async function refreshCourses({ silent = false } = {}) {
  const payload = await api.courses();
  state.courses = payload.courses || [];
  const stored = localStorage.getItem("exam-engine.course");
  if (!state.courseId || !state.courses.some((course) => course.course_id === state.courseId)) {
    state.courseId =
      (stored && state.courses.find((course) => course.course_id === stored)?.course_id) ||
      state.courses[0]?.course_id ||
      null;
  }
  drawCourseSelect();
  drawNav(state.route);
  drawBreadcrumbs(state.route);
  if (!silent) renderRoute({ focus: false, preserveScroll: true });
}

function drawCourseSelect() {
  courseSelect.replaceChildren();
  if (!state.courses.length) {
    courseSelect.append(h("option", { value: "", text: "No subjects yet" }));
    courseSelect.disabled = true;
    return;
  }
  courseSelect.disabled = false;
  for (const course of state.courses) {
    courseSelect.append(
      h("option", {
        value: course.course_id,
        text: `${course.course_code} · ${course.title}`,
        title: course.title,
      }),
    );
  }
  if (state.courseId) courseSelect.value = state.courseId;
}

function activeNavKey(route) {
  if (!route) return "dashboard";
  if (route.name === "dashboard") return "dashboard";
  if (route.name === "datasets") return "datasets";
  if (route.name === "new") return "new";
  if (route.name === "exams") return route.mode === "questions" ? "questions" : "exams";
  if (route.name !== "course") return "";
  if (route.view === "overview") return "overview";
  if (route.view === "course") return "course";
  if (route.view === "practice") return "practice";
  if (route.view === "tutor") return "tutor";
  if (route.view === "syllabus") return "syllabus";
  if (route.view === "learning") return route.tab === "paths" ? "paths" : "graph";
  if (route.view === "analysis") {
    if (route.tab === "distribution") return "analyzer";
    return ["coverage", "exam-dna", "evolution"].includes(route.tab) ? route.tab : "coverage";
  }
  return "";
}

function drawNav(route) {
  navRoot.replaceChildren();
  const active = activeNavKey(route);
  const courseId = state.courseId;
  for (const group of NAV_GROUPS) {
    const items = group.items.filter((item) => !item.requiresCourse || courseId);
    if (!items.length) continue;
    const section = h("section", { class: "nav-group" }, h("p", { class: "nav-label", text: group.label }));
    for (const item of items) {
      section.append(navLink(item, courseId, active === item.key));
    }
    navRoot.append(section);
  }
}

function navLink(item, courseId, active) {
  return h(
    "a",
    {
      class: `nav-item ${active ? "is-active" : ""}`.trim(),
      href: item.href(courseId),
      title: item.label,
      "data-label": item.label,
      "aria-current": active ? "page" : null,
    },
    h("span", { class: "nav-icon", "aria-hidden": "true" }, icon(item.icon)),
    h("span", { class: "nav-text", text: item.label }),
  );
}

function drawBreadcrumbs(route) {
  if (!breadcrumbs) return;
  breadcrumbs.replaceChildren();
  const pieces = [];
  const currentCourse = state.courses.find((course) => course.course_id === state.courseId);
  if (!route || route.name === "dashboard") {
    pieces.push({ label: "Intelligence overview", current: true });
  } else if (route.name === "notfound") {
    pieces.push({ label: "Not found", current: true });
  } else if (route.name === "datasets") {
    pieces.push({ label: "Resources" }, { label: "Dataset management", current: true });
  } else if (route.name === "new") {
    pieces.push({ label: "Resources" }, { label: "New course", current: true });
  } else if (route.name === "exams") {
    pieces.push({ label: "Examinations" });
    pieces.push({
      label: route.mode === "questions" ? "Question bank" : route.mode === "papers" ? "Question papers" : "Examination explorer",
      current: true,
    });
  } else if (route.name === "course" && currentCourse) {
    pieces.push({
      label: currentCourse.course_code,
      href: `#/c/${encodeURIComponent(currentCourse.course_id)}/overview`,
    });
    pieces.push({ label: courseViewLabel(route) , current: true });
  }
  for (const [index, piece] of pieces.entries()) {
    if (index) breadcrumbs.append(h("span", { class: "breadcrumb-separator", "aria-hidden": "true", text: "/" }));
    if (piece.href) {
      breadcrumbs.append(h("a", { class: "breadcrumb-link", href: piece.href, text: piece.label }));
    } else {
      breadcrumbs.append(
        h("span", {
          class: `breadcrumb-current ${piece.current ? "is-current" : "is-muted"}`.trim(),
          text: piece.label,
          "aria-current": piece.current ? "page" : null,
        }),
      );
    }
  }
}

function courseViewLabel(route) {
  if (route.view === "overview") return "Course overview";
  if (route.view === "course") return "Course & documents";
  if (route.view === "practice") return "Practice studio";
  if (route.view === "tutor") return "AI tutor";
  if (route.view === "syllabus") return "Syllabus explorer";
  if (route.view === "learning") return route.tab === "paths" ? "Learning paths" : "Knowledge graph";
  if (route.view === "analysis") {
    return (
      {
        coverage: "Coverage gaps",
        distribution: "Paper analyzer",
        "exam-dna": "Exam DNA",
        evolution: "Concept evolution",
      }[route.tab] || "Analysis"
    );
  }
  return route.view || "Workspace";
}

async function renderRoute({ focus = true, preserveScroll = false } = {}) {
  if (!booted && !state.health) return;
  const sequence = ++renderSequence;
  controller?.abort();
  runCleanups();
  controller = new AbortController();
  const signal = controller.signal;
  const scrollY = preserveScroll ? window.scrollY : 0;

  const route = parseRoute();
  state.route = route;
  drawNav(route);
  drawBreadcrumbs(route);
  updateCourseSelect();
  commandDialog?.close?.();

  if (route.name === "dashboard") {
    document.title = "Intelligence overview · Exam Engine";
    viewRoot.replaceChildren(renderDashboard(createContext(null, signal, route)));
    afterRender({ focus, scrollY });
    return;
  }

  if (route.name === "new") {
    document.title = "New course · Exam Engine";
    const node = renderNewCourse(createContext(null, signal, route));
    viewRoot.replaceChildren(node);
    afterRender({ focus, scrollY });
    return;
  }

  if (route.name === "notfound") {
    document.title = "Not found · Exam Engine";
    viewRoot.replaceChildren(notFoundView(route));
    afterRender({ focus, scrollY });
    return;
  }

  if (route.name === "datasets") {
    document.title = "Dataset management · Exam Engine";
    viewRoot.replaceChildren(renderDatasets(createContext(null, signal, route)));
    afterRender({ focus, scrollY });
    return;
  }

  if (route.name === "exams") {
    document.title =
      route.mode === "questions" ? "Question bank · Exam Engine" : "Examination explorer · Exam Engine";
    viewRoot.replaceChildren(
      renderExams(createContext(null, signal, route), { mode: route.mode, query: route.query }),
    );
    afterRender({ focus, scrollY });
    return;
  }

  if (!route.courseId) {
    navigate("#/new");
    return;
  }

  state.courseId = route.courseId;
  localStorage.setItem("exam-engine.course", route.courseId);
  updateCourseSelect();
  drawNav(route);
  drawBreadcrumbs(route);
  renderLoading("Loading course workspace");

  try {
    const course = await api.course(route.courseId, { signal });
    if (sequence !== renderSequence || signal.aborted) return;
    drawBreadcrumbs(route);
    document.title = `${course.course_code} · ${courseViewLabel(route)} · Exam Engine`;
    const ctx = createContext(course, signal, route);
    let node;
    if (route.view === "overview") node = renderOverview(ctx);
    else if (route.view === "course") node = renderCourse(ctx);
    else if (route.view === "analysis") node = renderAnalysis(ctx, route.tab || "coverage");
    else if (route.view === "learning") node = renderLearning(ctx, route.tab || "graph");
    else if (route.view === "practice") node = renderPractice(ctx);
    else if (route.view === "tutor") node = renderTutor(ctx);
    else if (route.view === "syllabus") node = renderSyllabus(ctx);
    else node = notFoundView(route);
    if (sequence !== renderSequence || signal.aborted) return;
    viewRoot.replaceChildren(node);
    afterRender({ focus, scrollY });
  } catch (error) {
    if (error?.name === "AbortError" || error?.code === "request_aborted" || sequence !== renderSequence) return;
    if (error?.code === "unknown_course") {
      viewRoot.replaceChildren(
        h(
          "div",
          { class: "view-stack" },
          h(
            "header",
            { class: "page-header" },
            h(
              "div",
              { class: "page-heading" },
              h("p", { class: "eyebrow", text: "Course" }),
              h("h1", { text: "Course not found" }),
            ),
          ),
          errorState(error, {
            retry: () => refreshCourses({ silent: true }).then(() => renderRoute({ focus: true })),
            context: "The course may have been removed or the database may have changed.",
          }),
        ),
      );
      return;
    }
    viewRoot.replaceChildren(
      h(
        "div",
        { class: "view-stack" },
        errorState(error, {
          retry: () => renderRoute({ focus: true }),
          context: "The local server may still be running. Retry after checking its terminal output.",
        }),
      ),
    );
  }
}

function createContext(course, signal, route = state.route) {
  const courseId = course?.course_id || state.courseId || null;
  let disposed = false;
  const localCleanups = [];
  const preferences = {
    get syllabusId() {
      if (!courseId) return "";
      return localStorage.getItem(`exam-engine.syllabus.${courseId}`) || "";
    },
  };
  const ctx = {
    api,
    course,
    courseId,
    courses: state.courses,
    activeCourseId: state.courseId,
    route,
    capabilities: state.capabilities,
    signal,
    preferences,
    addCleanup(fn) {
      if (typeof fn !== "function") return;
      if (disposed) {
        try {
          fn();
        } catch (error) {
          console.debug("Late cleanup failed:", error);
        }
      } else {
        localCleanups.push(fn);
      }
    },
    setPreference(key, value) {
      if (!courseId) return;
      if (key === "syllabusId") {
        localStorage.setItem(`exam-engine.syllabus.${courseId}`, value);
      }
    },
    toast,
    reload() {
      renderRoute({ focus: false, preserveScroll: true });
    },
    navigate,
    selectCourse(courseIdValue) {
      state.courseId = courseIdValue;
      localStorage.setItem("exam-engine.course", courseIdValue);
      updateCourseSelect();
      drawNav(state.route);
    },
    refreshCourses,
  };
  ctx.dispose = () => {
    disposed = true;
    for (const fn of localCleanups.splice(0)) {
      try {
        fn();
      } catch (error) {
        console.debug("Cleanup failed:", error);
      }
    }
  };
  // The route-level cleanup array owns the context disposal.
  cleanups.push(ctx.dispose);
  return ctx;
}

function afterRender({ focus, scrollY }) {
  window.scrollTo({ top: scrollY, left: 0, behavior: "instant" });
  viewRoot.classList.remove("view-enter");
  void viewRoot.offsetWidth;
  viewRoot.classList.add("view-enter");
  if (!focus) return;
  const heading = viewRoot.querySelector("h1");
  if (heading) {
    heading.setAttribute("tabindex", "-1");
    heading.focus({ preventScroll: true });
  } else {
    main.focus({ preventScroll: true });
  }
}

function runCleanups() {
  const pending = cleanups.splice(0);
  for (const fn of pending.reverse()) {
    try {
      fn();
    } catch (error) {
      console.debug("Route cleanup failed:", error);
    }
  }
}

function parseRoute() {
  const rawHash = location.hash.replace(/^#\/?/, "");
  const [path, queryString = ""] = rawHash.split("?");
  const parts = path.split("/").filter(Boolean).map(safeDecode);
  const query = new URLSearchParams(queryString);
  if (!parts.length) return { name: "dashboard", query };
  if (parts[0] === "dashboard") return { name: "dashboard", query };
  if (parts[0] === "new") return { name: "new", query };
  if (parts[0] === "datasets") return { name: "datasets", query };
  if (parts[0] === "exams") {
    const mode = ["questions", "papers"].includes(parts[1]) ? parts[1] : "explore";
    return { name: "exams", mode, query };
  }
  if (parts[0] !== "c" || !parts[1]) return { name: "notfound", query };
  const view = parts[2] || "overview";
  if (!COURSE_VIEWS.includes(view)) return { name: "notfound", courseId: parts[1], query };
  return {
    name: "course",
    courseId: parts[1],
    view,
    tab: parts[3] || null,
    query,
  };
}

function updateCourseSelect() {
  if (state.courseId) courseSelect.value = state.courseId;
}

function navigate(hash) {
  if (location.hash === hash) {
    renderRoute({ focus: true });
    return;
  }
  location.hash = hash;
}

function renderLoading(title) {
  viewRoot.replaceChildren(
    h(
      "div",
      { class: "view-stack" },
      h(
        "div",
        { class: "boot-panel", role: "status" },
        h("p", { class: "eyebrow", text: "Loading workspace" }),
        h("h1", { text: title }),
        h("div", { class: "boot-rule", "aria-hidden": "true" }),
        h(
          "div",
          { class: "skeleton-stack", "aria-hidden": "true" },
          h("span"),
          h("span"),
          h("span"),
        ),
      ),
    ),
  );
}

function renderFatal(error, context) {
  viewRoot.replaceChildren(
    h(
      "div",
      { class: "view-stack" },
      errorState(error, {
        retry: () => boot(),
        context,
      }),
    ),
  );
}

function notFoundView(route) {
  return h(
    "div",
    { class: "view-stack" },
    h(
      "header",
      { class: "page-header" },
      h(
        "div",
        { class: "page-heading" },
        h("p", { class: "eyebrow", text: "Navigation" }),
        h("h1", { text: "That workspace route does not exist" }),
        h("p", { class: "lede", text: "Use the navigation to return to a supported workspace view." }),
      ),
    ),
    h(
      "div",
      { class: "empty-state" },
      h(
        "div",
        { class: "empty-actions" },
        h("a", { class: "button button-primary", href: "#/dashboard" }, "Back to dashboard"),
        h(
          "a",
          {
            class: "button button-ghost",
            href: route.courseId ? `#/c/${encodeURIComponent(route.courseId)}/overview` : "#/exams",
          },
          route.courseId ? "Course overview" : "Examination explorer",
        ),
      ),
    ),
  );
}

function openCommandDialog() {
  commandEntries = buildCommandEntries();
  commandDialog.showModal();
  commandInput.value = "";
  drawCommandResults("");
  window.setTimeout(() => commandInput.focus(), 20);
}

function buildCommandEntries() {
  const entries = [];
  const courseId = state.courseId;
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (item.requiresCourse && !courseId) continue;
      entries.push({
        group: group.label,
        label: item.label,
        detail: item.requiresCourse ? "Current subject workspace" : "Workspace",
        href: item.href(courseId),
        icon: item.icon,
        search: `${group.label} ${item.label}`.toLowerCase(),
      });
    }
  }
  for (const course of state.courses) {
    entries.push({
      group: "Subjects",
      label: course.title,
      detail: `${course.course_code} · ${course.institution_name || "Local corpus"}`,
      href: `#/c/${encodeURIComponent(course.course_id)}/overview`,
      icon: "course",
      search: `${course.title} ${course.course_code} ${course.institution_name || ""}`.toLowerCase(),
      course: true,
    });
  }
  return entries;
}

function drawCommandResults(query) {
  const needle = query.trim().toLowerCase();
  const filtered = (needle
    ? commandEntries.filter((entry) => entry.search.includes(needle))
    : commandEntries
  ).slice(0, 36);
  commandNodes = [];
  commandIndex = 0;
  commandResults.replaceChildren();
  if (!filtered.length) {
    commandResults.append(
      h(
        "div",
        { class: "command-empty" },
        h("p", { text: "No matching workspace or subject." }),
        h("span", { text: "Try a subject code, examination category or workspace name." }),
      ),
    );
    commandInput.removeAttribute("aria-activedescendant");
    return;
  }
  const groups = new Map();
  for (const entry of filtered) {
    if (!groups.has(entry.group)) groups.set(entry.group, []);
    groups.get(entry.group).push(entry);
  }
  for (const [group, entries] of groups) {
    const section = h("section", { class: "command-group" }, h("p", { class: "command-group-label", text: group }));
    for (const entry of entries) {
      const index = commandNodes.length;
      const button = h(
        "button",
        {
          class: "command-result",
          type: "button",
          role: "option",
          id: `command-result-${index}`,
          "aria-selected": index === 0 ? "true" : "false",
          dataset: { href: entry.href },
          onClick: () => runCommand(entry.href),
        },
        h("span", { class: "command-result-icon", "aria-hidden": "true" }, icon(entry.icon)),
        h(
          "span",
          { class: "command-result-copy" },
          h("strong", { text: entry.label }),
          h("small", { text: entry.detail }),
        ),
        icon("arrow"),
      );
      button.addEventListener("mousemove", () => setCommandIndex(index));
      commandNodes.push(button);
      section.append(button);
    }
    commandResults.append(section);
  }
  setCommandIndex(0, { scroll: false });
}

function setCommandIndex(index, { scroll = true } = {}) {
  if (!commandNodes.length) return;
  commandIndex = Math.max(0, Math.min(index, commandNodes.length - 1));
  for (const [nodeIndex, node] of commandNodes.entries()) {
    node.classList.toggle("is-active", nodeIndex === commandIndex);
    node.setAttribute("aria-selected", nodeIndex === commandIndex ? "true" : "false");
  }
  const active = commandNodes[commandIndex];
  commandInput.setAttribute("aria-activedescendant", active.id);
  if (scroll) active.scrollIntoView({ block: "nearest" });
}

function handleCommandKeys(event) {
  if (event.key === "ArrowDown") {
    event.preventDefault();
    setCommandIndex(commandIndex + 1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    setCommandIndex(commandIndex - 1);
  } else if (event.key === "Enter") {
    event.preventDefault();
    const active = commandNodes[commandIndex];
    if (active) runCommand(active.dataset.href);
  }
}

function runCommand(href) {
  if (!href) return;
  commandDialog.close();
  navigate(href);
}

function toast(message, tone = "neutral") {
  const item = h("div", { class: `toast toast-${tone}`, role: "status" }, h("p", { text: message }));
  toastRegion.append(item);
  setTimeout(() => item.classList.add("is-leaving"), 5000);
  setTimeout(() => item.remove(), 5600);
}

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
