# AGENTS.md — guide for working on PanoForge

Condensed guide for a Code agent working on this repository. See `SPEC.md` for
the detailed contract (modules, API, binary formats); this file summarizes the essentials and
above all the **pitfalls** discovered empirically.

## What

**Local** web app (FastAPI + vanilla JS/three.js frontend, **English** interface)
that converts `.OSV` files from the DJI Osmo 360 to equirectangular 360° MP4, with
stabilization, spherical metadata, GPS from GPX (CAMM), and photo extraction.
Server on `127.0.0.1:8360`, single-user, no authentication.

> Naming: the product is called **PanoForge**. "DJI"/"Osmo" must appear
> only as compatibility mentions (`.OSV` format, source camera), never in the
> product, repository, or logo name. Independent project, not affiliated with DJI.

## Run / test

```bash
./run.sh                      # venv + deps (conditional) + uvicorn + browser
.venv/bin/pytest              # full suite (~106 tests)
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8360   # manual
```

- `run.sh` skips the install if `import fastapi, uvicorn, numpy` succeeds; force with
  `PANOFORGE_FORCE_INSTALL=1`.
- Some integration tests require a real `.OSV`/`.JPG`; they **skip** if absent
  (overridable via `PANOFORGE_TEST_DCIM` / `PANOFORGE_TEST_SAMPLES`).
- **Do not** start a 2nd server on 8360 if one is already running; use another
  port for browser tests.

## Architecture

```
app/main.py      # FastAPI app + static mount
app/api.py       # REST routes (see SPEC.md)
app/jobs.py      # FIFO queue, 1 ffmpeg at a time, -progress parsing
app/config.py    # persisted config ~/.config/panoforge, cache ~/.cache/panoforge
app/core/
  osv.py         # ffprobe probe + metadata extraction wrapper
  osv_meta/      # low-level djmd track extraction (protobuf) — DO NOT rewrite
  maps.py        # calibration -> ffmpeg remap maps + blending mask
  stitch.py      # ffmpeg command building (v360 / calibrated modes)
  stabilize.py   # per-frame orientation corrections (IMU quaternions -> sendcmd)
  gpx.py camm.py spherical.py   # GPS/GPX -> CAMM, V1+V2 spherical metadata
  photo.py       # photo extraction (4 projections) + navproxy
app/static/      # frontend: index.html, js/{app,viewer,filebrowser,api}.js, style.css
```

The `core` modules have **contractual signatures** (SPEC.md); `jobs.py` imports them
lazily and turns any error into an explicit `error` job, without crashing.

## Established facts (do not re-verify)

- `.OSV` = MP4: 2 fisheye HEVC 10-bit 3840×3840 (>180°), AAC audio, `djmd` tracks
  (DJI protobuf), and an equirect MJPEG thumbnail (stitching reference).
- The **factory optical calibration** (fx/fy, cx/cy, distortion, extrinsic quaternion,
  stitching LUT) is embedded in the 1st `djmd` sample of each file.
- The camera has **no GPS**: the external GPX is the only geo source.
- IMU: ~1 kHz orientation quaternions (no raw gyro) — enough to stabilize.

## Pitfalls to know (hard-won)

1. **v360 + sendcmd compose, they do not replace.** The `yaw/pitch/roll`
   commands sent to a `v360` filter are **post-multiplied** with the current
   orientation. For per-frame stabilization you must emit **deltas**
   (`C_i = T_{i-1}ᵀ·T_i`) and initialize v360 to neutral, otherwise growing
   cumulative drift. See `stabilize.py`.
2. **Viewer ↔ v360 angle conventions.** The three.js sphere samples
   `u = yaw/360` (yaw=0 → left edge); `v360 output=flat` aims at the center. Mapping
   applied in `photo.py`: flat `yaw_v360 = yaw+180`, roll inverted, pitch identical;
   cylindrical yaw unchanged; littleplanet rotation+90, +hflip, fixed `h_fov=250`. The
   WebGL frontend is the reference, the backend aligns to it.
3. **HEVC 10-bit not readable by Chrome/Linux.** Each job and the OSV extraction generate
   an **H.264 proxy** (`~/.cache/panoforge/previews/`) that the 360° preview reads.
4. **djmd quaternion = order `[w,x,y,z]`, body→world frame, world vertical = −Z.**
   Validated empirically; convention documented at the top of `stabilize.py`.
5. **Calibrated stitching**: the embedded LUTs describe the stitching circle (θ=90°) and
   serve to realign the optical scale — better seams than the polynomial alone.
6. Config/cache were renamed from `osmo360-studio` → `panoforge` with **soft migration**
   at startup (`config._migrate_legacy_dirs`); do not break this migration.

## Conventions

- Interface and user messages in **English**. No CDN dependency at runtime
  (three.js is vendored in `app/static/js/`).
- Add/update a pytest test for any behavior change; keep the
  suite green. Visually verify UI changes in a browser before
  concluding (do not trust the code alone).
- `work/` (gitignored) contains work artifacts and personal images: do not
  commit it.
