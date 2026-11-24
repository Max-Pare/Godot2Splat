"""All tunables, grouped per stage. A stage re-runs when its group changes.

Capture settings (box size, camera spacing, image size) are not here: they live on
the SplatCaptureVolume node (Godot) or actor (Unreal) and reach Python through capture.json.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CaptureCfg:
    engine: str = "godot"      # which engine scans: "godot" or "unreal"
    project: str = ""          # Godot project folder, or Unreal project folder
    scene: str = ""            # res:// path of the scene, or /Game/... path of the Unreal map
    godot: str = "godot"       # Godot editor binary (4.3+); also builds the splat scene
    unreal: str = ""           # UnrealEditor-Cmd binary; empty: from the project's engine version
    timeout: float = 3600.0    # seconds before a hanging Godot or Unreal is stopped
    # Unreal only. origin: scan from a temporary volume at the actor with this label.
    # box: "auto" (explored) or "LxWxH" metres. settings: "Name=Value" for the volume.
    origin: str = ""
    box: str = ""
    settings: list[str] = field(default_factory=list)


@dataclass
class IngestCfg:
    voxel: float = 0.02          # metres; start points are thinned to this grid
    max_points: int = 400_000
    point_stride: int = 2        # every n-th depth pixel becomes a start point candidate
    sky_width: int = 2048        # px; the baked sky panorama is this wide and half as high
    min_views: int = 30          # fewer training views than this is an error


@dataclass
class TrainCfg:
    # None: taken from the capture volume node, else sized to the capture (auto_size).
    steps: int | None = None
    cap: int | None = None
    max_steps: int = 20_000    # upper limits for the automatic sizing
    max_cap: int = 1_500_000   # sized for 8 GB VRAM
    # Percent of the captured image size used for training. The images are rendered,
    # not filmed, so the capture's own image size is the main quality setting.
    train_scale: float = 100.0
    sh_degree: int = 3
    ssim_weight: float = 0.2
    opacity_reg: float = 0.01
    scale_reg: float = 0.01
    depth_weight: float = 0.2  # L1 against the rendered scene depth
    alpha_weight: float = 0.1  # opaque where the scene has a surface, clear where it has none
    seed: int = 0


@dataclass
class CleanCfg:
    min_opacity: float = 0.02         # below this a Gaussian only adds haze
    # At most this many Gaussians: those that add most to the pictures (0 keeps all).
    # Rendering time and VRAM in Godot follow the count; a budget for weaker GPUs.
    keep: int = 0
    # Fewer kept than this share means training failed: stop before export.
    min_kept_share: float = 0.05
    # Size limits are the larger of a fixed length (metres) and a share of the
    # distance to the nearest camera, so far scenery seen through windows survives.
    max_size: float = 0.3
    max_size_per_m: float = 0.2
    needle_ratio: float = 20.0        # longest axis against the middle one
    needle_length: float = 0.25
    needle_length_per_m: float = 0.1
    isolated_gap: float = 0.15        # mean distance to the 3 nearest neighbours
    isolated_gap_per_m: float = 0.1
    # Drop Gaussians floating in space that the depth maps show as empty. Depth is
    # exact here (rendered, not measured), so the margins are tight.
    carve: bool = True
    margin: float = 0.03              # metres in front of the surface
    margin_rel: float = 0.01          # ... or this share of the depth, whichever is larger
    carve_min_views: int = 2
    carve_min_share: float = 0.8      # share of the views seeing the Gaussian that must agree
    # Drop Gaussians no view shows (behind the surface in every view that has them in
    # its picture): strays above ceilings and inside walls, which also blow up the
    # splat's bounding box. Looser margins than carving: hidden by more than this.
    drop_hidden: bool = True
    hidden_margin: float = 0.1
    hidden_margin_rel: float = 0.05


@dataclass
class ExportCfg:
    godot_import: bool = True   # run Godot to import the splat and build the scene
    # Godot project that receives the splat. Empty: the scanned project (Godot), or
    # the Godot Project set on the Unreal capture volume.
    godot_project: str = ""


@dataclass
class Config:
    # "auto", "cuda" or "rocm" (see device.py). Not part of any stage's group:
    # changing it does not re-run finished stages.
    device: str = "auto"
    capture: CaptureCfg = field(default_factory=CaptureCfg)
    ingest: IngestCfg = field(default_factory=IngestCfg)
    train: TrainCfg = field(default_factory=TrainCfg)
    clean: CleanCfg = field(default_factory=CleanCfg)
    export: ExportCfg = field(default_factory=ExportCfg)
