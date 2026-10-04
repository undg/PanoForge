"""Conversion queue .OSV -> 360° MP4.

Single worker thread, FIFO queue, one active ffmpeg process at a time.
Pipeline (see SPEC.md "Convert job pipeline"):
  1. probe + extract_metadata (job's temporary workdir)
  2. generate_remap_maps if calibrated mode (and calibration available)
  3. ffmpeg stitch -> temporary MP4, progress read from -progress pipe:1
  4. inject_spherical (always)
  5. if GPX provided: resample + inject_camm (+ export_windowed_gpx as side-car)
  6. atomic move to the output folder: <name>_360.mp4

The app/core/{maps,stitch,gpx,camm,spherical}.py modules are developed by
other agents in parallel: all imports of these modules are therefore lazy
(done inside the functions) and any absence/error is converted into a clean
job failure (status="error", explicit message), never a server crash.
"""
from __future__ import annotations

import hashlib
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote


class JobError(Exception):
    """Pipeline error meant to be displayed as-is in job.error."""


class _JobCancelled(Exception):
    """Internal signal: the job was cancelled during execution."""


PUBLIC_FIELDS = ("id", "input", "output", "status", "progress", "fps", "eta_s", "error",
                 "preview_url", "preview_error")

# The stitch (and intermediate steps) occupy [0, 0.95] of the progress;
# H.264 preview proxy generation occupies [0.95, 1.0].
STITCH_PROGRESS_SPAN = 0.95


@dataclass
class Job:
    id: str
    input: str
    output: str
    options: dict
    status: str = "queued"          # queued|running|done|error|cancelled
    progress: float = 0.0
    fps: Optional[float] = None
    eta_s: Optional[float] = None
    error: Optional[str] = None
    preview_url: Optional[str] = None      # H.264 proxy readable by browser (null while absent)
    preview_error: Optional[str] = None    # non-blocking failure of proxy generation
    duration_s: float = 0.0
    workdir: Optional[str] = None
    process: Optional[subprocess.Popen] = None
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)

    def to_public(self) -> dict:
        return {k: getattr(self, k) for k in PUBLIC_FIELDS}


def _parse_speed(v: str) -> Optional[float]:
    v = v.strip()
    if v.endswith("x"):
        v = v[:-1]
    try:
        return float(v)
    except ValueError:
        return None


