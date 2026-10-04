# PanoForge

> Independent project, not affiliated with DJI. "PanoForge" is the product name; the
> references to ".OSV" and "DJI Osmo 360" below designate only the file format and the
> supported source camera, not an association or partnership with DJI.

**Local** web application (single-user, no authentication, the server listens only on
`127.0.0.1`) that converts `.OSV` files from the DJI Osmo 360 into equirectangular 360°
MP4, with injection of spherical metadata (Google Spherical V1+V2), optional GPS from an
external GPX file (CAMM track), a batch conversion queue, and an interactive 360°
preview in the browser (three.js).

The `.OSV` format (two 10-bit HEVC fisheye streams + proprietary metadata tracks) has no
official conversion tool on Linux; PanoForge stitches the two lenses into an
equirectangular image using the **factory calibration embedded in each file**.

## Features

- **360° stitching** of the two fisheyes into equirectangular, with seam blending
  based on the optical calibration read from the file (`calibrated` mode), or the
  geometric `v360` method as a fallback.
- **Gyroscopic stabilization** (*horizon*, *locked*, *smooth* modes) from the camera's
  ~1 kHz IMU quaternions — levels the horizon and reduces shake.
- **Google Street View compatible**: 2:1 equirect, spherical metadata, GPS from an
  external GPX file (aligned by timestamp + manual offset) injected as a CAMM track.
- **Batch processing** with a queue, progress, ETA, cancel.
- **Interactive 360° preview** in the browser (automatic H.264 proxy for HEVC playback).
- **Photo extraction** from an OSV/MP4/360° JPEG: perspective (preset or free ratios),
  cylindrical panorama, GPano spherical photo, "little planet".
- **Optional GPU acceleration** (NVENC detected, automatic CPU fallback).

## Requirements

- **Linux** (developed and tested on Ubuntu/GNOME), Python 3.11+.
- `ffmpeg` / `ffprobe` (8.0+, with the `v360`/`remap`/`sendcmd` filters) in the `PATH`.
- For GPU acceleration (NVENC encoding): NVIDIA drivers + `hevc_nvenc`/`h264_nvenc`
  visible in `ffmpeg -encoders` (detected automatically, otherwise CPU fallback).
  Only NVENC is detected; VAAPI/QSV are not implemented.
- `exiftool` (optional) to verify the injected GPano/CAMM metadata.

## Platforms

The core (Python + ffmpeg + browser UI) is inherently cross-platform.
The current version targets **Linux**: some system-specific branches are Linux-only —
`run.sh`/`lancer.sh` launchers (bash), a `.desktop` shortcut (GNOME), detection of
removable volumes (`/run/media`, `/media`, gvfs), and the `~/.config` / `~/.cache` /
`xdg-user-dir` folders. A macOS/Windows port does not require rewriting the engine, only
adapting those points (paths, launchers, drive detection).

## Installation and launch

```bash
./run.sh
```

The script:
1. creates the `.venv` virtualenv if it does not exist yet;
2. installs the dependencies from `requirements.txt`;
3. starts the FastAPI/uvicorn server on `http://127.0.0.1:8360`;
4. automatically opens the default browser (`xdg-open`).

On subsequent launches, `run.sh` **skips the dependency installation** if they are
already present (near-instant startup). To force a reinstall/update:
`PANOFORGE_FORCE_INSTALL=1 ./run.sh`.

Two complementary launchers:
- `./lancer.sh`: simply opens the browser if the app is already running, otherwise starts
  it via `run.sh` — handy for a desktop shortcut.
- A GNOME shortcut (`~/.local/share/applications/panoforge.desktop`) can point to
  `lancer.sh` to launch PanoForge from the applications menu.

