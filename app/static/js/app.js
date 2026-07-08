// PanoForge — contrôleur principal du frontend (vanilla JS, sans framework)

import * as api from "/js/api.js";
import { Viewer360 } from "/js/viewer.js";
import { openFileBrowser, mountShortcuts } from "/js/filebrowser.js";

const state = {
  config: { source_dir: "", output_dir: "", has_nvenc: false, version: "" },
  files: [],
  selected: new Set(),
  jobs: [],
  view: "files",
  optionsInputs: [], // fichiers ciblés par le panneau d'options actuellement ouvert
  previewJobRef: null, // input path of file used for GPX analyze reference
  waitingPreviewJobId: null, // job dont on attend le proxy H.264 (preview_url)
  photoSource: null, // chemin du média source pour l'extraction de photo (OSV/MP4/JPG)
};

let viewer = null;

// ---------- Utilitaires de formatage ----------

function formatBytes(bytes) {
  if (bytes == null) return "—";
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatDuration(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h${String(m).padStart(2, "0")}m${String(sec).padStart(2, "0")}s`;
  return `${m}m${String(sec).padStart(2, "0")}s`;
}

function formatEta(seconds) {
  if (seconds == null || Number.isNaN(seconds) || seconds < 0) return "—";
  return formatDuration(seconds);
}

const STATUS_LABELS = {
  queued: "En attente",
  running: "En cours",
  done: "Terminé",
  error: "Erreur",
  cancelled: "Annulé",
};

function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}

function basename(path) {
  if (!path) return "";
  return path.split(/[/\\]/).pop();
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 4500);
}

// ---------- Vues ----------

const tabButtons = {
  files: document.getElementById("tab-files"),
  queue: document.getElementById("tab-queue"),
  preview: document.getElementById("tab-preview"),
};
const viewSections = {
  files: document.getElementById("view-files"),
  queue: document.getElementById("view-queue"),
  preview: document.getElementById("view-preview"),
};

function switchView(view) {
  state.view = view;
  for (const key of Object.keys(viewSections)) {
    const isActive = key === view;
    viewSections[key].hidden = !isActive;
    tabButtons[key].setAttribute("aria-selected", String(isActive));
  }
  if (view === "queue") {
    refreshJobs();
  }
}

for (const [key, btn] of Object.entries(tabButtons)) {
  btn.addEventListener("click", () => switchView(key));
}

// ---------- Paramètres (dossiers) ----------

const settingsOverlay = document.getElementById("settings-overlay");
const settingsSourceDir = document.getElementById("settings-source-dir");
const settingsOutputDir = document.getElementById("settings-output-dir");
const settingsVersion = document.getElementById("settings-version");

document.getElementById("settings-btn").addEventListener("click", () => {
  settingsSourceDir.value = state.config.source_dir || "";
  settingsOutputDir.value = state.config.output_dir || "";
  settingsVersion.textContent = state.config.version ? `Version : ${state.config.version}` : "";
  settingsOverlay.hidden = false;
});
document.getElementById("settings-close").addEventListener("click", () => (settingsOverlay.hidden = true));
document.getElementById("settings-cancel").addEventListener("click", () => (settingsOverlay.hidden = true));
settingsOverlay.addEventListener("click", (e) => {
  if (e.target === settingsOverlay) settingsOverlay.hidden = true;
});

document.getElementById("settings-source-browse").addEventListener("click", async () => {
  const chosen = await openFileBrowser({
    title: "Choisir le dossier source (.OSV)",
    startDir: settingsSourceDir.value.trim() || null,
  });
  if (chosen) settingsSourceDir.value = chosen;
});

document.getElementById("settings-output-browse").addEventListener("click", async () => {
  const chosen = await openFileBrowser({
    title: "Choisir le dossier de sortie",
    startDir: settingsOutputDir.value.trim() || null,
  });
  if (chosen) settingsOutputDir.value = chosen;
});

document.getElementById("settings-save").addEventListener("click", async () => {
  try {
    const payload = {
      source_dir: settingsSourceDir.value.trim(),
      output_dir: settingsOutputDir.value.trim(),
    };
    state.config = { ...state.config, ...(await api.postConfig(payload)) };
    settingsOverlay.hidden = true;
    toast("Paramètres enregistrés.");
    renderSourceDir();
    await loadFiles();
    refreshShortcuts();
  } catch (err) {
    toast(err.message, true);
  }
});

function renderGpuStatus() {
  const el = document.getElementById("gpu-status");
  if (state.config.has_nvenc) {
    el.textContent = "GPU : NVENC disponible";
    el.classList.add("ok");
  } else {
    el.textContent = "GPU : indisponible (CPU)";
    el.classList.remove("ok");
  }
}

async function loadConfig() {
  try {
    state.config = await api.getConfig();
    renderGpuStatus();
    renderSourceDir();
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------- Dossier source (source de vérité partagée barre Fichiers ↔ Réglages) ----------

const sourceDirPath = document.getElementById("source-dir-path");
const filesShortcuts = document.getElementById("files-shortcuts");

function renderSourceDir() {
  if (sourceDirPath) {
    sourceDirPath.textContent = state.config.source_dir || "(aucun dossier source défini)";
  }
}

// Rafraîchit le bandeau « Accès rapide » permanent de la vue Fichiers (détection
// live des supports amovibles / caméra + dossiers source/sortie configurés).
function refreshShortcuts() {
  if (!filesShortcuts) return;
  mountShortcuts(filesShortcuts, (path) => setSourceDir(path));
}

// Met à jour le dossier source côté serveur (POST /api/config), puis synchronise
// l'UI et recharge la liste des fichiers. Point d'entrée commun à la barre
// d'outils Fichiers (Parcourir / raccourci) — le panneau Réglages passe par le
// même /api/config, donc state.config reste l'unique source de vérité.
async function setSourceDir(path) {
  if (!path || path === state.config.source_dir) {
    if (path === state.config.source_dir) await loadFiles();
    return;
  }
  try {
    state.config = { ...state.config, ...(await api.postConfig({ source_dir: path })) };
    renderSourceDir();
    await loadFiles();
    refreshShortcuts();
  } catch (err) {
    toast(err.message, true);
  }
}

document.getElementById("source-browse-btn").addEventListener("click", async () => {
  const chosen = await openFileBrowser({
    title: "Choisir le dossier source (.OSV)",
    startDir: state.config.source_dir || null,
  });
  if (chosen) setSourceDir(chosen);
});

// ---------- Vue Fichiers ----------

const filesGrid = document.getElementById("files-grid");
const filesEmpty = document.getElementById("files-empty");
const filesLoading = document.getElementById("files-loading");
const selectAllCheckbox = document.getElementById("select-all");
const selectionCountEl = document.getElementById("selection-count");
const convertSelectionBtn = document.getElementById("convert-selection-btn");

async function loadFiles() {
  filesLoading.hidden = false;
  filesEmpty.hidden = true;
  try {
    state.files = await api.getFiles(state.config.source_dir);
    state.selected.clear();
    renderFiles();
  } catch (err) {
    toast(err.message, true);
    state.files = [];
    renderFiles();
  } finally {
    filesLoading.hidden = true;
  }
}

function renderFiles() {
  filesGrid.innerHTML = "";
  filesEmpty.hidden = state.files.length !== 0;
  for (const f of state.files) {
    const card = document.createElement("article");
    card.className = "file-card";
    card.dataset.path = f.path;

    const selectLabel = document.createElement("label");
    selectLabel.className = "file-card-select checkbox-label";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.setAttribute("aria-label", `Sélectionner ${f.name}`);
    checkbox.checked = state.selected.has(f.path);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selected.add(f.path);
      else state.selected.delete(f.path);
      card.classList.toggle("selected", checkbox.checked);
      updateSelectionUI();
    });
    selectLabel.appendChild(checkbox);
    card.appendChild(selectLabel);
    card.classList.toggle("selected", checkbox.checked);

    const thumbBtn = document.createElement("button");
    thumbBtn.type = "button";
    thumbBtn.className = "thumb-preview-btn";
    thumbBtn.title = "Aperçu 360° de la miniature";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = f.thumb_url || api.thumbUrl(f.path);
    img.alt = `Miniature équirectangulaire de ${f.name}`;
    thumbBtn.appendChild(img);
    thumbBtn.addEventListener("click", () => previewThumb(f));
    card.appendChild(thumbBtn);

    const info = document.createElement("div");
    info.className = "file-card-info";
    const name = document.createElement("p");
    name.className = "file-name";
    name.title = f.name;
    name.textContent = f.name;
    const meta = document.createElement("p");
    meta.className = "file-meta";
    meta.textContent = `${formatDuration(f.duration_s)} · ${formatBytes(f.size_bytes)}`;
    info.appendChild(name);
    info.appendChild(meta);

    // Deux actions explicites par carte : Convertir (→ options) et Ouvrir en 360°
    // (→ charge le média dans la visionneuse et bascule sur l'onglet Aperçu).
    const actions = document.createElement("div");
    actions.className = "file-card-actions";
    const convBtn = document.createElement("button");
    convBtn.type = "button";
    convBtn.className = "btn primary small";
    convBtn.textContent = "Convertir";
    convBtn.addEventListener("click", () => openOptionsPanel([f.path]));
    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "btn small";
    openBtn.textContent = "Ouvrir en 360°";
    openBtn.addEventListener("click", () => loadMediaIntoViewer(f.path));
    actions.appendChild(convBtn);
    actions.appendChild(openBtn);
    info.appendChild(actions);

    card.appendChild(info);

    filesGrid.appendChild(card);
  }
  updateSelectionUI();
}

function updateSelectionUI() {
  const n = state.selected.size;
  selectionCountEl.textContent = n === 0 ? "Aucun fichier sélectionné" : `${n} fichier${n > 1 ? "s" : ""} sélectionné${n > 1 ? "s" : ""}`;
  convertSelectionBtn.disabled = n === 0;
  selectAllCheckbox.checked = state.files.length > 0 && n === state.files.length;
}

selectAllCheckbox.addEventListener("change", () => {
  if (selectAllCheckbox.checked) {
    state.files.forEach((f) => state.selected.add(f.path));
  } else {
    state.selected.clear();
  }
  renderFiles();
});

document.getElementById("refresh-files-btn").addEventListener("click", loadFiles);

async function previewThumb(file) {
  switchView("preview");
  document.getElementById("preview-title").textContent = `Miniature — ${file.name}`;
  if (!viewer) {
    toast("Aperçu 360° indisponible (WebGL requis).", true);
    return;
  }
  setPreviewControlsEnabled({ playPause: false, reset: true });
  hideViewerStatus();
  state.waitingPreviewJobId = null; // on n'attend plus le proxy d'un job précédent
  try {
    await viewer.loadImage(file.thumb_url || api.thumbUrl(file.path));
    updatePlayPauseLabel();
    setPhotoSource(file.path); // extraction depuis l'OSV brut (frame stitchée)
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------- Panneau Options de conversion ----------

const optionsOverlay = document.getElementById("options-overlay");
const optQuality = document.getElementById("opt-quality");
const optQualityValue = document.getElementById("opt-quality-value");
const optStreetview = document.getElementById("opt-streetview");
const optEmbedCamm = document.getElementById("opt-embed-camm");
const optStabilize = document.getElementById("opt-stabilize");
const optStabilizeMode = document.getElementById("opt-stabilize-mode");
const optStabilizeStrength = document.getElementById("opt-stabilize-strength");
const optStabilizeStrengthValue = document.getElementById("opt-stabilize-strength-value");
const optStabilizeControls = document.getElementById("opt-stabilize-controls");
const optStabilizeLockedBadge = document.getElementById("opt-stabilize-locked-badge");
const optStabilizeStreetviewNote = document.getElementById("opt-stabilize-streetview-note");
const optGpxPath = document.getElementById("opt-gpx-path");
const optGpxOffset = document.getElementById("opt-gpx-offset");
const optGpxOffsetValue = document.getElementById("opt-gpx-offset-value");
const gpxResultEl = document.getElementById("gpx-analyze-result");

convertSelectionBtn.addEventListener("click", () => openOptionsPanel([...state.selected]));

// Ouvre le panneau d'options pour une liste explicite de fichiers. Sans argument,
// retombe sur la sélection multiple courante (« Convertir la sélection »).
function openOptionsPanel(inputs) {
  const list = Array.isArray(inputs) && inputs.length ? inputs : [...state.selected];
  state.optionsInputs = list;
  const n = list.length;
  document.getElementById("options-selection-summary").textContent =
    n === 1 ? "1 fichier sélectionné." : `${n} fichiers sélectionnés.`;
  gpxResultEl.hidden = true;
  gpxResultEl.innerHTML = "";
  updateStabilizeLockState();
  optionsOverlay.hidden = false;
}

function closeOptionsPanel() {
  optionsOverlay.hidden = true;
}

document.getElementById("options-close").addEventListener("click", closeOptionsPanel);
document.getElementById("options-cancel").addEventListener("click", closeOptionsPanel);
optionsOverlay.addEventListener("click", (e) => {
  if (e.target === optionsOverlay) closeOptionsPanel();
});

optQuality.addEventListener("input", () => {
  optQualityValue.textContent = optQuality.value;
});

optGpxOffset.addEventListener("input", () => {
  optGpxOffsetValue.textContent = optGpxOffset.value;
});
// Ré-analyse uniquement au relâchement du curseur (événement "change")
optGpxOffset.addEventListener("change", () => {
  if (optGpxPath.value.trim()) runGpxAnalyze();
});

optStreetview.addEventListener("change", () => {
  const forced = optStreetview.checked;
  optEmbedCamm.checked = forced ? true : optEmbedCamm.checked;
  optEmbedCamm.disabled = forced;
  updateStabilizeLockState();
});

// La stabilisation est désactivée et verrouillée visuellement tant que le profil
// Street View est actif (exigence Google : pas de stabilisation pour Street View).
function updateStabilizeLockState() {
  const locked = optStreetview.checked;
  optStabilize.disabled = locked;
  optStabilizeLockedBadge.hidden = !locked;
  optStabilizeStreetviewNote.hidden = !locked;
  if (locked) {
    optStabilize.checked = false;
    optStabilizeControls.hidden = true;
  } else {
    optStabilizeControls.hidden = !optStabilize.checked;
  }
}

optStabilize.addEventListener("change", () => {
  optStabilizeControls.hidden = !optStabilize.checked;
});

optStabilizeStrength.addEventListener("input", () => {
  optStabilizeStrengthValue.textContent = optStabilizeStrength.value;
});

function dirnameOf(path) {
  const p = (path || "").trim();
  const slash = p.lastIndexOf("/");
  return slash > 0 ? p.slice(0, slash) : null;
}

document.getElementById("opt-gpx-browse").addEventListener("click", async () => {
  const chosen = await openFileBrowser({
    title: "Choisir un fichier GPX",
    filter: "gpx",
    startDir: dirnameOf(optGpxPath.value),
  });
  if (chosen) {
    optGpxPath.value = chosen;
    gpxResultEl.hidden = true;
    gpxResultEl.innerHTML = "";
  }
});

function pickReferenceVideoPath() {
  const targets = state.optionsInputs.length ? state.optionsInputs : [...state.selected];
  const set = new Set(targets);
  return state.files.find((f) => set.has(f.path))?.path || targets[0];
}

function extractField(obj, ...keys) {
  for (const k of keys) {
    if (obj[k] !== undefined) return obj[k];
  }
  return undefined;
}

async function runGpxAnalyze() {
  const gpxPath = optGpxPath.value.trim();
  const videoPath = pickReferenceVideoPath();
  if (!gpxPath || !videoPath) {
    toast("Choisissez un fichier GPX et au moins un fichier vidéo sélectionné.", true);
    return;
  }
  gpxResultEl.hidden = false;
  gpxResultEl.innerHTML = "<p>Analyse en cours…</p>";
  try {
    const result = await api.gpxAnalyze({
      gpx_path: gpxPath,
      video_path: videoPath,
      offset_s: parseFloat(optGpxOffset.value),
    });
    renderGpxResult(result);
  } catch (err) {
    gpxResultEl.innerHTML = `<p class="gpx-warn">${err.message}</p>`;
  }
}

function renderGpxResult(result) {
  const coverage = extractField(result, "coverage_pct", "coverage");
  const overlap = extractField(result, "overlap_s", "overlap");
  const gaps = extractField(result, "gaps", "gaps_gt_5s", "gaps_over_5s", "gaps_5s");
  const gapsCount = Array.isArray(gaps) ? gaps.length : gaps ?? "—";
  const nPoints = extractField(result, "n_points_in_window", "points_in_window", "n_points");
  const suggested = extractField(result, "suggested_offset_s", "suggested_offset");

  const coverageClass = typeof coverage === "number" && coverage < 90 ? "gpx-warn" : "gpx-ok";
  const gapsClass = (Array.isArray(gaps) ? gaps.length : gaps) > 0 ? "gpx-warn" : "gpx-ok";

  gpxResultEl.innerHTML = "";
  const lines = [
    ["Couverture", typeof coverage === "number" ? `${coverage.toFixed(1)} %` : "—", coverageClass],
    ["Chevauchement", typeof overlap === "number" ? `${overlap.toFixed(1)} s` : "—", ""],
    ["Trous > 5 s", String(gapsCount), gapsClass],
    ["Points dans la fenêtre", nPoints != null ? String(nPoints) : "—", ""],
  ];
  for (const [label, value, cls] of lines) {
    const row = document.createElement("div");
    row.className = "gpx-line";
    row.innerHTML = `<span>${label}</span><span class="gpx-value ${cls}">${value}</span>`;
    gpxResultEl.appendChild(row);
  }
  if (typeof suggested === "number") {
    const row = document.createElement("div");
    row.className = "gpx-line";
    const applyBtn = document.createElement("button");
    applyBtn.type = "button";
    applyBtn.className = "btn";
    applyBtn.textContent = `Appliquer (${suggested.toFixed(1)} s)`;
    applyBtn.addEventListener("click", () => {
      optGpxOffset.value = String(suggested);
      optGpxOffsetValue.textContent = optGpxOffset.value;
      runGpxAnalyze();
    });
    row.innerHTML = `<span>Décalage suggéré</span>`;
    row.appendChild(applyBtn);
    gpxResultEl.appendChild(row);
  }
}

document.getElementById("opt-gpx-analyze").addEventListener("click", runGpxAnalyze);

document.getElementById("options-launch").addEventListener("click", async () => {
  const inputs = state.optionsInputs.length ? [...state.optionsInputs] : [...state.selected];
  if (inputs.length === 0) {
    toast("Aucun fichier sélectionné.", true);
    return;
  }
  const gpxPath = optGpxPath.value.trim();
  const options = {
    out_w: parseInt(document.getElementById("opt-resolution").value, 10),
    codec: document.getElementById("opt-codec").value,
    encoder: document.getElementById("opt-encoder").value,
    quality: parseInt(optQuality.value, 10),
    interp: document.getElementById("opt-interp").value,
    mode: document.getElementById("opt-mode").value,
    // La stabilisation reste forcée à false quand le profil Street View est actif
    // (case décochée + verrouillée par updateStabilizeLockState()).
    stabilize: optStreetview.checked ? false : optStabilize.checked,
    stabilize_mode: optStabilizeMode.value,
    stabilize_strength: parseInt(optStabilizeStrength.value, 10) / 100,
    streetview: optStreetview.checked,
    embed_camm: optStreetview.checked ? true : optEmbedCamm.checked,
  };
  if (optStreetview.checked) {
    options.fps_out = 5;
  }
  if (gpxPath) {
    options.gpx_path = gpxPath;
    options.gpx_offset_s = parseFloat(optGpxOffset.value);
  }
  try {
    await api.createJobs({ inputs, options });
    toast(`Conversion lancée pour ${inputs.length} fichier${inputs.length > 1 ? "s" : ""}.`);
    closeOptionsPanel();
    state.optionsInputs = [];
    state.selected.clear();
    renderFiles();
    switchView("queue");
  } catch (err) {
    toast(err.message, true);
  }
});

// ---------- Vue File d'attente ----------

const queueList = document.getElementById("queue-list");
const queueEmpty = document.getElementById("queue-empty");
const queueCountBadge = document.getElementById("queue-count");

function hasActiveJobs() {
  return state.jobs.some((j) => j.status === "queued" || j.status === "running");
}

async function refreshJobs() {
  try {
    state.jobs = await api.getJobs();
    renderQueue();
    // Si on attend le proxy H.264 d'un job (preview_url), relance l'aperçu dès qu'il apparaît.
    if (state.waitingPreviewJobId) {
      const job = state.jobs.find((j) => j.id === state.waitingPreviewJobId);
      if (job && job.preview_url) {
        state.waitingPreviewJobId = null;
        previewJobOutput(job);
      } else if (!job) {
        state.waitingPreviewJobId = null;
      }
    }
  } catch (err) {
    // Silencieux pendant le polling pour ne pas spammer l'utilisateur ; visible seulement
    // si on est explicitement sur la vue file d'attente.
    if (state.view === "queue") toast(err.message, true);
  }
}

function renderQueue() {
  const active = state.jobs.filter((j) => j.status === "queued" || j.status === "running").length;
  queueCountBadge.hidden = active === 0;
  queueCountBadge.textContent = String(active);

  queueList.innerHTML = "";
  queueEmpty.hidden = state.jobs.length !== 0;

  for (const job of state.jobs) {
    const row = document.createElement("article");
    row.className = "job-row";

    const info = document.createElement("div");
    info.className = "job-info";
    const name = document.createElement("p");
    name.className = "job-name";
    name.title = job.input;
    name.textContent = basename(job.input);
    const statusLine = document.createElement("p");
    statusLine.className = "job-status-line";
    const badge = document.createElement("span");
    badge.className = `status-badge ${job.status}`;
    badge.textContent = statusLabel(job.status);
    statusLine.appendChild(badge);
    info.appendChild(name);
    info.appendChild(statusLine);
    if (job.status === "error" && job.error) {
      const errEl = document.createElement("p");
      errEl.className = "job-error";
      errEl.textContent = job.error;
      info.appendChild(errEl);
    }
    row.appendChild(info);

    const progressWrap = document.createElement("div");
    progressWrap.className = "job-progress";
    const progress = document.createElement("progress");
    progress.max = 1;
    progress.value = job.progress || 0;
    const label = document.createElement("span");
    label.className = "job-progress-label";
    const pct = Math.round((job.progress || 0) * 100);
    const fpsTxt = job.fps != null ? `${job.fps.toFixed?.(1) ?? job.fps} im/s` : "— im/s";
    const etaTxt = `ETA ${formatEta(job.eta_s)}`;
    label.textContent = `${pct}% · ${fpsTxt} · ${etaTxt}`;
    progressWrap.appendChild(progress);
    progressWrap.appendChild(label);
    row.appendChild(progressWrap);

    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (job.status === "done") {
      const previewBtn = document.createElement("button");
      previewBtn.type = "button";
      previewBtn.className = "btn";
      previewBtn.textContent = "Aperçu 360°";
      previewBtn.addEventListener("click", () => previewJobOutput(job));
      actions.appendChild(previewBtn);
    }
    if (job.status === "queued" || job.status === "running") {
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn danger";
      cancelBtn.textContent = "Annuler";
      cancelBtn.addEventListener("click", () => cancelJob(job.id));
      actions.appendChild(cancelBtn);
    }
    row.appendChild(actions);

    queueList.appendChild(row);
  }
}

async function cancelJob(id) {
  try {
    await api.deleteJob(id);
    toast("Job annulé.");
    await refreshJobs();
  } catch (err) {
    toast(err.message, true);
  }
}

function showViewerStatus(message) {
  const el = document.getElementById("viewer-empty");
  el.textContent = message;
  el.hidden = false;
}

function hideViewerStatus() {
  document.getElementById("viewer-empty").hidden = true;
}

async function previewJobOutput(job) {
  switchView("preview");
  document.getElementById("preview-title").textContent = `Sortie — ${basename(job.output || job.input)}`;
  if (!viewer) {
    toast("Aperçu 360° indisponible (WebGL requis).", true);
    return;
  }
  setPreviewControlsEnabled({ playPause: true, reset: true });
  hideViewerStatus();
  state.waitingPreviewJobId = null;

  // Le backend fournit un proxy H.264 lisible navigateur (preview_url) quand il est
  // prêt ; sinon on tente le fichier de sortie directement (peut être du HEVC 10-bit
  // que Chrome/Linux ne sait pas décoder).
  const url = job.preview_url || api.mediaUrl(job.output);
  try {
    await viewer.loadVideo(url);
    updatePlayPauseLabel();
    setPhotoSource(job.output); // extraction pleine résolution depuis le MP4 converti
  } catch (err) {
    setPreviewControlsEnabled({ playPause: false, reset: false });
    if (!job.preview_url) {
      // Sortie non décodable et pas encore de proxy : on attend son apparition
      // via le polling de /api/jobs.
      state.waitingPreviewJobId = job.id;
      showViewerStatus(`${err.message} — préparation de l'aperçu…`);
    } else {
      showViewerStatus(err.message);
    }
    toast(err.message, true);
  }
}

