// PanoForge — client API REST (contrat défini dans SPEC.md)

async function request(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    throw new Error(`Impossible de contacter le serveur (${path}) : ${err.message}`);
  }
  if (!res.ok) {
    let detail = "";
    try {
      const data = await res.json();
      detail = data.detail || data.error || JSON.stringify(data);
    } catch {
      detail = await res.text().catch(() => "");
    }
    throw new Error(`Erreur ${res.status} sur ${path}${detail ? " : " + detail : ""}`);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return null;
}

export function getConfig() {
  return request("/api/config");
}

export function postConfig(data) {
  return request("/api/config", { method: "POST", body: data });
}

export function getFiles(dir) {
  const qs = dir ? `?dir=${encodeURIComponent(dir)}` : "";
  return request(`/api/files${qs}`);
}

export function thumbUrl(path) {
  return `/api/thumb?path=${encodeURIComponent(path)}`;
}

export function mediaUrl(path) {
  return `/api/media?path=${encodeURIComponent(path)}`;
}

export function probe(path) {
  return request("/api/probe", { method: "POST", body: { path } });
}

export function gpxAnalyze(payload) {
  return request("/api/gpx/analyze", { method: "POST", body: payload });
}

export function browse(dir, filter) {
  const params = new URLSearchParams();
  if (dir) params.set("dir", dir);
  if (filter) params.set("filter", filter);
  const qs = params.toString();
  return request(`/api/browse${qs ? "?" + qs : ""}`);
}

export function browseRoots() {
  return request("/api/browse/roots");
}

export function createJobs(payload) {
  return request("/api/jobs", { method: "POST", body: payload });
}

export function getJobs() {
  return request("/api/jobs");
}

export function deleteJob(id) {
  return request(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function photoExtract(payload) {
  return request("/api/photo/extract", { method: "POST", body: payload });
}

export function photoNavproxy(path) {
  return request(`/api/photo/navproxy?path=${encodeURIComponent(path)}`);
}
