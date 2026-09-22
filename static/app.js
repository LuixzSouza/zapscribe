const $ = (sel, el = document) => el.querySelector(sel);

const dropzone = $("#dropzone");
const fileInput = $("#fileInput");
const results = $("#results");
const modelSelect = $("#model");
const languageSelect = $("#language");
const timestampsCheck = $("#timestamps");
const toolbar = $("#toolbar");
const cardTpl = $("#cardTpl");

const config = { parallel: 3, precise: "large-v3-turbo", models: {} };
const jobs = []; // na ordem em que foram adicionados
let counter = 0;
let filter = "all";

// ---- Configuração inicial ----
fetch("/api/config")
  .then((r) => r.json())
  .then((cfg) => {
    Object.assign(config, cfg);
    for (const [name, label] of Object.entries(cfg.models)) {
      modelSelect.add(new Option(`${label} (${name})`, name, false, name === cfg.default));
    }
    const saved = safeStorage("get", "model");
    if (saved && cfg.models[saved]) modelSelect.value = saved;
  });

modelSelect.addEventListener("change", () => safeStorage("set", "model", modelSelect.value));
timestampsCheck.addEventListener("change", () => jobs.forEach(renderText));

// ---- Entrada de arquivos: clique, arrastar e colar ----
fileInput.addEventListener("change", () => {
  addFiles(fileInput.files);
  fileInput.value = "";
});

["dragenter", "dragover"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.add("over");
  })
);
["dragleave", "drop"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.remove("over");
  })
);
dropzone.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));

// Permite soltar em qualquer lugar da página
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault();
  if (!dropzone.contains(e.target)) addFiles(e.dataTransfer.files);
});

document.addEventListener("paste", (e) => {
  if (e.target.isContentEditable) return;
  if (e.clipboardData.files.length) addFiles(e.clipboardData.files);
});