// Polling 1 s : actif si la vue file d'attente est affichée, si des jobs tournent,
// ou si on attend le proxy H.264 (preview_url) d'un job terminé.
setInterval(() => {
  if (state.view === "queue" || hasActiveJobs() || state.waitingPreviewJobId) {
    refreshJobs();
  }
}, 1000);

// ---------- Vue Aperçu 360° ----------

const previewPlayPauseBtn = document.getElementById("preview-playpause");
const previewResetBtn = document.getElementById("preview-reset-view");

function setPreviewControlsEnabled({ playPause, reset }) {
  previewPlayPauseBtn.disabled = !playPause;
  previewResetBtn.disabled = !reset;
}

function updatePlayPauseLabel() {
  previewPlayPauseBtn.textContent = viewer && !viewer.isPaused ? "Pause" : "Lecture";
}

previewPlayPauseBtn.addEventListener("click", () => {
  if (!viewer) return;
  viewer.togglePlayPause();
  updatePlayPauseLabel();
});
previewResetBtn.addEventListener("click", () => viewer && viewer.resetView());

// ---------- Ouverture d'un fichier dans la visionneuse (depuis la vue Fichiers) ----------

// Charge un média (OSV/MP4/JPEG) dans la visionneuse et bascule sur l'onglet
// Aperçu. Point d'entrée commun au bouton « Ouvrir un fichier… » de la barre
// Fichiers et à l'action « Ouvrir en 360° » de chaque carte.
async function loadMediaIntoViewer(path) {
  if (!path) return;
  switchView("preview");
  if (!viewer) {
    toast("Aperçu 360° indisponible (WebGL requis).", true);
    return;
  }
  const ext = path.split(".").pop().toLowerCase();
  document.getElementById("preview-title").textContent = basename(path);
  hideViewerStatus();
  state.waitingPreviewJobId = null;
  try {
    if (ext === "jpg" || ext === "jpeg") {
      setPreviewControlsEnabled({ playPause: false, reset: true });
      await viewer.loadImage(api.mediaUrl(path));
    } else if (ext === "mp4") {
      setPreviewControlsEnabled({ playPause: true, reset: true });
      await viewer.loadVideo(api.mediaUrl(path));
    } else {
      // .OSV : le navigateur ne sait pas le lire → proxy de navigation généré côté backend
      setPreviewControlsEnabled({ playPause: false, reset: false });
      showViewerStatus("Préparation du proxy de navigation…");
      const { proxy_url } = await api.photoNavproxy(path);
      await viewer.loadVideo(proxy_url);
      hideViewerStatus();
      setPreviewControlsEnabled({ playPause: true, reset: true });
    }
    updatePlayPauseLabel();
    setPhotoSource(path);
  } catch (err) {
    setPreviewControlsEnabled({ playPause: false, reset: false });
    showViewerStatus(err.message);
    toast(err.message, true);
  }
}

