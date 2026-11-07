// The web page of the Pixheet app. The Rust controller keeps all the state. The page
// shows the updates that the controller sends, and calls a command for each action of
// the user. The page writes text only with textContent, never as HTML.
"use strict";

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);
const tbody = document.querySelector("#rows tbody");
const table = $("rows");
const wrap = $("table-wrap");

/** The rows of the list: id -> {view, tr}. */
const rows = new Map();
/** The ids of the rows, in the order of the list. */
let order = [];
let folderLabels = {};
let status = null;
let selection = [];
let anchor = null;
let follow = true;
let noteTimer = null;
let shownQuestion = null;

const NOTE_MS = 10000;
const ELLIPSIS = "…";
const ARROW = " → ";

// --- Text helpers -------------------------------------------------------------------

/** Shorten a path in the middle: a port of middle_ellipsis of _plan.py. */
function middleEllipsis(text, limit) {
  const chars = Array.from(text);
  if (chars.length <= limit) return text;
  if (limit <= 1) return ELLIPSIS.slice(0, limit);
  const sep = text.includes("\\") ? "\\" : "/";
  let head;
  let rest;
  if (text.startsWith(sep + sep)) {
    const parts = text.split(sep);
    head = parts.slice(0, 4).join(sep) + sep;
    rest = parts.slice(4);
  } else {
    const i = text.indexOf(sep);
    const first = i < 0 ? text : text.slice(0, i);
    const after = i < 0 ? "" : text.slice(i + 1);
    head = first + sep;
    rest = after ? after.split(sep) : [];
  }
  const tail = [];
  for (let i = rest.length - 1; i >= 0; i--) {
    const candidate = [rest[i], ...tail].join(sep);
    if (Array.from(head).length + 1 + sep.length + Array.from(candidate).length > limit) break;
    tail.unshift(rest[i]);
  }
  if (tail.length && tail.length < rest.length) return `${head}${ELLIPSIS}${sep}${tail.join(sep)}`;
  return ELLIPSIS + chars.slice(chars.length - (limit - 1)).join("");
}

const measure = document.createElement("canvas").getContext("2d");

/** The number of characters of the font of `el` that fit its width. */
function charBudget(el) {
  measure.font = getComputedStyle(el).font;
  const per = measure.measureText("abcdefghijklmnopqrstuvwxyz0123456789").width / 36 || 7;
  return Math.max(24, Math.floor(el.clientWidth / per) - 2);
}

/** Line 1 of the details: the image and its output, shortened to fit the line. */
function pathLine(el, src, dst, extra) {
  let budget = Math.max(24, charBudget(el) - Array.from(extra).length);
  if (dst == null) return middleEllipsis(src, budget) + extra;
  let source = src;
  let target = dst;
  if (Array.from(source).length + ARROW.length + Array.from(target).length > budget) {
    target = middleEllipsis(target, Math.max(12, Math.floor(budget / 2) - ARROW.length));
    source = middleEllipsis(source, Math.max(12, budget - ARROW.length - Array.from(target).length));
  }
  return `${source}${ARROW}${target}${extra}`;
}

// --- Commands -----------------------------------------------------------------------

async function call(command, args = {}) {
  try {
    const result = await invoke(command, args);
    if (result && typeof result === "object" && "status" in result) apply(result);
    return result;
  } catch (error) {
    showError(String(error));
    return null;
  }
}

function showError(text) {
  $("error-text").textContent = text;
  if (!$("error").open) $("error").showModal();
}

function sendFields() {
  return invoke("set_fields", {
    maxSide: $("max-side").value,
    background: $("background").value,
    compact: $("compact").checked,
  });
}

async function addFiles() {
  await sendFields();
  await call("browse_files");
}

async function addFolder() {
  await sendFields();
  await call("browse_folder");
}

// --- Rows ---------------------------------------------------------------------------

function stateClass(state) {
  if (state === "failed") return "failed";
  if (state === "converting") return "converting";
  if (state === "skipped" || state === "stopped") return "muted";
  return state;
}

function renderRow(tr, view) {
  tr.className = stateClass(view.state);
  tr.dataset.state = view.state;
  const [name, folder, statusCell] = tr.children;
  name.textContent = view.name;
  name.title = view.src;
  folder.textContent = folderLabels[view.folder] ?? "";
  statusCell.textContent = view.status;
  statusCell.title = view.state === "failed" ? view.error : view.status;
}

