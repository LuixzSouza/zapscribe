// =====================================================================
// Utilitários básicos
// =====================================================================
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const ICONS = "/static/vendor/icons.svg";
const icon = (name) => `<svg class="i"><use href="${ICONS}#i-${name}"/></svg>`;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const normalize = (s) => String(s ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

const store = {
  get(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* sem armazenamento */ }
  },
};

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["content-type"] = "application/json";
  }
  const res = await fetch(`/api${path}`, opts);
  if (!res.ok) {
    let detail = `Erro ${res.status}`;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : detail;
    } catch { /* sem corpo */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function debounce(fn, ms) {
  let t;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.flush = (...args) => {
    clearTimeout(t);
    fn(...args);
  };
  return wrapped;
}

// =====================================================================
// Estado
// =====================================================================
const AUDIO_EXT = /\.(ogg|opus|m4a|mp3|wav|aac|amr|webm|mp4|flac|wma|3gp)$/i;
const SPEEDS = [1, 1.25, 1.5, 2];
const LANGS = { pt: "Português", en: "Inglês", es: "Espanhol" };
const AI_TARGETS = {
  chatgpt: { label: "ChatGPT", url: "https://chatgpt.com/?q=" },
  claude: { label: "Claude", url: "https://claude.ai/new?q=" },
  copy: { label: "Só copiar", url: null },
};
const STATUS_ICON = { queued: "clock", running: "loader-circle", done: "check", error: "circle-alert" };
const AVATAR_COLORS = ["#0f766e", "#1d4ed8", "#7c3aed", "#be185d", "#b45309", "#15803d", "#0e7490", "#4338ca"];

const state = {
  config: { models: {}, default: "small", precise: "large-v3-turbo", parallel: 3 },
  settings: { ai: "chatgpt", template_id: null, resolve_on_send: false },
  items: new Map(),
  clients: [],
  templates: [],
  selectedId: null,
  checked: new Set(),
  lastChecked: null,
  filter: "all",
  clientFilter: "",
  query: "",
  model: null,
};

const rows = new Map(); // id -> <div class="item">
const peaksCache = new Map();

const el = {
  queue: $("#queue"),
  queueEmpty: $("#queueEmpty"),
  queueEmptyText: $("#queueEmptyText"),
  summary: $("#summary"),
  summaryText: $("#summaryText"),
  summaryBar: $("#summaryBar"),
  footStats: $("#footStats"),
  bulkbar: $("#bulkbar"),
  checkAll: $("#checkAll"),
  clientFilter: $("#clientFilter"),
  search: $("#search"),
  modelPicker: $("#modelPicker"),
  language: $("#language"),
  emptyState: $("#emptyState"),
  view: $("#view"),
  title: $("#vTitle"),
  meta: $("#vMeta"),
  client: $("#vClient"),
  resolved: $("#vResolved"),
  date: $("#vDate"),
  notes: $("#vNotes"),
  transcript: $("#transcript"),
  tInfo: $("#tInfo"),
  saveState: $("#saveState"),
  timestamps: $("#timestamps"),
  audio: $("#audio"),
  play: $("#pPlay"),
  wave: $("#pWave"),
  canvas: $("#pWave canvas"),
  time: $("#pTime"),
  speed: $("#pSpeed"),
};

const selected = () => state.items.get(state.selectedId) || null;
const clientById = (id) => state.clients.find((c) => c.id === id) || null;
const itemDate = (it) => (it.recorded_at || it.created_at) * 1000;
const defaultTemplate = () =>
  state.templates.find((t) => t.id === state.settings.template_id) || state.templates[0] || null;

// =====================================================================
// Inicialização
// =====================================================================
async function init() {
  const [config, items, clients, templates, settings] = await Promise.all([
    api("GET", "/config"),
    api("GET", "/transcriptions"),
    api("GET", "/clients"),
    api("GET", "/templates"),
    api("GET", "/settings"),
  ]);
  state.config = config;
  state.clients = clients;
  state.templates = templates;
  state.settings = settings;
  items.forEach((it) => state.items.set(it.id, it));

  setupModelPicker();
  const lang = store.get("language");
  if (lang && [...el.language.options].some((o) => o.value === lang)) el.language.value = lang;
  el.timestamps.checked = store.get("timestamps") === "1";
  el.transcript.classList.toggle("timed", el.timestamps.checked);

  renderClientFilter();
  renderList();
  const first = visibleItems()[0];
  if (first) select(first.id);
  else renderView();
  connectEvents();
}

function setupModelPicker() {
  const saved = store.get("model");
  state.model = state.config.models[saved] ? saved : state.config.default;
  el.modelPicker.innerHTML = "";
  for (const [name, label] of Object.entries(state.config.models)) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.dataset.model = name;
    b.textContent = label;
    b.title = `Modelo ${name}`;
    b.onclick = () => setModel(name);
    el.modelPicker.append(b);
  }
  setModel(state.model);
}

function setModel(name) {
  state.model = name;
  store.set("model", name);
  $$("button", el.modelPicker).forEach((b) => b.setAttribute("aria-checked", String(b.dataset.model === name)));
}

el.language.addEventListener("change", () => store.set("language", el.language.value));

// =====================================================================
// Eventos em tempo real (Server-Sent Events)
// =====================================================================
function connectEvents() {
  const source = new EventSource("/api/events");
  let connectedBefore = false;
  source.onopen = async () => {
    // Ao reconectar (ex.: servidor reiniciado), sincroniza a lista
    if (connectedBefore) await reloadItems();
    connectedBefore = true;
  };
  source.onmessage = (e) => handleEvent(JSON.parse(e.data));
}

async function reloadItems() {
  const items = await api("GET", "/transcriptions");
  const ids = new Set(items.map((i) => i.id));
  for (const id of [...state.items.keys()]) if (!ids.has(id)) removeLocal(id);
  items.forEach((it) => upsert(it, { silent: true }));
  renderList();
  if (state.selectedId) loadDetail(state.selectedId);
}

function handleEvent(ev) {
  if (ev.type === "item") upsert(ev.item);
  else if (ev.type === "segment") onSegment(ev);
  else if (ev.type === "deleted") {
    ev.ids.forEach(removeLocal);
    afterRemoval();
  }
}

function upsert(item, { silent = false } = {}) {
  const prev = state.items.get(item.id);
  const merged = { ...prev, ...item };
  const statusChanged = !prev || prev.status !== item.status;

  if (item.status === "running" && statusChanged) merged.segments = [];
  if (item.status === "queued" || item.id !== state.selectedId) delete merged.segments;
  state.items.set(item.id, merged);
  if (silent) return;

  if (!prev) renderList();
  else {
    renderRow(merged);
    renderCounts();
  }

  if (item.id === state.selectedId) {
    if (statusChanged && (item.status === "done" || item.status === "error")) loadDetail(item.id);
    else {
      renderHead(merged);
      if (statusChanged) renderTranscript(merged);
    }
  }
}

function onSegment({ id, index, segment, progress }) {
  const it = state.items.get(id);
  if (!it) return;
  it.progress = progress;
  renderRow(it);
  renderSummary();
  if (id === state.selectedId && it.segments) {
    it.segments[index] = segment;
    appendSegment(it, index);
  }
}