document.getElementById("open-file-btn").addEventListener("click", async () => {
  const chosen = await openFileBrowser({
    title: "Ouvrir un fichier 360°",
    filter: "osv,mp4,jpg,jpeg",
    startDir: dirnameOf(state.photoSource) || state.config.source_dir || null,
  });
  if (!chosen) return;
  loadMediaIntoViewer(chosen);
});

// ---------- Panneau « Extraire une photo » ----------

const photoPanel = document.getElementById("photo-panel");
const photoPanelToggle = document.getElementById("photo-panel-toggle");
const photoProjection = document.getElementById("photo-projection");
const photoFlatFields = document.getElementById("photo-flat-fields");
const photoCylFields = document.getElementById("photo-cyl-fields");
const photoPlanetFields = document.getElementById("photo-planet-fields");
const photoEquirectNote = document.getElementById("photo-equirect-note");
const photoRatio = document.getElementById("photo-ratio");
const photoHfov = document.getElementById("photo-hfov");
const photoHfovValue = document.getElementById("photo-hfov-value");
const photoYaw = document.getElementById("photo-yaw");
const photoPitch = document.getElementById("photo-pitch");
const photoRoll = document.getElementById("photo-roll");
const photoCylYaw = document.getElementById("photo-cyl-yaw");
const photoVspan = document.getElementById("photo-vspan");
const photoVspanValue = document.getElementById("photo-vspan-value");
const photoRotation = document.getElementById("photo-rotation");
const photoRotationValue = document.getElementById("photo-rotation-value");
const photoOutw = document.getElementById("photo-outw");
const photoTimeEl = document.getElementById("photo-time");
const photoExtractBtn = document.getElementById("photo-extract-btn");
const photoExtractAgainBtn = document.getElementById("photo-extract-again");
const photoSpinner = document.getElementById("photo-spinner");
const photoResult = document.getElementById("photo-result");
const photoResultImg = document.getElementById("photo-result-img");
const photoResultPath = document.getElementById("photo-result-path");
const photoErrorEl = document.getElementById("photo-error");
const captureOverlay = document.getElementById("capture-overlay");
const captureFrame = document.getElementById("capture-frame");
const captureWarn = document.getElementById("capture-overlay-warn");
const photoRatioCustom = document.getElementById("photo-ratio-custom");
const photoRatioW = document.getElementById("photo-ratio-w");
const photoRatioH = document.getElementById("photo-ratio-h");
const photoRatioWarn = document.getElementById("photo-ratio-warn");
const viewerProjectionNote = document.getElementById("viewer-projection-note");
const viewerHelp = document.getElementById("viewer-help");

