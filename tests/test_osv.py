"""Tests de app/core/osv.py — wrapper probe/extract_metadata/extract_thumbnail."""
import os
import tempfile

import pytest

from app.core import osv
from conftest import EXAMPLE_OSV

pytestmark = pytest.mark.skipif(
    not os.path.isfile(EXAMPLE_OSV),
    reason="fichier d'exemple .OSV absent (carte SD non montée)",
)


def test_probe_returns_expected_fields():
    info = osv.probe(EXAMPLE_OSV)
    # fisheyes de l'Osmo 360 : toujours 3840x3840 ; durée/fps dépendent du clip
    assert info.width == 3840
    assert info.height == 3840
    assert info.fps > 0
    assert 0 < info.duration_s < 3600
    assert info.audio is True
    assert info.size_bytes > 0
    assert info.creation_time_utc is not None


def test_probe_missing_file_raises():
    with pytest.raises(osv.OsvError):
        osv.probe("/tmp/does-not-exist-osmo360.OSV")


def test_extract_metadata_produces_calibration():
    with tempfile.TemporaryDirectory() as td:
        meta = osv.extract_metadata(EXAMPLE_OSV, td)
        assert meta["calibration"] is not None
        assert len(meta["calibration"]["lenses"]) >= 2
        assert os.path.isfile(meta["imu_perframe"])


def test_extract_thumbnail_creates_jpeg():
    with tempfile.TemporaryDirectory() as td:
        out_jpg = os.path.join(td, "thumb.jpg")
        result = osv.extract_thumbnail(EXAMPLE_OSV, out_jpg)
        assert result == out_jpg
        assert os.path.getsize(out_jpg) > 0
        with open(out_jpg, "rb") as fp:
            assert fp.read(2) == b"\xff\xd8"  # signature JPEG
