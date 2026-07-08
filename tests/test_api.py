"""Tests d'intégration de l'API REST (FastAPI TestClient)."""
import os
import time

from conftest import EXAMPLE_OSV, requires_example_file


def test_get_config_defaults(client, isolated_config):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["source_dir"] == str(isolated_config["source_dir"])
    assert body["output_dir"] == str(isolated_config["output_dir"])
    assert isinstance(body["has_nvenc"], bool)
    assert isinstance(body["version"], str)


def test_post_config_updates_dirs(client, isolated_config, tmp_path):
    new_out = tmp_path / "another_output"
    r = client.post("/api/config", json={"output_dir": str(new_out)})
    assert r.status_code == 200
    assert r.json()["output_dir"] == str(new_out)
    # persiste bien pour un appel suivant
    r2 = client.get("/api/config")
    assert r2.json()["output_dir"] == str(new_out)


def test_files_empty_source_dir(client, isolated_config):
    r = client.get("/api/files")
    assert r.status_code == 200
    assert r.json() == []


def test_files_dir_outside_source_forbidden(client, isolated_config, tmp_path):
    outside = tmp_path.parent
    r = client.get("/api/files", params={"dir": str(outside)})
    assert r.status_code == 403


def test_media_range_and_traversal(client, isolated_config):
    out_dir = isolated_config["output_dir"]
    payload = bytes(range(256)) * 4  # 1024 octets
    f = out_dir / "clip.mp4"
    f.write_bytes(payload)

    # sans Range : fichier complet
    r_full = client.get("/api/media", params={"path": str(f)})
    assert r_full.status_code == 200
    assert r_full.content == payload
    assert r_full.headers["accept-ranges"] == "bytes"

    # avec Range : contenu partiel 206
    r_partial = client.get("/api/media", params={"path": str(f)}, headers={"Range": "bytes=10-19"})
    assert r_partial.status_code == 206
    assert r_partial.content == payload[10:20]
    assert r_partial.headers["content-range"] == f"bytes 10-19/{len(payload)}"

    # suffix range (derniers octets)
    r_suffix = client.get("/api/media", params={"path": str(f)}, headers={"Range": "bytes=-5"})
    assert r_suffix.status_code == 206
    assert r_suffix.content == payload[-5:]

    # traversal hors des dossiers autorisés -> 403
    r_forbidden = client.get("/api/media", params={"path": "/etc/passwd"})
    assert r_forbidden.status_code == 403


def test_config_migrates_legacy_output_dir(tmp_path, monkeypatch):
    """L'ancien défaut exact (~/Videos/osmo360) est migré vers le nouveau défaut xdg."""
    import json

    import pytest
    from app import config as config_module

    if config_module.LEGACY_OUTPUT_DIR == config_module.DEFAULT_OUTPUT_DIR:
        pytest.skip("pas de migration à tester sur ce système")

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "source_dir": "/somewhere/dcim",
        "output_dir": config_module.LEGACY_OUTPUT_DIR,
    }))
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config_module, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config_module, "OLD_CONFIG_DIR", tmp_path / "legacy-config-absent")
    monkeypatch.setattr(config_module, "OLD_CACHE_DIR", tmp_path / "legacy-cache-absent")

    cfg = config_module.get_config()
    assert cfg.output_dir == config_module.DEFAULT_OUTPUT_DIR
    assert cfg.source_dir == "/somewhere/dcim"  # le reste est conservé
    # migration persistée sur disque
    assert json.loads(cfg_path.read_text())["output_dir"] == config_module.DEFAULT_OUTPUT_DIR


def test_config_does_not_migrate_custom_output_dir(tmp_path, monkeypatch):
    import json

    from app import config as config_module

    custom = str(tmp_path / "my_exports")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"output_dir": custom}))
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config_module, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config_module, "OLD_CONFIG_DIR", tmp_path / "legacy-config-absent")
    monkeypatch.setattr(config_module, "OLD_CACHE_DIR", tmp_path / "legacy-cache-absent")

    assert config_module.get_config().output_dir == custom


def test_migrate_legacy_dirs_renames_config_and_cache(tmp_path, monkeypatch):
    """Renommage osmo360-studio → panoforge : si l'ancien dossier existe et que le
    nouveau non, il est déplacé (config préservée) ; jamais d'écrasement sinon."""
    from app import config as config_module

    old_cfg = tmp_path / "old" / "osmo360-studio"
    new_cfg = tmp_path / "new" / "panoforge"
    old_cache = tmp_path / "oldc" / "osmo360-studio"
    new_cache = tmp_path / "newc" / "panoforge"
    old_cfg.mkdir(parents=True)
    (old_cfg / "config.json").write_text('{"source_dir": "/x", "output_dir": "/y"}')
    old_cache.mkdir(parents=True)
    (old_cache / "thumb.jpg").write_bytes(b"jpeg")

    monkeypatch.setattr(config_module, "OLD_CONFIG_DIR", old_cfg)
    monkeypatch.setattr(config_module, "CONFIG_DIR", new_cfg)
    monkeypatch.setattr(config_module, "OLD_CACHE_DIR", old_cache)
    monkeypatch.setattr(config_module, "CACHE_DIR", new_cache)

    config_module._migrate_legacy_dirs()

    assert not old_cfg.exists() and new_cfg.exists()
    assert (new_cfg / "config.json").read_text() == '{"source_dir": "/x", "output_dir": "/y"}'
    assert not old_cache.exists() and (new_cache / "thumb.jpg").read_bytes() == b"jpeg"