function newRow(view) {
  const tr = document.createElement("tr");
  tr.dataset.id = String(view.id);
  tr.tabIndex = -1;
  tr.setAttribute("aria-selected", "false");
  for (const cls of ["col-name", "col-folder", "col-status"]) {
    const td = document.createElement("td");
    td.className = cls;
    tr.appendChild(td);
  }
  renderRow(tr, view);
  return tr;
}

function resetRows(list, folders) {
  folderLabels = folders.labels;
  $("folder-heading").textContent = folders.heading;
  $("folder-heading").title = folders.heading;
  table.classList.toggle("no-folder", !folders.show);
  const fragment = document.createDocumentFragment();
  rows.clear();
  order = [];
  for (const view of list) {
    const tr = newRow(view);
    rows.set(view.id, { view, tr });
    order.push(view.id);
    fragment.appendChild(tr);
  }
  tbody.replaceChildren(fragment);
  selection = selection.filter((id) => rows.has(id));
  paintSelection();
}

function updateRow(view) {
  const entry = rows.get(view.id);
  if (!entry) return;
  entry.view = view;
  renderRow(entry.tr, view);
}

// --- Selection and follow -----------------------------------------------------------

function paintSelection() {
  const set = new Set(selection);
  for (const [id, { tr }] of rows) tr.setAttribute("aria-selected", set.has(id) ? "true" : "false");
}

function visibleOrder() {
  if (!status?.failed_only_on) return order;
  return order.filter((id) => rows.get(id)?.view.state === "failed");
}

function setSelection(ids, focusId = null) {
  selection = ids;
  paintSelection();
  if (focusId != null) {
    const entry = rows.get(focusId);
    if (entry) {
      for (const { tr } of rows.values()) tr.tabIndex = -1;
      entry.tr.tabIndex = 0;
      entry.tr.focus({ preventScroll: true });
      entry.tr.scrollIntoView({ block: "nearest" });
    }
  }
  if (ids.length) follow = false;
  call("select", { ids });
}

function onRowClick(event) {
  const tr = event.target.closest("tr[data-id]");
  if (!tr) return;
  const id = Number(tr.dataset.id);
  const visible = visibleOrder();
  if (event.shiftKey && anchor != null) {
    const a = visible.indexOf(anchor);
    const b = visible.indexOf(id);
    const [lo, hi] = a < b ? [a, b] : [b, a];
    setSelection(visible.slice(lo, hi + 1), id);
  } else if (event.ctrlKey || event.metaKey) {
    anchor = id;
    setSelection(selection.includes(id) ? selection.filter((s) => s !== id) : [...selection, id], id);
  } else {
    anchor = id;
    setSelection([id], id);
  }
}

function followCurrent() {
  if (!follow || !status?.list_mode || status.failed_only_on || status.current == null) return;
  const entry = rows.get(status.current);
  if (!entry) return;
  const index = order.indexOf(status.current);
  const target = rows.get(order[Math.max(0, index - 3)]);
  (target ?? entry).tr.scrollIntoView({ block: "start" });
  entry.tr.scrollIntoView({ block: "nearest" });
}

// --- Status -------------------------------------------------------------------------

function setButton(el, view) {
  el.hidden = !view;
  if (!view) return;
  el.textContent = view.label;
  el.disabled = !view.enabled;
  el.classList.toggle("accent", view.accent);
}

function setNote(text, action) {
  clearTimeout(noteTimer);
  $("note").hidden = !text;
  $("note-text").textContent = text;
  $("note-action").hidden = !action;
  $("note-action").textContent = action ?? "";
  if (text && !action) noteTimer = setTimeout(() => call("close_note"), NOTE_MS);
}

function showQuestion(question) {
  const dialog = $("question");
  if (!question) {
    if (dialog.open) dialog.close("cancel");
    shownQuestion = null;
    return;
  }
  if (shownQuestion === question.heading + question.text && dialog.open) return;
  shownQuestion = question.heading + question.text;
  $("question-heading").textContent = question.heading;
  $("question-text").textContent = question.text;
  const list = $("question-files");
  list.replaceChildren();
  for (const [folder, names] of question.folders) {
    const li = document.createElement("li");
    li.textContent = names.length === 1 ? `${names[0]} in ${folder}` : `${names.length} files in ${folder}`;
    li.title = names.join("\n");
    list.appendChild(li);
  }
  $("question-replace").textContent = question.replace;
  $("question-skip").hidden = !question.skip;
  $("question-skip").textContent = question.skip ?? "";
  dialog.returnValue = "cancel";
  dialog.showModal();
  // The Skip button has the focus, so Enter does not replace files. Without Skip,
  // Cancel has the focus.
  (question.skip ? $("question-skip") : $("question-cancel")).focus();
}