function removeLocal(id) {
  state.items.delete(id);
  state.checked.delete(id);
  rows.get(id)?.remove();
  rows.delete(id);
  peaksCache.delete(id);
}

function afterRemoval() {
  renderList();
  if (!state.items.has(state.selectedId)) {
    state.selectedId = null;
    el.audio.pause();
    el.audio.removeAttribute("src");
    const next = visibleItems()[0];
    if (next) select(next.id);
    else renderView();
  }
}

// =====================================================================
// Envio de arquivos: botão, arrastar para a janela e colar
// =====================================================================
for (const input of [$("#fileInput"), $("#fileInput2")]) {
  input.addEventListener("change", () => {
    addFiles(input.files);
    input.value = "";
  });
}

const overlay = $("#dropOverlay");
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");

window.addEventListener("dragenter", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth++;
  overlay.classList.add("show");
});
window.addEventListener("dragover", (e) => hasFiles(e) && e.preventDefault());
window.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) {
    dragDepth = 0;
    overlay.classList.remove("show");
  }
});
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  overlay.classList.remove("show");
  if (e.dataTransfer?.files.length) addFiles(e.dataTransfer.files);
});

document.addEventListener("paste", (e) => {
  if (isEditing(e.target)) return;
  if (e.clipboardData?.files.length) addFiles(e.clipboardData.files);
});

async function addFiles(files) {
  const list = [...files].filter((f) => /^(audio|video)\//.test(f.type) || AUDIO_EXT.test(f.name));
  if (!list.length) return toast("Nenhum arquivo de áudio encontrado", true);

  // Se estiver filtrando por um cliente, os áudios novos já entram nele
  const clientId = /^\d+$/.test(state.clientFilter) ? Number(state.clientFilter) : null;
  if (state.filter === "resolved") setFilter("all");

  let firstId = null;
  let failed = 0;
  const pending = [...list];
  const worker = async () => {
    while (pending.length) {
      const file = pending.shift();
      const form = new FormData();
      form.append("file", file);
      form.append("model", state.model);
      form.append("language", el.language.value);
      form.append("last_modified", String(file.lastModified || ""));
      if (clientId) form.append("client_id", String(clientId));
      try {
        const item = await api("POST", "/transcriptions", form);
        upsert(item);
        firstId ??= item.id;
      } catch {
        failed++;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(3, list.length) }, worker));

  if (firstId && !state.selectedId) select(firstId);
  const ok = list.length - failed;
  if (failed) toast(`${failed} arquivo(s) não puderam ser enviados`, true);
  else toast(ok > 1 ? `${ok} áudios adicionados à fila` : "Áudio adicionado à fila");
}

// =====================================================================
// Lista: filtros, busca, grupos por dia
// =====================================================================
function matchesClientAndSearch(it) {
  if (state.clientFilter === "none" && it.client_id) return false;
  if (/^\d+$/.test(state.clientFilter) && it.client_id !== Number(state.clientFilter)) return false;
  if (state.query) {
    const hay = normalize([it.title, it.text, it.notes, it.original_name, clientById(it.client_id)?.name].join(" "));
    return normalize(state.query).split(/\s+/).filter(Boolean).every((t) => hay.includes(t));
  }
  return true;
}

function matchesTab(it) {
  if (state.filter === "pending") return !it.resolved;
  if (state.filter === "resolved") return it.resolved;
  return true;
}

function sortedItems() {
  return [...state.items.values()].sort((a, b) => itemDate(b) - itemDate(a) || b.id - a.id);
}

function visibleItems() {
  return sortedItems().filter((it) => matchesClientAndSearch(it) && matchesTab(it));
}

function dayLabel(ms) {
  const d = new Date(ms);
  const today = new Date();
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(today) - start(d)) / 86400000);
  if (diff === 0) return "Hoje";
  if (diff === 1) return "Ontem";
  if (diff > 1 && diff < 7) return d.toLocaleDateString("pt-BR", { weekday: "long" });
  return d.toLocaleDateString("pt-BR", {
    day: "numeric",
    month: "long",
    ...(d.getFullYear() !== today.getFullYear() ? { year: "numeric" } : {}),
  });
}

function renderList() {
  const list = visibleItems();
  const frag = document.createDocumentFragment();
  let lastLabel = null;
  for (const it of list) {
    const label = dayLabel(itemDate(it));
    if (label !== lastLabel) {
      const g = document.createElement("div");
      g.className = "group-label";
      g.textContent = label;
      frag.append(g);
      lastLabel = label;
    }
    frag.append(renderRow(it));
  }
  el.queue.replaceChildren(frag);

  el.queueEmpty.hidden = list.length > 0;
  el.queueEmptyText.textContent = !state.items.size
    ? "Nenhum áudio ainda"
    : state.query ? "Nenhum resultado para a busca" : "Nada por aqui";
  renderCounts();
}

function renderRow(it) {
  let row = rows.get(it.id);
  if (!row) {
    row = document.createElement("div");
    row.className = "item";
    row.dataset.id = it.id;
    rows.set(it.id, row);
  }
  row.dataset.state = it.status;
  row.classList.toggle("selected", it.id === state.selectedId);
  row.classList.toggle("checked", state.checked.has(it.id));
  row.classList.toggle("resolved", it.resolved);
  row.title = it.original_name;

  const client = clientById(it.client_id);
  const sub = [];
  if (it.duration) sub.push(formatTime(it.duration));
  if (it.status === "running") sub.push(it.progress ? `Transcrevendo ${Math.round(it.progress * 100)}%` : "Transcrevendo");
  else if (it.status === "queued") sub.push("Na fila");
  else if (it.status === "error") sub.push("Falhou");
  else sub.push(`${wordCount(it.text)} palavras`);

  const unread = it.status === "done" && !it.resolved;
  const time = new Date(itemDate(it)).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });

  row.innerHTML = `
    <label class="check"><input type="checkbox" ${state.checked.has(it.id) ? "checked" : ""} aria-label="Selecionar"><span class="box"></span></label>
    <span class="item-status">${icon(it.resolved && it.status === "done" ? "check-check" : STATUS_ICON[it.status])}</span>
    <div class="item-main">
      <div class="item-name">${esc(it.title)}</div>
      <div class="item-sub">${client ? `<span class="client-tag">${esc(client.name)}</span>` : ""}<span>${sub.join(" · ")}</span></div>
      <div class="item-meter"><div style="width:${(it.progress || 0) * 100}%"></div></div>
    </div>
    <div class="item-side"><span>${time}</span>${unread ? '<span class="unread" title="Pendente"></span>' : ""}</div>`;
  return row;
}

function renderCounts() {
  const base = [...state.items.values()].filter(matchesClientAndSearch);
  const counts = {
    all: base.length,
    pending: base.filter((i) => !i.resolved).length,
    resolved: base.filter((i) => i.resolved).length,
  };
  $$("#tabs .tab").forEach((t) => ($(".count", t).textContent = counts[t.dataset.filter] || ""));
  renderSummary();
  renderSelection();
  renderFooter();
  const running = [...state.items.values()].filter((i) => i.status === "running").length;
  document.title = running ? `(${running}) Transcrevendo · Zapscribe` : "Zapscribe";
}

