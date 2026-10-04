"""Shared fixtures: isolate app.config from the user's real config file."""
import glob
import os

import pytest

# Camera recordings root for local integration tests.
# Auto-detected (first removable volume), overridden by PANOFORGE_TEST_DCIM.
# The affected tests are skipped if the path is absent.
from app import config as _config  # noqa: E402

SD_DCIM_ROOT = os.environ.get("PANOFORGE_TEST_DCIM") or _config.DEFAULT_SOURCE_DIR

# First .OSV on the SD card (dynamic discovery: the user re-records
# regularly, file names change).
_osvs = sorted(glob.glob(os.path.join(SD_DCIM_ROOT, "*", "*.OSV")))
EXAMPLE_OSV = _osvs[0] if _osvs else os.path.join(SD_DCIM_ROOT, "CAM_001", "aucun.OSV")

requires_example_file = pytest.mark.skipif(
    not os.path.isfile(EXAMPLE_OSV),
    reason="example .OSV file absent (SD card not mounted)",
)


@pytest.fixture(autouse=True)
def _neutralize_legacy_migration(tmp_path_factory, monkeypatch):
    """Global safety net: by default, the soft migration of the rename
    (osmo360-studio → panoforge) must NEVER touch the user's real ~/.config and
    ~/.cache during tests. The old folders are pointed at nonexistent temporary
    paths for all tests; those that specifically test the migration override
    these attributes themselves."""
    from app import config as config_module

    base = tmp_path_factory.mktemp("no-legacy")
    monkeypatch.setattr(config_module, "OLD_CONFIG_DIR", base / "legacy-config-absent")
    monkeypatch.setattr(config_module, "OLD_CACHE_DIR", base / "legacy-cache-absent")


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect the config (and its cache) to temporary folders for tests
    that do not need the real .OSV file (source_dir/output_dir in tmp_path)."""
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
    # Neutralize the soft migration of the rename (osmo360-studio → panoforge): the
    # old folders point to nonexistent temporary paths so that
    # _migrate_legacy_dirs() never touches the real ~/.config|.cache.
    monkeypatch.setattr(config_module, "OLD_CONFIG_DIR", tmp_path / "legacy-config-absent")
    monkeypatch.setattr(config_module, "OLD_CACHE_DIR", tmp_path / "legacy-cache-absent")
    config_module.update_config(source_dir=str(src), output_dir=str(out))
    return {"source_dir": src, "output_dir": out, "config_module": config_module}


@pytest.fixture
def isolated_config_real_source(tmp_path, monkeypatch):
    """Like isolated_config but source_dir = the real DCIM folder (to test
    /api/files, /api/probe, /api/thumb, /api/jobs on the real example file)."""
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