function applyStatus(s) {
  status = s;
  document.title = s.title;
  $("zone").hidden = s.list_mode;
  $("list").hidden = !s.list_mode;
  $("count-line").textContent = s.count_line;
  $("count-line").title = s.count_line;
  $("zone-empty").hidden = s.single != null;
  $("zone-single").hidden = s.single == null;
  if (s.single) {
    $("single-name").textContent = s.single.name;
    $("single-status").textContent = s.single.status;
  }
  for (const id of ["max-side", "background", "compact", "save-to"]) $(id).disabled = s.running;
  const save = $("save-to");
  const values = s.save_values.join("\n");
  if (save.dataset.values !== values) {
    save.replaceChildren(...s.save_values.map((v, i) => new Option(v, String(i))));
    save.dataset.values = values;
  }
  save.selectedIndex = s.save_index;

  $("progress-text").textContent = s.progress;
  $("eta").textContent = s.eta;
  $("eta").hidden = !s.eta;
  $("bar").value = s.bar;
  setButton($("failed-only"), s.failed_only);
  $("failed-only").setAttribute("aria-pressed", String(s.failed_only_on));
  table.classList.toggle("failed-only", s.failed_only_on);

  setButton($("left"), s.left);
  setButton($("retry"), s.retry);
  $("clear").hidden = !s.clear;
  setButton($("folders-menu"), s.folders_menu);
  $("folder-open").hidden = !s.folder_open;
  setButton($("show"), s.show);
  setButton($("open"), s.open);

  if ($("note-text").textContent !== s.note || $("note-action").hidden === !!s.note_action) {
    setNote(s.note, s.note_action);
  }

  const d = s.details;
  const path = $("path-line");
  const line = d.src ? pathLine(path, d.src, d.dst, d.extra) : d.path_text;
  path.textContent = line;
  path.title = d.src ? [d.src, d.dst].filter(Boolean).join(ARROW) : "";
  path.hidden = !line;
  const statusLine = $("status-line");
  statusLine.textContent = d.text;
  statusLine.hidden = !d.text;
  statusLine.classList.toggle("failed", d.failed);

  showQuestion(s.question);
  followCurrent();
}

function apply(updates) {
  if (updates.rows) resetRows(updates.rows[0], updates.rows[1]);
  for (const view of updates.changed) updateRow(view);
  applyStatus(updates.status);
  if (updates.error) showError(updates.error);
}

// --- Context menu -------------------------------------------------------------------

function selectedViews() {
  return selection.map((id) => rows.get(id)?.view).filter(Boolean);
}

function openMenu(x, y) {
  const views = selectedViews();
  const states = new Set(views.map((v) => v.state));
  const oneDone = views.length === 1 && views[0].state === "done";
  const converting = views.find((v) => v.state === "converting");
  const items = [
    ["Open", "Enter", oneDone, () => call("open_output")],
    ["Show in folder", "", oneDone, () => call("show_output")],
    ["Copy full path", "", views.length > 0, () => copy(views.map((v) => v.src).join("\n"))],
    ["Copy error", "", states.has("failed"), async () => copy(await invoke("errors_text", { ids: selection }))],
    null,
    ["Retry", "", states.has("failed") || states.has("stopped"), () => call("retry", { ids: selection, ask: true })],
    ["Convert again", "", states.has("skipped"), () => call("retry", { ids: views.filter((v) => v.state === "skipped").map((v) => v.id), ask: false })],
    ["Skip this file", "", converting != null, () => call("skip_current", { id: converting.id })],
    ["Remove from list", "Delete", [...states].some((s) => s !== "converting"), () => call("remove_rows", { ids: selection })],
    null,
    ["Select failed files", "", (status?.tally.failed ?? 0) > 0, selectFailed],
    ["Remove converted files", "", (status?.tally.done ?? 0) > 0, () => call("remove_done")],
  ];
  showMenu(items, x, y);
}

