"""Stage 1: render the Godot scene or the Unreal map from many viewpoints.

The engine does the work: it loads the scene, finds its capture volume, spreads
cameras through the free space inside the box and saves a colour picture and a
depth map per view.
  Godot:  res://addons/godot2splat/capture/capture_cli.gd
  Unreal: the G2SCapture plugin (unreal/G2SCapture), in an editor started for it

Both write the same folder, in Godot's axes and metres (see ingest.py). Both run
with a window: neither renders without one.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import project as project_mod
from .. import unreal
from ..config import Config
from ..workspace import Workspace

SCRIPT = "res://addons/godot2splat/capture/capture_cli.gd"


def run(ws: Workspace, cfg: Config) -> None:
    c = cfg.capture
    out = ws.reset("capture")
    if c.engine == "unreal":
        uproject = unreal.find_uproject(Path(c.project))
        code, text = unreal.run_capture(uproject, c.scene, out.resolve(), c.unreal, c.timeout,
                                        c.origin, c.box, c.settings)
        if code != 0 or not (out / "capture.json").exists():
            hint = "" if "capture:" in text else "\nThe plugin printed nothing; is the G2SCapture plugin enabled in the project?"
            raise SystemExit(f"capture: Unreal ended with exit code {code} and "
                             f"{'no' if not (out / 'capture.json').exists() else 'a'} capture.json.{hint}")
        report(out)
        return
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
    report(out)


def report(out) -> None:
    cap = json.loads((out / "capture.json").read_text())
    print(f"capture: {len(cap['views'])} views of {cap['image_size'][0]}x{cap['image_size'][1]} px "
          f"from {cap['positions']} camera positions")