let syncingFromFields = false; // garde anti-boucle champs ↔ vue
let syncingFromProjWheel = false; // garde anti-boucle molette (mode projection) ↔ champs

// ---- Ratio (préréglages + « Libre ») ----

function currentRatioParts() {
  if (photoRatio.value === "custom") {
    const rw = parseFloat(photoRatioW.value) || 16;
    const rh = parseFloat(photoRatioH.value) || 9;
    return { rw: Math.max(0.1, rw), rh: Math.max(0.1, rh) };
  }
  const [rw, rh] = photoRatio.value.split(":").map(Number);
  return { rw, rh };
}

function currentRatioString() {
  if (photoRatio.value !== "custom") return photoRatio.value;
  const { rw, rh } = currentRatioParts();
  const fmt = (x) => (Number.isInteger(x) ? String(x) : x.toFixed(2).replace(/0+$/, "").replace(/\.$/, ""));
  return `${fmt(rw)}:${fmt(rh)}`;
}

function ratioInBounds() {
  const { rw, rh } = currentRatioParts();
  const r = rw / rh;
  return r >= 0.2 && r <= 8;
}

function updateRatioUI() {
  photoRatioCustom.hidden = photoRatio.value !== "custom";
  photoRatioWarn.hidden = ratioInBounds();
  updateCaptureOverlay();
}

