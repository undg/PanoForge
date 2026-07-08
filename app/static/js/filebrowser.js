// PanoForge — sélecteur de fichiers/dossiers (modal, GET /api/browse)
// openFileBrowser({title, filter, startDir}) → Promise<string|null>
//   filter = extensions CSV (ex. "gpx") → choix de fichier ; sans filter → choix de dossier.

import * as api from "/js/api.js";

let els = null;
let current = null; // { resolve, filter, dir, parent, entries, selectedIndex, lastFocus }

// Icônes du bandeau « Accès rapide » par nature de raccourci (GET /api/browse/roots)
const SHORTCUT_ICONS = {
  home: "🏠",
  removable: "💾",
  source: "📥",
  output: "📤",
  camera: "📷",
};
const SHORTCUT_LABELS_FALLBACK = {
  home: "Dossier personnel",
  removable: "Support amovible",
  source: "Dossier source",
  output: "Dossier de sortie",
  camera: "Caméra",
};

function ensureEls() {
  if (els) return;
  els = {
    overlay: document.getElementById("browser-overlay"),
    title: document.getElementById("browser-title"),
    path: document.getElementById("browser-path"),
    list: document.getElementById("browser-list"),
    shortcuts: document.getElementById("browser-shortcuts"),
    hint: document.getElementById("browser-hint"),
    choose: document.getElementById("browser-choose"),
    cancel: document.getElementById("browser-cancel"),
    close: document.getElementById("browser-close"),
  };
  els.cancel.addEventListener("click", () => finish(null));
  els.close.addEventListener("click", () => finish(null));
  els.overlay.addEventListener("click", (e) => {
    if (e.target === els.overlay) finish(null);
  });
  els.choose.addEventListener("click", chooseCurrent);
  els.list.addEventListener("keydown", onListKeydown);
  els.overlay.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      finish(null);
    }
  });
}

function finish(value) {
  if (!current) return;
  const { resolve, lastFocus } = current;
  current = null;
  els.overlay.hidden = true;
  if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
  resolve(value);
}

function chooseCurrent() {
  if (!current) return;
  const entry = current.entries[current.selectedIndex];
  if (current.filter) {
    // Mode fichier : il faut un fichier sélectionné
    if (entry && entry.kind === "file") finish(entry.path);
  } else {
    // Mode dossier : dossier sélectionné, sinon dossier courant
    if (entry && entry.kind === "dir") finish(entry.path);
    else finish(current.dir);
  }
}

function activateEntry(entry) {
  if (!entry) return;
  if (entry.kind === "parent" || entry.kind === "dir") {
    load(entry.path);
  } else if (entry.kind === "file") {
    finish(entry.path);
  }
}

function onListKeydown(e) {
  if (!current) return;
  const n = current.entries.length;
  switch (e.key) {
    case "ArrowDown":
      setSelected(Math.min(n - 1, current.selectedIndex + 1));
      break;
    case "ArrowUp":
      setSelected(Math.max(0, current.selectedIndex - 1));
      break;
    case "Home":
      setSelected(0);
      break;
    case "End":
      setSelected(n - 1);
      break;
    case "Enter": {
      const entry = current.entries[current.selectedIndex];
      if (entry && entry.kind === "file") finish(entry.path);
      else if (entry) activateEntry(entry);
      else if (!current.filter) finish(current.dir);
      break;
    }
    case "Backspace":
      if (current.parent) load(current.parent);
      break;
    default:
      return;
  }
  e.preventDefault();
}

function setSelected(index) {
  if (!current || current.entries.length === 0) return;
  current.selectedIndex = index;
  const items = els.list.querySelectorAll("[role=option]");
  items.forEach((el, i) => {
    const sel = i === index;
    el.classList.toggle("selected", sel);
    el.setAttribute("aria-selected", String(sel));
    if (sel) {
      els.list.setAttribute("aria-activedescendant", el.id);
      el.scrollIntoView({ block: "nearest" });
    }
  });
  updateChooseState();
}