function renderSummary() {
  const active = [...state.items.values()].filter((i) => i.status === "running" || i.status === "queued");
  el.summary.hidden = active.length === 0;
  if (!active.length) return;
  const running = active.filter((i) => i.status === "running").length;
  const queued = active.length - running;
  const parts = [];
  if (running) parts.push(`<b>${running}</b> transcrevendo`);
  if (queued) parts.push(`<b>${queued}</b> na fila`);
  el.summaryText.innerHTML = `<span>${parts.join(" · ")}</span>`;
  const progress = active.reduce((s, i) => s + (i.progress || 0), 0) / active.length;
  el.summaryBar.style.width = `${progress * 100}%`;
}

function renderFooter() {
  const all = [...state.items.values()];
  const minutes = Math.round(all.reduce((s, i) => s + (i.status === "done" ? i.duration : 0), 0) / 60);
  el.footStats.innerHTML = all.length
    ? `${icon("shield-check")}<span>${all.length} ${all.length === 1 ? "áudio" : "áudios"}</span><span class="sep">·</span><span>${minutes} min transcritos</span><span class="sep">·</span><span>100% local</span>`
    : `${icon("shield-check")}<span>Tudo é processado e salvo neste computador</span>`;
}

function renderClientFilter() {
  const current = state.clientFilter;
  el.clientFilter.innerHTML =
    `<option value="">Todos os clientes</option><option value="none">Sem cliente</option>` +
    state.clients.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  el.clientFilter.value = [...el.clientFilter.options].some((o) => o.value === current) ? current : "";
  state.clientFilter = el.clientFilter.value;
}

function setFilter(filter) {
  state.filter = filter;
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.filter === filter));
  renderList();
}

$$("#tabs .tab").forEach((tab) => (tab.onclick = () => setFilter(tab.dataset.filter)));
el.clientFilter.addEventListener("change", () => {
  state.clientFilter = el.clientFilter.value;
  renderList();
});
el.search.addEventListener("input", debounce(() => {
  state.query = el.search.value.trim();
  renderList();
  const it = selected();
  if (it) renderTranscript(it);
}, 120));

// Clique na lista: selecionar, marcar caixa (Shift = intervalo, Ctrl = alternar)
el.queue.addEventListener("click", (e) => {
  const row = e.target.closest(".item");
  if (!row) return;
  const id = Number(row.dataset.id);
  if (e.target.closest(".check")) {
    if (e.target.tagName === "INPUT") toggleCheck(id, e.target.checked, e.shiftKey);
    return;
  }
  if (e.ctrlKey || e.metaKey) return toggleCheck(id, !state.checked.has(id), false);
  if (e.shiftKey && state.selectedId) {
    state.lastChecked ??= state.selectedId;
    return toggleCheck(id, true, true);
  }
  select(id);
});

function toggleCheck(id, on, range) {
  const ids = [id];
  if (range && state.lastChecked !== null) {
    const order = visibleItems().map((i) => i.id);
    const a = order.indexOf(state.lastChecked);
    const b = order.indexOf(id);
    if (a !== -1 && b !== -1) ids.splice(0, 1, ...order.slice(Math.min(a, b), Math.max(a, b) + 1));
  }
  ids.forEach((i) => (on ? state.checked.add(i) : state.checked.delete(i)));
  state.lastChecked = id;
  ids.forEach((i) => state.items.has(i) && renderRow(state.items.get(i)));
  renderSelection();
}

el.checkAll.addEventListener("change", () => {
  const ids = visibleItems().map((i) => i.id);
  ids.forEach((i) => (el.checkAll.checked ? state.checked.add(i) : state.checked.delete(i)));
  ids.forEach((i) => renderRow(state.items.get(i)));
  renderSelection();
});

function clearChecked() {
  const ids = [...state.checked];
  state.checked.clear();
  ids.forEach((i) => state.items.has(i) && renderRow(state.items.get(i)));
  renderSelection();
}

function renderSelection() {
  for (const id of [...state.checked]) if (!state.items.has(id)) state.checked.delete(id);
  const n = state.checked.size;
  el.queue.classList.toggle("selecting", n > 0);
  el.bulkbar.hidden = n === 0;
  el.footStats.hidden = n > 0;
  $("#bulkCount").textContent = `${n} ${n === 1 ? "selecionado" : "selecionados"}`;

  const visible = visibleItems().map((i) => i.id);
  const inView = visible.filter((i) => state.checked.has(i)).length;
  el.checkAll.checked = visible.length > 0 && inView === visible.length;
  el.checkAll.indeterminate = inView > 0 && inView < visible.length;

  const checkedItems = [...state.checked].map((i) => state.items.get(i));
  const allResolved = checkedItems.length && checkedItems.every((i) => i.resolved);
  $("#bulkResolve").title = allResolved ? "Marcar como pendentes" : "Marcar como resolvidos";
  $("#bulkResolve").innerHTML = icon(allResolved ? "circle-dashed" : "check-check");
}

// =====================================================================
// Detalhe do áudio selecionado
// =====================================================================
async function select(id) {
  if (!state.items.has(id)) return;
  const prevId = state.selectedId;
  state.selectedId = id;
  if (prevId && prevId !== id) {
    flushEdits();
    const prev = state.items.get(prevId);
    if (prev) {
      delete prev.segments;
      renderRow(prev);
    }
  }
  const it = state.items.get(id);
  renderRow(it);
  rows.get(id)?.scrollIntoView({ block: "nearest" });

  if (prevId !== id) {
    el.audio.pause();
    el.audio.src = `/api/transcriptions/${id}/audio`;
    el.audio.playbackRate = SPEEDS[speedIdx];
    setPlayIcon(false);
    lastCurrent = -1;
  }
  renderView();
  drawWave();
  loadPeaks(id);
  await loadDetail(id);
}

async function loadDetail(id) {
  try {
    const full = await api("GET", `/transcriptions/${id}`);
    if (state.selectedId !== id) return;
    const it = { ...state.items.get(id), ...full };
    state.items.set(id, it);
    renderRow(it);
    renderHead(it);
    renderTranscript(it);
  } catch {
    /* removido enquanto carregava */
  }
}

function renderView() {
  const it = selected();
  el.emptyState.hidden = !!it;
  el.view.hidden = !it;
  if (!it) return;
  renderHead(it);
  renderTranscript(it);
  updateTime();
}