def test_migrate_legacy_dirs_never_overwrites_existing_new(tmp_path, monkeypatch):
    """Si le nouveau dossier existe déjà, l'ancien n'est pas déplacé (aucune perte)."""
    from app import config as config_module

    old_cfg = tmp_path / "old" / "osmo360-studio"
    new_cfg = tmp_path / "new" / "panoforge"
    old_cfg.mkdir(parents=True)
    (old_cfg / "config.json").write_text('{"old": true}')
    new_cfg.mkdir(parents=True)
    (new_cfg / "config.json").write_text('{"new": true}')

    monkeypatch.setattr(config_module, "OLD_CONFIG_DIR", old_cfg)
    monkeypatch.setattr(config_module, "CONFIG_DIR", new_cfg)
    monkeypatch.setattr(config_module, "OLD_CACHE_DIR", tmp_path / "legacy-cache-absent")
    monkeypatch.setattr(config_module, "CACHE_DIR", tmp_path / "newc" / "panoforge")

    config_module._migrate_legacy_dirs()

    # les deux subsistent, le nouveau est intact
    assert old_cfg.exists() and (new_cfg / "config.json").read_text() == '{"new": true}'


def test_browse_navigation_filter_and_403(client, tmp_path, monkeypatch):
    # Path.home() lit $HOME : on le redirige vers tmp_path pour le test
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Docs").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / ".hidden_dir").mkdir()
    (tmp_path / "clip_b.osv").write_bytes(b"x" * 10)
    (tmp_path / "CLIP_A.OSV").write_bytes(b"y" * 20)
    (tmp_path / "trace.gpx").write_text("<gpx/>")
    (tmp_path / ".secret.osv").write_bytes(b"z")

    home_real = os.path.realpath(tmp_path)

    # défaut : dir = home, parent = null (racine autorisée la plus haute),
    # files vide sans filter (choix de dossier)
    r = client.get("/api/browse")
    assert r.status_code == 200
    body = r.json()
    assert body["dir"] == home_real
    assert body["parent"] is None
    assert [d["name"] for d in body["dirs"]] == ["Alpha", "Docs"]  # cachés exclus, tri alpha
    assert body["files"] == []

    # filtre osv insensible à la casse ; fichiers cachés exclus
    r2 = client.get("/api/browse", params={"filter": "osv"})
    files = r2.json()["files"]
    assert [f["name"] for f in files] == ["CLIP_A.OSV", "clip_b.osv"]
    assert all(f["size_bytes"] > 0 and f["path"].startswith(home_real) for f in files)

    # filtre gpx
    r3 = client.get("/api/browse", params={"filter": "gpx"})
    assert [f["name"] for f in r3.json()["files"]] == ["trace.gpx"]

    # sous-dossier : parent = home
    r4 = client.get("/api/browse", params={"dir": str(tmp_path / "Docs")})
    assert r4.status_code == 200
    assert r4.json()["parent"] == home_real

    # hors périmètre -> 403 ; inexistant sous home -> 404
    assert client.get("/api/browse", params={"dir": "/etc"}).status_code == 403
    assert client.get("/api/browse", params={"dir": "/"}).status_code == 403
    assert client.get("/api/browse", params={"dir": str(tmp_path / "nope")}).status_code == 404


def test_browse_media_root_allowed(client):
    import pytest
    if not os.path.isdir("/run/media"):
        pytest.skip("/run/media absent")
    r = client.get("/api/browse", params={"dir": "/run/media"})
    assert r.status_code == 200
    assert r.json()["parent"] is None  # racine autorisée la plus haute


# ---------------------------------------------------------------------------
# /api/browse/roots — accès rapide volumes amovibles / caméra (voir SPEC.md)
# ---------------------------------------------------------------------------

