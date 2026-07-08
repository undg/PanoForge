"""Configuration persistante de PanoForge (ex-« Osmo 360 Studio »).

Un seul utilisateur, une seule config, persistée en JSON dans
``~/.config/panoforge/config.json``. Valeurs par défaut :
  - source_dir : carte SD DJI Osmo 360 (DCIM)
  - output_dir : ``xdg-user-dir VIDEOS``/PanoForge (soit ~/Vidéos/PanoForge sur
    un système en français ; repli ~/Vidéos puis ~/Videos puis ~), créé au besoin

Migration douce du renommage (2026-07-08) : si l'ancien dossier
``~/.config/osmo360-studio`` (resp. ``~/.cache/osmo360-studio``) existe et que le
nouveau dossier ``~/.config/panoforge`` (resp. ``~/.cache/panoforge``) n'existe pas
encore, l'ancien est déplacé vers le nouveau au premier démarrage — la config
personnalisée (source_dir/output_dir) de l'utilisateur est ainsi conservée telle
quelle, aucun fichier de sortie déjà produit n'est déplacé.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

VERSION = "0.1.0"

def _default_source_dir() -> str:
    """Dossier source par défaut : premier volume amovible monté (carte SD, clé USB…),
    en préférant son sous-dossier ``DCIM`` s'il existe ; sinon le dossier personnel.
    Auto-détecté pour ne coder en dur ni utilisateur ni chemin particulier."""
    user = os.environ.get("USER") or os.environ.get("USERNAME") or Path.home().name
    for base in (f"/run/media/{user}", f"/media/{user}"):
        try:
            subs = sorted(p for p in Path(base).iterdir() if p.is_dir())
        except OSError:
            continue
        for vol in subs:
            if (vol / "DCIM").is_dir():
                return str(vol / "DCIM")
        if subs:
            return str(subs[0])
    return str(Path.home())


DEFAULT_SOURCE_DIR = _default_source_dir()

# Ancien défaut (versions précédentes) : migré vers le nouveau au chargement.
LEGACY_OUTPUT_DIR = str(Path.home() / "Videos" / "osmo360")


def _xdg_videos_dir() -> str:
    """Dossier Vidéos utilisateur : `xdg-user-dir VIDEOS`, replis ~/Vidéos, ~/Videos, ~."""
    try:
        proc = subprocess.run(
            ["xdg-user-dir", "VIDEOS"], capture_output=True, text=True, timeout=5,
        )
        candidate = proc.stdout.strip()
        if proc.returncode == 0 and candidate and os.path.isdir(candidate) \
                and candidate != str(Path.home()):
            return candidate
    except (OSError, subprocess.TimeoutExpired):
        pass
    for name in ("Vidéos", "Videos"):
        p = Path.home() / name
        if p.is_dir():
            return str(p)
    return str(Path.home())


DEFAULT_OUTPUT_DIR = str(Path(_xdg_videos_dir()) / "PanoForge")

CONFIG_DIR = Path.home() / ".config" / "panoforge"
CONFIG_PATH = CONFIG_DIR / "config.json"

CACHE_DIR = Path.home() / ".cache" / "panoforge"

# Anciens emplacements (nom de produit précédent) : migrés au démarrage si
# présents et que le nouvel emplacement n'existe pas encore (voir _migrate_legacy_dirs).
OLD_CONFIG_DIR = Path.home() / ".config" / "osmo360-studio"
OLD_CACHE_DIR = Path.home() / ".cache" / "osmo360-studio"


def _migrate_legacy_dirs() -> None:
    """Déplace ~/.config|.cache/osmo360-studio vers .../panoforge si besoin.

    Idempotent (basé sur des tests d'existence) : ne fait rien si l'ancien
    dossier est absent ou si le nouveau existe déjà (jamais d'écrasement).
    """
    for old_dir, new_dir in ((OLD_CONFIG_DIR, CONFIG_DIR), (OLD_CACHE_DIR, CACHE_DIR)):
        try:
            if old_dir.exists() and not new_dir.exists():
                new_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_dir), str(new_dir))
        except OSError:
            pass


@dataclass
class AppConfig:
    source_dir: str = DEFAULT_SOURCE_DIR
    output_dir: str = DEFAULT_OUTPUT_DIR

    def to_dict(self) -> dict:
        return asdict(self)


_lock = threading.Lock()
_config: AppConfig | None = None
_nvenc_cache: bool | None = None


def _load() -> AppConfig:
    global _config
    if _config is not None:
        return _config
    _migrate_legacy_dirs()
    cfg = AppConfig()
    migrated = False
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.source_dir = data.get("source_dir", cfg.source_dir)
            cfg.output_dir = data.get("output_dir", cfg.output_dir)
            # Migration : l'ancien défaut exact (~/Videos/osmo360) devient le
            # nouveau défaut basé sur xdg-user-dir VIDEOS (~/Vidéos/osmo360).
            if cfg.output_dir == LEGACY_OUTPUT_DIR and LEGACY_OUTPUT_DIR != DEFAULT_OUTPUT_DIR:
                cfg.output_dir = DEFAULT_OUTPUT_DIR
                migrated = True
    except (OSError, json.JSONDecodeError):
        pass
    _config = cfg
    _ensure_output_dir(cfg.output_dir)
    if migrated:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return cfg


def _ensure_output_dir(path: str) -> None:
    try:
        os.makedirs(os.path.expanduser(path), exist_ok=True)
    except OSError:
        pass


def get_config() -> AppConfig:
    with _lock:
        return _load()


def update_config(source_dir: str | None = None, output_dir: str | None = None) -> AppConfig:
    with _lock:
        cfg = _load()
        if source_dir is not None:
            cfg.source_dir = source_dir
        if output_dir is not None:
            cfg.output_dir = output_dir
            _ensure_output_dir(cfg.output_dir)
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return cfg


def has_nvenc() -> bool:
    """Détecte si ffmpeg dispose des encodeurs NVENC (GPU NVIDIA)."""
    global _nvenc_cache
    if _nvenc_cache is not None:
        return _nvenc_cache
    result = False
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10,
            )
            result = "hevc_nvenc" in proc.stdout or "h264_nvenc" in proc.stdout
        except (OSError, subprocess.TimeoutExpired):
            result = False
    _nvenc_cache = result
    return result


def cache_dir() -> Path:
    _migrate_legacy_dirs()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


# ---------------------------------------------------------------------------
# Détection live des volumes amovibles / caméra (voir SPEC.md — navigation)
# ---------------------------------------------------------------------------

def gvfs_root() -> str:
    """Racine GVfs de la session courante : /run/user/<uid>/gvfs."""
    return f"/run/user/{os.getuid()}/gvfs"


def removable_volumes() -> list[dict]:
    """Sous-dossiers montés de /run/media/$USER et /media/$USER.

    Renvoie ``[{"label": nom_du_volume, "path": chemin_reel}]``, dédoublonné par
    chemin réel. Best-effort : dossiers inexistants ou illisibles sont ignorés.
    """
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    volumes: list[dict] = []
    seen: set[str] = set()
    for base in (f"/run/media/{user}", f"/media/{user}"):
        try:
            if not user or not os.path.isdir(base):
                continue
            entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=True):
                    continue
                real = os.path.realpath(entry.path)
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            volumes.append({"label": entry.name, "path": real})
    return volumes


def camera_mounts() -> list[dict]:
    """Montages MTP/PTP (gvfs) sous /run/user/<uid>/gvfs, best-effort.

    Ne remonte que les entrées dont le nom commence par ``mtp:`` ou
    ``gphoto2:`` (autres protocoles gvfs ignorés). Absence ou dossier vide
    tolérés silencieusement.
    """
    mounts: list[dict] = []
    base = gvfs_root()
    try:
        if not os.path.isdir(base):
            return mounts
        entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
    except OSError:
        return mounts
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=True):
                continue
        except OSError:
            continue
        if entry.name.startswith("mtp:") or entry.name.startswith("gphoto2:"):
            mounts.append({"label": entry.name, "path": entry.path})
    return mounts


def config_summary() -> dict:
    cfg = get_config()
    return {
        "source_dir": cfg.source_dir,
        "output_dir": cfg.output_dir,
        "has_nvenc": has_nvenc(),
        "version": VERSION,
    }
