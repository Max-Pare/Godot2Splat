"""The whole pipeline on the test room built in Unreal, with real Unreal and Godot runs.

Not part of the default test run (slow, needs Unreal 5.8 with a C++ toolchain, Godot and a GPU):
    python -m pytest -m unreal

tests/unreal/build_room.py builds the room of fixture_room.py as an Unreal map, so the
capture can be checked against the same known shape as the Godot scan. The splat goes
into a copy of the Godot test project.
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
from g2s import geometry, unreal

pytestmark = pytest.mark.unreal

ROOT = Path(__file__).resolve().parents[1]
GODOT = os.environ.get("G2S_GODOT", "godot")
STEPS = "2500"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Copies of the Unreal and Godot test projects after `g2s build`. Returns (Godot project, workspace)."""
    if shutil.which(GODOT) is None:
        pytest.skip("Godot not found")
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("no GPU")
    base = tmp_path_factory.mktemp("g2s_unreal")
    ue, gd = base / "unreal", base / "godot"
    shutil.copytree(ROOT / "tests" / "unreal_fixture", ue)
    shutil.copytree(ROOT / "tests" / "fixture_project", gd,
                    ignore=shutil.ignore_patterns(".godot", ".g2s", "addons", "splats", "*.import", "*.uid"))
    uproject = ue / "G2SFixture.uproject"
    try:
        editor = unreal.editor_binary(uproject, os.environ.get("G2S_UNREAL", ""))
    except SystemExit as err:
        pytest.skip(str(err))

    run = subprocess.run([sys.executable, "-m", "g2s", "install", str(ue)], cwd=ROOT,
                         capture_output=True, text=True, timeout=1800)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    room = base / "room.json"
    room.write_text(json.dumps(fixture_room.room_data()))
    env = dict(os.environ, G2S_ROOM_JSON=str(room), G2S_GODOT_PROJECT=str(gd))
    run = subprocess.run([str(editor), str(uproject), f"-ExecutePythonScript={ROOT / 'tests' / 'unreal' / 'build_room.py'}",
                          "-unattended", "-nosplash", "-nop4", "-stdout", "-FullStdOutLogOutput"],
                         env=env, capture_output=True, text=True, errors="replace", timeout=1800)
    assert (ue / "Content" / "Room.umap").exists(), run.stdout[-3000:]

    # The Godot project comes from the volume (G2S_GODOT_PROJECT above), not the command line.
    run = subprocess.run([sys.executable, "-m", "g2s", "build", str(ue), "/Game/Room", "--steps", STEPS, "--godot", GODOT],
                         cwd=ROOT, capture_output=True, text=True, timeout=3600)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    return gd, ue / ".g2s" / "Room"


def _capture(ws):
    return json.loads((ws / "capture" / "capture.json").read_text())


def test_depth_and_cameras_match_the_room(built):
    """Every depth pixel, pushed back into the world with its camera, lands on a surface
    of the room. Fails on a wrong axis conversion, depth unit, field of view or image
    orientation. The engine's sphere is a low-poly mesh, so its pixels may miss by a few mm."""
    _, ws = built
    cap = _capture(ws)
    assert cap["engine"] == "unreal"
    w, h = cap["image_size"]
    dw, dh = cap["depth_size"]
    K = geometry.scale_K(geometry.intrinsics(w, h, cap["fov_y"]), dw / w, dh / h)
    distances, sky_views = [], 0
    for view in cap["views"][::7]:
        raw = np.fromfile(ws / "capture" / "depth" / f"{view['name']}.bin", dtype="<f2").reshape(dh, dw, 3)
        mm = geometry.decode_depth(raw, cap["far"])
        sky_views += bool((mm == 0).any())
        points, _ = geometry.backproject(mm / 1000.0, K, geometry.camera_from_godot(view["transform"]))
        distances.append(fixture_room.surface_distance(points))
    d = np.concatenate(distances)
    assert np.median(d) < 0.001 and d.max() < 0.01, f"median {np.median(d) * 1000:.2f} mm, worst {d.max() * 1000:.1f} mm"
    assert sky_views > 0, "no view saw the sky through the window"


def test_cameras_stand_in_open_air(built):
    _, ws = built
    cap = _capture(ws)
    positions = np.unique(np.array([v["transform"][9:] for v in cap["views"]]).round(5), axis=0)
    # Fill placement (the default): the room's open space, and nothing outside its window.
    assert len(positions) == cap["positions"] >= 40
    assert fixture_room.in_open_air(positions).all()
    # Finer points in tight spots may come within half of Min Clearance (30 cm).
    assert fixture_room.surface_distance(positions).min() > 0.12


def test_held_out_views_are_reproduced(built):
    _, ws = built
    score = json.loads((ws / "eval" / "clean" / "score.json").read_text())
    assert score["held_out"] and score["views"] >= 30
    assert score["psnr"] > 32.0, score
    assert score["depth_error_m"] < 0.03, score


def test_scene_is_built_in_the_godot_project(built):
    gd, _ = built
    out = gd / "splats" / "Room"
    manifest = json.loads((out / "Room_splat.json").read_text())
    text = (out / "Room_splat.tscn").read_text()
    for name in ('name="SplatPivot"', 'name="Splat"', 'name="SplatEnvironment"'):
        assert name in text
    assert manifest["source"]["engine"] == "unreal" and manifest["source_scene"] == ""
    # The capture volume's position becomes the Godot origin.
    assert np.allclose(manifest["source"]["origin"], fixture_room.VOLUME["position"], atol=0.01)
    assert "StaticBody3D" not in text   # collision is copied from Godot scenes only
    assert (out / "Room_sky.png").exists()
    # The player starts at the origin (the start point), flying: there is no collision.
    assert 'name="Player"' in text and "start_flying = true" in text
    assert np.allclose(manifest["spawn"], 0.0, atol=1e-6)