def test_browse_roots_structure_and_home(client, isolated_config):
    r = client.get("/api/browse/roots")
    assert r.status_code == 200
    body = r.json()
    shortcuts = body["shortcuts"]
    assert isinstance(shortcuts, list) and shortcuts

    for sc in shortcuts:
        assert set(sc.keys()) == {"label", "path", "kind"}
        assert sc["kind"] in ("home", "removable", "source", "output", "camera")
        assert isinstance(sc["label"], str) and sc["label"]
        assert os.path.isabs(sc["path"])

    home_entries = [s for s in shortcuts if s["kind"] == "home"]
    assert len(home_entries) == 1
    assert home_entries[0]["path"] == os.path.realpath(str(os.path.expanduser("~")))

    # source/output configurés (isolated_config) remontés
    kinds = {s["kind"] for s in shortcuts}
    assert "source" in kinds
    assert "output" in kinds
    source_entry = next(s for s in shortcuts if s["kind"] == "source")
    output_entry = next(s for s in shortcuts if s["kind"] == "output")
    assert source_entry["path"] == os.path.realpath(str(isolated_config["source_dir"]))
    assert output_entry["path"] == os.path.realpath(str(isolated_config["output_dir"]))

    # pas de doublons de chemin réel
    paths = [s["path"] for s in shortcuts]
    assert len(paths) == len(set(paths))


def test_browse_roots_detects_real_sd_card(client, isolated_config):
    import pytest
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    sd_path = f"/run/media/{user}/SD_Card"
    if not user or not os.path.isdir(sd_path):
        pytest.skip("carte SD non montée sous /run/media/$USER sur cette machine")

    r = client.get("/api/browse/roots")
    assert r.status_code == 200
    shortcuts = r.json()["shortcuts"]
    matches = [s for s in shortcuts if s["kind"] == "removable" and s["label"] == "SD_Card"]
    assert matches, shortcuts
    assert matches[0]["path"] == os.path.realpath(sd_path)


def test_browse_roots_gvfs_absent_tolerated(client, isolated_config, monkeypatch):
    """Aucun montage gvfs (dossier inexistant) : pas d'erreur, simplement pas
    d'entrée kind=camera."""
    from app import config as config_module

    monkeypatch.setattr(config_module, "gvfs_root", lambda: "/nonexistent/gvfs/xyz")
    r = client.get("/api/browse/roots")
    assert r.status_code == 200
    kinds = {s["kind"] for s in r.json()["shortcuts"]}
    assert "camera" not in kinds


def test_browse_roots_gvfs_empty_tolerated(client, isolated_config, monkeypatch, tmp_path):
    """Dossier gvfs présent mais vide (ou sans montage mtp:/gphoto2:) : toléré."""
    from app import config as config_module

    empty_gvfs = tmp_path / "gvfs"
    empty_gvfs.mkdir()
    monkeypatch.setattr(config_module, "gvfs_root", lambda: str(empty_gvfs))
    r = client.get("/api/browse/roots")
    assert r.status_code == 200
    kinds = {s["kind"] for s in r.json()["shortcuts"]}
    assert "camera" not in kinds


def test_browse_roots_detects_mtp_camera_mount(client, isolated_config, monkeypatch, tmp_path):
    """Montage MTP simulé sous un faux gvfs : remonté en kind=camera."""
    from app import config as config_module

    fake_gvfs = tmp_path / "gvfs"
    fake_gvfs.mkdir()
    mtp_dir = fake_gvfs / "mtp:host=some_device"
    mtp_dir.mkdir()
    (fake_gvfs / "smb-share:server=nas,share=data").mkdir()  # autre protocole : ignoré

    monkeypatch.setattr(config_module, "gvfs_root", lambda: str(fake_gvfs))
    r = client.get("/api/browse/roots")
    assert r.status_code == 200
    shortcuts = r.json()["shortcuts"]
    camera_entries = [s for s in shortcuts if s["kind"] == "camera"]
    assert len(camera_entries) == 1
    assert camera_entries[0]["label"] == "mtp:host=some_device"
    assert camera_entries[0]["path"] == str(mtp_dir)


def test_browse_gvfs_root_allowed(client):
    """La racine /run/user/<uid>/gvfs est acceptée par /api/browse (navigation
    vers un montage caméra MTP)."""
    import pytest

    gvfs = f"/run/user/{os.getuid()}/gvfs"
    if not os.path.isdir(gvfs):
        pytest.skip("gvfs absent sur cette machine")
    r = client.get("/api/browse", params={"dir": gvfs})
    assert r.status_code == 200
    assert r.json()["parent"] is None  # racine autorisée la plus haute


