# PanoForge — Architecture Specification

> **Rename (2026-07-08)**: the product is now called **PanoForge** (former name
> "Osmo 360 Studio", abandoned to avoid the DJI "Osmo" trademark). "DJI" and "Osmo"
> must appear ONLY in compatibility sentences ("for .OSV files
> from DJI Osmo 360 cameras"), never in the product name, repository, or logo.
> README: add a disclaimer "independent project, not affiliated with DJI".
> Config/cache migration: see the "Rename" section below.

**Local** web app (Ubuntu/GNOME): Python backend (FastAPI) + browser interface.
Converts `.OSV` files from the DJI Osmo 360 to equirectangular 360° MP4, with
spherical metadata, optional GPS from external GPX (CAMM track), batch queue
and interactive 360° preview. Audience: a single user, on their machine (no
auth, bind 127.0.0.1).

## Established facts (analysis phase — do not re-verify)

- `.OSV` = ISOM MP4: stream 0 and 1 = fisheye HEVC 10-bit 3840×3840 (>180°),
  stream 2 = AAC, djmd streams = DJI protobuf metadata, last stream = equirectangular
  MJPEG thumbnail 688×344 (stitching reference).
- Factory optical calibration embedded in the 1st djmd sample: per lens
  fx/fy, cx/cy, distortion (4 Brown-Conrady coeffs, to validate), yaw/pitch,
  extrinsic quaternion, 2 radial LUTs. Extraction: `app/core/osv_meta/extract_djmd.py`
  (produces `calibration.json`, `imu_perframe.csv`, `imu_highrate.csv` ~995 Hz quaternions).
  Real example: `tests/fixtures/calibration.json`.
- No internal GPS in the camera → external GPX = only geo source.
- Stitching baseline validated empirically (ffmpeg 8.0):
  `[0:0][0:1]hstack[s];[s]v360=input=dfisheye:output=e:ih_fov=190:iv_fov=190:yaw=90:w=7680:h=3840:interp=lanczos`
  → correct alignment vs thumbnail. Flaw: visible parallax on close objects (no blending).
- GTX 1650: `hevc_nvenc`/`h264_nvenc` OK up to 7680×3840. Bottleneck = CPU v360 filter
  (~3.6 fps in lanczos 8K, ~7.4 fps bilinear).
- Street View Studio: 2:1 equirect MP4/MOV, duration 2–60 min, GPS without gap > 5 s,
  ≥ 10 points, stabilization DISABLED for Street View. Embedded GPS = CAMM track
  (type 6 packets: `uint16 reserved=0, uint16 type=6, double time_gps_epoch,
  int32 gps_fix_type, double lat, double lon, float alt, float h_acc, float v_acc,
  float vel_e, float vel_n, float vel_up, float speed_acc`, little-endian, monotonic PTS).
  Code ref: trek-view/telemetry-injector (note: it does NOT temporally align
  GPX↔video, bug not to reproduce).
- Spherical metadata: V1 (GSpherical XML uuid box) + V2 (sv3d/proj/equi) — write both.

## Directory tree

```
osmo360-studio/
  SPEC.md  README.md  requirements.txt  run.sh
  app/
    main.py            # FastAPI app + uvicorn entry (port 8360, 127.0.0.1)
    api.py             # REST routes (contract below)
    jobs.py            # queue + ffmpeg execution + progress
    config.py          # default paths (source; output = `xdg-user-dir VIDEOS`/osmo360, i.e. ~/Vidéos/osmo360 here, fallback ~/Videos then ~)
    core/
      osv.py           # ffprobe probe + extract_djmd wrapper → OsvInfo, metadata
      osv_meta/        # djmd extraction (delivered, do not rewrite)
      maps.py          # calibration.json → ffmpeg remap maps + blending masks
      stitch.py        # ffmpeg command builder (v360 / calibrated modes)
      gpx.py           # GPX parsing, windowing, interpolation, offset
      camm.py          # CAMM track muxing into an existing MP4
      spherical.py     # V1+V2 spherical metadata injection
    static/            # frontend (vanilla JS + three.js vendored in static/vendor/)
  tests/               # pytest; fixtures/ = real calibration
  tools/               # reverse-engineering notes
```

## Core module contracts (signatures to respect)

```python
# osv.py
@dataclass
class OsvInfo:
    path: str; duration_s: float; fps: float; width: int; height: int
    creation_time_utc: datetime | None; size_bytes: int; audio: bool
def probe(path: str) -> OsvInfo
def extract_metadata(path: str, workdir: str) -> dict   # {"calibration": dict|None, "imu_perframe": str, "imu_highrate": str}
def extract_thumbnail(path: str, out_jpg: str) -> str    # embedded equirect thumbnail

# maps.py
def generate_remap_maps(calibration: dict, out_w: int, out_h: int, workdir: str) -> MapSet
# MapSet: per-lens 16-bit PGM xmap/ymap (ffmpeg `remap` filter) + blending mask(s)
# (gradient at the seams, gray PNG). Must handle the calibration=None fallback.

# stitch.py
@dataclass
class StitchOptions:
    out_w: int = 7680            # 7680, 6144 or 3840 (h = w/2)
    codec: str = "hevc"          # "hevc" | "h264"
    encoder: str = "auto"        # "auto"→nvenc if available else cpu; "nvenc" | "cpu"
    quality: int = 20            # cq nvenc / crf cpu
    interp: str = "lanczos"      # "lanczos" | "line"
    mode: str = "auto"           # "auto"→calibrated if maps available else v360; "v360" | "calibrated"
    fps_out: float | None = None # None = keep; else e.g. 5 for Street View
def build_command(input_path, output_path, opts, maps: MapSet | None) -> list[str]
# v360 mode = baseline command above. calibrated mode = split → per-lens remap
# → blend by mask (maskedmerge/mergeplanes or blend) → output. Audio copied.
# Always add -progress pipe:1 -nostats for tracking.

# gpx.py
@dataclass
class GpxPoint: t: datetime; lat: float; lon: float; ele: float | None; speed: float | None
def parse_gpx(path) -> list[GpxPoint]                    # stdlib xml, chronological sort, UTC
def analyze(points, video_start_utc, duration_s) -> dict # {overlap_s, coverage_pct, gaps>5s, n_points_in_window, suggested_offset_s}
def resample(points, video_start_utc, duration_s, offset_s, rate_hz=1.0) -> list[GpxPoint]
# windowing [start+offset, start+offset+duration], linear interpolation, computed E/N velocity

# camm.py
def inject_camm(mp4_in, mp4_out, samples: list[GpxPoint], video_start_utc) -> None
# new meta/camm track, type 6 packets, fine timescale, monotonic PTS

# spherical.py
def inject_spherical(mp4_in, mp4_out) -> None            # V1 uuid XML + V2 sv3d, mono
def export_windowed_gpx(points, video_start_utc, duration_s, offset_s, out_path) -> None
```

## Pipeline of a "convert" job

1. `probe` + `extract_metadata` (workdir = job temp folder)
2. `generate_remap_maps` if calibrated mode
3. ffmpeg stitch → temporary MP4 (progress parsed from `-progress`)
4. `inject_spherical` (always)
5. if GPX provided: `resample` + `inject_camm` (+ `export_windowed_gpx` as side-car)
6. atomic move to the output folder: `<name>_360.mp4`

"Street View" profile = UI preset: fps_out=5, CAMM required, stabilization off, HEVC cq 20.

## REST API (backend ↔ frontend contract)

- `GET  /api/config` → `{source_dir, output_dir, has_nvenc, version}`
- `POST /api/config` → update folders
- `GET  /api/files?dir=` → `[{path, name, size_bytes, mtime, duration_s?, thumb_url}]` (*.OSV, recursive 1 level)
- `GET  /api/thumb?path=` → embedded thumbnail JPEG (disk cache)
- `POST /api/probe {path}` → OsvInfo + `{has_calibration: bool}`
- `POST /api/gpx/analyze {gpx_path, video_path, offset_s?}` → return of `gpx.analyze`
- `POST /api/jobs {inputs: [path], options: StitchOptions-like + {gpx_path?, gpx_offset_s?, embed_camm: bool, streetview: bool}}` → `[{job_id}]` (one job per file)
- `GET  /api/jobs` → list `{id, input, output, status: queued|running|done|error|cancelled, progress: 0..1, fps, eta_s, error?}`
- `DELETE /api/jobs/{id}` → cancels (kills ffmpeg if running)
- `GET  /api/media?path=` → serves a video file with **Range** support (three.js preview)
- `GET  /api/browse?dir=&filter=` → file/folder browser for the UI:
  `{dir, parent: str|null, dirs: [{name, path, mtime}], files: [{name, path, size_bytes, mtime}]}`
  (`mtime` = modification date in epoch seconds, displayed in the picker;
  the picker truncates the stem but keeps the extension visible + tooltip of the full name).
  `dir` defaults to home. `filter` = comma-separated extensions, case-insensitive
  (e.g. `osv` or `gpx`); without `filter`, `files` stays empty (folder choice).
  Navigation restricted to $HOME, /run/media and /media; hidden entries (.*) excluded;
  folders sorted before files, alphabetical order. Outside allowed scope → 403.
- `GET  /` → static frontend

One ffmpeg job at a time (worker thread + queue). State in memory (no DB).

## Frontend (app/static/)

Vanilla JS + vendored three.js (NO CDN at runtime). Interface in **English**.
3 views: **Files** (grid with thumbnails, multi-select, Convert button),
**Queue** (progress, cancel, open folder), **360° Preview**
(three.js: inverted sphere + VideoTexture, drag to rotate, wheel = zoom/FOV,
play/pause; works on converted output AND as a pre-view of the
embedded thumbnail). Conversion options panel (resolution, codec, quality,
interpolation, stitching mode, Street View profile, GPX: file + analysis + offset
slider in seconds with visual coverage feedback).
Polling `GET /api/jobs` every 1 s. Simple dark theme, hand-rolled CSS.

## 360 photo extraction ("photo" feature)

Extract photos from three sources: converted 360° MP4 (seek to the chosen time),
raw `.OSV` (calibrated stitching of ONE frame, full resolution), 360° JPEG photo from the
camera (2:1 equirect, used as-is). Output: JPEG quality 95 in
`<output_dir>/photos/<basename>_<time>_<projection>.jpg`.

- `app/core/photo.py`:
  - `get_equirect_frame(source_path, time_s, workdir) -> str` — full-resolution
    equirect PNG depending on the source type (for OSV: reuse the existing maps/stitch).
  - `reproject(equirect_path, projection, params, out_jpg, quality=95) -> (w, h)` —
    via ffmpeg v360:
    - `flat`: perspective (yaw/pitch/roll, `h_fov` 30–140°, ratio among 16:9, 21:9,
      32:9, 4:3, 1:1, 9:16 **or free: any "a:b" string** with a, b > 0 and
      a/b ∈ [0.2, 8], otherwise explicit 400; `v_fov = 2·atan(tan(h_fov/2)·h/w)` for
      a correct perspective, no stretching).
    - `cylindrical`: full 360° turn, adjustable `v_span_deg` (default 60°), starting yaw.
    - `equirect360`: full 2:1 equirect + **GPano XMP** injected (ProjectionType,
      UsePanoramaViewer, FullPano dimensions) → interactive spherical photo
      Google Photos/Facebook.
    - `littleplanet`: downward stereographic view (pitch −90°, roll = rotation).
  - `nav_proxy(source_path, cache_dir) -> str` — low-resolution equirect navigation
    proxy (~688 px, fast H.264) to choose the moment in an OSV that the
    browser cannot read; cached in previews.
- API:
  - `POST /api/photo/extract {source_path, time_s, projection, yaw_deg, pitch_deg,
    roll_deg, h_fov_deg, ratio, v_span_deg, out_w}` → `{photo_path, preview_url,
    width, height}` (synchronous; out_w default = max width of the source).
  - `GET /api/photo/navproxy?path=` → `{proxy_url}` (generates + caches).
- UI (360° Preview view): "Open a file…" button (browse, filter osv,mp4,jpg,jpeg)
  and "Extract a photo" panel: projection, ratio (presets + "Free" with
  a:b fields), FOV, numeric yaw/pitch/roll fields **synchronized both ways**
  with the viewer view, capture frame overlay (exact area per
  ratio+FOV), resolution, Extract button → result preview + file path.
  For cylindrical/equirect360/littleplanet, only show the relevant settings.
- **Interactive frame** (flat projection): the overlay is manipulated with the mouse —
  drag inside = moves the aim (yaw/pitch), drag corner/edge handles =
  adjusts the FOV (preset ratio: homothety) or the ratio (Free mode: the a:b
  fields follow). Adapted cursors (move/resize), continuous sync with the numeric fields.
- **Real-time preview of the other projections** (cylindrical, equirect360,
  littleplanet): preview panel rendered CLIENT-SIDE in WebGL (shader applying the
  same reprojection as the backend to the current equirect texture of the viewer,
  low resolution ~512 px, updated continuously — including during playback and
  when settings change). Server-side extraction remains the full-quality reference.

## Projection preview in the main view (evolution)

The cylindrical / equirect / little planet previews must NO LONGER be limited
to the small canvas of the panel: the **main view** (large viewer canvas) directly
renders the selected projection.
- `viewer.js` manages two modes: `sphere` (the "flat" projection — navigable
  sphere + interactive frame, current behavior) and `projection` (cylindrical/equirect360/
  littleplanet — renders the full-frame projection via the shader of `projpreview.js`,
  merged into viewer.js or driven by it on the main canvas). Changing
  projection switches the mode; returning to "flat" restores the sphere.
- The small panel canvas becomes useless → remove it (or keep it as a thumbnail
  only if trivial). The "low-resolution preview" note remains.
- **Starting yaw with the wheel**: in cylindrical mode (and rotation in little planet),
  the wheel on the main view adjusts the starting yaw / rotation (instead of
  FOV zoom, which is meaningless for these projections). The numeric field stays synchronized.
  Wheel = zoom only in sphere/flat mode.

## Navigation to removable volumes / camera (evolution)

The file picker must give quick access to removable media.
- `GET /api/browse/roots` → `{shortcuts: [{label, path, kind}]}` where `kind` ∈
  `home | removable | source | output | camera`. Live detection on each call:
  - `home`: $HOME.
  - `removable`: each mounted subfolder of `/run/media/$USER` and `/media/$USER`
    (label = volume name, e.g. "SD_Card").
  - `camera` (best-effort): MTP mounts under `/run/user/<uid>/gvfs/` whose name
    contains the device (prefix `mtp:` / `gphoto2:`) — listed if present, ignored
    otherwise; document that the Osmo "USB storage" mode shows up as
    `removable` instead.
  - `source`/`output`: current configured folders.
- The allowed browse roots already include `/run/media` and `/media`; add
  `/run/user/<uid>/gvfs` to the whitelist for the MTP camera.
- UI (`filebrowser.js`): "Quick access" column/banner listing these shortcuts
  (icon per kind), a click navigates to the folder. Refreshed when the modal opens.

## Gyroscopic stabilization (phase 2 — activation)

Goal: level the horizon and smooth shakes by counter-rotating each frame
according to the IMU orientation, as the camera does on its thumbnail. Data already
extracted by `extract_djmd.py`: `imu_highrate.csv` (~1 kHz quaternions) and
`imu_perframe.csv`. **Axis convention/quaternion order to be VALIDATED empirically**
first (cf. NOTES-djmd: `[w,x,y,z]` assumed, frame to be confirmed).

- `app/core/stabilize.py`:
  - `load_orientations(imu_csv, fps, n_frames, time_base) -> list[quat]` — one
    orientation per video frame (resampling/slerp from the high-frequency stream,
    aligned with the PTS).
  - `compute_corrections(quats, mode, params) -> list[(yaw,pitch,roll)]` in degrees,
    to apply at equirect output (v360 rotation after stitch):
    - `horizon`: cancels pitch+roll (leveled horizon), keeps yaw (heading).
    - `lock`: locks the absolute orientation (full counter-rotation to a ref).
    - `smooth`: follows a smoothed orientation (low-pass filter / moving average,
      adjustable window) → removes shakes while keeping slow movements.
  - Must handle the absence of IMU (returns null corrections + flag).
- `stitch.py` integration: `StitchOptions.stabilize: bool` + `stabilize_mode` +
  `stabilize_strength`. When active, inject the per-frame rotations into the `v360`
  filter via `sendcmd`/`zmq` (time-varying yaw/pitch/roll) — in
  calibrated mode as well as v360. Stay NVENC-compatible.
- `jobs.py`: call stabilize in the pipeline before encoding when the option is
  active; **the Street View profile forces `stabilize=False`** (Google requirement).
- UI: un-gray the "Stabilization" field (checkbox + mode choice + strength slider),
  disabled and explicitly locked when "Street View Profile" is checked.
- Empirical validation MANDATORY on a real handheld clip
  (`~/Vidéos/osmo360/echantillons/CAM_20260708072843_0001_D.OSV`, 6.3 s): compare
  frames before/after, check that the horizon is leveled and drift is
  reduced, by LOOKING at the images. Document the retained axis convention.

## Ergonomics reorganization (2026-07-08 — validated mockup)

Reference mockup validated by the user: `work/ux/maquette_validee.html`
(LOOK at it for the target layout). Principle: **the "Files" view becomes the SINGLE
entry point for all loading**; the viewer is now only a destination.

Changes to implement (frontend `app/static/`):
1. **Toolbar at the top of the Files view** grouping what used to be scattered:
   - current source folder + "Browse…" button (opens the filebrowser, kind folder);
   - "Quick access" banner (home/removable/camera shortcuts via /api/browse/roots) —
     the SAME component as today, but displayed here permanently, not only
     in the modal;
   - **"Open a file…"** button (filter osv,mp4,jpg,jpeg) — **MOVED** from the
     Preview view to this bar. It is the only point for opening an arbitrary media.
2. **Each file card exposes two explicit actions**: "Convert" (→ options
   panel, current behavior) and "Open in 360°" (→ loads the media into the
   viewer and switches to the Preview tab). Multi-select + "Convert
   selection" remain for the batch.
3. **The 360° Preview view loses its "Open a file…" button**: it now only serves to
   view + extract. When no media is loaded, display an empty state that points
   to the Files tab ("To open a media, go through the Files tab"), not an
   open button. The existing entries (preview of a finished job, "Open in 360°"
   from a card) remain the ways to load a media there.
4. ⚙ Settings: the source/output folder may stay in Settings, but the source
   folder must ALSO be controllable from the Files toolbar (shared source of truth).
   Avoid logic duplication.
Keep keyboard accessibility, the dark theme, and all the rest of the behavior
(photo extraction, previews in the main view, wheel, stabilization) intact.

## Rename "Osmo 360 Studio" → "PanoForge"

- **Visible strings** (HTML title `<title>`, UI header "Osmo 360 Studio",
  README, `run.sh`/`lancer.sh` messages, `.desktop` Name/Comment) → "PanoForge".
- **Project folder name**: leave `osmo360-studio/` as-is to not break the
  paths of this session (the Git repository may be named PanoForge on push; out of
  code scope).
- **User config/cache**: move from `~/.config/osmo360-studio` and
  `~/.cache/osmo360-studio` to `~/.config/panoforge` and `~/.cache/panoforge`, WITH
  soft migration at startup: if the old folder exists and the new one does not,
  move it (or copy config.json). Do not lose the user's current config
  (source_dir/output_dir already customized).
- **Default output folder**: for a new install → `<Vidéos>/PanoForge`;
  but do NOT move already-produced files nor overwrite an `output_dir` already
  saved in the config (the user keeps `~/Vidéos/osmo360` if they already have it).
- **DJI compatibility**: the mentions ".OSV / DJI Osmo 360" remain allowed in
  descriptive texts (README, subtitle), never as a product name.
- Update the tests that reference the old name (config/cache paths)
  accordingly; `.venv/bin/pytest` must stay green.

## Environment constraints

- Python 3.14, PEP 668 → `run.sh` creates/activates a local venv `.venv` and installs requirements.
- Minimal dependencies: fastapi, uvicorn, (numpy for maps.py). No gpxpy (stdlib xml).
- ffmpeg 8.0 system. Sample file: an `.OSV` under
  `<removable volume>/DCIM/CAM_001/` (test paths overridable via
  `PANOFORGE_TEST_DCIM` / `PANOFORGE_TEST_SAMPLES`).
- Phase 2 (out of initial scope, do not block on it): quaternion stabilization
  (v360 sendcmd yaw/pitch/roll per frame) — provide the `stabilize: bool` field in the
  options but leave it inactive/grayed.
