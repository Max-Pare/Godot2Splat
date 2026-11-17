"""Stage 5: put the splat into the Godot project.

That is the scanned project for a Godot scan; for an Unreal scan, the Godot project
given with --godot-project or set on the capture volume.

Written into the capture volume's output folder (default res://splats/<scene>/):
  <scene>.ply            the Gaussians, centred on their mean position
  <scene>_sky.png        equirectangular sky; only when the capture saw the sky
  <scene>_splat.json     what the scene builder needs (see below)
  <scene>_splat.tscn     built by Godot: splat node, environment, collision copies

The last file is made by res://addons/godot2splat/import/splat_scene_builder.gd,
either here through a headless Godot run or by the editor plugin.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

from .. import project as project_mod
from .. import splat_io
from ..config import Config
from ..workspace import Workspace
from . import clean, ingest

BUILD_SCRIPT = "res://addons/godot2splat/import/build_scene_cli.gd"


def mean_colour(ply: Path) -> list[float]:
    """Opacity-weighted average base colour of a splat, sRGB 0-1: shown where nothing is."""
    names, data = splat_io.read_ply(ply)
    if len(data) == 0:
        return [0.0, 0.0, 0.0]
    rgb = np.clip(0.5 + splat_io.SH_C0 * data[:, [names.index(f"f_dc_{i}") for i in range(3)]], 0, 1)
    weight = splat_io._sigmoid(data[:, names.index("opacity")]).astype(np.float64)[:, None] + 1e-9
    return [float(v) for v in (rgb * weight).sum(axis=0) / weight.sum()]


def target_project(cfg: Config, cap: dict) -> Path:
    """The Godot project that receives the splat."""
    given = cfg.export.godot_project or (cap.get("godot_project", "") if cfg.capture.engine == "unreal" else "")
    if cfg.capture.engine == "unreal" and not given:
        raise SystemExit("export: no Godot project to put the splat in. Set Godot Project on the Splat Capture "
                         "Volume, or pass --godot-project.")
    project = Path(given or cfg.capture.project)
    project_mod.check_project(project)
    return project


def run(ws: Workspace, cfg: Config) -> None:
    c = cfg.capture
    cap = ingest.load_capture(ws)
    project = target_project(cfg, cap)
    name = Path(c.scene).stem
    out_res = str(cap.get("output_dir") or f"res://splats/{name}/").rstrip("/") + "/"
    out = project_mod.res_to_path(project, out_res)
    out.mkdir(parents=True, exist_ok=True)

    src = clean.output(ws, "scene.ply")
    names, data = splat_io.read_ply(src)
    xyz = data[:, [names.index(k) for k in ("x", "y", "z")]].astype(np.float64)
    center = xyz.mean(axis=0) if len(xyz) else np.zeros(3)
    # GDGS recentres imported data on its mean and the node has to be placed there.
    # Centring the file ourselves keeps that offset in one known place: the manifest.
    splat_io.transform_ply(src, out / f"{name}.ply", 1.0, center)

    sky = ws.dir("ingest") / "sky.png"
    sky_res = ""
    stale_sky = out / f"{name}_sky.png"
    if sky.exists():
        shutil.copy2(sky, stale_sky)
        sky_res = f"{out_res}{name}_sky.png"
    else:
        for old in (stale_sky, Path(str(stale_sky) + ".import")):
            old.unlink(missing_ok=True)

    manifest = {
        "version": 1,
        # Collision is copied from a Godot source scene only.
        "source_scene": c.scene if c.engine == "godot" else "",
        "source": {"engine": c.engine, "project": c.project, "scene": c.scene},
        "splat": f"{out_res}{name}.ply",
        "center": [float(v) for v in center],
        "sky": sky_res,
        "background": mean_colour(src),
        "scene": f"{out_res}{name}_splat.tscn",
        "gaussians": int(len(data)),
    }
    manifest_path = out / f"{name}_splat.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"export: {len(data)} Gaussians written to {out / (name + '.ply')}")

    if not cfg.export.godot_import:
        print(f"export: left for the editor: import and build {manifest['scene']}")
        return
    if c.engine != "godot":
        project_mod.ensure_ready(project, c.godot)
    print("export: Godot is importing the splat (about 1.5 minutes per million Gaussians)", flush=True)
    project_mod.import_assets(project, c.godot)
    code, text = project_mod.run_godot(
        c.godot, ["--headless", "--path", str(project), "--script", BUILD_SCRIPT,
                  "--", f"{out_res}{name}_splat.json"], 600.0, "")
    if code != 0 or not (out / f"{name}_splat.tscn").exists():
        raise SystemExit(f"export: Godot could not build the splat scene (exit code {code})")
    print(f"export: open {manifest['scene']} in the Godot editor")