function renderHead(it) {
  if (document.activeElement !== el.title) el.title.value = it.title;
  el.title.title = it.original_name;

  const chipIcon = it.status === "done" ? "circle-check" : STATUS_ICON[it.status];
  const statusLabel = { queued: "Na fila", running: "Transcrevendo", done: "Transcrito", error: "Erro" }[it.status];
  const parts = [`<span class="chip ${it.status}">${icon(chipIcon)}${statusLabel}</span>`];
  if (it.duration) parts.push(`<span>${icon("clock")}<span class="mono">${formatTime(it.duration)}</span></span>`);
  parts.push(`<span>${icon("file-audio")}${formatSize(it.size)}</span>`);
  parts.push(`<span title="Modelo ${esc(it.model)}">${icon("cpu")}${esc(state.config.models[it.model] || it.model)}</span>`);
  const lang = it.detected_language || (it.language !== "auto" ? it.language : null);
  if (lang) parts.push(`<span>${icon("languages")}${esc(LANGS[lang] || lang.toUpperCase())}</span>`);
  if (it.status === "done" && it.elapsed) {
    const speed = it.duration ? ` · ${(it.duration / it.elapsed).toFixed(1).replace(".", ",")}× tempo real` : "";
    parts.push(`<span class="mono">${formatSeconds(it.elapsed)}${speed}</span>`);
  }
  el.meta.innerHTML = parts.join("");

  // Propriedades
  const client = clientById(it.client_id);
  el.client.innerHTML = client
    ? `${avatar(client.name)}<span>${esc(client.name)}</span>`
    : `${icon("user-plus")}<span class="placeholder">Definir cliente</span>`;
  el.resolved.className = `prop-btn ${it.resolved ? "is-resolved" : "is-pending"}`;
  el.resolved.innerHTML = it.resolved
    ? `${icon("circle-check")}<span>Resolvido</span>`
    : `${icon("circle-dashed")}<span>Pendente</span>`;
  el.resolved.title = it.resolved ? "Marcar como pendente" : "Marcar como resolvido";
  el.date.textContent = formatDateTime(itemDate(it));
  if (document.activeElement !== el.notes) {
    el.notes.value = it.notes || "";
    autosize(el.notes);
  }

  const ready = it.status === "done" && !!it.text;
  $("#vAi").disabled = $("#vAiMenu").disabled = $("#vCopy").disabled = !ready;
}

function renderTranscript(it) {
  const t = el.transcript;
  t.innerHTML = "";
  if (it.status === "error") {
    t.innerHTML = `<p class="transcript-note error"></p>`;
    $("p", t).textContent = "Não foi possível transcrever: " + it.error;
  } else if (!it.segments?.length) {
    if (it.status === "done" && it.segments) {
      t.innerHTML = `<p class="transcript-note">Nenhuma fala detectada neste áudio.</p>`;
    } else if (it.status === "queued") {
      t.innerHTML = `<p class="transcript-note">Na fila. A transcrição começa assim que um dos áudios em andamento terminar.</p>`;
    } else {
      t.innerHTML = `<div class="skeleton"><span></span><span></span><span></span></div>`;
    }
  } else {
    it.segments.forEach((_, i) => appendSegment(it, i));
  }
  renderTranscriptInfo(it);
}

function appendSegment(it, index) {
  const t = el.transcript;
  if (!t.querySelector(".seg")) t.innerHTML = "";
  $(".live-caret", t)?.remove();

  const s = it.segments[index];
  const seg = document.createElement("div");
  seg.className = "seg";
  seg.dataset.index = index;

  const ts = document.createElement("button");
  ts.className = "ts";
  ts.type = "button";
  ts.textContent = formatTime(s.start);
  ts.title = "Ouvir a partir daqui";
  ts.onclick = () => seek(s.start, true);

  const text = document.createElement("span");
  text.className = "seg-text";
  text.contentEditable = String(it.status === "done");
  text.spellcheck = true;
  text.innerHTML = highlight(s.text, state.query);
  text.oninput = () => {
    s.text = text.textContent;
    saveSegments(it.id);
  };

  seg.append(ts, text, " ");
  t.append(seg);

  if (it.status === "running") {
    const caret = document.createElement("span");
    caret.className = "live-caret";
    t.append(caret);
  }
  renderTranscriptInfo(it);
}

function renderTranscriptInfo(it) {
  const info = [];
  const words = it.segments ? wordCount(it.segments.map((s) => s.text).join(" ")) : wordCount(it.text);
  if (words) info.push(`${words} palavras`);
  if (it.status === "running" && it.progress) info.push(`${Math.round(it.progress * 100)}%`);
  el.tInfo.textContent = info.join(" · ");
}

