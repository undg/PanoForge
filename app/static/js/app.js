// PanoForge — main frontend controller (vanilla JS, no framework)

import * as api from "/js/api.js";
import { Viewer360 } from "/js/viewer.js";
import { openFileBrowser, mountShortcuts } from "/js/filebrowser.js";

const state = {
  config: { source_dir: "", output_dir: "", has_nvenc: false, version: "" },
  files: [],
  selected: new Set(),
  jobs: [],
  view: "files",
  optionsInputs: [], // files targeted by the currently open options panel
  previewJobRef: null, // input path of file used for GPX analyze reference
  waitingPreviewJobId: null, // job whose H.264 proxy (preview_url) we are waiting for
  photoSource: null, // path of the source media for photo extraction (OSV/MP4/JPG)
};

let viewer = null;

// ---------- Formatting helpers ----------

function formatBytes(bytes) {
  if (bytes == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
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
  queued: "Queued",
  running: "Running",
  done: "Done",
  error: "Error",
  cancelled: "Cancelled",
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

// ---------- Views ----------

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

// ---------- Settings (folders) ----------

const settingsOverlay = document.getElementById("settings-overlay");
const settingsSourceDir = document.getElementById("settings-source-dir");
const settingsOutputDir = document.getElementById("settings-output-dir");
const settingsVersion = document.getElementById("settings-version");

document.getElementById("settings-btn").addEventListener("click", () => {
  settingsSourceDir.value = state.config.source_dir || "";
  settingsOutputDir.value = state.config.output_dir || "";
  settingsVersion.textContent = state.config.version ? `Version: ${state.config.version}` : "";
  settingsOverlay.hidden = false;
});
document.getElementById("settings-close").addEventListener("click", () => (settingsOverlay.hidden = true));
document.getElementById("settings-cancel").addEventListener("click", () => (settingsOverlay.hidden = true));
settingsOverlay.addEventListener("click", (e) => {
  if (e.target === settingsOverlay) settingsOverlay.hidden = true;
});

document.getElementById("settings-source-browse").addEventListener("click", async () => {
  const chosen = await openFileBrowser({
    title: "Choose the source folder (.OSV)",
    startDir: settingsSourceDir.value.trim() || null,
  });
  if (chosen) settingsSourceDir.value = chosen;
});

document.getElementById("settings-output-browse").addEventListener("click", async () => {
  const chosen = await openFileBrowser({
    title: "Choose the output folder",
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
    toast("Settings saved.");
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
    el.textContent = "GPU: NVENC available";
    el.classList.add("ok");
  } else {
    el.textContent = "GPU: unavailable (CPU)";
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

// ---------- Source folder (shared source of truth for Files toolbar ↔ Settings) ----------

const sourceDirPath = document.getElementById("source-dir-path");
const filesShortcuts = document.getElementById("files-shortcuts");

function renderSourceDir() {
  if (sourceDirPath) {
    sourceDirPath.textContent = state.config.source_dir || "(no source folder defined)";
  }
}

// Refreshes the permanent "Quick access" bar of the Files view (live
// detection of removable volumes / camera + configured source/output folders).
function refreshShortcuts() {
  if (!filesShortcuts) return;
  mountShortcuts(filesShortcuts, (path) => setSourceDir(path));
}

// Updates the source folder server-side (POST /api/config), then syncs
// the UI and reloads the file list. Common entry point for the Files
// toolbar (Browse / shortcut) — the Settings panel goes through the
// same /api/config, so state.config remains the single source of truth.
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
    title: "Choose the source folder (.OSV)",
    startDir: state.config.source_dir || null,
  });
  if (chosen) setSourceDir(chosen);
});

// ---------- Files view ----------

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
    checkbox.setAttribute("aria-label", `Select ${f.name}`);
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
    thumbBtn.title = "360° preview of the thumbnail";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = f.thumb_url || api.thumbUrl(f.path);
    img.alt = `Equirectangular thumbnail of ${f.name}`;
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

    // Two explicit actions per card: Convert (→ options) and Open in 360°
    // (→ loads the media into the viewer and switches to the Preview tab).
    const actions = document.createElement("div");
    actions.className = "file-card-actions";
    const convBtn = document.createElement("button");
    convBtn.type = "button";
    convBtn.className = "btn primary small";
    convBtn.textContent = "Convert";
    convBtn.addEventListener("click", () => openOptionsPanel([f.path]));
    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "btn small";
    openBtn.textContent = "Open in 360°";
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
  selectionCountEl.textContent = n === 0 ? "No file selected" : `${n} file${n > 1 ? "s" : ""} selected`;
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
  document.getElementById("preview-title").textContent = `Thumbnail — ${file.name}`;
  if (!viewer) {
    toast("360° preview unavailable (WebGL required).", true);
    return;
  }
  setPreviewControlsEnabled({ playPause: false, reset: true });
  hideViewerStatus();
  state.waitingPreviewJobId = null; // no longer waiting for a previous job's proxy
  try {
    await viewer.loadImage(file.thumb_url || api.thumbUrl(file.path));
    updatePlayPauseLabel();
    setPhotoSource(file.path); // extraction from the raw OSV (stitched frame)
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------- Conversion options panel ----------

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

// Opens the options panel for an explicit list of files. Without an argument,
// falls back to the current multiple selection ("Convert selection").
function openOptionsPanel(inputs) {
  const list = Array.isArray(inputs) && inputs.length ? inputs : [...state.selected];
  state.optionsInputs = list;
  const n = list.length;
  document.getElementById("options-selection-summary").textContent =
    n === 1 ? "1 file selected." : `${n} files selected.`;
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
// Re-analyze only when the slider is released ("change" event)
optGpxOffset.addEventListener("change", () => {
  if (optGpxPath.value.trim()) runGpxAnalyze();
});

optStreetview.addEventListener("change", () => {
  const forced = optStreetview.checked;
  optEmbedCamm.checked = forced ? true : optEmbedCamm.checked;
  optEmbedCamm.disabled = forced;
  updateStabilizeLockState();
});

// Stabilization is disabled and visually locked while the
// Street View profile is active (Google requirement: no stabilization for Street View).
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
    title: "Choose a GPX file",
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
    toast("Choose a GPX file and at least one selected video file.", true);
    return;
  }
  gpxResultEl.hidden = false;
  gpxResultEl.innerHTML = "<p>Analysis in progress…</p>";
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
    ["Coverage", typeof coverage === "number" ? `${coverage.toFixed(1)} %` : "—", coverageClass],
    ["Overlap", typeof overlap === "number" ? `${overlap.toFixed(1)} s` : "—", ""],
    ["Gaps > 5 s", String(gapsCount), gapsClass],
    ["Points in window", nPoints != null ? String(nPoints) : "—", ""],
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
    applyBtn.textContent = `Apply (${suggested.toFixed(1)} s)`;
    applyBtn.addEventListener("click", () => {
      optGpxOffset.value = String(suggested);
      optGpxOffsetValue.textContent = optGpxOffset.value;
      runGpxAnalyze();
    });
    row.innerHTML = `<span>Suggested offset</span>`;
    row.appendChild(applyBtn);
    gpxResultEl.appendChild(row);
  }
}

document.getElementById("opt-gpx-analyze").addEventListener("click", runGpxAnalyze);

document.getElementById("options-launch").addEventListener("click", async () => {
  const inputs = state.optionsInputs.length ? [...state.optionsInputs] : [...state.selected];
  if (inputs.length === 0) {
    toast("No file selected.", true);
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
    // Stabilization stays forced to false while the Street View profile is active
    // (checkbox unchecked + locked by updateStabilizeLockState()).
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
    toast(`Conversion started for ${inputs.length} file${inputs.length > 1 ? "s" : ""}.`);
    closeOptionsPanel();
    state.optionsInputs = [];
    state.selected.clear();
    renderFiles();
    switchView("queue");
  } catch (err) {
    toast(err.message, true);
  }
});

// ---------- Queue view ----------

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
    // If we are waiting for a job's H.264 proxy (preview_url), restart the preview as soon as it appears.
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
    // Silent during polling to avoid spamming the user; visible only
    // if we are explicitly on the queue view.
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
    const fpsTxt = job.fps != null ? `${job.fps.toFixed?.(1) ?? job.fps} fps` : "— fps";
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
      previewBtn.textContent = "360° preview";
      previewBtn.addEventListener("click", () => previewJobOutput(job));
      actions.appendChild(previewBtn);
    }
    if (job.status === "queued" || job.status === "running") {
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn danger";
      cancelBtn.textContent = "Cancel";
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
    toast("Job cancelled.");
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
  document.getElementById("preview-title").textContent = `Output — ${basename(job.output || job.input)}`;
  if (!viewer) {
    toast("360° preview unavailable (WebGL required).", true);
    return;
  }
  setPreviewControlsEnabled({ playPause: true, reset: true });
  hideViewerStatus();
  state.waitingPreviewJobId = null;

  // The backend provides a browser-readable H.264 proxy (preview_url) when it is
  // ready; otherwise we try the output file directly (may be 10-bit HEVC
  // that Chrome/Linux cannot decode).
  const url = job.preview_url || api.mediaUrl(job.output);
  try {
    await viewer.loadVideo(url);
    updatePlayPauseLabel();
    setPhotoSource(job.output); // full-resolution extraction from the converted MP4
  } catch (err) {
    setPreviewControlsEnabled({ playPause: false, reset: false });
    if (!job.preview_url) {
      // Output not decodable and no proxy yet: wait for it to appear
      // via the /api/jobs polling.
      state.waitingPreviewJobId = job.id;
      showViewerStatus(`${err.message} — preparing preview…`);
    } else {
      showViewerStatus(err.message);
    }
    toast(err.message, true);
  }
}

