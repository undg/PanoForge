"""REST routes — backend/frontend contract, see SPEC.md."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import config
from app.jobs import manager as job_manager

router = APIRouter(prefix="/api")

CHUNK_SIZE = 1024 * 1024
OSV_MEDIA_TYPE = "video/mp4"


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _allowed_roots() -> list[str]:
    cfg = config.get_config()
    roots = []
    # source, output + preview proxy cache (H.264 readable by browser)
    previews_dir = str(config.CACHE_DIR / "previews")
    for d in (cfg.source_dir, cfg.output_dir, previews_dir):
        try:
            roots.append(os.path.realpath(os.path.expanduser(d)))
        except OSError:
            pass
    return roots


def _resolve_within_roots(path: str) -> str:
    """Resolve a path and check it stays within source_dir/output_dir
    (or the preview proxy cache) — no arbitrary traversal."""
    real = os.path.realpath(os.path.expanduser(path))
    roots = _allowed_roots()
    for root in roots:
        if real == root or real.startswith(root + os.sep):
            return real
    raise HTTPException(status_code=403, detail="path outside allowed folders (source/output)")


def _guess_media_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".osv", ".mp4", ".mov", ".m4v"):
        return OSV_MEDIA_TYPE
    mt, _ = mimetypes.guess_type(path)
    return mt or "application/octet-stream"


# ---------------------------------------------------------------------------
# /api/config
# ---------------------------------------------------------------------------

class ConfigUpdate(BaseModel):
    source_dir: Optional[str] = None
    output_dir: Optional[str] = None


@router.get("/config")
def get_config():
    return config.config_summary()


@router.post("/config")
def post_config(body: ConfigUpdate):
    config.update_config(source_dir=body.source_dir, output_dir=body.output_dir)
    return config.config_summary()


# ---------------------------------------------------------------------------
# /api/files
# ---------------------------------------------------------------------------

def _thumb_url(path: str) -> str:
    from urllib.parse import quote
    return f"/api/thumb?path={quote(path)}"


def _scan_osv(base_dir: str) -> list[dict]:
    """List *.OSV in base_dir, 1 level recursive (base_dir + direct subfolders)."""
    results = []
    base = Path(base_dir)
    if not base.is_dir():
        return results
    candidates = [base] + sorted(p for p in base.iterdir() if p.is_dir())
    for d in candidates:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.suffix.lower() == ".osv":
                try:
                    st = entry.stat()
                except OSError:
                    continue
                duration_s = None
                try:
                    from app.core import osv
                    duration_s = osv.probe(str(entry)).duration_s
                except Exception:  # noqa: BLE001 - best effort, does not block the listing
                    duration_s = None
                results.append({
                    "path": str(entry),
                    "name": entry.name,
                    "size_bytes": st.st_size,
                    "mtime": st.st_mtime,
                    "duration_s": duration_s,
                    "thumb_url": _thumb_url(str(entry)),
                })
    return results


@router.get("/files")
def get_files(dir: Optional[str] = None):
    cfg = config.get_config()
    base_dir = dir or cfg.source_dir
    real_base = os.path.realpath(os.path.expanduser(base_dir))
    src_root = os.path.realpath(os.path.expanduser(cfg.source_dir))
    if real_base != src_root and not real_base.startswith(src_root + os.sep):
        raise HTTPException(status_code=403, detail="folder outside source_dir")
    if not os.path.isdir(real_base):
        raise HTTPException(status_code=404, detail=f"folder not found: {base_dir}")
    return _scan_osv(real_base)


# ---------------------------------------------------------------------------
# /api/browse — file/folder browser for the UI
# ---------------------------------------------------------------------------

def _browse_roots() -> list[str]:
    """Allowed roots for navigation: $HOME, /run/media, /media,
    /run/user/<uid>/gvfs (MTP camera mounts)."""
    return [os.path.realpath(r) for r in (
        str(Path.home()), "/run/media", "/media", config.gvfs_root(),
    )]


def _resolve_browse_dir(path: str) -> tuple[str, str]:
    """Resolve a folder and return (real path, allowed root containing it)."""
    real = os.path.realpath(os.path.expanduser(path))
    for root in _browse_roots():
        if real == root or real.startswith(root + os.sep):
            return real, root
    raise HTTPException(status_code=403, detail="folder outside the navigation scope")


@router.get("/browse")
def get_browse(dir: Optional[str] = None, filter: Optional[str] = None):
    base = dir or str(Path.home())
    real, root = _resolve_browse_dir(base)
    if not os.path.isdir(real):
        raise HTTPException(status_code=404, detail=f"folder not found: {base}")

    exts: set[str] = set()
    if filter:
        exts = {e.strip().lower().lstrip(".") for e in filter.split(",") if e.strip()}

    dirs: list[dict] = []
    files: list[dict] = []
    try:
        entries = sorted(os.scandir(real), key=lambda e: e.name.lower())
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"unreadable folder: {exc}") from exc
    for entry in entries:
        if entry.name.startswith("."):
            continue  # hidden entries excluded
        try:
            if entry.is_dir(follow_symlinks=True):
                try:
                    d_mtime = entry.stat().st_mtime
                except OSError:
                    d_mtime = 0
                dirs.append({"name": entry.name, "path": entry.path, "mtime": d_mtime})
            elif entry.is_file(follow_symlinks=True) and exts:
                ext = os.path.splitext(entry.name)[1].lower().lstrip(".")
                if ext in exts:
                    st = entry.stat()
                    files.append({
                        "name": entry.name,
                        "path": entry.path,
                        "size_bytes": st.st_size,
                        "mtime": st.st_mtime,
                    })
        except OSError:
            continue

    parent = None if real == root else os.path.dirname(real)
    return {"dir": real, "parent": parent, "dirs": dirs, "files": files}


@router.get("/browse/roots")
def get_browse_roots():
    """Quick-access shortcuts for the file picker: home, removable
    volumes, MTP camera (best-effort), configured source/output. Live
    detection on every call (see SPEC.md — Navigation to removable volumes)."""
    shortcuts: list[dict] = []
    seen: set[str] = set()

    def _add(label: str, path: str, kind: str) -> None:
        try:
            real = os.path.realpath(os.path.expanduser(path))
        except OSError:
            return
        if not real or real in seen:
            return
        try:
            if not os.path.isdir(real):
                return
        except OSError:
            return
        seen.add(real)
        shortcuts.append({"label": label, "path": real, "kind": kind})

    _add("Home", str(Path.home()), "home")

    for vol in config.removable_volumes():
        _add(vol["label"], vol["path"], "removable")

    for mount in config.camera_mounts():
        _add(mount["label"], mount["path"], "camera")

    cfg = config.get_config()
    _add("Source", cfg.source_dir, "source")
    _add("Output", cfg.output_dir, "output")

    return {"shortcuts": shortcuts}


# ---------------------------------------------------------------------------
# /api/thumb
# ---------------------------------------------------------------------------

@router.get("/thumb")
def get_thumb(path: str):
    real_path = _resolve_within_roots(path)
    if not os.path.isfile(real_path):
        raise HTTPException(status_code=404, detail="file not found")

    try:
        mtime = os.path.getmtime(real_path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f"{real_path}:{mtime}".encode("utf-8")).hexdigest()
    cache_path = config.cache_dir() / f"{key}.jpg"

    if not cache_path.is_file():
        try:
            from app.core import osv
            osv.extract_thumbnail(real_path, str(cache_path))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"thumbnail unavailable: {exc}") from exc

    return FileResponse(str(cache_path), media_type="image/jpeg")


# ---------------------------------------------------------------------------
# /api/probe
# ---------------------------------------------------------------------------

class ProbeRequest(BaseModel):
    path: str


@router.post("/probe")
def post_probe(body: ProbeRequest):
    real_path = _resolve_within_roots(body.path)
    from app.core import osv
    try:
        info = osv.probe(real_path)
    except osv.OsvError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    has_calibration = False
    import tempfile
    with tempfile.TemporaryDirectory(prefix="osmoprobe_") as td:
        try:
            meta = osv.extract_metadata(real_path, td)
            has_calibration = meta.get("calibration") is not None
        except osv.OsvError:
            has_calibration = False

    result = info.to_dict()
    result["has_calibration"] = has_calibration
    return result


# ---------------------------------------------------------------------------
# /api/gpx/analyze
# ---------------------------------------------------------------------------

class GpxAnalyzeRequest(BaseModel):
    gpx_path: str
    video_path: str
    offset_s: Optional[float] = 0.0


@router.post("/gpx/analyze")
def post_gpx_analyze(body: GpxAnalyzeRequest):
    video_path = _resolve_within_roots(body.video_path)
    if not os.path.isfile(body.gpx_path):
        raise HTTPException(status_code=404, detail=f"GPX file not found: {body.gpx_path}")

    try:
        from app.core.gpx import parse_gpx, analyze
    except ImportError as exc:
        raise HTTPException(status_code=501, detail=f"module core/gpx.py unavailable: {exc}") from exc

    from app.core import osv
    try:
        info = osv.probe(video_path)
    except osv.OsvError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        points = parse_gpx(body.gpx_path)
        result = analyze(points, info.creation_time_utc, info.duration_s)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"GPX analysis failed: {exc}") from exc
    return result


# ---------------------------------------------------------------------------
# /api/jobs
# ---------------------------------------------------------------------------

class JobOptions(BaseModel):
    out_w: Optional[int] = 7680
    codec: Optional[str] = "hevc"
    encoder: Optional[str] = "auto"
    quality: Optional[int] = 20
    interp: Optional[str] = "lanczos"
    mode: Optional[str] = "auto"
    fps_out: Optional[float] = None
    stabilize: Optional[bool] = False  # phase 2, inactive
    gpx_path: Optional[str] = None
    gpx_offset_s: Optional[float] = None
    embed_camm: Optional[bool] = False
    streetview: Optional[bool] = False


class JobsCreateRequest(BaseModel):
    inputs: list[str]
    options: JobOptions = JobOptions()


@router.post("/jobs")
def post_jobs(body: JobsCreateRequest):
    if not body.inputs:
        raise HTTPException(status_code=400, detail="no file provided")
    cfg = config.get_config()
    out_dir = os.path.realpath(os.path.expanduser(cfg.output_dir))
    os.makedirs(out_dir, exist_ok=True)

    created = []
    for input_path in body.inputs:
        real_input = _resolve_within_roots(input_path)
        if not os.path.isfile(real_input):
            raise HTTPException(status_code=404, detail=f"file not found: {input_path}")
        stem = Path(real_input).stem
        output_path = os.path.join(out_dir, f"{stem}_360.mp4")
        options = body.options.model_dump()
        job = job_manager.submit(real_input, output_path, options)
        created.append({"job_id": job.id})
    return created


@router.get("/jobs")
def get_jobs():
    return job_manager.list_jobs()


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    ok = job_manager.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found or already finished")
    return {"cancelled": True}


# ---------------------------------------------------------------------------
# /api/photo — 360 photo extraction (see SPEC.md)
# ---------------------------------------------------------------------------

class PhotoExtractRequest(BaseModel):
    source_path: str
    time_s: float = 0.0
    projection: str = "flat"          # flat | cylindrical | equirect360 | littleplanet
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    h_fov_deg: float = 90.0
    ratio: str = "16:9"
    v_span_deg: float = 60.0
    out_w: Optional[int] = None       # default: max equirect width of the source


def _media_url(path: str) -> str:
    from urllib.parse import quote
    return f"/api/media?path={quote(path)}"


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


@router.post("/photo/extract")
def post_photo_extract(body: PhotoExtractRequest):
    """Synchronous photo extraction from an OSV / 360° MP4 / equirect JPEG."""
    import tempfile

    from app.core import photo

    source, _ = _resolve_browse_dir(body.source_path)  # same roots as browse
    if not os.path.isfile(source):
        raise HTTPException(status_code=404, detail=f"file not found: {body.source_path}")

    cfg = config.get_config()
    photos_dir = os.path.join(os.path.realpath(os.path.expanduser(cfg.output_dir)), "photos")
    os.makedirs(photos_dir, exist_ok=True)
    stem = Path(source).stem
    out_jpg = _unique_path(os.path.join(
        photos_dir, f"{stem}_{body.time_s:.2f}s_{body.projection}.jpg"))

    params = {
        "yaw_deg": body.yaw_deg, "pitch_deg": body.pitch_deg, "roll_deg": body.roll_deg,
        "h_fov_deg": body.h_fov_deg, "ratio": body.ratio, "v_span_deg": body.v_span_deg,
        "out_w": body.out_w,
    }
    try:
        if body.projection == "flat":
            photo._parse_ratio(body.ratio)  # immediate 400, before costly stitching
        with tempfile.TemporaryDirectory(prefix="osmophoto_") as td:
            equirect = photo.get_equirect_frame(source, body.time_s, td)
            if params["out_w"] is None and body.projection != "littleplanet":
                params["out_w"] = photo.default_out_w(source)
            w, h = photo.reproject(equirect, body.projection, params, out_jpg, quality=95)
    except photo.PhotoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"photo_path": out_jpg, "preview_url": _media_url(out_jpg),
            "width": w, "height": h}


@router.get("/photo/navproxy")
def get_photo_navproxy(path: str):
    """Low-resolution equirect proxy to navigate within an OSV (disk cache)."""
    from app.core import photo

    source, _ = _resolve_browse_dir(path)
    if not os.path.isfile(source):
        raise HTTPException(status_code=404, detail="file not found")
    ext = os.path.splitext(source)[1].lower()
    if ext in (".mp4", ".mov", ".m4v"):
        # already readable by the browser: no proxy needed
        return {"proxy_url": _media_url(source)}
    if ext != ".osv":
        raise HTTPException(status_code=400,
                            detail="navigation proxy: .OSV or .mp4 source expected")
    previews_dir = str(config.cache_dir() / "previews")
    try:
        proxy = photo.nav_proxy(source, previews_dir)
    except photo.PhotoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"proxy_url": _media_url(proxy)}


# ---------------------------------------------------------------------------
# /api/media (Range support for browser playback)
# ---------------------------------------------------------------------------

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@router.get("/media")
def get_media(path: str, request: Request):
    real_path = _resolve_within_roots(path)
    if not os.path.isfile(real_path):
        raise HTTPException(status_code=404, detail="file not found")

    file_size = os.path.getsize(real_path)
    media_type = _guess_media_type(real_path)
    range_header = request.headers.get("range")

    start, end = 0, file_size - 1
    status_code = 200
    if range_header:
        m = _RANGE_RE.match(range_header.strip())
        if not m:
            raise HTTPException(status_code=416, detail="invalid Range header")
        start_str, end_str = m.groups()
        if start_str == "" and end_str == "":
            raise HTTPException(status_code=416, detail="invalid Range header")
        if start_str == "":
            length = int(end_str)
            start = max(0, file_size - length)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
        if start > end or start >= file_size:
            headers = {"Content-Range": f"bytes */{file_size}"}
            raise HTTPException(status_code=416, detail="range out of bounds", headers=headers)
        end = min(end, file_size - 1)
        status_code = 206

    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    def iterfile():
        with open(real_path, "rb") as fp:
            fp.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fp.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(iterfile(), status_code=status_code, headers=headers, media_type=media_type)