function addFiles(files) {
  const list = [...files].filter((f) => /^(audio|video)\//.test(f.type) || /\.(ogg|opus|m4a|mp3|wav|aac|amr|webm|mp4|flac|wma)$/i.test(f.name));
  if (!list.length) return showToast("Nenhum arquivo de áudio encontrado");

  for (const file of list) {
    const job = createJob(file);
    jobs.push(job);
    results.append(job.card);
  }
  updateToolbar();
  pump();
}

// ---- Job / card ----
function createJob(file) {
  const card = cardTpl.content.firstElementChild.cloneNode(true);
  const job = {
    id: ++counter,
    file,
    card,
    state: "queued",
    model: modelSelect.value,
    language: languageSelect.value,
    segments: [],
    duration: 0,
    progress: 0,
  };

  $(".num", card).textContent = `#${job.id}`;
  $(".file-name", card).textContent = friendlyName(file.name);
  $(".file-name", card).title = file.name;
  const audio = $("audio", card);
  audio.src = URL.createObjectURL(file);
  audio.addEventListener("loadedmetadata", () => {
    if (!job.duration && isFinite(audio.duration)) job.duration = audio.duration;
    renderMeta(job);
  });
  renderMeta(job);

  $(".collapse", card).onclick = () => card.classList.toggle("collapsed");
  $(".copy", card).onclick = async () => {
    await navigator.clipboard.writeText(getPlainText(job));
    showToast("Texto copiado!");
  };
  $(".download", card).onclick = () =>
    downloadText(baseName(file.name) + ".txt", getPlainText(job));
  $(".retry", card).onclick = () => {
    job.model = config.precise;
    resetJob(job);
    pump();
  };
  $(".remove", card).onclick = () => removeJob(job);

  return job;
}

function resetJob(job) {
  job.segments = [];
  job.progress = 0;
  job.startedAt = null;
  $(".text", job.card).textContent = "";
  $(".bar", job.card).style.width = "0";
  $(".copy", job.card).disabled = true;
  $(".download", job.card).disabled = true;
  $(".retry", job.card).hidden = true;
  setState(job, "queued", "Na fila");
  renderMeta(job);
}

function removeJob(job) {
  job.controller?.abort();
  URL.revokeObjectURL($("audio", job.card).src);
  job.card.remove();
  jobs.splice(jobs.indexOf(job), 1);
  updateToolbar();
  pump();
}

function setState(job, state, label) {
  job.state = state;
  job.card.dataset.state = state;
  $(".status", job.card).textContent = label;
  applyFilter(job);
  updateToolbar();
}

// ---- Fila: vários áudios ao mesmo tempo ----
function pump() {
  const running = jobs.filter((j) => j.state === "running").length;
  const waiting = jobs.filter((j) => j.state === "queued");
  for (const job of waiting.slice(0, Math.max(0, config.parallel - running))) {
    transcribe(job);
  }
}

async function transcribe(job) {
  const { card } = job;
  const progress = $(".progress", card);
  const bar = $(".bar", card);

  setState(job, "running", "Enviando...");
  progress.classList.add("indeterminate");
  job.startedAt = performance.now();
  job.timer = setInterval(() => renderMeta(job), 1000);

  const form = new FormData();
  form.append("file", job.file);
  form.append("model", job.model);
  form.append("language", job.language);
  job.controller = new AbortController();

  try {
    const res = await fetch("/api/transcribe", {
      method: "POST",
      body: form,
      signal: job.controller.signal,
    });
    if (!res.ok) throw new Error(`Erro ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finished = false;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.trim()) continue;
        const msg = JSON.parse(line);
        if (msg.type === "status") {
          setState(job, "running", msg.message);
        } else if (msg.type === "info") {
          job.duration = msg.duration;
          progress.classList.remove("indeterminate");
          setState(job, "running", "Transcrevendo...");
        } else if (msg.type === "segment") {
          job.segments.push(msg);
          job.progress = job.duration ? Math.min(1, msg.end / job.duration) : 0;
          bar.style.width = `${job.progress * 100}%`;
          renderText(job);
          updateToolbar();
        } else if (msg.type === "done") {
          finished = true;
          job.elapsed = msg.elapsed;
          job.progress = 1;
          if (!job.segments.length) $(".text", card).textContent = "(Nenhuma fala detectada)";
        } else if (msg.type === "error") {
          throw new Error(msg.message);
        }
      }
    }
    if (!finished) throw new Error("Conexão interrompida");

    setState(job, "done", `Concluído em ${formatSeconds(job.elapsed)}`);
    $(".copy", card).disabled = false;
    $(".download", card).disabled = false;
    $(".retry", card).hidden = job.model === config.precise;
  } catch (err) {
    if (err.name === "AbortError") return;
    setState(job, "error", "Erro");
    $(".text", card).textContent = "Falha ao transcrever: " + err.message;
    $(".retry", card).hidden = false;
  } finally {
    clearInterval(job.timer);
    progress.classList.remove("indeterminate");
    renderMeta(job);
    pump();
  }
}

// ---- Barra de resumo, abas e ações em lote ----
function updateToolbar() {
  toolbar.hidden = jobs.length === 0;
  dropzone.classList.toggle("compact", jobs.length > 0);

  const c = { queued: 0, running: 0, done: 0, error: 0 };
  jobs.forEach((j) => c[j.state]++);

  const parts = [];
  if (c.running) parts.push(`<b>${c.running}</b> transcrevendo`);
  if (c.queued) parts.push(`<b>${c.queued}</b> na fila`);
  parts.push(`<b>${c.done}</b> de ${jobs.length} concluídos`);
  if (c.error) parts.push(`<b>${c.error}</b> com erro`);
  $("#counts").innerHTML = parts.join(" · ");

  const total = jobs.reduce((sum, j) => sum + (j.state === "done" || j.state === "error" ? 1 : j.progress), 0);
  $("#overallBar").style.width = jobs.length ? `${(total / jobs.length) * 100}%` : "0";

  const tabCounts = { all: jobs.length, active: c.running + c.queued, done: c.done, error: c.error };
  document.querySelectorAll(".tab").forEach((t) => {
    $("span", t).textContent = tabCounts[t.dataset.filter];
  });
  $("#tabs [data-filter=error]").hidden = c.error === 0 && filter !== "error";

  const doneJobs = jobs.filter((j) => j.state === "done");
  $("#copyAll").disabled = $("#downloadAll").disabled = $("#clearDone").disabled = !doneJobs.length;

  const visible = jobs.filter(matchesFilter).length;
  $("#emptyFilter").hidden = !jobs.length || visible > 0;
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    filter = tab.dataset.filter;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    jobs.forEach(applyFilter);
    updateToolbar();
  };
});

function matchesFilter(job) {
  if (filter === "all") return true;
  if (filter === "active") return job.state === "running" || job.state === "queued";
  return job.state === filter;
}

function applyFilter(job) {
  job.card.hidden = !matchesFilter(job);
}

function allText() {
  return jobs
    .filter((j) => j.state === "done")
    .map((j) => `#${j.id} · ${friendlyName(j.file.name)}\n${getPlainText(j)}`)
    .join("\n\n");
}

$("#copyAll").onclick = async () => {
  await navigator.clipboard.writeText(allText());
  showToast("Todas as transcrições copiadas!");
};
$("#downloadAll").onclick = () => downloadText("transcricoes.txt", allText());
$("#clearDone").onclick = () => jobs.filter((j) => j.state === "done").forEach(removeJob);

// ---- Renderização ----
function renderText(job) {
  const el = $(".text", job.card);
  if (!job.segments.length) return;
  el.innerHTML = "";
  if (timestampsCheck.checked) {
    job.segments.forEach((s, i) => {
      const ts = document.createElement("span");
      ts.className = "ts";
      ts.textContent = `[${formatTime(s.start)}]`;
      el.append(ts, s.text, i < job.segments.length - 1 ? "\n" : "");
    });
  } else {
    el.textContent = job.segments.map((s) => s.text).join(" ");
  }
}

function renderMeta(job) {
  const parts = [];
  if (job.duration) parts.push(formatTime(job.duration));
  parts.push(formatSize(job.file.size));
  parts.push(config.models[job.model] || job.model);
  if (job.state === "running" && job.startedAt) {
    parts.push(`${Math.floor((performance.now() - job.startedAt) / 1000)}s`);
  }
  $(".meta", job.card).textContent = parts.join(" · ");
}

function getPlainText(job) {
  // Usa o conteúdo da caixa (o texto pode ter sido editado)
  return $(".text", job.card).innerText.trim();
}

// ---- Utilitários ----

// "WhatsApp Ptt 2026-09-22 at 14.03.12.ogg" -> "Áudio do WhatsApp · 22/09/2026 14:03"
function friendlyName(name) {
  const m = name.match(/whatsapp.*?(\d{4})-(\d{2})-(\d{2}).*?(\d{2})\.(\d{2})(?:\.(\d{2}))?/i);
  if (!m) return name;
  const [, y, mo, d, h, mi] = m;
  const dup = name.match(/\((\d+)\)\.\w+$/);
  return `Áudio do WhatsApp · ${d}/${mo}/${y} ${h}:${mi}${dup ? ` (${dup[1]})` : ""}`;
}

function baseName(name) {
  return name.replace(/\.[^.]+$/, "");
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatSeconds(sec) {
  return sec < 60 ? `${sec.toFixed(1).replace(".", ",")}s` : formatTime(sec);
}

function formatSize(bytes) {
  return bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1).replace(".", ",")} MB`;
}

function safeStorage(op, key, value) {
  try {
    return op === "get" ? localStorage.getItem(key) : localStorage.setItem(key, value);
  } catch {
    return null;
  }
}

let toastTimer;
function showToast(msg) {
  const toast = $("#toast");
  toast.textContent = msg;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 1800);
}
