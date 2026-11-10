// The page of the Pixheet app. The Rust side keeps the state; the page shows the updates
// and calls a command for each action. Text goes into the page only with textContent.
"use strict";

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);
const list = $("cards");
const template = $("card-template");

/** id -> { card, li } */
const cards = new Map();
let order = [];
const previews = new Map();
let summary = null;
let settings = null;
let selection = new Set();
let anchor = null;
/** The card with the keyboard focus. Arrow keys move from it; a range starts at `anchor`. */
let focused = null;
let toastTimer = null;

const THEMES = ["system", "light", "dark"];
const THEME_ICONS = { system: "#i-auto", light: "#i-sun", dark: "#i-moon" };

// --- Commands -----------------------------------------------------------------------

async function call(command, args = {}) {
  try {
    const result = await invoke(command, args);
    if (result && typeof result === "object" && "summary" in result) apply(result);
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

function showToast(text) {
  const toast = $("toast");
  toast.textContent = text;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 6000);
}

function saveSettings(change) {
  settings = { ...settings, ...change };
  renderSettings();
  call("set_settings", { settings });
}

// --- Cards --------------------------------------------------------------------------

function newCard(card) {
  const li = template.content.firstElementChild.cloneNode(true);
  li.dataset.id = String(card.id);
  li.querySelector(".card-actions").addEventListener("click", onCardAction);
  renderCard(li, card);
  return li;
}

function renderCard(li, card) {
  li.dataset.state = card.state;
  li.dataset.percent = String(card.percent);
  li.querySelector(".card-name").textContent = card.name;
  li.querySelector(".card-name").title = card.src;
  li.querySelector(".status-text").textContent = card.status;
  const detail = li.querySelector(".card-detail");
  detail.textContent = card.detail;
  detail.title = card.state === "done" ? card.dst : card.detail;
  li.querySelector(".thumb-bar div").style.width = card.state === "converting" && card.percent > 0 ? `${card.percent}%` : "";
  li.setAttribute("aria-label", `${card.name}, ${card.status}${card.detail ? `, ${card.detail}` : ""}`);
  const show = (act, visible) => { li.querySelector(`[data-act="${act}"]`).hidden = !visible; };
  show("open", card.state === "done");
  show("reveal", card.state === "done");
  show("again", ["done", "failed", "skipped", "stopped"].includes(card.state));
  show("skip", card.state === "converting");
  show("remove", card.state !== "converting");
  const again = li.querySelector('[data-act="again"]');
  again.setAttribute("aria-label", card.state === "done" ? "Convert again" : "Retry");
  again.title = again.getAttribute("aria-label");
  for (const button of li.querySelectorAll(".card-actions button")) button.title = button.getAttribute("aria-label");
  const url = previews.get(card.id);
  const img = li.querySelector("img");
  if (url && card.state !== "ready" && card.state !== "stopped") {
    if (img.src !== url) img.src = url;
    img.hidden = false;
    li.querySelector(".thumb").classList.add("has-preview");
  } else {
    img.hidden = true;
    img.removeAttribute("src");
    li.querySelector(".thumb").classList.remove("has-preview");
  }
}

function resetCards(all) {
  const fragment = document.createDocumentFragment();
  const keep = new Map(cards);
  cards.clear();
  order = [];
  for (const card of all) {
    const old = keep.get(card.id);
    const li = old ? old.li : newCard(card);
    if (old) renderCard(li, card);
    cards.set(card.id, { card, li });
    order.push(card.id);
    fragment.appendChild(li);
  }
  list.replaceChildren(fragment);
  for (const id of [...selection]) if (!cards.has(id)) selection.delete(id);
  for (const id of [...previews.keys()]) if (!cards.has(id)) previews.delete(id);
  paintSelection();
}

function updateCard(card) {
  const entry = cards.get(card.id);
  if (!entry) return;
  entry.card = card;
  renderCard(entry.li, card);
}

// --- Selection and keys -------------------------------------------------------------

function paintSelection() {
  for (const [id, { li }] of cards) li.setAttribute("aria-selected", selection.has(id) ? "true" : "false");
}

function focusCard(id) {
  const entry = cards.get(id);
  if (!entry) return;
  for (const { li } of cards.values()) li.tabIndex = -1;
  entry.li.tabIndex = 0;
  focused = id;
  entry.li.focus({ preventScroll: true });
  entry.li.scrollIntoView({ block: "nearest" });
}

function select(id, event) {
  if (event.shiftKey && anchor != null) {
    const a = order.indexOf(anchor);
    const b = order.indexOf(id);
    selection = new Set(order.slice(Math.min(a, b), Math.max(a, b) + 1));
  } else if (event.ctrlKey || event.metaKey) {
    if (selection.has(id)) selection.delete(id); else selection.add(id);
    anchor = id;
  } else {
    selection = new Set([id]);
    anchor = id;
  }
  paintSelection();
  focusCard(id);
}

function selected() {
  return [...selection].filter((id) => cards.has(id));
}

function onCardAction(event) {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  event.stopPropagation();
  const id = Number(button.closest(".card").dataset.id);
  const act = button.dataset.act;
  if (act === "open") call("open", { id });
  else if (act === "reveal") call("reveal", { id });
  else if (act === "again") call("convert_again", { ids: [id] });
  else if (act === "skip") call("skip", { id });
  else if (act === "remove") call("remove", { ids: [id] });
}

function columns() {
  const style = getComputedStyle(list).gridTemplateColumns;
  return Math.max(1, style.split(" ").length);
}

function onKey(event) {
  if ($("error").open) return;
  const target = event.target;
  if (target.matches("input, textarea")) return;
  const ids = selected();
  // Shift+arrow keeps the anchor and moves the focus, so the range grows from the anchor.
  const current = [focused, anchor, ids[ids.length - 1]].find((id) => id != null && cards.has(id));
  const index = current == null ? -1 : order.indexOf(current);
  const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: columns(), ArrowUp: -columns() }[event.key];
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "o") {
    event.preventDefault();
    call("browse_files");
  } else if (!target.closest(".cards")) {
    return;
  } else if (step && order.length) {
    event.preventDefault();
    const next = order[Math.min(order.length - 1, Math.max(0, index + step))];
    select(next, event);
  } else if (event.key === "Enter" && target.closest("button")) {
    // The browser clicks the focused button: Enter must not also open the file.
  } else if (event.key === "Enter" && ids.length === 1 && cards.get(ids[0])?.card.state === "done") {
    call("open", { id: ids[0] });
  } else if (event.key === "Delete" || event.key === "Backspace") {
    if (ids.length) call("remove", { ids });
  } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    event.preventDefault();
    selection = new Set(order);
    paintSelection();
  } else if (event.key === "Escape") {
    selection.clear();
    paintSelection();
  }
}

