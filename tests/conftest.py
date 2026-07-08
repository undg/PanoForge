"""Fixtures partagées : isole app.config du fichier de config réel de l'utilisateur."""
import glob
import os

import pytest

# Racine des enregistrements caméra pour les tests d'intégration locaux.
# Auto-détectée (premier volume amovible), surchargée par PANOFORGE_TEST_DCIM.
# Les tests concernés se marquent skip si le chemin est absent.
from app import config as _config  # noqa: E402

SD_DCIM_ROOT = os.environ.get("PANOFORGE_TEST_DCIM") or _config.DEFAULT_SOURCE_DIR

# Premier .OSV de la carte SD (découverte dynamique : l'utilisateur ré-enregistre
# régulièrement, les noms de fichiers changent).
_osvs = sorted(glob.glob(os.path.join(SD_DCIM_ROOT, "*", "*.OSV")))
EXAMPLE_OSV = _osvs[0] if _osvs else os.path.join(SD_DCIM_ROOT, "CAM_001", "aucun.OSV")

requires_example_file = pytest.mark.skipif(
    not os.path.isfile(EXAMPLE_OSV),
    reason="fichier d'exemple .OSV absent (carte SD non montée)",
)


@pytest.fixture(autouse=True)
def _neutralize_legacy_migration(tmp_path_factory, monkeypatch):
    """Sécurité globale : par défaut, la migration douce du renommage
    (osmo360-studio → panoforge) ne doit JAMAIS toucher les vrais ~/.config et
    ~/.cache de l'utilisateur pendant les tests. On fait pointer les anciens
    dossiers vers des chemins temporaires inexistants pour tous les tests ; ceux
    qui testent spécifiquement la migration surchargent ces attributs eux-mêmes."""
    from app import config as config_module

    base = tmp_path_factory.mktemp("no-legacy")
    monkeypatch.setattr(config_module, "OLD_CONFIG_DIR", base / "legacy-config-absent")
    monkeypatch.setattr(config_module, "OLD_CACHE_DIR", base / "legacy-cache-absent")


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirige la config (et son cache) vers des dossiers temporaires pour les tests
    qui n'ont pas besoin du vrai fichier .OSV (source_dir/output_dir en tmp_path)."""
    from app import config as config_module

    src = tmp_path / "source"
    out = tmp_path / "output"
    src.mkdir()
    out.mkdir()
    cache = tmp_path / "cache"

    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CACHE_DIR", cache)
    # Neutralise la migration douce du renommage (osmo360-studio → panoforge) : les
    # anciens dossiers pointent vers des chemins temporaires inexistants pour que
    # _migrate_legacy_dirs() ne touche jamais aux vrais ~/.config|.cache réels.
    monkeypatch.setattr(config_module, "OLD_CONFIG_DIR", tmp_path / "legacy-config-absent")
    monkeypatch.setattr(config_module, "OLD_CACHE_DIR", tmp_path / "legacy-cache-absent")
    config_module.update_config(source_dir=str(src), output_dir=str(out))
    return {"source_dir": src, "output_dir": out, "config_module": config_module}


@pytest.fixture
def isolated_config_real_source(tmp_path, monkeypatch):
    """Comme isolated_config mais source_dir = le vrai dossier DCIM (pour tester
    /api/files, /api/probe, /api/thumb, /api/jobs sur le fichier d'exemple réel)."""
    from app import config as config_module

    out = tmp_path / "output"
    out.mkdir()
    cache = tmp_path / "cache"

    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CACHE_DIR", cache)
    monkeypatch.setattr(config_module, "OLD_CONFIG_DIR", tmp_path / "legacy-config-absent")
    monkeypatch.setattr(config_module, "OLD_CACHE_DIR", tmp_path / "legacy-cache-absent")
    config_module.update_config(source_dir=SD_DCIM_ROOT, output_dir=str(out))
    return {"source_dir": SD_DCIM_ROOT, "output_dir": out, "config_module": config_module}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)