def test_preview_proxy_generation_and_serving(client, isolated_config):
    """Flux proxy d'aperçu complet, sans dépendre de la carte SD : sortie de job
    simulée par une petite vidéo synthétique -> _generate_preview -> preview_url
    -> GET /api/media (Range) -> ffprobe h264/yuv420p/1920x960 + faststart."""
    import json
    import subprocess
    from urllib.parse import parse_qs, urlparse

    from app.jobs import Job, manager

    out_dir = isolated_config["output_dir"]
    fake_output = str(out_dir / "FAKE_360.mp4")
    gen = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=320x160:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", fake_output],
        capture_output=True, text=True, timeout=60,
    )
    assert gen.returncode == 0, gen.stderr

    job = Job(id="testprev", input="ignored.OSV", output=fake_output, options={},
              status="running", progress=0.95)
    manager._generate_preview(job, 1.0)

    assert job.preview_error is None, job.preview_error
    assert job.preview_url and job.preview_url.startswith("/api/media?path=")

    # servi par /api/media avec Range
    r = client.get(job.preview_url, headers={"Range": "bytes=0-127"})
    assert r.status_code == 206

    # caractéristiques décodables navigateur
    preview_path = parse_qs(urlparse(job.preview_url).query)["path"][0]
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt,width,height",
         "-print_format", "json", preview_path],
        capture_output=True, text=True, timeout=30,
    ).stdout)["streams"][0]
    assert probe["codec_name"] == "h264"
    assert probe["pix_fmt"] == "yuv420p"
    assert (probe["width"], probe["height"]) == (1920, 960)

    # faststart : l'atome moov précède mdat
    with open(preview_path, "rb") as fp:
        head = fp.read(64 * 1024)
    assert head.find(b"moov") != -1 and head.find(b"moov") < head.find(b"mdat")

    # idempotence : un second appel réutilise le cache et redonne une URL
    job2 = Job(id="testprev2", input="ignored.OSV", output=fake_output, options={},
               status="running", progress=0.95)
    manager._generate_preview(job2, 1.0)
    assert job2.preview_url == job.preview_url


def test_delete_unknown_job_returns_404(client, isolated_config):
    r = client.delete("/api/jobs/does-not-exist")
    assert r.status_code == 404


def test_get_jobs_lists_created_fields(client, isolated_config):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert r.json() == []


@requires_example_file
def test_files_lists_real_example(client, isolated_config_real_source):
    r = client.get("/api/files")
    assert r.status_code == 200
    paths = [f["path"] for f in r.json()]
    assert EXAMPLE_OSV in paths
    entry = next(f for f in r.json() if f["path"] == EXAMPLE_OSV)
    assert entry["name"] == os.path.basename(EXAMPLE_OSV)
    assert entry["size_bytes"] > 0
    assert entry["thumb_url"].startswith("/api/thumb?path=")


@requires_example_file
def test_probe_real_example(client, isolated_config_real_source):
    r = client.post("/api/probe", json={"path": EXAMPLE_OSV})
    assert r.status_code == 200
    body = r.json()
    assert body["width"] == 3840
    assert body["height"] == 3840
    assert body["audio"] is True
    assert body["has_calibration"] is True


@requires_example_file
def test_thumb_real_example(client, isolated_config_real_source):
    r = client.get("/api/thumb", params={"path": EXAMPLE_OSV})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"


@requires_example_file
def test_job_lifecycle_queued_to_terminal(client, isolated_config_real_source):
    # options légères (3840, interp rapide, mode v360) pour garder le test court
    r = client.post("/api/jobs", json={
        "inputs": [EXAMPLE_OSV],
        "options": {"out_w": 3840, "interp": "line", "mode": "v360"},
    })
    assert r.status_code == 200
    job_id = r.json()[0]["job_id"]

    status = None
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        jobs = client.get("/api/jobs").json()
        job = next(j for j in jobs if j["id"] == job_id)
        status = job["status"]
        assert "preview_url" in job  # champ toujours exposé (null tant qu'absent)
        if status in ("done", "error", "cancelled"):
            break
        time.sleep(0.3)

    assert status in ("done", "error", "cancelled")
    if status == "error":
        assert job["error"]  # message explicite, pas un crash silencieux
    if status == "done":
        assert os.path.isfile(job["output"])
        assert job["progress"] == 1.0
        # proxy d'aperçu : soit une URL /api/media lisible (Range OK),
        # soit un échec non bloquant documenté dans preview_error
        if job["preview_url"]:
            assert job["preview_url"].startswith("/api/media?path=")
            r_prev = client.get(job["preview_url"], headers={"Range": "bytes=0-255"})
            assert r_prev.status_code == 206
            # le proxy doit être décodable navigateur : h264 yuv420p 1920x960
            import json
            import subprocess
            from urllib.parse import parse_qs, unquote, urlparse
            preview_path = parse_qs(urlparse(job["preview_url"]).query)["path"][0]
            probe = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,pix_fmt,width,height",
                 "-print_format", "json", preview_path],
                capture_output=True, text=True, timeout=30,
            ).stdout)["streams"][0]
            assert probe["codec_name"] == "h264"
            assert probe["pix_fmt"] == "yuv420p"
            assert (probe["width"], probe["height"]) == (1920, 960)
        else:
            assert job["preview_error"]
