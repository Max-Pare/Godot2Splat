"""`g2s build`: a Godot scene or an Unreal map in, a Gaussian splat scene in a Godot project out."""
from __future__ import annotations

import importlib
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import typer

from . import project as project_mod
from . import unreal as unreal_mod
from .config import Config
from .device import CHOICES as DEVICES
from .workspace import Workspace, file_fingerprint, make_key

app = typer.Typer(add_completion=False, help=__doc__)

STAGES = ["capture", "ingest", "train", "clean", "export"]


def default_workspace(project: Path, scene_res: str) -> Path:
    """One cache folder per scene, inside the project but hidden from Godot."""
    return Path(project) / project_mod.WORK_DIR / Path(scene_res).stem


@app.callback()
def main(
    merge_stderr: bool = typer.Option(False, hidden=True, help="Send error output to standard output. The editor button reads one stream."),
) -> None:
    if merge_stderr:
        sys.stderr.flush()
        os.dup2(sys.stdout.fileno(), sys.stderr.fileno())


@app.command()
def build(
    project: Path = typer.Argument(..., help="Project to scan: a Godot project folder (holds project.godot) or an Unreal project (.uproject or its folder)."),
    scene: str = typer.Argument(..., help="Scene to scan. Godot: res:// path or a path inside the project, with a SplatCaptureVolume node. Unreal: map as /Game/... or a .umap path, with a Splat Capture Volume actor."),
    godot_project: Optional[Path] = typer.Option(None, help="Godot project that receives the splat (default: the scanned Godot project; for Unreal, the Godot Project set on the capture volume)."),
    workspace: Optional[Path] = typer.Option(None, help="Cache folder for stage outputs (default <project>/.g2s/<scene name>)."),
    steps: Optional[int] = typer.Option(None, help="Training steps (default: from the capture volume, else sized to the capture)."),
    cap: Optional[int] = typer.Option(None, help="Maximum number of Gaussians (default: from the capture volume, else sized to the capture)."),
    keep: int = typer.Option(0, min=0, help="After cleanup keep at most this many Gaussians: the ones that add most to the pictures. 0 keeps all. Rendering time and VRAM in Godot follow the count."),
    train_scale: float = typer.Option(100.0, min=10.0, max=100.0, help="Percent of the captured image size used for training. Lower is faster and a little softer."),
    depth_weight: float = typer.Option(0.2, help="Weight of the depth term in training; 0 turns it off."),
    godot: str = typer.Option("godot", envvar="G2S_GODOT", help="Godot editor binary (4.3 or newer)."),
    unreal: str = typer.Option("", envvar="G2S_UNREAL", help="UnrealEditor-Cmd binary (default: the engine version the .uproject names)."),
    origin: str = typer.Option("", help="Unreal: scan from the actor with this label instead of a Splat Capture Volume. The level is not changed."),
    box: str = typer.Option("", help="Unreal: box in which cameras are placed: 'auto' (sized to the open space around the start point; the default with --origin) or LxWxH in metres along Unreal's X, Y, Z."),
    set_: list[str] = typer.Option([], "--set", help="Unreal: capture volume setting as Name=Value, e.g. --set MaxPositions=200 --set ImageSize=640. Repeatable."),
    godot_import: bool = typer.Option(True, help="Let Godot import the splat and build the scene at the end. The Godot editor button turns this off and does it inside the editor."),
    only: Optional[str] = typer.Option(None, help=f"Run a single stage: {', '.join(STAGES)}."),
    force: bool = typer.Option(False, help="Ignore the cache."),
    captured: bool = typer.Option(False, hidden=True, help="The capture folder was just filled by the Unreal editor button: record it and go on."),
    device: str = typer.Option("auto", envvar="G2S_DEVICE", help="Processor for training and cleanup: auto (whichever the installed torch was built for), cuda (NVIDIA) or rocm (AMD Radeon)."),
) -> None:
    """Run the pipeline. Finished stages are skipped unless their inputs changed."""
    uproject = unreal_mod.find_uproject(project)
    if uproject is not None:
        engine = "unreal"
        project = uproject.parent.resolve()
        scene_res, scene_file = unreal_mod.to_map(project, scene)
    else:
        engine = "godot"
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
    if godot_project is not None:
        project_mod.check_project(godot_project)
    if engine == "godot" and (origin or box or set_):
        raise SystemExit("--origin, --box and --set are for Unreal maps; in Godot, set them on the SplatCaptureVolume node")
    if origin and not box:
        box = "auto"
    if box and box != "auto" and not re.fullmatch(r"\d+(\.\d+)?x\d+(\.\d+)?x\d+(\.\d+)?", box):
        raise SystemExit(f"--box {box}: give 'auto' or LxWxH in metres, e.g. 20x6x4")
    for item in set_:
        if "=" not in item or ";" in item:
            raise SystemExit(f"--set {item}: give one Name=Value")

    cfg = Config(device=device)
    cfg.capture.engine = engine
    cfg.capture.project = str(project)
    cfg.capture.scene = scene_res
    cfg.capture.godot = godot
    cfg.capture.unreal = unreal
    cfg.capture.origin = origin
    cfg.capture.box = box
    cfg.capture.settings = list(set_)
    cfg.train.steps = steps
    cfg.train.cap = cap
    cfg.train.train_scale = train_scale
    cfg.train.depth_weight = depth_weight
    cfg.clean.keep = keep
    cfg.export.godot_import = godot_import
    cfg.export.godot_project = str(godot_project.resolve()) if godot_project else ""

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
            # Not inputs of the pictures (and absent from caches made before Unreal support).
            for name in ("timeout", "engine", "unreal"):
                payload.pop(name)
            for name in ("origin", "box", "settings"):
                if not payload[name]:
                    payload.pop(name)
        if stage == "clean" and not payload["keep"]:
            payload.pop("keep")   # absent from caches made before --keep
        key = make_key(key, payload)
        if stage == "capture" and captured:
            if not (ws.dir("capture") / "capture.json").exists():
                raise SystemExit(f"--captured: no capture.json in {ws.dir('capture')}")
            ws.mark(stage, key)
            print("capture: done in the editor")
        elif stage in todo and (force or not ws.is_fresh(stage, key)):
            plan.append((stage, key))
        elif stage in todo:
            note = " (after changing what the scene shows, run with --force)" if stage == "capture" else ""
            print(f"{stage}: cached{note}")

    if engine == "godot" and any(stage in ("capture", "export") for stage, _ in plan):
        project_mod.ensure_ready(project, godot)
    if engine == "unreal" and any(stage == "capture" for stage, _ in plan):
        for line in unreal_mod.install(project):
            print(f"install: {line}")

    for stage, stage_key in plan:
        print(f"== {stage} ==", flush=True)
        importlib.import_module(f"g2s.stages.{stage}").run(ws, cfg)
        ws.mark(stage, stage_key)


@app.command()
def install(
    project: Path = typer.Argument(..., help="Godot project folder (holds project.godot), or Unreal project (.uproject or its folder)."),
    force: bool = typer.Option(False, help="Godot: also replace a gdgs addon the project already has."),
) -> None:
    """Godot: copy the godot2splat and gdgs addons into the project and enable them.
    Unreal: compile the G2SCapture plugin into the project."""
    if unreal_mod.is_project(project):
        done = unreal_mod.install(project)
        for line in done or ["nothing to do"]:
            print(f"install: {line}")
        print("install: add a Splat Capture Volume actor to a level, set its Godot Project and press "
              "'Scan to Splat' in the toolbar, or run: g2s build <Unreal project> /Game/<map> --godot-project <Godot project>")
        return
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