function updateChooseState() {
  if (!current) return;
  const entry = current.entries[current.selectedIndex];
  if (current.filter) {
    els.choose.disabled = !(entry && entry.kind === "file");
  } else {
    els.choose.disabled = false;
    els.choose.textContent =
      entry && entry.kind === "dir" ? "Choisir ce dossier" : "Choisir le dossier courant";
  }
}

function formatSize(bytes) {
  if (bytes == null) return "";
  const units = ["o", "Ko", "Mo", "Go"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatDate(mtime) {
  if (!mtime) return "";
  const d = new Date(mtime * 1000);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// Sépare le radical et l'extension pour que l'extension reste toujours visible
// (le radical est tronqué par CSS, jamais l'extension).
function splitName(name) {
  const dot = name.lastIndexOf(".");
  if (dot > 0 && dot < name.length - 1) {
    return { base: name.slice(0, dot), ext: name.slice(dot) };
  }
  return { base: name, ext: "" };
}

function render() {
  els.path.textContent = current.dir;
  els.list.innerHTML = "";
  current.entries.forEach((entry, i) => {
    const li = document.createElement("li");
    li.id = `browser-opt-${i}`;
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", "false");
    li.className = `browser-item ${entry.kind}`;
    const icon = entry.kind === "file" ? "🗎" : entry.kind === "parent" ? "↩" : "🗀";
    const label = entry.kind === "parent" ? ".. (dossier parent)" : entry.name;
    li.innerHTML =
      `<span class="browser-icon" aria-hidden="true">${icon}</span>` +
      `<span class="browser-name"><span class="browser-name-base"></span><span class="browser-ext"></span></span>` +
      `<span class="browser-date muted small"></span>` +
      `<span class="browser-size muted small"></span>`;
    if (entry.kind === "file") {
      const { base, ext } = splitName(label);
      li.querySelector(".browser-name-base").textContent = base;
      li.querySelector(".browser-ext").textContent = ext;
    } else {
      li.querySelector(".browser-name-base").textContent = label;
    }
    li.querySelector(".browser-name").title = label; // nom complet au survol
    li.querySelector(".browser-date").textContent =
      entry.kind === "parent" ? "" : formatDate(entry.mtime);
    li.querySelector(".browser-size").textContent =
      entry.kind === "file" ? formatSize(entry.size_bytes) : "";
    li.addEventListener("click", () => setSelected(i));
    li.addEventListener("dblclick", () => activateEntry(entry));
    els.list.appendChild(li);
  });
  if (current.entries.length === 0) {
    const li = document.createElement("li");
    li.className = "browser-item empty muted";
    li.textContent = current.filter ? "Aucun fichier correspondant dans ce dossier." : "Dossier vide.";
    els.list.appendChild(li);
  }
  setSelected(Math.min(current.selectedIndex, Math.max(0, current.entries.length - 1)));
  updateChooseState();
}

// ---------- Bandeau « Accès rapide » (GET /api/browse/roots) ----------

// Construit un bouton de raccourci (icône + libellé) ; `onPick(path)` est appelé au clic.
// Composant partagé entre le modal (navigation) et la barre d'outils Fichiers
// (changement du dossier source).
function buildShortcutButton(s, onPick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "browser-shortcut-btn";
  btn.title = s.path;
  const icon = document.createElement("span");
  icon.className = "browser-shortcut-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = SHORTCUT_ICONS[s.kind] || "🗀";
  const label = document.createElement("span");
  label.className = "browser-shortcut-label";
  label.textContent = s.label || SHORTCUT_LABELS_FALLBACK[s.kind] || s.path;
  btn.appendChild(icon);
  btn.appendChild(label);
  btn.addEventListener("click", () => onPick(s.path));
  return btn;
}

function fillShortcuts(container, shortcuts, onPick) {
  container.innerHTML = "";
  if (!shortcuts || shortcuts.length === 0) {
    const empty = document.createElement("p");
    empty.className = "browser-shortcuts-empty";
    empty.textContent = "Aucun raccourci disponible.";
    container.appendChild(empty);
    return;
  }
  for (const s of shortcuts) container.appendChild(buildShortcutButton(s, onPick));
}

function renderShortcuts(shortcuts) {
  if (!els.shortcuts) return;
  // Dans le modal, cliquer un raccourci navigue vers le dossier.
  fillShortcuts(els.shortcuts, shortcuts, (path) => {
    if (current) load(path);
  });
}

async function loadShortcuts() {
  if (!els.shortcuts) return;
  els.shortcuts.innerHTML = '<p class="browser-shortcuts-empty">Chargement…</p>';
  try {
    const data = await api.browseRoots();
    if (!current) return; // le modal a été fermé entre-temps
    renderShortcuts(data.shortcuts || []);
  } catch (err) {
    if (!current) return;
    els.shortcuts.innerHTML = '<p class="browser-shortcuts-empty">Accès rapide indisponible.</p>';
  }
}

/**
 * Monte le bandeau « Accès rapide » (mêmes raccourcis /api/browse/roots que le
 * modal) dans un conteneur permanent hors du modal — utilisé par la barre d'outils
 * de la vue Fichiers. `onPick(path)` est appelé au clic d'un raccourci.
 * @param {HTMLElement} container
 * @param {(path: string) => void} onPick
 * @returns {Promise<void>}
 */
export async function mountShortcuts(container, onPick) {
  if (!container) return;
  container.innerHTML = '<p class="browser-shortcuts-empty">Chargement…</p>';
  try {
    const data = await api.browseRoots();
    fillShortcuts(container, data.shortcuts || [], onPick);
  } catch (err) {
    container.innerHTML = '<p class="browser-shortcuts-empty">Accès rapide indisponible.</p>';
  }
}

async function load(dir) {
  try {
    const data = await api.browse(dir, current.filter);
    current.dir = data.dir;
    current.parent = data.parent ?? null;
    const entries = [];
    if (current.parent) entries.push({ kind: "parent", name: "..", path: current.parent });
    for (const d of data.dirs || [])
      entries.push({ kind: "dir", name: d.name, path: d.path, mtime: d.mtime });
    for (const f of data.files || [])
      entries.push({ kind: "file", name: f.name, path: f.path, size_bytes: f.size_bytes, mtime: f.mtime });
    current.entries = entries;
    current.selectedIndex = 0;
    render();
  } catch (err) {
    if (dir) {
      // Dossier de départ hors périmètre / inexistant : retomber sur le défaut serveur
      load(null);
    } else {
      els.path.textContent = "";
      els.list.innerHTML = "";
      const li = document.createElement("li");
      li.className = "browser-item empty muted";
      li.textContent = err.message;
      els.list.appendChild(li);
      current.entries = [];
      updateChooseState();
    }
  }
}

/**
 * Ouvre le sélecteur.
 * @param {{title: string, filter?: string|null, startDir?: string|null}} opts
 * @returns {Promise<string|null>} chemin choisi, ou null si annulé
 */
export function openFileBrowser({ title, filter = null, startDir = null }) {
  ensureEls();
  return new Promise((resolve) => {
    if (current) {
      // Un seul sélecteur à la fois : annule le précédent
      const prev = current;
      current = null;
      prev.resolve(null);
    }
    current = {
      resolve,
      filter,
      dir: null,
      parent: null,
      entries: [],
      selectedIndex: 0,
      lastFocus: document.activeElement,
    };
    els.title.textContent = title;
    els.hint.textContent = filter
      ? `Fichiers affichés : .${filter.split(",").join(", .")}`
      : "Choix d'un dossier";
    els.choose.textContent = filter ? "Choisir" : "Choisir le dossier courant";
    els.choose.disabled = Boolean(filter);
    els.overlay.hidden = false;
    els.list.focus();
    loadShortcuts(); // rafraîchi à chaque ouverture du modal
    load(startDir);
  });
}