// 1 s polling: active if the queue view is displayed, if jobs are running,
// or if we are waiting for a finished job's H.264 proxy (preview_url).
setInterval(() => {
  if (state.view === "queue" || hasActiveJobs() || state.waitingPreviewJobId) {
    refreshJobs();
  }
}, 1000);

// ---------- 360° preview view ----------

const previewPlayPauseBtn = document.getElementById("preview-playpause");
const previewResetBtn = document.getElementById("preview-reset-view");

function setPreviewControlsEnabled({ playPause, reset }) {
  previewPlayPauseBtn.disabled = !playPause;
  previewResetBtn.disabled = !reset;
}

function updatePlayPauseLabel() {
  previewPlayPauseBtn.textContent = viewer && !viewer.isPaused ? "Pause" : "Play";
}

previewPlayPauseBtn.addEventListener("click", () => {
  if (!viewer) return;
  viewer.togglePlayPause();
  updatePlayPauseLabel();
});
previewResetBtn.addEventListener("click", () => viewer && viewer.resetView());

// ---------- Opening a file in the viewer (from the Files view) ----------

// Loads a media (OSV/MP4/JPEG) into the viewer and switches to the Preview
// tab. Common entry point for the "Open a file…" button of the Files
// toolbar and the "Open in 360°" action of each card.
async function loadMediaIntoViewer(path) {
  if (!path) return;
  switchView("preview");
  if (!viewer) {
    toast("360° preview unavailable (WebGL required).", true);
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
      // .OSV: the browser cannot read it → navigation proxy generated server-side
      setPreviewControlsEnabled({ playPause: false, reset: false });
      showViewerStatus("Preparing navigation proxy…");
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
    title: "Open a 360° file",
    filter: "osv,mp4,jpg,jpeg",
    startDir: dirnameOf(state.photoSource) || state.config.source_dir || null,
  });
  if (!chosen) return;
  loadMediaIntoViewer(chosen);
});