function showMenu(items, x, y) {
  const menu = $("menu");
  menu.replaceChildren();
  for (const item of items) {
    if (item === null) {
      menu.appendChild(document.createElement("hr"));
      continue;
    }
    const [label, key, enabled, action] = item;
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "menuitem");
    button.disabled = !enabled;
    const text = document.createElement("span");
    text.textContent = label;
    button.appendChild(text);
    if (key) {
      const k = document.createElement("span");
      k.className = "key";
      k.textContent = key;
      button.appendChild(k);
    }
    button.addEventListener("click", () => {
      closeMenu();
      action();
    });
    menu.appendChild(button);
  }
  menu.hidden = false;
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, innerWidth - rect.width - 4)}px`;
  menu.style.top = `${Math.min(y, innerHeight - rect.height - 4)}px`;
  menu.querySelector("button:not(:disabled)")?.focus();
}

function closeMenu() {
  $("menu").hidden = true;
}

function selectFailed() {
  const failed = order.filter((id) => rows.get(id)?.view.state === "failed");
  setSelection(failed, failed[0] ?? null);
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (error) {
    showError(`Cannot copy the text: ${error}`);
  }
}

// --- Events -------------------------------------------------------------------------

function bind() {
  for (const el of document.querySelectorAll('[data-action="add-files"]')) el.addEventListener("click", addFiles);
  for (const el of document.querySelectorAll('[data-action="add-folder"]')) el.addEventListener("click", addFolder);
  for (const id of ["max-side", "background", "compact"]) $(id).addEventListener("change", sendFields);
  $("save-to").addEventListener("change", (e) => {
    const select = e.target;
    call("choose_save", { index: select.selectedIndex, count: select.options.length });
  });
  $("left").addEventListener("click", async () => {
    await sendFields();
    call("left_action");
  });
  $("retry").addEventListener("click", async () => {
    await sendFields();
    call("retry_failed");
  });
  $("clear").addEventListener("click", () => call("clear_list"));
  $("open").addEventListener("click", () => call("open_output"));
  $("show").addEventListener("click", () => call("show_output"));
  $("folder-open").addEventListener("click", () => call("show_folder", { folder: null }));
  $("folders-menu").addEventListener("click", async (e) => {
    const folders = await invoke("folders");
    const rect = e.target.getBoundingClientRect();
    showMenu(folders.map(([path, label]) => [label, "", true, () => call("show_folder", { folder: path })]), rect.left, rect.bottom);
  });
  $("failed-only").addEventListener("click", () => call("set_failed_only", { on: !status?.failed_only_on }));
  $("note-close").addEventListener("click", () => call("close_note"));
  $("note-action").addEventListener("click", () => call("note_action"));
  $("question").addEventListener("close", () => {
    const choice = $("question").returnValue || "cancel";
    shownQuestion = null;
    call("answer", { choice });
  });

  tbody.addEventListener("click", onRowClick);
  tbody.addEventListener("dblclick", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (tr && rows.get(Number(tr.dataset.id))?.view.state === "done") call("open_output");
  });
  tbody.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    const tr = e.target.closest("tr[data-id]");
    if (tr && !selection.includes(Number(tr.dataset.id))) onRowClick(e);
    openMenu(e.clientX, e.clientY);
  });
  wrap.addEventListener("wheel", () => { follow = false; }, { passive: true });
  wrap.addEventListener("keydown", onListKey);
  document.addEventListener("click", (e) => {
    if (!$("menu").contains(e.target)) closeMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("menu").hidden) {
      closeMenu();
      e.preventDefault();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "o") {
      e.preventDefault();
      addFiles();
    }
  });
  addEventListener("resize", () => status && applyStatus(status));
}

function onListKey(e) {
  const visible = visibleOrder();
  const current = selection.length ? selection[selection.length - 1] : null;
  const index = current == null ? -1 : visible.indexOf(current);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const next = visible[Math.min(visible.length - 1, Math.max(0, index + (e.key === "ArrowDown" ? 1 : -1)))];
    if (next != null) {
      anchor = next;
      setSelection([next], next);
    }
  } else if (e.key === "Enter") {
    const views = selectedViews();
    if (views.length === 1 && views[0].state === "done") call("open_output");
  } else if (e.key === "Delete") {
    call("remove_rows", { ids: selection });
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    e.preventDefault();
    setSelection([...visible]);
  } else if (e.key === "Escape") {
    // Clear the selection and keep the converting row in view again.
    follow = true;
    setSelection([]);
    followCurrent();
  } else if ((e.shiftKey && e.key === "F10") || e.key === "ContextMenu") {
    e.preventDefault();
    const entry = rows.get(current);
    const rect = (entry?.tr ?? wrap).getBoundingClientRect();
    openMenu(rect.left + 24, rect.bottom);
  }
}

async function start() {
  bind();
  await listen("updates", (event) => apply(event.payload));
  await listen("drag", (event) => document.body.classList.toggle("dragging", event.payload));
  await sendFields();
  await call("ready");
}

start();