function highlight(text, query) {
  const safe = esc(text);
  const terms = query.split(/\s+/).filter((t) => t.length > 1).map((t) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return safe;
  return safe.replace(new RegExp(`(${terms.join("|")})`, "gi"), "<mark>$1</mark>");
}

// ------------------------------------------------------------ edição

function showSaving(text) {
  el.saveState.textContent = text;
  el.saveState.style.opacity = 1;
  if (text === "Salvo") setTimeout(() => (el.saveState.style.opacity = 0), 1500);
}

async function patch(id, fields, { quiet = false } = {}) {
  if (!quiet) showSaving("Salvando…");
  try {
    const item = await api("PATCH", `/transcriptions/${id}`, fields);
    const prev = state.items.get(id);
    state.items.set(id, { ...prev, ...item, segments: prev?.segments });
    renderRow(state.items.get(id));
    renderCounts();
    if (id === state.selectedId) renderHead(state.items.get(id));
    if (!quiet) showSaving("Salvo");
    return item;
  } catch (err) {
    showSaving("");
    toast(err.message, true);
  }
}

const saveSegments = debounce((id) => {
  const it = state.items.get(id);
  if (it?.segments) patch(id, { segments: it.segments });
}, 700);

const saveNotes = debounce((id, notes) => patch(id, { notes }), 600);

function flushEdits() {
  const it = selected();
  if (!it) return;
  if (document.activeElement === el.notes && el.notes.value !== (it.notes || "")) saveNotes.flush(it.id, el.notes.value);
  if (document.activeElement === el.title) el.title.blur();
}

el.title.addEventListener("keydown", (e) => {
  if (e.key === "Enter") el.title.blur();
  if (e.key === "Escape") {
    el.title.value = selected()?.title || "";
    el.title.blur();
  }
});
el.title.addEventListener("blur", () => {
  const it = selected();
  const value = el.title.value.trim();
  if (!it) return;
  if (!value) el.title.value = it.title;
  else if (value !== it.title) patch(it.id, { title: value });
});

el.notes.addEventListener("input", () => {
  autosize(el.notes);
  const it = selected();
  if (it) saveNotes(it.id, el.notes.value);
});

el.resolved.onclick = () => {
  const it = selected();
  if (it) patch(it.id, { resolved: !it.resolved }, { quiet: true });
};

el.client.onclick = () => {
  const it = selected();
  if (it) pickClient(el.client, it.client_id, (clientId) => patch(it.id, { client_id: clientId }, { quiet: true }));
};

function pickClient(anchor, currentId, onPick) {
  openMenu(anchor, {
    search: true,
    placeholder: "Buscar ou criar cliente…",
    items: [
      { label: "Sem cliente", icon: "x", tick: !currentId, onClick: () => onPick(null) },
      { type: "sep" },
      ...state.clients.map((c) => ({
        label: c.name,
        avatar: c.name,
        hint: c.count ? String(c.count) : "",
        tick: c.id === currentId,
        onClick: () => onPick(c.id),
      })),
    ],
    onCreate: async (name) => {
      try {
        const client = await api("POST", "/clients", { name });
        await reloadClients();
        onPick(client.id);
        toast(`Cliente “${client.name}” criado`);
      } catch (err) {
        toast(err.message, true);
      }
    },
  });
}

async function reloadClients() {
  state.clients = await api("GET", "/clients");
  renderClientFilter();
  renderList();
  if (selected()) renderHead(selected());
}

// ------------------------------------------------------------ ações do áudio

$("#vAi").onclick = () => {
  const it = selected();
  if (it) sendToAI([it], defaultTemplate());
};
$("#vAiMenu").onclick = (e) => {
  const it = selected();
  if (it) openAiMenu(e.currentTarget, [it]);
};
$("#vCopy").onclick = () => {
  const it = selected();
  if (it) copyText(transcriptText(it), "Transcrição copiada");
};

$("#vMore").onclick = (e) => {
  const it = selected();
  if (!it) return;
  const busy = it.status === "queued" || it.status === "running";
  const hasText = it.status === "done" && it.segments?.length;
  openMenu(e.currentTarget, {
    align: "right",
    items: [
      { label: "Baixar texto (.txt)", icon: "file-text", disabled: !hasText, onClick: () => download(`${fileBase(it)}.txt`, transcriptText(it)) },
      { label: "Baixar legenda (.srt)", icon: "captions", disabled: !hasText, onClick: () => download(`${fileBase(it)}.srt`, toSrt(it.segments)) },
      { label: "Copiar com tempos", icon: "clock", disabled: !hasText, onClick: () => copyText(timedText(it), "Copiado com tempos") },
      { label: "Baixar áudio original", icon: "download", onClick: () => downloadUrl(`/api/transcriptions/${it.id}/audio`, it.original_name) },
      { type: "sep" },
      { type: "header", label: "Refazer transcrição" },
      ...Object.entries(state.config.models).map(([name, label]) => ({
        label: `Com o modelo ${label}`,
        icon: "rotate-ccw",
        hint: name === it.model ? "atual" : "",
        disabled: busy,
        onClick: () => retranscribe(it.id, name),
      })),
      { type: "sep" },
      { label: "Excluir", icon: "trash-2", danger: true, onClick: () => deleteItems([it.id]) },
    ],
  });
};

async function retranscribe(id, model) {
  try {
    const item = await api("POST", `/transcriptions/${id}/retranscribe`, { model, language: el.language.value });
    upsert(item);
    toast(`Refazendo com o modelo ${state.config.models[model]}`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteItems(ids) {
  const n = ids.length;
  const one = n === 1 ? state.items.get(ids[0]) : null;
  const ok = await confirmDialog({
    title: n === 1 ? "Excluir este áudio?" : `Excluir ${n} áudios?`,
    text: one
      ? `“${one.title}” e a transcrição serão apagados deste computador. Não dá para desfazer.`
      : "Os áudios e as transcrições serão apagados deste computador. Não dá para desfazer.",
  });
  if (!ok) return;
  try {
    if (n === 1) await api("DELETE", `/transcriptions/${ids[0]}`);
    else await api("POST", "/transcriptions/bulk", { ids, action: "delete" });
    ids.forEach(removeLocal);
    afterRemoval();
    toast(n === 1 ? "Áudio excluído" : `${n} áudios excluídos`);
  } catch (err) {
    toast(err.message, true);
  }
}

// ------------------------------------------------------------ ações em lote

const checkedItems = () =>
  [...state.checked].map((id) => state.items.get(id)).filter(Boolean).sort((a, b) => itemDate(a) - itemDate(b));
const checkedReady = () => checkedItems().filter((i) => i.status === "done" && i.text);

$("#bulkClear").onclick = clearChecked;
$("#bulkAi").onclick = () => {
  const list = checkedReady();
  if (!list.length) return toast("Nenhum dos selecionados terminou de transcrever", true);
  sendToAI(list, defaultTemplate());
};
$("#bulkAi").oncontextmenu = (e) => {
  e.preventDefault();
  const list = checkedReady();
  if (list.length) openAiMenu(e.currentTarget, list);
};
$("#bulkCopy").onclick = () => {
  const list = checkedReady();
  if (!list.length) return toast("Nenhum dos selecionados tem texto ainda", true);
  copyText(combinedText(list), `${list.length} transcrições copiadas`);
};
$("#bulkDownload").onclick = () => {
  const list = checkedReady();
  if (list.length) download("transcricoes.txt", combinedText(list));
};
$("#bulkResolve").onclick = async () => {
  const list = checkedItems();
  const action = list.every((i) => i.resolved) ? "unresolve" : "resolve";
  await bulk(list.map((i) => i.id), action);
  toast(action === "resolve" ? "Marcados como resolvidos" : "Marcados como pendentes");
};
$("#bulkClient").onclick = (e) => {
  const ids = [...state.checked];
  pickClient(e.currentTarget, null, async (clientId) => {
    await bulk(ids, "set_client", clientId);
    toast(clientId ? `Cliente definido para ${ids.length} áudios` : "Cliente removido");
  });
};
$("#bulkDelete").onclick = () => deleteItems([...state.checked]);

async function bulk(ids, action, clientId = null) {
  try {
    await api("POST", "/transcriptions/bulk", { ids, action, client_id: clientId });
    ids.forEach((id) => {
      const it = state.items.get(id);
      if (!it) return;
      if (action === "resolve" || action === "unresolve") it.resolved = action === "resolve";
      if (action === "set_client") it.client_id = clientId;
    });
    if (action === "set_client") await reloadClients();
    else renderList();
    if (selected()) renderHead(selected());
  } catch (err) {
    toast(err.message, true);
  }
}

// =====================================================================
// Enviar para a IA
// =====================================================================
function openAiMenu(anchor, list) {
  const current = defaultTemplate();
  openMenu(anchor, {
    align: "right",
    items: [
      { type: "header", label: list.length > 1 ? `Prompt para ${list.length} áudios` : "Escolha o prompt" },
      ...state.templates.map((t) => ({
        label: t.name,
        icon: "message-square-text",
        tick: t.id === current?.id,
        onClick: () => sendToAI(list, t),
      })),
      { type: "sep" },
      { type: "header", label: "Abrir em" },
      ...Object.entries(AI_TARGETS).map(([key, target]) => ({
        label: target.label,
        icon: key === "copy" ? "copy" : "external-link",
        tick: state.settings.ai === key,
        keepOpen: true,
        onClick: async () => {
          await saveSettings({ ai: key });
          openAiMenu(anchor, list);
        },
      })),
      { type: "sep" },
      { label: "Editar prompts…", icon: "pencil", onClick: () => openSettings("templates") },
    ],
  });
}

function buildPrompt(template, list) {
  const clients = [...new Set(list.map((i) => clientById(i.client_id)?.name).filter(Boolean))];
  const texto = list.length === 1
    ? list[0].text
    : list.map((it, i) => `[Áudio ${i + 1} · ${formatDateTime(itemDate(it))}]\n${it.text}`).join("\n\n");
  const data = list.length === 1
    ? formatDateTime(itemDate(list[0]))
    : `${formatDateTime(itemDate(list[0]))} a ${formatDateTime(itemDate(list[list.length - 1]))}`;
  const values = {
    texto,
    cliente: clients.length ? clients.join(" e ") : "um cliente",
    data,
    titulo: list.map((i) => i.title).join("; "),
    notas: list.map((i) => i.notes).filter(Boolean).join("\n") || "(sem notas)",
  };
  const body = template?.body || "{texto}";
  return body.replace(/\{(texto|cliente|data|titulo|notas)\}/g, (_, key) => values[key]);
}

async function sendToAI(list, template) {
  const prompt = buildPrompt(template, list);
  const target = AI_TARGETS[state.settings.ai] || AI_TARGETS.copy;
  // Copia antes de abrir a aba: o navegador só permite copiar com a página em foco
  const copied = await copyText(prompt);
  if (!copied) return;

  if (target.url) {
    const url = target.url + encodeURIComponent(prompt);
    const fits = url.length < 8000;
    window.open(fits ? url : target.url.split("?")[0], "_blank", "noopener");
    toast(fits
      ? `Prompt copiado e aberto no ${target.label}`
      : `Texto longo: abri o ${target.label}, é só colar com Ctrl+V`);
  } else {
    toast("Prompt copiado. É só colar na IA");
  }

  if (state.settings.resolve_on_send) {
    const ids = list.filter((i) => !i.resolved).map((i) => i.id);
    if (ids.length) bulk(ids, "resolve");
  }
}

// =====================================================================
// Menu suspenso genérico
// =====================================================================
const menu = $("#menu");
let menuCleanup = null;

function openMenu(anchor, { items, search = false, placeholder = "Buscar…", onCreate = null, align = "left" }) {
  closeMenu();
  menu.innerHTML = "";
  menu.hidden = false;

  let input = null;
  if (search) {
    input = document.createElement("input");
    input.className = "menu-search";
    input.placeholder = placeholder;
    menu.append(input);
  }
  const list = document.createElement("div");
  menu.append(list);

  const render = () => {
    const q = normalize(input?.value.trim() || "");
    list.innerHTML = "";
    const shown = q ? items.filter((it) => !it.type && normalize(it.label).includes(q)) : items;
    for (const it of shown) {
      if (it.type === "sep") {
        list.insertAdjacentHTML("beforeend", '<div class="menu-sep"></div>');
      } else if (it.type === "header") {
        list.insertAdjacentHTML("beforeend", `<div class="menu-header">${esc(it.label)}</div>`);
      } else {
        const b = document.createElement("button");
        b.type = "button";
        b.className = `menu-item${it.danger ? " danger" : ""}`;
        b.disabled = !!it.disabled;
        if (it.disabled) b.style.opacity = ".45";
        b.innerHTML = `${it.avatar ? avatar(it.avatar) : it.icon ? icon(it.icon) : ""}<span class="label">${esc(it.label)}</span>${
          it.hint ? `<span class="hint">${esc(it.hint)}</span>` : ""}${it.tick ? `<span class="tick">${icon("check")}</span>` : ""}`;
        b.onclick = () => {
          if (!it.keepOpen) closeMenu();
          it.onClick();
        };
        list.append(b);
      }
    }
    const typed = input?.value.trim();
    if (typed && onCreate && !items.some((it) => !it.type && normalize(it.label) === q)) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "menu-item";
      b.innerHTML = `${icon("plus")}<span class="label">Criar “${esc(typed)}”</span>`;
      b.onclick = () => {
        closeMenu();
        onCreate(typed);
      };
      list.append(b);
    }
    if (!list.children.length) list.innerHTML = '<div class="menu-empty">Nada encontrado</div>';
  };
  render();

  if (input) {
    input.addEventListener("input", render);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        $(".menu-item:not(:disabled)", list)?.click();
      }
    });
  }

  // Posiciona junto ao botão, sem sair da tela
  const r = anchor.getBoundingClientRect();
  const w = menu.offsetWidth;
  const h = menu.offsetHeight;
  let left = align === "right" ? r.right - w : r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
  let top = r.bottom + 4;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  input?.focus();

  const onDown = (e) => {
    if (!menu.contains(e.target) && !anchor.contains(e.target)) closeMenu();
  };
  const onKey = (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      closeMenu();
    }
  };
  setTimeout(() => document.addEventListener("pointerdown", onDown), 0);
  document.addEventListener("keydown", onKey, true);
  window.addEventListener("resize", closeMenu, { once: true });
  menuCleanup = () => {
    document.removeEventListener("pointerdown", onDown);
    document.removeEventListener("keydown", onKey, true);
  };
}