// --- Summary and settings -----------------------------------------------------------

function renderSummary(s) {
  summary = s;
  document.title = s.title;
  const c = s.counts;
  $("empty").hidden = c.total > 0;
  $("gallery").hidden = c.total === 0;
  $("runbar").hidden = c.total === 0;
  $("run-text").textContent = s.text;
  $("run-eta").textContent = s.eta;
  $("bar-fill").style.width = `${Math.round(s.bar * 1000) / 10}%`;
  $("bar").setAttribute("aria-valuenow", String(Math.round(s.bar * 100)));
  const primary = $("primary");
  if (s.running) {
    primary.textContent = s.stopping ? "Stopping" : "Stop";
    primary.disabled = s.stopping;
    primary.classList.remove("primary");
    primary.dataset.action = "stop";
  } else {
    primary.textContent = c.ready ? `Convert ${c.ready.toLocaleString("en-US")}` : "Convert";
    primary.disabled = c.ready === 0;
    primary.classList.add("primary");
    primary.dataset.action = "start";
  }
  primary.hidden = !s.running && c.ready === 0;
  const retry = c.failed + c.stopped;
  $("retry").hidden = s.running || retry === 0;
  $("retry").textContent = `Retry failed (${retry.toLocaleString("en-US")})`;
  $("open-folder").hidden = !s.folder;
  $("open-folder").title = s.folder ?? "";
  $("clear").textContent = s.running ? "Clear finished" : "Clear";
  if (JSON.stringify(s.settings) !== JSON.stringify(settings)) {
    settings = s.settings;
    renderSettings();
  }
}

function renderSettings() {
  const s = settings;
  document.documentElement.dataset.theme = s.theme;
  const theme = $("theme");
  theme.setAttribute("aria-label", `Theme: ${s.theme}`);
  theme.title = `Theme: ${s.theme}`;
  theme.querySelector("use").setAttribute("href", THEME_ICONS[s.theme] ?? THEME_ICONS.system);
  const side = $("max-side");
  if (document.activeElement !== side) side.value = String(s.maxSide);
  for (const chip of document.querySelectorAll("[data-side]")) {
    chip.setAttribute("aria-pressed", String(Number(chip.dataset.side) === s.maxSide));
  }
  const custom = !["white", "black", "none"].includes(s.background);
  for (const swatch of document.querySelectorAll("[data-bg]")) {
    swatch.setAttribute("aria-checked", String(swatch.dataset.bg === s.background));
  }
  const customSwatch = document.querySelector(".swatch.custom");
  customSwatch.setAttribute("aria-checked", String(custom));
  customSwatch.classList.toggle("selected", custom);
  customSwatch.style.borderColor = custom ? "var(--accent)" : "";
  if (custom) {
    $("bg-color").value = s.background;
    $("bg-custom").style.background = s.background;
  } else {
    $("bg-custom").style.background = "";
  }
  for (const button of document.querySelectorAll("[data-out]")) {
    const on = (button.dataset.out === "folder") === (s.outDir != null);
    button.setAttribute("aria-checked", String(on));
  }
  $("folder-row").hidden = s.outDir == null;
  $("out-dir").textContent = s.outDir ?? "";
  $("out-dir").title = s.outDir ?? "";
  for (const button of document.querySelectorAll("[data-existing]")) {
    button.setAttribute("aria-checked", String(button.dataset.existing === s.existing));
  }
  $("auto-start").checked = s.autoStart;
  $("subfolders").checked = s.subfolders;
  $("compact").checked = s.compact;
}