// ---------- "Extract a photo" panel ----------

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

let syncingFromFields = false; // guard against fields ↔ view loop
let syncingFromProjWheel = false; // guard against wheel (projection mode) ↔ fields loop

// ---- Ratio (presets + "Custom") ----

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
  const open = photoPanel.hidden; // state after toggle
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
    // Pre-fills the start yaw from the current view orientation
    photoCylYaw.value = viewer.yaw.toFixed(1);
  }
  updateCaptureOverlay();
  updateViewerProjection();
}

const VIEWER_HELP_DEFAULT = "Drag: rotate · Wheel: zoom · Arrows: rotate · +/-: zoom";
const VIEWER_HELP_BY_PROJECTION = {
  cylindrical: "Wheel: start yaw · Drag/Arrows/+/-: no effect on the projection",
  equirect360: "Full equirectangular projection — no settings",
  littleplanet: "Wheel: rotation · Drag/Arrows/+/-: no effect on the projection",
};

// Switches the main view between the navigable sphere ("flat") and the full-frame
// rendering of the chosen projection (cylindrical / full equirect / little planet),
// via the "projection" mode merged into viewer.js (formerly projpreview.js). Only active
// when the "Extract a photo" panel is open.
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

// Wheel sync (projection mode) → numeric fields: the wheel on the main view
// adjusts yawStart (cylindrical) / rotation (little planet) instead of
// zoom; the matching field must stay synced in both directions.
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