function setPhotoSource(path) {
  state.photoSource = path || null;
  photoExtractBtn.disabled = !state.photoSource;
}

function photoPanelOpen() {
  return !photoPanel.hidden;
}

photoPanelToggle.addEventListener("click", () => {
  const open = photoPanel.hidden; // état après bascule
  photoPanel.hidden = !open;
  photoPanelToggle.setAttribute("aria-expanded", String(open));
  if (open) {
    syncFieldsFromView();
    updateProjectionFields();
  }
  updateCaptureOverlay();
  updateViewerProjection();
});

function updateProjectionFields() {
  const p = photoProjection.value;
  photoFlatFields.hidden = p !== "flat";
  photoCylFields.hidden = p !== "cylindrical";
  photoPlanetFields.hidden = p !== "littleplanet";
  photoEquirectNote.hidden = p !== "equirect360";
  if (p === "cylindrical" && viewer) {
    // Pré-remplit le yaw de départ depuis l'orientation courante de la vue
    photoCylYaw.value = viewer.yaw.toFixed(1);
  }
  updateCaptureOverlay();
  updateViewerProjection();
}

const VIEWER_HELP_DEFAULT = "Glisser : orienter · Molette : zoomer · Flèches : orienter · +/- : zoomer";
const VIEWER_HELP_BY_PROJECTION = {
  cylindrical: "Molette : yaw de départ · Glisser/Flèches/+/- : sans effet sur la projection",
  equirect360: "Projection équirectangulaire intégrale — aucun réglage",
  littleplanet: "Molette : rotation · Glisser/Flèches/+/- : sans effet sur la projection",
};