function apply(updates) {
  for (const [id, url] of updates.previews) previews.set(id, url);
  if (updates.cards) resetCards(updates.cards);
  for (const card of updates.changed) updateCard(card);
  if (!updates.cards) {
    for (const [id] of updates.previews) {
      const entry = cards.get(id);
      if (entry) renderCard(entry.li, entry.card);
    }
  }
  renderSummary(updates.summary);
  if (updates.toast) showToast(updates.toast);
  if (updates.error) showError(updates.error);
}

// --- Events -------------------------------------------------------------------------

function bind() {
  for (const el of document.querySelectorAll('[data-action="add-files"]')) el.addEventListener("click", () => call("browse_files"));
  for (const el of document.querySelectorAll('[data-action="add-folder"]')) el.addEventListener("click", () => call("browse_folder"));
  $("theme").addEventListener("click", () => {
    const next = THEMES[(THEMES.indexOf(settings.theme) + 1) % THEMES.length];
    saveSettings({ theme: next });
  });
  $("primary").addEventListener("click", (e) => call(e.currentTarget.dataset.action === "stop" ? "stop" : "start"));
  $("retry").addEventListener("click", () => call("retry_failed"));
  $("clear").addEventListener("click", () => call("clear"));
  $("open-folder").addEventListener("click", () => call("open_folder"));
  $("repository").addEventListener("click", () => call("open_repository"));

  $("max-side").addEventListener("change", (e) => {
    const value = Number.parseInt(e.target.value, 10);
    if (Number.isInteger(value) && value >= 0) saveSettings({ maxSide: value });
    else e.target.value = String(settings.maxSide);
  });
  for (const chip of document.querySelectorAll("[data-side]")) {
    chip.addEventListener("click", () => saveSettings({ maxSide: Number(chip.dataset.side) }));
  }
  for (const swatch of document.querySelectorAll("[data-bg]")) {
    swatch.addEventListener("click", () => saveSettings({ background: swatch.dataset.bg }));
  }
  $("bg-color").addEventListener("change", (e) => saveSettings({ background: e.target.value }));
  for (const button of document.querySelectorAll("[data-out]")) {
    button.addEventListener("click", () => {
      if (button.dataset.out === "next") saveSettings({ outDir: null });
      else call("choose_out_dir");
    });
  }
  $("choose-dir").addEventListener("click", () => call("choose_out_dir"));
  for (const button of document.querySelectorAll("[data-existing]")) {
    button.addEventListener("click", () => saveSettings({ existing: button.dataset.existing }));
  }
  $("auto-start").addEventListener("change", (e) => saveSettings({ autoStart: e.target.checked }));
  $("subfolders").addEventListener("change", (e) => saveSettings({ subfolders: e.target.checked }));
  $("compact").addEventListener("change", (e) => saveSettings({ compact: e.target.checked }));

  list.addEventListener("click", (e) => {
    const li = e.target.closest(".card");
    if (li) select(Number(li.dataset.id), e);
  });
  list.addEventListener("dblclick", (e) => {
    const li = e.target.closest(".card");
    const entry = li && cards.get(Number(li.dataset.id));
    if (entry?.card.state === "done") call("open", { id: entry.card.id });
  });
  $("gallery").addEventListener("click", (e) => {
    if (e.target === $("gallery") || e.target === list) {
      selection.clear();
      paintSelection();
    }
  });
  document.addEventListener("keydown", onKey);
}

async function start() {
  bind();
  await listen("updates", (event) => apply(event.payload));
  await listen("drag", (event) => document.body.classList.toggle("dragging", event.payload));
  await call("ready");
  const version = await call("version");
  if (typeof version === "string") {
    $("version").textContent = `v${version}`;
    $("version").hidden = false;
  }
}

start();
