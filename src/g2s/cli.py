"""`g2s build`: a Godot scene in, a Gaussian splat scene in the same project out."""
from __future__ import annotations

import importlib
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import typer

from . import project as project_mod
from .config import Config
from .device import CHOICES as DEVICES
from .workspace import Workspace, file_fingerprint, make_key

app = typer.Typer(add_completion=False, help=__doc__)

STAGES = ["capture", "ingest", "train", "clean", "export"]


def default_workspace(project: Path, scene_res: str) -> Path:
    """One cache folder per scene, inside the project but hidden from Godot."""
    return Path(project) / project_mod.WORK_DIR / Path(scene_res).stem


@app.command()
def build(
    project: Path = typer.Argument(..., help="Godot project folder (holds project.godot)."),
    scene: str = typer.Argument(..., help="Scene to scan: res:// path, or a path inside the project. It needs a SplatCaptureVolume node."),
    workspace: Optional[Path] = typer.Option(None, help="Cache folder for stage outputs (default <project>/.g2s/<scene name>)."),
    steps: Optional[int] = typer.Option(None, help="Training steps (default: from the capture volume node, else sized to the capture)."),
    cap: Optional[int] = typer.Option(None, help="Maximum number of Gaussians (default: from the capture volume node, else sized to the capture)."),
    train_scale: float = typer.Option(100.0, min=10.0, max=100.0, help="Percent of the captured image size used for training. Lower is faster and a little softer."),
    depth_weight: float = typer.Option(0.2, help="Weight of the depth term in training; 0 turns it off."),
    godot: str = typer.Option("godot", envvar="G2S_GODOT", help="Godot editor binary (4.3 or newer)."),
    godot_import: bool = typer.Option(True, help="Let Godot import the splat and build the scene at the end. The editor button turns this off and does it inside the editor."),
    only: Optional[str] = typer.Option(None, help=f"Run a single stage: {', '.join(STAGES)}."),
    force: bool = typer.Option(False, help="Ignore the cache."),
    device: str = typer.Option("auto", envvar="G2S_DEVICE", help="Processor for training and cleanup: auto (whichever the installed torch was built for), cuda (NVIDIA) or rocm (AMD Radeon)."),
) -> None:
    """Run the pipeline. Finished stages are skipped unless their inputs changed."""
    project_mod.check_project(project)
    project = project.resolve()
    scene_res = project_mod.to_res(project, scene)
    scene_file = project_mod.res_to_path(project, scene_res)
    if not scene_file.is_file():
        raise SystemExit(f"scene not found: {scene_file}")
    if only and only not in STAGES:
        raise SystemExit(f"unknown stage '{only}'; choose from {', '.join(STAGES)}")
    if device not in DEVICES:
        raise SystemExit(f"unknown device '{device}'; choose from {', '.join(DEVICES)}")

    cfg = Config(device=device)
    cfg.capture.project = str(project)
    cfg.capture.scene = scene_res
    cfg.capture.godot = godot
    cfg.train.steps = steps
    cfg.train.cap = cap
    cfg.train.train_scale = train_scale
    cfg.train.depth_weight = depth_weight
    cfg.export.godot_import = godot_import

    ws = Workspace(workspace or default_workspace(project, scene_res))
    todo = [only] if only else STAGES
    key = ""
    plan = []
    for stage in STAGES:
        payload = asdict(getattr(cfg, stage))
        if stage == "capture":
            # The scene file stands in for everything it shows. Meshes, materials and
            # scripts it uses are not tracked: after changing those, use --force.
            payload["inputs"] = [file_fingerprint(scene_file)]
            payload.pop("timeout")
        key = make_key(key, payload)
        if stage in todo and (force or not ws.is_fresh(stage, key)):
            plan.append((stage, key))
        elif stage in todo:
            note = " (after changing what the scene shows, run with --force)" if stage == "capture" else ""
            print(f"{stage}: cached{note}")

    if any(stage in ("capture", "export") for stage, _ in plan):
        if not project_mod.addons_current(project):
            for line in project_mod.install(project):
                print(f"install: {line}")
        if not (project / ".godot" / "imported").is_dir() or not (
                project / ".godot" / "global_script_class_cache.cfg").exists():
            print("install: first import of the project by Godot", flush=True)
            project_mod.import_assets(project, godot)

    for stage, stage_key in plan:
        print(f"== {stage} ==", flush=True)
        importlib.import_module(f"g2s.stages.{stage}").run(ws, cfg)
        ws.mark(stage, stage_key)


@app.command()
def install(
    project: Path = typer.Argument(..., help="Godot project folder (holds project.godot)."),
    force: bool = typer.Option(False, help="Also replace a gdgs addon the project already has."),
) -> None:
    """Copy the godot2splat and gdgs addons into a Godot project and enable them."""
    done = project_mod.install(project, force)
    for line in done or ["nothing to do"]:
        print(f"install: {line}")
    print("install: add a SplatCaptureVolume node to a scene, select it and press 'Scan to Splat', "
          "or run: g2s build <project> <scene>")


@app.command()
def stages() -> None:
    """List pipeline stages in order."""
    print(", ".join(STAGES))


if __name__ == "__main__":
    app()
