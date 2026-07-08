"""Tests de app/core/gpx.py — parsing, analyse de couverture, rééchantillonnage."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.core import gpx

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "track_streetview.gpx")

# La trace synthétique fixtures/track_streetview.gpx couvre
# 2026-07-07T19:59:30Z .. 2026-07-07T20:00:40Z (71 points, 1 Hz), en ligne
# quasi-droite (~5 m/s), pour une "vidéo" fictive de 10 s démarrant à 20:00:00Z
# (donc 30 s de marge GPX de part et d'autre de la fenêtre vidéo).
VIDEO_START = datetime(2026, 7, 7, 20, 0, 0, tzinfo=timezone.utc)
DURATION_S = 10.0


def test_parse_gpx_sorted_and_utc():
    points = gpx.parse_gpx(FIXTURE)
    assert len(points) == 71
    assert all(p.t.tzinfo is not None for p in points)
    assert points == sorted(points, key=lambda p: p.t)
    assert points[0].t == datetime(2026, 7, 7, 19, 59, 30, tzinfo=timezone.utc)
    assert points[-1].t == datetime(2026, 7, 7, 20, 0, 40, tzinfo=timezone.utc)
    assert points[0].ele == pytest.approx(250.0)


def test_parse_gpx_missing_file_raises():
    with pytest.raises(gpx.GpxError):
        gpx.parse_gpx("/tmp/does-not-exist-osmo360.gpx")


def test_analyze_full_overlap_at_zero_offset():
    points = gpx.parse_gpx(FIXTURE)
    result = gpx.analyze(points, VIDEO_START, DURATION_S)
    assert result["overlap_s"] == pytest.approx(DURATION_S)
    assert result["coverage_pct"] == pytest.approx(100.0)
    assert result["gaps_gt_5s"] == []
    assert result["n_points_in_window"] == 11  # 20:00:00 .. 20:00:10 inclus, 1 Hz
    assert result["suggested_offset_s"] == pytest.approx(0.0)


def test_analyze_no_overlap_suggests_offset():
    points = gpx.parse_gpx(FIXTURE)
    far_start = points[-1].t + timedelta(hours=1)
    result = gpx.analyze(points, far_start, DURATION_S)
    assert result["overlap_s"] == 0.0
    assert result["coverage_pct"] == 0.0
    assert result["n_points_in_window"] == 0
    # décale la fenêtre pour retomber exactement sur le début de la trace
    assert result["suggested_offset_s"] == pytest.approx((points[0].t - far_start).total_seconds())


def test_analyze_detects_gap_gt_5s():
    points = [
        gpx.GpxPoint(t=VIDEO_START, lat=47.9, lon=7.15, ele=250.0, speed=None),
        gpx.GpxPoint(t=VIDEO_START + timedelta(seconds=12), lat=47.901, lon=7.151, ele=251.0, speed=None),
    ]
    result = gpx.analyze(points, VIDEO_START, DURATION_S)
    assert len(result["gaps_gt_5s"]) == 1
    assert result["gaps_gt_5s"][0]["duration_s"] == pytest.approx(12.0)


def test_resample_spans_full_window_at_rate_hz():
    points = gpx.parse_gpx(FIXTURE)
    samples = gpx.resample(points, VIDEO_START, DURATION_S, offset_s=0.0, rate_hz=1.0)
    assert len(samples) == 11  # floor(10*1)+1
    assert samples[0].t == VIDEO_START
    assert samples[-1].t == VIDEO_START + timedelta(seconds=DURATION_S)
    # espacement rigoureusement régulier (garanti par construction, camm.py en dépend)
    deltas = {(b.t - a.t).total_seconds() for a, b in zip(samples, samples[1:])}
    assert deltas == {1.0}


def test_resample_interpolates_position_matching_raw_track_at_zero_offset():
    points = gpx.parse_gpx(FIXTURE)
    samples = gpx.resample(points, VIDEO_START, DURATION_S, offset_s=0.0, rate_hz=1.0)
    # à offset nul, l'échantillon vidéo t=0 doit coïncider avec le point brut
    # horodaté exactement à VIDEO_START (20:00:00Z -> lat/lon/ele connus du fixture)
    raw_at_t0 = next(p for p in points if p.t == VIDEO_START)
    assert samples[0].lat == pytest.approx(raw_at_t0.lat)
    assert samples[0].lon == pytest.approx(raw_at_t0.lon)
    assert samples[0].ele == pytest.approx(raw_at_t0.ele)


def test_resample_offset_shifts_which_raw_point_is_sampled():
    points = gpx.parse_gpx(FIXTURE)
    samples_0 = gpx.resample(points, VIDEO_START, DURATION_S, offset_s=0.0, rate_hz=1.0)
    samples_5 = gpx.resample(points, VIDEO_START, DURATION_S, offset_s=5.0, rate_hz=1.0)
    # la timeline vidéo (.t) reste identique quel que soit l'offset ...
    assert [s.t for s in samples_0] == [s.t for s in samples_5]
    # ... mais la position échantillonnée à offset=5 doit correspondre à la
    # position brute 5 s plus tard dans le GPX (donc == position à l'indice t=5
    # de l'échantillonnage à offset=0, qui lit le GPX à VIDEO_START+5s).
    raw_at_t5 = next(p for p in points if p.t == VIDEO_START + timedelta(seconds=5))
    assert samples_5[0].lat == pytest.approx(raw_at_t5.lat)
    assert samples_5[0].lon == pytest.approx(raw_at_t5.lon)


def test_resample_computes_plausible_speed():
    points = gpx.parse_gpx(FIXTURE)
    samples = gpx.resample(points, VIDEO_START, DURATION_S, offset_s=0.0, rate_hz=1.0)
    assert all(s.speed is not None for s in samples)
    # trace quasi-rectiligne à ~5 m/s (cf. fixture, ~0.045e-3 deg/s de lat)
    for s in samples:
        assert 4.5 < s.speed < 5.5


def test_resample_empty_points_returns_empty():
    assert gpx.resample([], VIDEO_START, DURATION_S, 0.0, 1.0) == []


def test_resample_extrapolates_constant_outside_track():
    # fenêtre vidéo bien au-delà de la trace GPX : la position doit rester
    # figée sur le dernier point connu plutôt que de planter/extrapoler à l'infini.
    points = gpx.parse_gpx(FIXTURE)
    late_start = points[-1].t + timedelta(hours=1)
    samples = gpx.resample(points, late_start, 5.0, offset_s=0.0, rate_hz=1.0)
    assert len(samples) == 6
    assert all(s.lat == pytest.approx(points[-1].lat) for s in samples)
    assert all(s.lon == pytest.approx(points[-1].lon) for s in samples)
