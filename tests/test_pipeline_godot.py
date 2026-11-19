"""The whole pipeline on the test room, with real Godot runs and a real training.

Not part of the default test run (slow, needs Godot with a window and a GPU):
    python -m pytest -m godot

One short build is shared by all tests here. They check, in order of the pipeline:
the capture's geometry against the room's known shape, the camera placement, the
trained splat on held-out views, and what Godot itself shows of the generated scene.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import fixture_room
from g2s import geometry

pytestmark = pytest.mark.godot

ROOT = Path(__file__).resolve().parents[1]
GODOT = os.environ.get("G2S_GODOT", "godot")
STEPS = "2500"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """A copy of the test project after `g2s build`. Returns (project, workspace)."""
    if shutil.which(GODOT) is None:
        pytest.skip("Godot not found")
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("no GPU")
    project = tmp_path_factory.mktemp("g2s") / "project"
    shutil.copytree(ROOT / "tests" / "fixture_project", project,
                    ignore=shutil.ignore_patterns(".godot", ".g2s", "addons", "splats", "*.import", "*.uid"))
    run = subprocess.run(
        [sys.executable, "-m", "g2s", "build", str(project), "res://room.tscn",
         "--steps", STEPS, "--godot", GODOT],
        cwd=ROOT, capture_output=True, text=True, timeout=3600)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    return project, project / ".g2s" / "room"


def _capture(ws):
    return json.loads((ws / "capture" / "capture.json").read_text())


def test_depth_and_cameras_match_the_room(built):
    """Every depth pixel, pushed back into the world with its camera, lands on a
    surface of the room. Wrong depth, intrinsics, axes or image orientation all fail this."""
    _, ws = built
    cap = _capture(ws)
    w, h = cap["image_size"]
    dw, dh = cap["depth_size"]
    K = geometry.scale_K(geometry.intrinsics(w, h, cap["fov_y"]), dw / w, dh / h)
    worst, sky_views = 0.0, 0
    for view in cap["views"][::7]:
        raw = np.fromfile(ws / "capture" / "depth" / f"{view['name']}.bin", dtype="<f2").reshape(dh, dw, 3)
        mm = geometry.decode_depth(raw, cap["far"])
        sky_views += bool((mm == 0).any())
        points, _ = geometry.backproject(mm / 1000.0, K, geometry.camera_from_godot(view["transform"]))
        worst = max(worst, float(fixture_room.surface_distance(points).max()))
    assert worst < 0.003, f"a depth pixel lies {worst * 1000:.1f} mm from every surface"
    assert sky_views > 0, "no view saw the sky through the window"


def test_cameras_stand_in_open_air(built):
    _, ws = built
    cap = _capture(ws)
    positions = np.unique(np.array([v["transform"][9:] for v in cap["views"]]).round(5), axis=0)
    assert len(positions) == cap["positions"] >= 30
    # The planner samples distances on a 64 px grid per cube face, so allow it 3 cm.
    clearance = fixture_room.surface_distance(positions)
    assert fixture_room.in_open_air(positions).all()
    assert clearance.min() > 0.27, f"a camera is {clearance.min():.2f} m from a surface"
    # The free space of the room is one piece: most grid points should be reached.
    assert len(positions) >= 50


def test_held_out_views_are_reproduced(built):
    _, ws = built
    score = json.loads((ws / "eval" / "clean" / "score.json").read_text())
    assert score["held_out"] and score["views"] >= 30
    assert score["psnr"] > 32.0, score
    assert score["depth_error_m"] < 0.03, score
    before = json.loads((ws / "eval" / "train" / "score.json").read_text())
    assert score["gaussians"] < before["gaussians"]
    assert score["psnr"] > before["psnr"] - 0.3   # cleaning must not cost quality


def test_scene_is_built_in_the_project(built):
    project, ws = built
    out = project / "splats" / "room"
    manifest = json.loads((out / "room_splat.json").read_text())
    text = (out / "room_splat.tscn").read_text()
    for name in ('name="SplatPivot"', 'name="Splat"', 'name="SplatEnvironment"', 'name="Collision"',
                 'name="Player"'):
        assert name in text
    assert "start_flying = true" not in text   # the copied collision is walkable
    assert len(manifest["spawn"]) == 3
    assert text.count('type="StaticBody3D"') == sum(1 for *_, body in fixture_room.SHELL + fixture_room.PROP_BOXES if body)
    assert "res://room.tscn" not in text      # the splat scene stands on its own
    assert (out / "room_sky.png").exists() and manifest["sky"] == "res://splats/room/room_sky.png"
    # The Gaussians are centred in the file; the scene puts them back.
    assert np.linalg.norm(manifest["center"]) > 0.1


def test_godot_shows_what_was_scanned(built, tmp_path):
    """Godot renders the generated scene from held-out cameras; the pictures must
    match the scan. Fails on a turned or mirrored splat, a wrong position, wrong
    colour space, or a wrong sky."""
    cv2 = pytest.importorskip("cv2")
    project, ws = built
    cap = _capture(ws)
    held_out = [v["name"] for v in cap["views"] if v["split"] == "test"]
    assert held_out
    names = held_out[::max(1, len(held_out) // 6)][:6]
    w, h = cap["image_size"]
    run = subprocess.run(
        [GODOT, "--path", str(project), "--resolution", f"{w}x{h}",
         "--script", str(ROOT / "tests" / "godot" / "render_views.gd"), "--",
         "--scene", "res://splats/room/room_splat.tscn", "--capture", str(ws / "capture" / "capture.json"),
         "--views", ",".join(names), "--out", str(tmp_path)],
        capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stdout[-2000:]
    scores = []
    for name in names:
        want = cv2.imread(str(ws / "capture" / "images" / f"{name}.png")).astype(np.float64) / 255
        got = cv2.imread(str(tmp_path / f"{name}.png")).astype(np.float64) / 255
        assert got.shape == want.shape, f"Godot rendered {got.shape}, the scan is {want.shape}"
        scores.append(-10 * np.log10(((want - got) ** 2).mean()))
    assert min(scores) > 28.0, dict(zip(names, np.round(scores, 1)))


def test_splat_object_highlights_and_survives_a_rebuild(built):
    """A SplatObject (ported from Real2Virtu) over the ball tints the ball and
    nothing else, moves it, glows when the camera comes near, and is kept when
    the splat scene is built again."""
    project, _ = built
    run = subprocess.run(
        [GODOT, "--path", str(project), "--resolution", "640x480",
         "--script", str(ROOT / "tests" / "godot" / "check_highlight.gd"),
         "--", "--scene", "res://splats/room/room_splat.tscn", "--save"],
        capture_output=True, text=True, timeout=600)
    line = next((l for l in run.stdout.splitlines() if l.startswith("highlight: RESULT ")), None)
    assert line, run.stdout[-3000:]
    r = json.loads(line.removeprefix("highlight: RESULT "))
    assert r["highlight"]["inside"] > 0.3 * r["highlight"]["ball_px"], r
    assert r["highlight"]["outside"] < 0.02 * r["highlight"]["inside"], r
    assert r["lift"]["inside"] + r["lift"]["outside"] > 0.3 * r["lift"]["ball_px"], r
    assert r["near"] and r["glow"] > 0.05, r

    run = subprocess.run(
        [sys.executable, "-m", "g2s", "build", str(project), "res://room.tscn", "--steps", STEPS,
         "--godot", GODOT, "--only", "export", "--force"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    text = (project / "splats" / "room" / "room_splat.tscn").read_text()
    assert '[node name="BallHighlight" type="Node3D" parent="."' in text
    assert "res://addons/godot2splat/objects/splat_object.gd" in text
    assert text.count('name="SplatPivot"') == 1      # rebuilt, not doubled
    assert text.count('[node name="Player"') == 1    # kept, not doubled