// Bascule la vue principale entre la sphère navigable (« flat ») et le rendu plein
// cadre de la projection choisie (cylindrique / équirect intégral / petite planète),
// via le mode "projection" fusionné dans viewer.js (ex-projpreview.js). N'est actif
// que lorsque le panneau « Extraire une photo » est ouvert.
function updateViewerProjection() {
  const p = photoProjection.value;
  const active = photoPanelOpen() && p !== "flat";
  if (viewerProjectionNote) viewerProjectionNote.hidden = !active;
  if (viewerHelp) viewerHelp.textContent = active ? VIEWER_HELP_BY_PROJECTION[p] || VIEWER_HELP_DEFAULT : VIEWER_HELP_DEFAULT;
  if (!viewer) return;
  if (!active) {
    viewer.setProjectionMode(null);
    return;
  }
  viewer.setProjectionMode(p, {
    yawStartDeg: parseFloat(photoCylYaw.value) || 0,
    vSpanDeg: parseFloat(photoVspan.value) || 60,
    rotationDeg: parseFloat(photoRotation.value) || 0,
  });
}

// Synchro molette (mode projection) → champs numériques : la molette sur la vue
// principale ajuste yawStart (cylindrique) / rotation (petite planète) au lieu du
// zoom ; le champ correspondant doit rester synchronisé dans les deux sens.
function hookViewerProjectionSync() {
  if (!viewer) return;
  viewer.onProjectionParamsChange = (v) => {
    syncingFromProjWheel = true;
    if (v.projection === "cylindrical") {
      photoCylYaw.value = v.projParams.yawStartDeg.toFixed(1);
    } else if (v.projection === "littleplanet") {
      const r = Math.round(v.projParams.rotationDeg);
      photoRotation.value = String(r);
      photoRotationValue.textContent = String(r);
    }
    syncingFromProjWheel = false;
  };
}

