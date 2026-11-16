"""Stage 1: render the Godot scene from many viewpoints.

Godot does the work (res://addons/godot2splat/capture/capture_cli.gd): it loads the
scene, finds its SplatCaptureVolume node, spreads cameras through the free space
inside the box and saves a colour picture and a depth map per view.

Godot runs with a window: its headless mode has no renderer.
"""
from __future__ import annotations

import json

from .. import project as project_mod
from ..config import Config
from ..workspace import Workspace

SCRIPT = "res://addons/godot2splat/capture/capture_cli.gd"


def run(ws: Workspace, cfg: Config) -> None:
    c = cfg.capture
    out = ws.reset("capture")
    code, text = project_mod.run_godot(
        c.godot,
        ["--path", c.project, "--disable-vsync", "--resolution", "640x360",
         "--script", SCRIPT, "--", "--scene", c.scene, "--out", str(out.resolve())],
        c.timeout, "")
    manifest = out / "capture.json"
    if code != 0 or not manifest.exists():
        hint = ""
        if "SplatCaptureVolume" not in text and "capture:" not in text:
            hint = "\nGodot printed nothing from the capture script; is the project's renderer Forward+ or Mobile?"
        raise SystemExit(f"capture: Godot ended with exit code {code} and "
                         f"{'no' if not manifest.exists() else 'a'} capture.json.{hint}")
    cap = json.loads(manifest.read_text())
    print(f"capture: {len(cap['views'])} views of {cap['image_size'][0]}x{cap['image_size'][1]} px "
          f"from {cap['positions']} camera positions")