def _parse_out_time_s(block: dict) -> Optional[float]:
    if "out_time_us" in block:
        try:
            return max(0.0, int(block["out_time_us"]) / 1_000_000.0)
        except ValueError:
            pass
    if "out_time_ms" in block:
        try:
            return max(0.0, int(block["out_time_ms"]) / 1_000_000.0)
        except ValueError:
            pass
    ot = block.get("out_time") or block.get("out_time_str")
    if ot and ot != "N/A":
        try:
            h, m, s = ot.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        except ValueError:
            return None
    return None


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    # -- Public API -------------------------------------------------------

    def submit(self, input_path: str, output_path: str, options: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], input=input_path, output=output_path, options=dict(options))
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._queue.put(job.id)
        return job

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [self._jobs[jid].to_public() for jid in self._order if jid in self._jobs]

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status == "queued":
                job.status = "cancelled"
                return True
            if job.status == "running":
                job.cancel_requested = True
                proc = job.process
            else:
                return False
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        return True

    # -- worker -------------------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                continue
            with self._lock:
                if job.status == "cancelled":
                    continue
            self._run_job(job)

    def _set(self, job: Job, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(job, k, v)

    def _run_job(self, job: Job) -> None:
        self._set(job, status="running", progress=0.0)
        workdir = tempfile.mkdtemp(prefix=f"osmojob_{job.id}_")
        job.workdir = workdir
        try:
            from app.core import osv

            try:
                info = osv.probe(job.input)
            except osv.OsvError as exc:
                raise JobError(f"probe: {exc}") from exc
            job.duration_s = info.duration_s or 0.0

            try:
                meta = osv.extract_metadata(job.input, workdir)
            except osv.OsvError as exc:
                raise JobError(f"metadata extraction: {exc}") from exc
            calibration = meta.get("calibration")

            opts = job.options
            if opts.get("streetview"):
                opts.setdefault("fps_out", 5.0)
                opts["fps_out"] = 5.0
                opts.setdefault("codec", "hevc")
                opts.setdefault("quality", 20)
                opts["embed_camm"] = True
                # Google requirement: Street View REQUIRES stabilization disabled.
                opts["stabilize"] = False

            try:
                from app.core.stitch import StitchOptions, build_command
            except ImportError as exc:
                raise JobError(f"module core/stitch.py unavailable: {exc}") from exc

            stitch_kwargs = {}
            for key in ("out_w", "codec", "encoder", "quality", "interp", "mode",
                        "fps_out", "stabilize", "stabilize_mode", "stabilize_strength"):
                if key in opts and opts[key] is not None:
                    stitch_kwargs[key] = opts[key]
            stitch_opts = StitchOptions(**stitch_kwargs)

            maps = None
            wants_calibrated = stitch_opts.mode in ("calibrated", "auto") and calibration is not None
            if wants_calibrated:
                try:
                    from app.core.maps import generate_remap_maps
                    out_h = stitch_opts.out_w // 2
                    maps = generate_remap_maps(calibration, stitch_opts.out_w, out_h, workdir)
                except ImportError as exc:
                    if stitch_opts.mode == "calibrated":
                        raise JobError(f"calibrated mode requested but module core/maps.py unavailable: {exc}") from exc
                    maps = None
                except Exception as exc:  # noqa: BLE001 - third-party module under development
                    if stitch_opts.mode == "calibrated":
                        raise JobError(f"failed to generate remap maps: {exc}") from exc
                    maps = None

            # Gyroscopic stabilization: generates the sendcmd command file
            # (per-frame yaw/pitch/roll rotation) when the option is active and
            # the IMU is present. No IMU -> no stabilization (silent).
            stabilize_cmd = None
            if stitch_opts.stabilize:
                try:
                    from app.core import stabilize as stab
                    from app.core.stitch import _resolve_mode
                    imu_csv = meta.get("imu_highrate") or meta.get("imu_perframe")
                    eff_fps = float(stitch_opts.fps_out or info.fps or 25.0)
                    n_frames = max(1, int(round((info.duration_s or 0.0) * eff_fps)))
                    # v360 mode: fold the yaw=90 baseline alignment into the angles.
                    resolved = _resolve_mode(stitch_opts, maps)
                    fold = stab.BASELINE_YAW_DEG if resolved == "v360" else None
                    res = stab.frame_corrections(
                        imu_csv, eff_fps, n_frames,
                        mode=stitch_opts.stabilize_mode,
                        strength=stitch_opts.stabilize_strength,
                        fold_baseline_yaw=fold)
                    if res.has_imu and res.corrections:
                        stabilize_cmd = os.path.join(workdir, "stabilize.cmd")
                        stab.build_sendcmd(res.corrections, eff_fps, stabilize_cmd)
                except ImportError as exc:
                    raise JobError(f"stabilization requested but module core/stabilize.py unavailable: {exc}") from exc
                except Exception as exc:  # noqa: BLE001
                    raise JobError(f"stabilization preparation: {exc}") from exc

            try:
                cmd = build_command(job.input, os.path.join(workdir, "stitched.mp4"),
                                    stitch_opts, maps, stabilize_cmd=stabilize_cmd)
            except Exception as exc:  # noqa: BLE001
                raise JobError(f"ffmpeg command construction: {exc}") from exc

            stitched = os.path.join(workdir, "stitched.mp4")
            self._run_ffmpeg(job, cmd, info.duration_s or job.duration_s,
                             progress_base=0.0, progress_span=STITCH_PROGRESS_SPAN)
            if job.cancel_requested:
                raise _JobCancelled()
            if not os.path.isfile(stitched):
                raise JobError("ffmpeg produced no output (silent failure)")

            current = stitched
            try:
                from app.core.spherical import inject_spherical
            except ImportError as exc:
                raise JobError(f"module core/spherical.py unavailable: {exc}") from exc
            sph_out = os.path.join(workdir, "spherical.mp4")
            try:
                inject_spherical(current, sph_out)
            except Exception as exc:  # noqa: BLE001
                raise JobError(f"spherical metadata injection: {exc}") from exc
            current = sph_out if os.path.isfile(sph_out) else current

            gpx_path = opts.get("gpx_path")
            if gpx_path:
                try:
                    from app.core.gpx import parse_gpx, resample
                    from app.core.camm import inject_camm
                except ImportError as exc:
                    raise JobError(f"GPX/CAMM requested but module unavailable: {exc}") from exc
                try:
                    points = parse_gpx(gpx_path)
                    offset_s = float(opts.get("gpx_offset_s") or 0.0)
                    samples = resample(points, info.creation_time_utc, info.duration_s, offset_s)
                    camm_out = os.path.join(workdir, "camm.mp4")
                    inject_camm(current, camm_out, samples, info.creation_time_utc)
                    current = camm_out if os.path.isfile(camm_out) else current
                    try:
                        from app.core.spherical import export_windowed_gpx
                        sidecar = os.path.splitext(job.output)[0] + ".gpx"
                        export_windowed_gpx(points, info.creation_time_utc, info.duration_s, offset_s, sidecar)
                    except ImportError:
                        pass
                except JobError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise JobError(f"GPX/CAMM processing: {exc}") from exc
            elif opts.get("embed_camm"):
                raise JobError("embed_camm requested but no gpx_path provided")

            os.makedirs(os.path.dirname(os.path.abspath(job.output)), exist_ok=True)
            tmp_final = job.output + ".part"
            shutil.move(current, tmp_final)
            os.replace(tmp_final, job.output)

            # Final phase (0.95 -> 1.0): H.264 preview proxy readable by browser.
            # Non-blocking: on failure the job stays "done" (preview_error set).
            self._set(job, progress=STITCH_PROGRESS_SPAN, fps=None, eta_s=None)
            self._generate_preview(job, info.duration_s or job.duration_s)
            self._set(job, status="done", progress=1.0, eta_s=0.0)
        except _JobCancelled:
            self._set(job, status="cancelled")
        except JobError as exc:
            self._set(job, status="error", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - last safety net, never crash the worker
            self._set(job, status="error", error=f"internal error: {exc}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _generate_preview(self, job: Job, duration_s: float) -> None:
        """Generate an 8-bit 1920x960 H.264 proxy readable by browsers
        (10-bit HEVC output is not decodable by Chrome/Linux).
        Stored in ~/.cache/panoforge/previews/<hash>.mp4."""
        try:
            from app import config

            previews_dir = config.cache_dir() / "previews"
            previews_dir.mkdir(parents=True, exist_ok=True)
            try:
                mtime = os.path.getmtime(job.output)
            except OSError:
                mtime = 0
            key = hashlib.sha1(f"{job.output}:{mtime}".encode("utf-8")).hexdigest()
            preview_path = str(previews_dir / f"{key}.mp4")

            if not os.path.isfile(preview_path):
                tmp_preview = preview_path + ".part"
                cmd = [
                    "ffmpeg", "-y", "-v", "error",
                    "-i", job.output,
                    "-vf", "scale=1920:960",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "23", "-preset", "veryfast",
                    "-c:a", "aac",
                    "-movflags", "+faststart",
                    "-progress", "pipe:1", "-nostats",
                    "-f", "mp4",  # the .part suffix is not a format known to ffmpeg
                    tmp_preview,
                ]
                self._run_ffmpeg(job, cmd, duration_s,
                                 progress_base=STITCH_PROGRESS_SPAN,
                                 progress_span=1.0 - STITCH_PROGRESS_SPAN)
                if job.cancel_requested:
                    # The final output already exists: the job stays "done",
                    # only the preview thumbnail is dropped.
                    try:
                        os.remove(tmp_preview)
                    except OSError:
                        pass
                    self._set(job, preview_error="preview generation interrupted")
                    return
                os.replace(tmp_preview, preview_path)

            self._set(job, preview_url=f"/api/media?path={quote(preview_path)}")
        except JobError as exc:
            self._set(job, preview_error=str(exc))
        except Exception as exc:  # noqa: BLE001 - never blocking
            self._set(job, preview_error=f"internal error: {exc}")

    def _run_ffmpeg(self, job: Job, cmd: list[str], duration_s: float,
                    progress_base: float = 0.0, progress_span: float = 1.0) -> None:
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            )
        except OSError as exc:
            raise JobError(f"cannot launch ffmpeg: {exc}") from exc

        self._set(job, process=proc)
        start = time.monotonic()
        block: dict[str, str] = {}
        stderr_tail: list[str] = []

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key == "progress":
                out_time_s = _parse_out_time_s(block)
                fps_val = None
                if "fps" in block:
                    try:
                        fps_val = float(block["fps"])
                    except ValueError:
                        fps_val = None
                speed = _parse_speed(block["speed"]) if "speed" in block else None
                progress = progress_base
                if duration_s > 0 and out_time_s is not None:
                    raw = max(0.0, min(1.0, out_time_s / duration_s))
                    progress = progress_base + raw * progress_span
                eta_s = None
                if out_time_s is not None and duration_s > 0:
                    remaining = max(0.0, duration_s - out_time_s)
                    if speed and speed > 0:
                        eta_s = remaining / speed
                    else:
                        elapsed = time.monotonic() - start
                        if out_time_s > 0:
                            eta_s = elapsed * (remaining / out_time_s)
                self._set(job, progress=progress, fps=fps_val, eta_s=eta_s)
                block = {}
                if job.cancel_requested:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    break
            else:
                block[key] = value

        if proc.stderr is not None:
            try:
                stderr_tail = proc.stderr.readlines()[-40:]
            except (OSError, ValueError):
                stderr_tail = []
        proc.wait(timeout=10)
        self._set(job, process=None)

        if job.cancel_requested:
            return
        if proc.returncode != 0:
            tail = "".join(stderr_tail).strip()[-800:]
            raise JobError(f"ffmpeg failed (code {proc.returncode}): {tail or 'no details'}")


manager = JobManager()
