# godot2splat

Scans a 3D Godot scene into a Gaussian splat and puts the result back into the same project as a ready-to-use scene.

Godot renders the scene from many viewpoints (colour and exact depth), gsplat trains a splat from those pictures, and the splat is imported back through the GDGS addon.
Version 1 is for interiors and walkable levels: from a start point you place, cameras fill all the open space they can reach, without going out through windows into the open. Or they stay inside a box you size.

The scan also works on Unreal Engine 5 levels: an Unreal plugin takes the pictures, and the splat goes into a Godot project. See "Scanning an Unreal level" below.

The training, cleanup and Godot display code is taken from [Real2Virtu](https://github.com/Max-Pare/Real2Virtu), which does the same for phone scans of real rooms. See "Taken from Real2Virtu" below.

## Status

Verified on the test room in `tests/fixture_project` (6 x 5 m room, a window to the sky, five props, sun and lamp with shadows, filmic tonemapping), RX 7800 XT, Godot 4.7.2, 640 px pictures:

| Check | Result |
|---|---|
| Capture | 402 pictures from 67 camera positions in 7 s; every camera at least 0.30 m from a surface |
| Depth and camera conventions | 40 million depth pixels pushed back into the world: median 0.2 mm, worst 1.0 mm from the room's true surfaces |
| Training, default size (20,000 steps, 8.5 min) | 38.49 dB PSNR on 48 held-out pictures after cleanup (median picture 39.0, worst 25.8), depth error 1.2 cm, 1,200,000 Gaussians cut to 854,999 by cleanup for 0.09 dB |
| Training, short (3,000 steps, 0.8 min) | 37.12 dB on the same pictures |
| Godot shows the generated scene (3,000-step splat) | 34 to 41 dB PSNR against the scan pictures on six held-out views |
| Editor button | ran through its test hook: starts g2s, shows its output, imports the splat, builds and opens the scene |

The worst held-out picture looks up at the underside of the table from close by. Few cameras see that surface, and it comes out soft.

The rows above predate Fill placement, which is now the default; they used a box. On Windows 11 with an RTX 5060 Laptop GPU, Godot 4.7.2 and Unreal 5.8.4, with Fill placement:

| Check | Result |
|---|---|
| Godot pipeline test (`pytest -m godot`, 2,500 steps) | passes: cameras in open air and none outside the window, held-out PSNR over 32 dB, Godot shows the scanned room |
| Unreal capture of the same room (`tests/unreal/build_room.py`) | 300 pictures from 50 positions in 96 s, all inside the room (5 cells past the window rejected as outdoors); 5.8 million depth pixels median 0.2 mm, worst 3.5 mm (the engine's low-poly sphere) from the room's true surfaces |
| Unreal pipeline test (`pytest -m unreal`, 2,500 steps) | 41.2 dB PSNR on 36 held-out pictures (worst 32.6), depth error 1.0 cm; scene built in the Godot project |
| A large Unreal level (World Partition, about 60 x 30 m of corridors), started from an actor | ran end to end. With the old fixed limit of 120 positions (one per 10 reachable spots) it scored only 19 dB, so the limit is now set from memory (390 positions at 800 px) |

Not verified: scenes other than the test rooms (beyond the run above), the Mobile renderer, Linux with Unreal, and clicking either editor button by hand.

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
   - Its position is the start point and must be in open air, at least `Min Clearance` from any surface. Cameras spread from there through everything they can see.
   - `Placement` Fill (the default): the open space is filled cell by cell, `Camera Spacing` apart, however far it goes (up to `Fill Reach`). A cell that sees mostly sky (`Outdoor Sky Share`) counts as outdoors, and the fill stops there, so it does not leave an interior through its windows or doors. Box: cameras stay inside the box (`Size`). Either way, what lies beyond is still captured when it can be seen.
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
| Placement | Fill | Fill: fill the reachable open space; Box: only inside the box |
| Fill Reach | 150 m | Fill: cameras stay within this distance of the volume |
| Outdoor Sky Share | 0.3 | Fill: a cell that sees more than this share of sky is outdoors and stops the fill; 1 never stops |
| Size | 10 x 4 x 10 m | Box: box in which cameras are placed |
| Camera Spacing | 1 m | distance between camera positions; each gives 6 pictures |
| Min Clearance | 0.3 m | cameras keep this distance from every surface |
| Max Positions | 0 | more than this are thinned out evenly. 0: as many as training holds in memory, 1.5 gigapixels of pictures (390 positions at 800 px, 610 at 640 px, about 6 GB of RAM). Raise it on a machine with more memory |
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

## Scanning an Unreal level

Only the scan runs in Unreal. The `G2SCapture` plugin (`unreal/G2SCapture`) writes the same capture folder as the Godot scan, already turned into Godot's axes and metres, so training and cleanup are shared. The splat scene is built in a Godot project you name.

Needs Unreal Engine 5.8 (other 5.x versions untested) with its C++ toolchain: on Windows, Visual Studio 2022 or its Build Tools with the "Desktop development with C++" workload and the .NET Framework 4.8 SDK. Godot is still needed for the last stage.

1. Compile the plugin into the Unreal project (about 2 minutes; again after updating g2s), then restart the editor if it is open:

   ```
   g2s install "path/to/MyGame.uproject"
   ```

2. Either place a **Splat Capture Volume** actor in the level (same settings as the Godot node, in centimetres; set **Godot Project** under Output) and press **Scan to Splat** in the level editor toolbar, or run:

   ```
   g2s build "path/to/MyGame.uproject" /Game/Maps/MyLevel --godot-project path/to/godot_project
   ```

   Without a volume in the level, start from any actor instead. The level is not changed; cameras fill the open space reachable from that actor:

   ```
   g2s build "path/to/MyGame.uproject" /Game/Maps/MyLevel --origin PlayerStart --godot-project path/to/godot_project
   ```

3. Open `res://splats/<level>/<level>_splat.tscn` in the Godot project.

`g2s build` opens a second Unreal editor with a window for the scan and closes it afterwards. It works while your own editor has the project open; the scan uses the level as saved on disk.

Unreal-only `g2s build` options: `--origin <actor label>`, `--box auto|LxWxH` (`auto`: Fill placement, the default with `--origin`; LxWxH: Box placement, metres along Unreal's X, Y, Z), `--set Name=Value` for any volume setting (`--set MaxPositions=200 --set ImageSize=640`), `--unreal <UnrealEditor-Cmd>` (default: the engine the `.uproject` names).

How the Unreal scan differs from the Godot one:

- The editor's level is scanned, not a running game: nothing moves anyway, and Blueprints' construction scripts have run, their BeginPlay has not.
- Each view is rendered `Warmup Frames` times (default 16) before the picture is taken, so that temporal anti-aliasing and Lumen settle after the camera moves.
- Exposure is the same for every picture. It is measured once at the start point, the way Unreal's auto exposure would (plus `Exposure Compensation`, default 1 like Unreal's), unless a post process volume there sets Manual exposure. Tick `Exposure EV100` on the volume to set it yourself.
- Switched off unless `Keep Screen Space Effects`: screen-space reflections, SSAO, bloom, lens flares, depth of field, motion blur, vignette, grain, chromatic aberration.
- World Partition levels: the region around the volume or `--origin` actor is loaded for the scan (`Fill Reach` plus 100 m around it).
- The sky picture shows Sky Atmosphere, fog, meshes whose material is marked Is Sky, and meshes larger than `Far Clip`.
- Collision is not carried over: the generated Godot scene has no `Collision` node.

## Running on another machine

For example scans and training on a PC with a bigger GPU (Windows, NVIDIA). RTX 50-series cards need the CUDA 12.8 torch build that `--extra gpu` installs.

1. Get the repository there, either way:
   - `git clone <repository URL>`: works once your changes are committed and pushed.
   - Copy it without the caches and the Python environment, as a zip or straight to a USB drive or network share:

     ```
     scripts\pack.ps1                              # zip next to the repository
     scripts\pack.ps1 E:\godot2splat               # or a folder; a path ending in .zip writes a zip there
     ```

     It leaves out `.venv`, `tools\`, `.g2s\` and other caches, `__pycache__`, and the generated folders of `tests\unreal_fixture`. `-DryRun` lists what it would copy.

2. On the other PC, from the repository folder:

   ```
   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
   ```

   It installs `uv` for the current user if it is missing, runs `uv sync --python 3.12 --extra gpu --extra dev`, checks that torch sees the GPU and gsplat imports, compiles and tests gsplat's kernels, and looks for the CUDA Toolkit, Visual Studio 2022 C++ tools, the .NET Framework 4.8 SDK, Godot (`G2S_GODOT`) and Unreal Engine 5.8. It installs nothing system-wide: it ends with a list of what is missing and the command for each. `-DryRun` only checks; `-NoUnreal` skips the Unreal checks.

3. Install into the projects on that PC. The Unreal plugin is compiled there, so `g2s install` must run on each machine, even when the Unreal project was copied with the plugin already in it:

   ```
   g2s install "D:\Projects\MyGame\MyGame.uproject"
   g2s install D:\Projects\godot_project
   ```

4. Scan: `g2s build ...` as above. With more VRAM, raise `--cap` and the number of camera positions (`--set MaxPositions=...`, or a smaller `Camera Spacing`) for large levels.

The Unreal and Godot projects being scanned are not part of this repository: copy them to that PC too (or have them there already), opened with the same engine versions (Unreal 5.8, Godot 4.3 or newer). Their paths may differ from this PC's: pass the paths on that PC to `g2s install` and `g2s build`, and check the **Godot Project** setting of any Splat Capture Volume placed in an Unreal level.

## How it works

**Capture.** Godot loads the scene, lets it run for a few frames, then freezes it (scripts, physics, animations, particles, shader time). Camera positions come from a flood fill of free space that uses rendered depth only, so it works with anything that draws and needs no collision shapes: candidate points sit on a grid (inside the box, or without end for Fill), found as the walk reaches them; from each accepted point a small depth cube map shows which neighbours are in view and how close the nearest surface is. Points inside walls or solid objects are hidden from every point in open air and are never reached. At each position the six faces of a randomly turned cube are photographed (90 degree field of view).

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

- Surfaces that few cameras see, such as the underside of low furniture, come out soft. Lower `Camera Spacing` to get more positions near them.
- A static snapshot: moving objects, animated lights and particles are frozen as they were after `Settle Frames`.
- Transparent materials write no depth. Glass is learned as a tint on what lies behind it. Mirrors and reflections are approximated by view-dependent colour.
- SDFGI lighting moves with the camera and settles over many frames; expect shimmer, or raise `Warmup Frames` to 30 or more. Baked lightmaps and VoxelGI capture cleanly. The scan prints a note when SDFGI or volumetric fog is on.
- Meshes you add to the generated scene are lit and tonemapped by the generated (linear) environment, not the source one.
- Collision: only `StaticBody3D` nodes with `CollisionShape3D` children are copied, read from the scene file. Bodies created by scripts, CSG and GridMap collision are not.
- Projects that use physical light units or a custom exposure may alter the depth pass; `ingest` then stops with a message instead of using wrong depth.
- Very close to large splats (a camera inside a wall, a very high resolution), the renderer can run out of tile sort space. It then leaves out some splats, so the picture gets softer; before the fix it showed flat grey squares. Projects set up before the fix need `g2s install <project> --force` to get it.
- The gdgs importer claims every `.ply` file in the project, and takes about 1.5 minutes per million Gaussians on first import. The editor does not respond during that time.
- Cameras fill the space in 3D, so a large level with a small spacing reaches `Max Positions` quickly. The positions are then thinned evenly, which lowers detail everywhere. A machine with more RAM and VRAM can take a higher `Max Positions` and `--cap`.
- Fill tells indoors from outdoors by how much sky a cell sees. An interior with no sky behind its openings (a building inside a larger enclosed level) is not told apart: limit it with `Fill Reach` or Box placement.

## Tests

```
python -m pytest            # unit tests, a few seconds
python -m pytest -m godot   # the whole pipeline on the test room: Godot with a window, a short training
python -m pytest -m unreal  # the same room built in Unreal (tests/unreal/build_room.py), scanned there, shown in Godot
```

The Unreal test compiles the plugin into a copy of `tests/unreal_fixture` and builds the room map with Unreal's Python. Set `G2S_GODOT` (and `G2S_UNREAL` when the engine is not where the Epic launcher puts it).

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
| `godot/addons/gdgs` | `godot_template/addons/gdgs` | GDGS 3.3.0 (MIT, ReconWorldLab) with one fix: close to large splats the tile sort buffer overflowed and the picture showed flat grey squares; entries past its capacity are now dropped, and the buffer holds 16 entries per splat instead of 10 |

Not needed here: video frames, COLMAP, alignment, pose refinement, TSDF collision.