function closeMenu() {
  menu.hidden = true;
  menuCleanup?.();
  menuCleanup = null;
}

// =====================================================================
// Configurações: geral, prompts e clientes
// =====================================================================
const settingsModal = $("#settingsModal");

$("#openSettings").onclick = () => openSettings("general");
$$("[data-close]").forEach((b) => (b.onclick = () => b.closest("dialog").close()));
$$("dialog").forEach((d) =>
  d.addEventListener("click", (e) => {
    if (e.target === d) d.close();
  })
);

function openSettings(pane = "general") {
  closeMenu();
  renderGeneral();
  renderTemplateList();
  renderClientList();
  if (!editingTemplate) editTemplate(defaultTemplate());
  if (!editingClient) editClient(state.clients[0] || null);
  showPane(pane);
  if (!settingsModal.open) settingsModal.showModal();
}

function showPane(pane) {
  $$("#settingsTabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.pane === pane));
  $$(".pane", settingsModal).forEach((p) => (p.hidden = p.dataset.pane !== pane));
}
$$("#settingsTabs .tab").forEach((t) => (t.onclick = () => showPane(t.dataset.pane)));

async function saveSettings(values) {
  try {
    state.settings = await api("PUT", "/settings", values);
    renderGeneral();
  } catch (err) {
    toast(err.message, true);
  }
}

function renderGeneral() {
  $$("#aiPicker button").forEach((b) => {
    b.setAttribute("aria-checked", String(b.dataset.ai === state.settings.ai));
    b.onclick = () => saveSettings({ ai: b.dataset.ai });
  });
  const select = $("#defaultTemplate");
  select.innerHTML = state.templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  select.value = String(defaultTemplate()?.id ?? "");
  select.onchange = () => {
    saveSettings({ template_id: Number(select.value) });
    renderTemplateList();
  };
  const resolve = $("#resolveOnSend");
  resolve.checked = !!state.settings.resolve_on_send;
  resolve.onchange = () => saveSettings({ resolve_on_send: resolve.checked });
}

// ------------------------------------------------------------ prompts (CRUD)
let editingTemplate = null;
const templateForm = $("#templateForm");

function renderTemplateList() {
  const def = defaultTemplate();
  $("#templateList").innerHTML = state.templates.length
    ? state.templates.map((t) => `
        <li data-id="${t.id}" class="${editingTemplate?.id === t.id ? "active" : ""}">
          ${icon("message-square-text")}<span class="name">${esc(t.name)}</span>
          ${t.id === def?.id ? '<span class="star" title="Prompt padrão">padrão</span>' : ""}
        </li>`).join("")
    : '<li class="empty-li">Nenhum prompt</li>';
  $$("#templateList li[data-id]").forEach((li) =>
    (li.onclick = () => editTemplate(state.templates.find((t) => t.id === Number(li.dataset.id))))
  );
}

function editTemplate(t) {
  editingTemplate = t;
  templateForm.name.value = t?.name || "";
  templateForm.body.value = t?.body || "Mensagem de {cliente} ({data}):\n\n{texto}";
  $("#deleteTemplate").hidden = !t;
  renderTemplateList();
}

$("#newTemplate").onclick = () => {
  editTemplate(null);
  templateForm.name.focus();
};

$$(".var", templateForm).forEach((b) =>
  (b.onclick = () => {
    const ta = templateForm.body;
    const { selectionStart: s, selectionEnd: e } = ta;
    ta.setRangeText(b.dataset.var, s, e, "end");
    ta.focus();
  })
);

templateForm.onsubmit = async (e) => {
  e.preventDefault();
  const data = { name: templateForm.name.value.trim(), body: templateForm.body.value };
  try {
    const saved = editingTemplate
      ? await api("PATCH", `/templates/${editingTemplate.id}`, data)
      : await api("POST", "/templates", data);
    state.templates = await api("GET", "/templates");
    editTemplate(state.templates.find((t) => t.id === saved.id));
    renderGeneral();
    toast("Prompt salvo");
  } catch (err) {
    toast(err.message, true);
  }
};

$("#deleteTemplate").onclick = async () => {
  if (!editingTemplate) return;
  const ok = await confirmDialog({
    title: "Excluir este prompt?",
    text: `“${editingTemplate.name}” deixará de aparecer no menu “Enviar para IA”.`,
  });
  if (!ok) return;
  try {
    await api("DELETE", `/templates/${editingTemplate.id}`);
    state.templates = await api("GET", "/templates");
    if (state.settings.template_id === editingTemplate.id) await saveSettings({ template_id: null });
    editTemplate(state.templates[0] || null);
    renderGeneral();
    toast("Prompt excluído");
  } catch (err) {
    toast(err.message, true);
  }
};

// ------------------------------------------------------------ clientes (CRUD)
let editingClient = null;
const clientForm = $("#clientForm");

function renderClientList() {
  $("#clientList").innerHTML = state.clients.length
    ? state.clients.map((c) => `
        <li data-id="${c.id}" class="${editingClient?.id === c.id ? "active" : ""}">
          ${avatar(c.name)}<span class="name">${esc(c.name)}</span><span class="meta">${c.count}</span>
        </li>`).join("")
    : '<li class="empty-li">Nenhum cliente ainda</li>';
  $$("#clientList li[data-id]").forEach((li) =>
    (li.onclick = () => editClient(state.clients.find((c) => c.id === Number(li.dataset.id))))
  );
}

function editClient(c) {
  editingClient = c;
  clientForm.name.value = c?.name || "";
  clientForm.phone.value = c?.phone || "";
  clientForm.notes.value = c?.notes || "";
  $("#deleteClient").hidden = !c;
  $("#clientUsage").textContent = c
    ? c.count ? `${c.count} ${c.count === 1 ? "áudio vinculado" : "áudios vinculados"}` : "Nenhum áudio vinculado"
    : "Novo cliente";
  renderClientList();
}

$("#newClient").onclick = () => {
  editClient(null);
  clientForm.name.focus();
};

clientForm.onsubmit = async (e) => {
  e.preventDefault();
  const data = { name: clientForm.name.value.trim(), phone: clientForm.phone.value.trim(), notes: clientForm.notes.value };
  try {
    const saved = editingClient
      ? await api("PATCH", `/clients/${editingClient.id}`, data)
      : await api("POST", "/clients", data);
    await reloadClients();
    editClient(state.clients.find((c) => c.id === saved.id));
    toast("Cliente salvo");
  } catch (err) {
    toast(err.message, true);
  }
};

$("#deleteClient").onclick = async () => {
  if (!editingClient) return;
  const c = editingClient;
  const ok = await confirmDialog({
    title: `Excluir “${c.name}”?`,
    text: c.count
      ? `Os ${c.count} áudios deste cliente continuam salvos, só ficam sem cliente.`
      : "Este cliente não tem áudios vinculados.",
  });
  if (!ok) return;
  try {
    await api("DELETE", `/clients/${c.id}`);
    state.items.forEach((it) => it.client_id === c.id && (it.client_id = null));
    await reloadClients();
    editClient(state.clients[0] || null);
    toast("Cliente excluído");
  } catch (err) {
    toast(err.message, true);
  }
};

// ------------------------------------------------------------ confirmação
function confirmDialog({ title, text, ok = "Excluir" }) {
  const dialog = $("#confirmModal");
  $("#confirmTitle").textContent = title;
  $("#confirmText").textContent = text;
  $("#confirmOk").textContent = ok;
  dialog.showModal();
  $("#confirmOk").focus();
  return new Promise((resolve) => {
    const done = (value) => {
      dialog.removeEventListener("close", onClose);
      resolve(value);
    };
    const onClose = () => done(dialog.returnValue === "ok");
    dialog.returnValue = "";
    dialog.addEventListener("close", onClose);
    $("#confirmOk").onclick = () => dialog.close("ok");
  });
}

// =====================================================================
// Teclado
// =====================================================================
document.addEventListener("keydown", (e) => {
  if ($$("dialog").some((d) => d.open)) return;
  if (isEditing(e.target)) {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;

  if (e.key === "/") {
    e.preventDefault();
    el.search.focus();
    el.search.select();
  } else if (e.key === "Escape") {
    if (state.checked.size) clearChecked();
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const list = visibleItems();
    if (!list.length) return;
    e.preventDefault();
    const i = list.findIndex((it) => it.id === state.selectedId);
    const next = e.key === "ArrowDown" ? Math.min(list.length - 1, i + 1) : Math.max(0, i - 1);
    select(list[i === -1 ? 0 : next].id);
  } else if (e.code === "Space" && selected() && e.target.tagName !== "BUTTON") {
    e.preventDefault();
    togglePlay();
  } else if (e.key.toLowerCase() === "i") {
    if (state.checked.size) $("#bulkAi").click();
    else if (!$("#vAi").disabled) $("#vAi").click();
  }
});

function isEditing(target) {
  return target.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName);
}

// =====================================================================
// Player com forma de onda
// =====================================================================
let speedIdx = 0;
let lastCurrent = -1;

el.play.onclick = togglePlay;
el.speed.onclick = () => {
  speedIdx = (speedIdx + 1) % SPEEDS.length;
  el.audio.playbackRate = SPEEDS[speedIdx];
  el.speed.textContent = `${SPEEDS[speedIdx]}×`.replace(".", ",");
};

function togglePlay() {
  if (!selected()) return;
  el.audio.paused ? el.audio.play() : el.audio.pause();
}

function setPlayIcon(playing) {
  el.play.innerHTML = icon(playing ? "pause" : "play");
  el.play.setAttribute("aria-label", playing ? "Pausar" : "Tocar");
}

el.audio.addEventListener("play", () => setPlayIcon(true));
el.audio.addEventListener("pause", () => setPlayIcon(false));
el.audio.addEventListener("ended", () => setPlayIcon(false));
el.audio.addEventListener("loadedmetadata", updateTime);
el.audio.addEventListener("timeupdate", () => {
  updateTime();
  drawWave();
  highlightSegment();
});

function audioDuration() {
  return isFinite(el.audio.duration) ? el.audio.duration : selected()?.duration || 0;
}

function updateTime() {
  el.time.textContent = `${formatTime(el.audio.currentTime || 0)} / ${formatTime(audioDuration())}`;
}

function seek(sec, play = false) {
  el.audio.currentTime = Math.max(0, Math.min(sec, audioDuration()));
  drawWave();
  if (play) el.audio.play();
}

function highlightSegment() {
  const it = selected();
  if (!it?.segments) return;
  const t = el.audio.currentTime;
  const idx = el.audio.paused && t === 0 ? -1 : it.segments.findIndex((s) => t >= s.start && t < s.end);
  if (idx === lastCurrent) return;
  $$(".seg.current", el.transcript).forEach((n) => n.classList.remove("current"));
  if (idx >= 0) $(`.seg[data-index="${idx}"]`, el.transcript)?.classList.add("current");
  lastCurrent = idx;
}

let scrubbing = false;
el.wave.addEventListener("pointerdown", (e) => {
  scrubbing = true;
  el.wave.setPointerCapture(e.pointerId);
  scrubTo(e);
});
el.wave.addEventListener("pointermove", (e) => scrubbing && scrubTo(e));
el.wave.addEventListener("pointerup", () => (scrubbing = false));

function scrubTo(e) {
  const r = el.wave.getBoundingClientRect();
  seek(((e.clientX - r.left) / r.width) * audioDuration());
}

let audioCtx;
async function loadPeaks(id) {
  const it = state.items.get(id);
  if (peaksCache.has(id) || !it || it.size > 60 * 1024 * 1024) return;
  peaksCache.set(id, null);
  try {
    const data = await (await fetch(`/api/transcriptions/${id}/audio`)).arrayBuffer();
    audioCtx ??= new AudioContext();
    const buffer = await audioCtx.decodeAudioData(data);
    const channel = buffer.getChannelData(0);
    const n = 600;
    const block = Math.floor(channel.length / n) || 1;
    const peaks = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      let max = 0;
      for (let j = i * block, end = Math.min(channel.length, j + block); j < end; j += 4) {
        const v = Math.abs(channel[j]);
        if (v > max) max = v;
      }
      peaks[i] = max;
    }
    const top = Math.max(...peaks) || 1;
    peaksCache.set(id, peaks.map((p) => Math.pow(p / top, 0.8)));
    if (state.selectedId === id) drawWave();
  } catch {
    // Formato não suportado pelo navegador: a onda fica lisa e o player continua funcionando
  }
}