photoProjection.addEventListener("change", updateProjectionFields);

photoHfov.addEventListener("input", () => {
  photoHfovValue.textContent = photoHfov.value;
  updateCaptureOverlay();
});
photoRatio.addEventListener("change", updateRatioUI);
photoRatioW.addEventListener("input", updateRatioUI);
photoRatioH.addEventListener("input", updateRatioUI);
photoVspan.addEventListener("input", () => {
  photoVspanValue.textContent = photoVspan.value;
  updateViewerProjection();
});
photoRotation.addEventListener("input", () => {
  if (syncingFromProjWheel) return;
  photoRotationValue.textContent = photoRotation.value;
  updateViewerProjection();
});
photoCylYaw.addEventListener("input", () => {
  if (syncingFromProjWheel) return;
  updateViewerProjection();
});

// Synchro champs → vue (éditer yaw/pitch/roll oriente la visionneuse)
for (const input of [photoYaw, photoPitch, photoRoll]) {
  input.addEventListener("input", () => {
    if (!viewer) return;
    syncingFromFields = true;
    viewer.setOrientation(
      parseFloat(photoYaw.value),
      parseFloat(photoPitch.value),
      parseFloat(photoRoll.value)
    );
    syncingFromFields = false;
  });
}

// Synchro vue → champs (bouger la vue met à jour yaw/pitch/roll)
function syncFieldsFromView() {
  if (!viewer) return;
  photoYaw.value = viewer.yaw.toFixed(1);
  photoPitch.value = viewer.pitch.toFixed(1);
  photoRoll.value = viewer.roll.toFixed(1);
}

function hookViewerSync() {
  if (!viewer) return;
  viewer.onViewChange = () => {
    if (photoPanelOpen() && !syncingFromFields) syncFieldsFromView();
    updateCaptureOverlay();
  };
}

// Overlay du cadre de capture : rectangle angulaire (ratio + FOV demandés)
// projeté dans la vue courante de la visionneuse.
function updateCaptureOverlay() {
  const active =
    photoPanelOpen() && photoProjection.value === "flat" && viewer && state.view === "preview";
  captureOverlay.hidden = !active;
  if (!active) return;

  const container = document.getElementById("viewer-container");
  const W = container.clientWidth;
  const H = container.clientHeight;

  const deg2rad = (d) => (d * Math.PI) / 180;
  const { rw, rh } = currentRatioParts();
  const hfovReq = parseFloat(photoHfov.value);
  // v_fov = 2·atan(tan(h_fov/2)·h/w) — même formule que le backend (pas d'étirement)
  const vfovReq = (2 * Math.atan(Math.tan(deg2rad(hfovReq) / 2) * (rh / rw)) * 180) / Math.PI;

  const hfovView = viewer.hFov;
  const vfovView = viewer.fov;

  // Fraction de l'écran occupée par le cadre (projection perspective, cadre centré)
  let fx = Math.tan(deg2rad(hfovReq) / 2) / Math.tan(deg2rad(hfovView) / 2);
  let fy = Math.tan(deg2rad(vfovReq) / 2) / Math.tan(deg2rad(vfovView) / 2);

  const overflow = fx > 1 || fy > 1;
  captureWarn.hidden = !overflow;
  fx = Math.min(fx, 1);
  fy = Math.min(fy, 1);

  const w = Math.round(fx * W);
  const h = Math.round(fy * H);
  captureFrame.style.width = `${w}px`;
  captureFrame.style.height = `${h}px`;
  captureFrame.style.left = `${Math.round((W - w) / 2)}px`;
  captureFrame.style.top = `${Math.round((H - h) / 2)}px`;
}

// ---- Cadre interactif : glisser l'intérieur = pan de la visée ; poignées = FOV/ratio ----

const deg2rad = (d) => (d * Math.PI) / 180;
const rad2deg = (r) => (r * 180) / Math.PI;

let frameDrag = null; // { mode: "pan"|"resize", handle, lastX, lastY }

function hfovFromFraction(fx) {
  // fraction d'écran → FOV horizontal (projection perspective, cadre centré)
  return rad2deg(2 * Math.atan(fx * Math.tan(deg2rad(viewer.hFov) / 2)));
}

function vfovFromFraction(fy) {
  return rad2deg(2 * Math.atan(fy * Math.tan(deg2rad(viewer.fov) / 2)));
}

function hfovFromVfov(vfovDeg, rw, rh) {
  return rad2deg(2 * Math.atan(Math.tan(deg2rad(vfovDeg) / 2) * (rw / rh)));
}

function vfovFromHfov(hfovDeg, rw, rh) {
  return rad2deg(2 * Math.atan(Math.tan(deg2rad(hfovDeg) / 2) * (rh / rw)));
}

function setHfovClamped(v) {
  const clamped = Math.max(30, Math.min(140, v));
  photoHfov.value = String(Math.round(clamped * 10) / 10);
  photoHfovValue.textContent = String(Math.round(clamped));
  return clamped;
}

captureFrame.addEventListener("pointerdown", (e) => {
  if (!viewer) return;
  const handle = e.target.closest(".capture-handle");
  frameDrag = {
    mode: handle ? "resize" : "pan",
    handle: handle ? handle.dataset.handle : null,
    lastX: e.clientX,
    lastY: e.clientY,
  };
  e.target.setPointerCapture(e.pointerId);
  e.preventDefault();
  e.stopPropagation();
});