To launch manually (venv already prepared):

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8360
```

## Configuration

On first launch, the default folders are:
- **Source** (SD card / camera): **auto-detected** — the first removable volume mounted
  under `/run/media/<user>` or `/media/<user>` (preferring its `DCIM` subfolder),
  otherwise the home folder.
- **Output** (converted videos): the user's Videos folder detected via
  `xdg-user-dir VIDEOS` + `/PanoForge` — i.e. `~/Vidéos/PanoForge` on a French system
  (fallbacks: `~/Vidéos`, `~/Videos`, then `~`); created automatically.
  An existing config still pointing to an old default (`~/Videos/osmo360`)
  is migrated automatically on startup; an output folder already customized
  by the user (including `~/Videos/osmo360`) is never moved or overwritten.

These paths can be changed from the UI (configuration panel) or via
`POST /api/config`. They are persisted in `~/.config/panoforge/config.json`.

The cache (extracted thumbnails and preview proxies) is stored in
`~/.cache/panoforge/`.

> **Rename**: the product used to be called "Osmo 360 Studio". If you upgrade from an
> old version, the configuration and cache are moved automatically from
> `~/.config/osmo360-studio` and `~/.cache/osmo360-studio`
> to `~/.config/panoforge` and `~/.cache/panoforge` on first launch (nothing is
> lost; the operation only runs if the old folder exists and the new one does not).

## Usage

1. **Files** (single entry point): the toolbar at the top brings together the source
   folder, quick-access shortcuts (Home, removable volumes, camera) and the
   **"Open a file…"** button (OSV/MP4/JPEG). The grid shows the `.OSV` files of the
   source folder with their thumbnail; each card offers two actions: **Convert**
   and **Open in 360°**. Multiple selection is possible for batch conversion.
2. Set the conversion options: output resolution (7680/6144/3840),
   codec (HEVC/H.264), encoder (auto/NVENC/CPU), quality, interpolation, stitching mode
   (`v360` baseline or `calibrated` from the factory calibration),
   **stabilization** (horizon/locked/smooth + strength; automatically disabled in the
   Street View profile), **Street View** profile (5 fps, mandatory CAMM), and
   optionally a GPX file with a time-offset slider (offset).
3. Start the conversion: one job is created per file and processed by the queue
   (only one `ffmpeg` active at a time).
4. **Queue**: track progress (0-100%, fps, ETA), cancel a running or queued job, open
   the output folder once finished.
5. **360° preview**: view the embedded thumbnail or the converted result on an
   interactive three.js sphere (drag to rotate, wheel to zoom).

## REST API

See `SPEC.md` for the full contract. Summary:

| Method | Route              | Description                                             |
|--------|---------------------|----------------------------------------------------------|
| GET     | `/api/config`       | Current configuration (folders, NVENC, version)          |
| POST    | `/api/config`       | Update the source/output folders                         |
| GET     | `/api/files`        | List `.OSV` files (recursive, 1 level)                    |
| GET     | `/api/thumb`        | Embedded JPEG thumbnail (on-disk cache)                   |
| POST    | `/api/probe`        | Technical info + calibration available or not             |
| GET     | `/api/browse`       | Folder/file navigation for the UI (`dir`, `filter`) — restricted to `$HOME`, `/run/media`, `/media`, gvfs, and the configured source/output folders |
| GET     | `/api/browse/roots` | Quick-access shortcuts (Home, removable volumes, camera, source/output) |
| POST    | `/api/gpx/analyze`  | Analyze GPX coverage vs. video                            |
| POST    | `/api/jobs`         | Create one conversion job per file                        |
| GET     | `/api/jobs`         | List jobs (status, progress, fps, ETA)                    |
| DELETE  | `/api/jobs/{id}`    | Cancel a job (kills the ffmpeg process if running)        |
| POST    | `/api/photo/extract` | **Synchronous** photo extraction (flat/cylindrical/equirect360/littleplanet) |
| GET     | `/api/photo/navproxy` | Lightweight equirect proxy to navigate a `.OSV` (on-disk cache) |
| GET     | `/api/media`        | Serve a video file with **Range** support (browser playback) |
| GET     | `/`                 | Static frontend                                           |

## Conversion job pipeline

1. `probe` (ffprobe) + `extract_metadata` (calibration + IMU from the `djmd` track).
2. Generation of remap maps if `calibrated` mode (and calibration available).
3. `ffmpeg` execution (stitching), progress tracked via `-progress pipe:1`.
4. Injection of spherical metadata (V1 XML + V2 `sv3d`).
5. If a GPX is provided: resampling + injection of the CAMM track (+ sidecar windowed
   GPX export).
6. Atomic move to the output folder: `<name>_360.mp4`.
7. Generation of a H.264 8-bit 1920×960 **preview proxy** (`yuv420p`, `faststart`)
   in `~/.cache/panoforge/previews/` — since the 10-bit HEVC output is not
   decodable by Chrome/Linux, this is the proxy that the browser's 360° preview reads.
   Non-blocking step: if it fails, the job stays `done` and the `preview_error` field
   explains the problem; otherwise `preview_url` (served by
   `/api/media`) is set in `GET /api/jobs`. Job progress covers
   stitching from 0 → 0.95 then the proxy from 0.95 → 1.0.

The modules `app/core/{maps,stitch,gpx,camm,spherical}.py` each implement one
step of this pipeline; if absent or on error, the job in question moves to the `error`
state with an explicit message (no server crash).

## 360° photo extraction

`POST /api/photo/extract` (synchronous, ~8 s warm / ~14 s on the first call for an
8K OSV) extracts a JPEG photo (quality 95) to `<output>/photos/` from:
- a **converted 360° MP4** (precise seek to the chosen instant);
- a **raw `.OSV`**: calibrated stitching of a single full-resolution frame
  (7680×3840) reusing the factory calibration maps — cached by
  (file, resolution) in `~/.cache/panoforge/maps/`;
- a camera **360° JPEG** (2:1 equirect, used as-is;
  a JPEG with a different ratio is rejected with an explicit error).

Four projections (ffmpeg `v360`):
- `flat`: classic perspective (yaw/pitch/roll, horizontal FOV 30–140°, preset ratios
  16:9, 21:9, 32:9, 4:3, 1:1, 9:16 or a **free ratio** "a:b" with a and
  b numeric > 0, decimals accepted (e.g. `2.35:1`), a/b bounded to [0.2, 8]) — the
  vertical FOV is computed from the ratio for a perspective without stretching;
- `cylindrical`: full 360° turn panorama, adjustable vertical band (default 60°);
- `equirect360`: full 2:1 equirect with **XMP GPano** injected (interactive spherical
  photo recognized by Google Photos/Facebook, verifiable with `exiftool`);
- `littleplanet`: stereographic look-down view ("little planet").

`GET /api/photo/navproxy?path=` provides a 688×344 H.264 equirect proxy (generated and
cached) to choose the instant in an `.OSV` that the browser cannot read. Note: the
low-resolution `.LRF` file written by the camera next to each `.OSV` could speed up
this proxy in the future, but it could not be tested.

## Tests

```bash
.venv/bin/pytest
```

The tests requiring the real sample file (`tests/conftest.py::requires_example_file`,
`tests/test_photo.py::requires_sample`) are skipped automatically if the SD card is not
mounted.

## Structure

```
app/
  main.py     # FastAPI app + uvicorn entry
  api.py      # REST routes
  jobs.py     # queue + ffmpeg execution + progress
  config.py   # persisted configuration
  core/
    osv.py       # probe + djmd metadata extraction wrapper
    osv_meta/    # low-level extraction (protobuf djmd), shipped
    maps.py stitch.py gpx.py camm.py spherical.py  # stitching/GPS pipeline
  static/     # frontend (vanilla JS + vendored three.js in static/vendor/)
tests/        # pytest (API + core)
tools/        # notes on the .OSV `djmd` track format
```

## Known limits

- Stabilization: the *horizon* mode at maximum strength can produce a transient "little
  planet" rendering when the camera points at the sky/ground; the *smooth* mode is more
  natural for video. Stabilization multiplies conversion time by roughly 4
  (the `v360` filter rebuilds its projection on every frame).
- GPS/spherical injection loads the file into memory: to be revisited with streaming
  for multi-GB 8K MP4s (long videos).
- Browser playback: since 10-bit HEVC is not decodable by Chrome/Linux, the preview
  goes through an H.264 proxy.

## License

Distributed under the **MIT** license — see [`LICENSE`](LICENSE).

PanoForge is an independent project. "DJI" and "Osmo" are trademarks of their respective
owners; they are used here only to describe compatibility with the `.OSV` file format and
the source camera, with no affiliation or endorsement.