function drawWave() {
  const canvas = el.canvas;
  const dpr = window.devicePixelRatio || 1;
  const { width, height } = el.wave.getBoundingClientRect();
  if (!width) return;
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const styles = getComputedStyle(document.documentElement);
  const played = styles.getPropertyValue("--accent").trim();
  const rest = styles.getPropertyValue("--wave").trim();

  const bar = 2;
  const gap = 2;
  const count = Math.floor(width / (bar + gap));
  const peaks = peaksCache.get(state.selectedId);
  const dur = audioDuration();
  const progress = dur ? el.audio.currentTime / dur : 0;

  for (let i = 0; i < count; i++) {
    let v = 0.08;
    if (peaks) {
      const from = Math.floor((i / count) * peaks.length);
      const to = Math.max(from + 1, Math.floor(((i + 1) / count) * peaks.length));
      v = 0;
      for (let k = from; k < to; k++) v = Math.max(v, peaks[k]);
      v = Math.max(0.06, v);
    }
    const h = Math.max(2, v * height);
    ctx.fillStyle = i / count < progress ? played : rest;
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(i * (bar + gap), (height - h) / 2, bar, h, 1) : ctx.rect(i * (bar + gap), (height - h) / 2, bar, h);
    ctx.fill();
  }
}

new ResizeObserver(drawWave).observe(el.wave);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawWave);