captureFrame.addEventListener("pointermove", (e) => {
  if (!frameDrag || !viewer) return;
  const container = document.getElementById("viewer-container");
  const rect = container.getBoundingClientRect();
  const W = rect.width;
  const H = rect.height;

  if (frameDrag.mode === "pan") {
    // Déplacer le cadre = déplacer la visée (le cadre reste centré à l'écran)
    const dx = e.clientX - frameDrag.lastX;
    const dy = e.clientY - frameDrag.lastY;
    frameDrag.lastX = e.clientX;
    frameDrag.lastY = e.clientY;
    const degPerPxX = viewer.hFov / W;
    const degPerPxY = viewer.fov / H;
    viewer.setOrientation(viewer.lon + dx * degPerPxX, viewer.lat - dy * degPerPxY, null);
    return;
  }

  // Redimensionnement par poignée : demi-étendues visées depuis le centre du canvas
  const cx = rect.left + W / 2;
  const cy = rect.top + H / 2;
  const fx = Math.max(0.02, Math.min(0.995, Math.abs(e.clientX - cx) / (W / 2)));
  const fy = Math.max(0.02, Math.min(0.995, Math.abs(e.clientY - cy) / (H / 2)));
  const h = frameDrag.handle;
  const horiz = h.includes("e") || h.includes("w");
  const vert = h.includes("n") || h.includes("s");
  const { rw, rh } = currentRatioParts();

  if (photoRatio.value !== "custom") {
    // Ratio préréglé : homothétie centrée → seule h_fov change
    let hfov;
    if (horiz && vert) {
      hfov = Math.max(hfovFromFraction(fx), hfovFromVfov(vfovFromFraction(fy), rw, rh));
    } else if (horiz) {
      hfov = hfovFromFraction(fx);
    } else {
      hfov = hfovFromVfov(vfovFromFraction(fy), rw, rh);
    }
    setHfovClamped(hfov);
  } else {
    // Ratio libre : largeur et hauteur s'ajustent indépendamment, champs a:b suivis
    let hfov = parseFloat(photoHfov.value);
    let vfov = vfovFromHfov(hfov, rw, rh);
    if (horiz) hfov = Math.max(30, Math.min(140, hfovFromFraction(fx)));
    if (vert) vfov = vfovFromFraction(fy);
    hfov = setHfovClamped(hfov);
    // rh/rw = tan(v/2)/tan(h/2), borné pour rester dans a/b ∈ [0.2, 8]
    let hRatio = (parseFloat(photoRatioW.value) || 16) * (Math.tan(deg2rad(vfov) / 2) / Math.tan(deg2rad(hfov) / 2));
    const w0 = parseFloat(photoRatioW.value) || 16;
    hRatio = Math.max(w0 / 8, Math.min(w0 * 5, hRatio));
    photoRatioH.value = String(Math.round(hRatio * 100) / 100);
    photoRatioWarn.hidden = ratioInBounds();
  }
  updateCaptureOverlay();
});

const endFrameDrag = () => {
  frameDrag = null;
};
captureFrame.addEventListener("pointerup", endFrameDrag);
captureFrame.addEventListener("pointercancel", endFrameDrag);

// Temps courant de la vidéo (affiché dans le panneau)
setInterval(() => {
  if (!photoPanelOpen()) return;
  const t = viewer ? viewer.currentTime : null;
  photoTimeEl.textContent = t == null ? "—" : `${t.toFixed(1)} s`;
}, 500);

async function extractPhoto() {
  if (!state.photoSource) {
    toast("Aucun média source pour l'extraction.", true);
    return;
  }
  const projection = photoProjection.value;
  const payload = {
    source_path: state.photoSource,
    time_s: viewer && viewer.currentTime != null ? Math.round(viewer.currentTime * 10) / 10 : 0,
    projection,
    yaw_deg: projection === "cylindrical" ? parseFloat(photoCylYaw.value) || 0 : parseFloat(photoYaw.value) || 0,
    pitch_deg: projection === "flat" ? parseFloat(photoPitch.value) || 0 : 0,
    roll_deg:
      projection === "littleplanet"
        ? parseFloat(photoRotation.value) || 0
        : parseFloat(photoRoll.value) || 0,
    h_fov_deg: parseFloat(photoHfov.value),
    ratio: currentRatioString(),
    v_span_deg: parseFloat(photoVspan.value),
    out_w: photoOutw.value ? parseInt(photoOutw.value, 10) : null,
  };

  if (projection === "flat" && !ratioInBounds()) {
    toast("Ratio hors limites : largeur/hauteur doit rester entre 0,2 et 8.", true);
    return;
  }

  photoErrorEl.hidden = true;
  photoResult.hidden = true;
  photoSpinner.hidden = false;
  photoExtractBtn.disabled = true;
  try {
    const res = await api.photoExtract(payload);
    photoResultImg.src = res.preview_url;
    photoResultPath.textContent = `${res.photo_path} (${res.width}×${res.height})`;
    photoResult.hidden = false;
  } catch (err) {
    photoErrorEl.textContent = err.message;
    photoErrorEl.hidden = false;
  } finally {
    photoSpinner.hidden = true;
    photoExtractBtn.disabled = !state.photoSource;
  }
}

photoExtractBtn.addEventListener("click", extractPhoto);
photoExtractAgainBtn.addEventListener("click", extractPhoto);

// ---------- Initialisation ----------

async function init() {
  const canvas = document.getElementById("viewer-canvas");
  try {
    viewer = new Viewer360(canvas);
  } catch (err) {
    // WebGL indisponible (pilotes, contexte perdu…) : le reste de l'application
    // doit continuer à fonctionner, seule la visionneuse est désactivée.
    viewer = null;
    showViewerStatus(`Aperçu 360° indisponible (WebGL requis) : ${err.message}`);
  }
  hookViewerSync();
  hookViewerProjectionSync();

  await loadConfig();
  refreshShortcuts();
  await loadFiles();
  switchView("files");
}

init();