// Sync fields → view (editing yaw/pitch/roll orients the viewer)
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

// Sync view → fields (moving the view updates yaw/pitch/roll)
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

// Capture frame overlay: angular rectangle (requested ratio + FOV)
// projected into the viewer's current view.
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
  // v_fov = 2·atan(tan(h_fov/2)·h/w) — same formula as the backend (no stretching)
  const vfovReq = (2 * Math.atan(Math.tan(deg2rad(hfovReq) / 2) * (rh / rw)) * 180) / Math.PI;

  const hfovView = viewer.hFov;
  const vfovView = viewer.fov;

  // Fraction of the screen occupied by the frame (perspective projection, centered frame)
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

// ---- Interactive frame: dragging the inside = panning the aim; handles = FOV/ratio ----

const deg2rad = (d) => (d * Math.PI) / 180;
const rad2deg = (r) => (r * 180) / Math.PI;

let frameDrag = null; // { mode: "pan"|"resize", handle, lastX, lastY }

function hfovFromFraction(fx) {
  // screen fraction → horizontal FOV (perspective projection, centered frame)
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
    // Moving the frame = moving the aim (the frame stays centered on screen)
    const dx = e.clientX - frameDrag.lastX;
    const dy = e.clientY - frameDrag.lastY;
    frameDrag.lastX = e.clientX;
    frameDrag.lastY = e.clientY;
    const degPerPxX = viewer.hFov / W;
    const degPerPxY = viewer.fov / H;
    viewer.setOrientation(viewer.lon + dx * degPerPxX, viewer.lat - dy * degPerPxY, null);
    return;
  }

  // Handle resize: target half-extents from the center of the canvas
  const cx = rect.left + W / 2;
  const cy = rect.top + H / 2;
  const fx = Math.max(0.02, Math.min(0.995, Math.abs(e.clientX - cx) / (W / 2)));
  const fy = Math.max(0.02, Math.min(0.995, Math.abs(e.clientY - cy) / (H / 2)));
  const h = frameDrag.handle;
  const horiz = h.includes("e") || h.includes("w");
  const vert = h.includes("n") || h.includes("s");
  const { rw, rh } = currentRatioParts();

  if (photoRatio.value !== "custom") {
    // Preset ratio: centered homothety → only h_fov changes
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
    // Custom ratio: width and height adjust independently, a:b fields follow
    let hfov = parseFloat(photoHfov.value);
    let vfov = vfovFromHfov(hfov, rw, rh);
    if (horiz) hfov = Math.max(30, Math.min(140, hfovFromFraction(fx)));
    if (vert) vfov = vfovFromFraction(fy);
    hfov = setHfovClamped(hfov);
    // rh/rw = tan(v/2)/tan(h/2), bounded to stay within a/b ∈ [0.2, 8]
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

// Current video time (shown in the panel)
setInterval(() => {
  if (!photoPanelOpen()) return;
  const t = viewer ? viewer.currentTime : null;
  photoTimeEl.textContent = t == null ? "—" : `${t.toFixed(1)} s`;
}, 500);

async function extractPhoto() {
  if (!state.photoSource) {
    toast("No source media for extraction.", true);
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
    toast("Ratio out of bounds: width/height must stay between 0.2 and 8.", true);
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

// ---------- Initialization ----------

async function init() {
  const canvas = document.getElementById("viewer-canvas");
  try {
    viewer = new Viewer360(canvas);
  } catch (err) {
    // WebGL unavailable (drivers, lost context…): the rest of the application
    // must keep working, only the viewer is disabled.
    viewer = null;
    showViewerStatus(`360° preview unavailable (WebGL required): ${err.message}`);
  }
  hookViewerSync();
  hookViewerProjectionSync();

  await loadConfig();
  refreshShortcuts();
  await loadFiles();
  switchView("files");
}

init();