el.timestamps.addEventListener("change", () => {
  store.set("timestamps", el.timestamps.checked ? "1" : "0");
  el.transcript.classList.toggle("timed", el.timestamps.checked);
});

// =====================================================================
// Textos, exportação e formatação
// =====================================================================
function transcriptText(it) {
  return el.timestamps.checked && it.segments?.length ? timedText(it) : it.text;
}

function timedText(it) {
  return (it.segments || []).filter((s) => s.text.trim()).map((s) => `[${formatTime(s.start)}] ${s.text.trim()}`).join("\n");
}

function combinedText(list) {
  return list
    .map((it) => {
      const client = clientById(it.client_id);
      const head = [it.title, formatDateTime(itemDate(it)), client?.name].filter(Boolean).join(" · ");
      return `${head}\n${it.text}`;
    })
    .join("\n\n---\n\n");
}

function toSrt(segments) {
  const ts = (sec) => {
    const ms = Math.round(sec * 1000);
    const h = String(Math.floor(ms / 3600000)).padStart(2, "0");
    const m = String(Math.floor((ms % 3600000) / 60000)).padStart(2, "0");
    const s = String(Math.floor((ms % 60000) / 1000)).padStart(2, "0");
    return `${h}:${m}:${s},${String(ms % 1000).padStart(3, "0")}`;
  };
  return segments.map((s, i) => `${i + 1}\n${ts(s.start)} --> ${ts(s.end)}\n${s.text.trim()}\n`).join("\n");
}

function fileBase(it) {
  return (it.title || "transcricao").replace(/[\\/:*?"<>|…]+/g, "").trim().slice(0, 80) || "transcricao";
}

function wordCount(text) {
  return String(text || "").match(/\S+/g)?.length || 0;
}

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    if (message) toast(message);
    return true;
  } catch {
    toast("Não consegui copiar. Clique na página e tente de novo", true);
    return false;
  }
}

function download(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  downloadUrl(url, filename);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function downloadUrl(url, filename) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
}

function avatar(name) {
  const initials = name.trim().split(/\s+/).slice(0, 2).map((p) => p[0]).join("").toUpperCase();
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return `<span class="avatar" style="background:${AVATAR_COLORS[hash % AVATAR_COLORS.length]}">${esc(initials)}</span>`;
}

function autosize(ta) {
  ta.style.height = "auto";
  ta.style.height = `${ta.scrollHeight}px`;
}

function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatSeconds(sec) {
  return sec < 60 ? `${sec.toFixed(1).replace(".", ",")}s` : formatTime(sec);
}

function formatDateTime(ms) {
  return new Date(ms).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).replace(",", " às");
}

function formatSize(bytes) {
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : `${(bytes / 1024 / 1024).toFixed(1).replace(".", ",")} MB`;
}

let toastTimer;
function toast(message, isError = false) {
  const t = $("#toast");
  t.classList.toggle("error", isError);
  t.innerHTML = `${icon(isError ? "circle-alert" : "check")}<span></span>`;
  $("span", t).textContent = message;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

init().catch((err) => toast(`Falha ao carregar: ${err.message}`, true));
