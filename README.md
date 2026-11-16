# godot2splat

Scans a 3D Godot scene into a Gaussian splat and puts the result back into the same project as a ready-to-use scene.

Godot renders the scene from many viewpoints (colour and exact depth), gsplat trains a splat from those pictures, and the splat is imported back through the GDGS addon.
Version 1 is for interiors and walkable levels: cameras are spread through the open space inside a box you place.

The training, cleanup and Godot display code is taken from [Real2Virtu](https://github.com/Max-Pare/Real2Virtu), which does the same for phone scans of real rooms. See "Taken from Real2Virtu" below.

## Status

Verified on the test room in `tests/fixture_project` (6 x 5 m room, a window to the sky, five props, sun and lamp with shadows, filmic tonemapping), RX 7800 XT, Godot 4.7.2, 640 px pictures:

| Check | Result |
|---|---|
| Capture | 402 pictures from 67 camera positions in 7 s |
| Depth and camera conventions | 40 million depth pixels pushed back into the world: median 0.2 mm, worst 1.0 mm from the room's true surfaces |
| Short training (2,500 steps, under a minute) | see `pytest -m godot` thresholds: over 32 dB PSNR on held-out views |
| Godot shows the generated scene | 34 to 41 dB PSNR against the scan pictures on six held-out views |

Not verified: scenes other than the test room, the Mobile renderer, Windows, NVIDIA. The editor button was exercised through its test hook, not by clicking.

## Setup

Needs Godot 4.3 or newer (Forward+ or Mobile renderer), Python 3.11 or 3.12, `uv`, and an NVIDIA GPU or an AMD Radeon on Linux.

NVIDIA:

```
uv sync --extra gpu --extra dev
```

AMD Radeon on Linux (ROCm):

```
sudo pacman -S rocm-hip-sdk            # compiler and headers; other distros: the ROCm SDK
uv sync --python 3.12 --extra amd --extra dev
scripts/build_amd_gsplat.sh            # about 80 s; clones AMD's gsplat fork into tools/amd_gsplat
```

- gsplat has no AMD build on PyPI. The script builds AMD's fork (`AMD-Ecosystem/gsplat`, tag `gsplat-1.5.3b2`) and patches it for Radeon cards; `tests/test_gsplat_gpu.py` guards the result.
- `uv sync` removes packages it did not install, gsplat included. Use `uv sync --inexact ...` later on, or re-run the script.
- The ROCm SDK and the torch wheel should share a major.minor version (the wheel index is set in `pyproject.toml`, now `rocm7.2`). When they do, g2s makes torch use the installed ROCm runtime instead of the copy inside the wheel, which crashed at random in Real2Virtu. Each GPU stage prints which one is in use. `G2S_ROCM_RUNTIME=bundled` switches this off.

## Use

1. Install the addons into your project (copies `addons/godot2splat` and `addons/gdgs`, enables both):

   ```
   g2s install path/to/project
   ```

2. Open the scene in the Godot editor and add a **SplatCaptureVolume** node.
   - Its box (`Size`) limits where cameras are placed. What lies outside the box is still captured when it can be seen from inside.
   - Its position is the start point and must be in open air, at least `Min Clearance` from any surface. Cameras spread from there through everything they can see.
   - A room behind a closed door is not reached: add a `Marker3D` child inside it as another start point.

3. Scan, either way:
   - select the volume and press **Scan to Splat** in the 3D editor toolbar, or
   - `g2s build path/to/project res://level.tscn`

4. Open the generated `res://splats/<scene>/<scene>_splat.tscn`.

Stages run in order and are cached in `<project>/.g2s/<scene>/`: `capture`, `ingest`, `train`, `clean`, `export`.
Re-run one with `--only <stage>`; ignore the cache with `--force`.
The cache notices changes to the scene file, not to meshes, materials or scripts it uses: after changing those, use `--force`.

The capture opens a small Godot window for a few seconds. Godot's headless mode has no renderer, so a display is needed.

### Capture volume settings

| Setting | Default | Meaning |
|---|---|---|
| Size | 10 x 4 x 10 m | box in which cameras are placed |
| Camera Spacing | 1 m | distance between camera positions; each gives 6 pictures |
| Min Clearance | 0.3 m | cameras keep this distance from every surface |
| Max Positions | 120 | more than this are thinned out evenly |
| Image Size | 800 px | width and height of each picture; training time follows the pixel count |
| Test Every | 10 | every n-th position is held out of training and used to score the result; 0 holds nothing out |
| Warmup Frames | 2 | frames rendered at each view before the picture is taken |
| Settle Frames | 5 | frames the scene runs before it is frozen |
| Far | 500 m | far plane of the capture cameras |
| Exclude | none | nodes hidden during the scan (player model, moving objects) |
| Keep Screen Space Effects | off | leave SSR, SSAO, SSIL, glow, depth of field and auto exposure on |
| Max Gaussians, Training Steps | 0 | 0 sizes the training to the scan |
| Output Dir | `res://splats/<scene>/` | where the splat and the generated scene go |

`g2s build` options: `--steps`, `--cap` (override the node), `--train-scale` (percent of the picture size used for training), `--depth-weight`, `--workspace`, `--godot`, `--device auto|cuda|rocm`, `--only`, `--force`, `--no-godot-import`.

## How it works

**Capture.** Godot loads the scene, lets it run for a few frames, then freezes it (scripts, physics, animations, particles, shader time). Camera positions come from a flood fill of free space that uses rendered depth only, so it works with anything that draws and needs no collision shapes: a grid of candidate points fills the box; from each accepted point a small depth cube map shows which neighbours are in view and how close the nearest surface is. Points inside walls or solid objects are hidden from every point in open air and are never reached. At each position the six faces of a randomly turned cube are photographed (90 degree field of view).

Depth is written by a full-screen shader pass in whole millimetres, split over two half-float channels so that it survives the trip through the renderer exactly; a third channel repeats it as a check, and `ingest` refuses a capture where the two disagree.

Effects that differ from view to view (SSR, SSAO, SSIL, glow, depth of field, auto exposure) are switched off for the scan and listed in the log.

**Training.** gsplat with the MCMC strategy, as in Real2Virtu. Poses, intrinsics and depth are exact, so nothing about the cameras is learned. Pixels where the scene has no surface (sky) are kept out of the splat: there the render is laid over the picture itself, so staying clear is the best the splat can do; on surfaces it is laid over a random colour, so it has to be opaque.

**Clean.** Removes near-transparent, oversized, needle-shaped and isolated Gaussians, and any that float in space the depth maps show as empty.

**Export.** Writes `<scene>.ply` (centred on its mean position), `<scene>_sky.png` when the scan saw the sky, and builds `<scene>_splat.tscn`:

```
<Scene>Splat (Node3D)
  SplatPivot (Node3D)                  half a turn about Z, at the splat's centre
    Splat (GaussianSplatNode)          half a turn about Z
  SplatEnvironment (WorldEnvironment)  linear tonemapping, baked sky, the gdgs compositor effect
  Collision (Node3D)                   copies of the source scene's StaticBody3D nodes and shapes
```

The two half turns cancel. They are there because `GaussianSplatNode` turns itself upside down when it enters the tree unrotated (it assumes a Y-down file), and leaves a node that is already rotated alone.

The splat's colours are the scanned scene's final colours, tonemapping included, and gdgs draws them before tonemapping. So the generated environment uses linear tonemapping, and the sky is a panorama photographed with the scene's tonemapper, so that it matches.
If you instance the splat scene into a scene with its own `WorldEnvironment`, delete `SplatEnvironment` and add a `GaussianCompositorEffect` to your environment's compositor.

## Limits

- A static snapshot: moving objects, animated lights and particles are frozen as they were after `Settle Frames`.
- Transparent materials write no depth. Glass is learned as a tint on what lies behind it. Mirrors and reflections are approximated by view-dependent colour.
- SDFGI lighting moves with the camera and settles over many frames; expect shimmer, or raise `Warmup Frames` to 30 or more. Baked lightmaps and VoxelGI capture cleanly. The scan prints a note when SDFGI or volumetric fog is on.
- Meshes you add to the generated scene are lit and tonemapped by the generated (linear) environment, not the source one.
- Collision: only `StaticBody3D` nodes with `CollisionShape3D` children are copied, read from the scene file. Bodies created by scripts, CSG and GridMap collision are not.
- Projects that use physical light units or a custom exposure may alter the depth pass; `ingest` then stops with a message instead of using wrong depth.
- The gdgs importer claims every `.ply` file in the project, and takes about 1.5 minutes per million Gaussians on first import. The editor does not respond during that time.
- One floor of cameras is not assumed: the box is filled in 3D. Large boxes with small spacing reach `Max Positions` quickly; the positions are then thinned evenly, which lowers detail everywhere.

## Tests

```
python -m pytest            # unit tests, a few seconds
python -m pytest -m godot   # the whole pipeline on the test room: Godot with a window, a short training
```

`tests/fixture_room.py` defines the test room once, for the Godot scene (`python tests/fixture_room.py` rewrites `room.tscn`) and for the checks against its known shape.

The editor button can be run without clicking: `G2S_EDITOR_SCAN=res://room.tscn G2S_EDITOR_QUIT=1 godot --path <project> --editor`.

## Taken from Real2Virtu

Copied, so this repository stands alone:

| Here | From Real2Virtu | Changes |
|---|---|---|
| `src/g2s/splat_io.py`, `workspace.py`, `device.py`, `gsplat_compat.py` | same names under `src/r2v/` | names only |
| `src/g2s/stages/train.py` | `stages/splat.py` | rasteriser call, losses, AMD speed-ups and MCMC setup kept; pose and exposure learning removed; new loop with sky handling and held-out views |
| `src/g2s/stages/clean.py` | `stages/clean.py` | haze and free-space filters kept; hole filling removed |
| `src/g2s/geometry.py` | parts of `stages/ingest.py` | `backproject`, `thin`, `scale_K` |
| `src/g2s/evaluate.py` | `tools/eval_render.py` | scores held-out views |
| `scripts/`, `tests/test_gsplat_gpu.py` and others | same | names only |
| `godot/addons/gdgs` | `godot_template/addons/gdgs` | none (GDGS 3.3.0, MIT, ReconWorldLab) |

Not needed here: video frames, COLMAP, alignment, pose refinement, TSDF collision.
