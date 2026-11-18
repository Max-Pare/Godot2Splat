# Project notes

Working notes: what was found, what went wrong, what is still open. README.md describes how
to use the tool; this file records the state of the work. Last updated 2026-11-17.

## Open problems

### Training collapses on large Unreal levels (unsolved)

Run on the RTX 5090 machine: corridor level, Fill placement, 351 positions (2,106 training
views at 800 px), `--steps 80000`, default cap 1.2M.

- The loss never settled: 0.4 to 1.8 for the whole run (the test room ends near 0.05).
  The depth error swung between 15 cm and 3.8 m.
- Held-out PSNR was 11.5 dB after training. Cleanup removed 1,192,556 of 1,200,000 Gaussians
  as transparent (opacity under 0.02), leaving 639 large blobs: "a few massive white splats".

Tested on this machine (RTX 5060 Laptop) with the older corridor capture (720 views, Box
placement), training only, at 50% picture size:

| Run | Held-out PSNR | Transparent after training |
|---|---|---|
| 5,000 steps, current code | 21.44 dB | not measured |
| 5,000 steps, learning rate scaled to the viewing distance (1.8 m) instead of the camera spread (~33 m) | 21.54 dB | not measured |
| 20,000 steps, viewing-distance scale, depth term on (0.2) | 19.90 dB | 15.6% |
| 20,000 steps, viewing-distance scale, depth term off | 19.39 dB | 17.6% |
| 20,000 steps, full size, current code (the first corridor run) | 18.64 dB | 59% |

Conclusions so far:
- Not the learning-rate scale: changing it 18-fold changed nothing. The change was reverted, `train.py` is as before.
- Not the depth term: without it the result is slightly worse.
- Not reproduced here: no run on this machine collapsed. But held-out PSNR falls the longer
  training runs (21.4 dB at 5k steps, 19.9 dB at 20k). Something degrades with steps on large levels.
- The gsplat warnings in the 5090 log are harmless. The `_jit_compile() missing 2 required
  positional arguments` TypeError is caught and retried by gsplat itself (torch 2.11 added SYCL
  arguments). "Error checking compiler version for cl" is torch failing to run `cl.exe` from that
  shell. The kernels were already built, so training used them.

Next steps:
1. On the 5090, rerun the same capture with default steps (no `--steps`). Watch the train log
   for the step where the loss starts climbing, and whether cleanup still removes nearly everything.
   Every progress line now shows the share of "faint" Gaussians (opacity under 0.02, what
   cleanup drops). Note the step where that share starts rising.
2. Look at the 5090 capture pictures (`<project>/.g2s/DerelictCorridor/capture/images`). This
   capture is different from the one tested here (Fill, another project copy, start actor
   `SplatStart`). Check for black, white or blown-out frames and views inside geometry.
3. Suspects not yet tested: MCMC relocation spiralling once many Gaussians are dim; the cap
   (1.2M) too small for 2,106 views of a large level, so Gaussians are stretched over big
   areas; views that see mostly unlit, pure black areas.
4. Possible guard: stop or warn when the share of Gaussians under 0.02 opacity keeps rising,
   and keep the best checkpoint by held-out score.

### Corridor quality even without collapse

The first corridor scan (Box placement, 120 positions over a 60 x 30 m space) scored 19 dB.
Too few cameras: one per ten reachable spots. Fill and the memory-based `Max Positions`
were added for this. The 5090 run used them (351 positions) but collapsed, so the gain is unmeasured.

## Done and checked

- Unreal capture plugin (`unreal/G2SCapture`), UE 5.8.4, Windows. Unreal test room: 5.8 million
  depth pixels median 0.2 mm, worst 3.5 mm from the true surfaces (axes, depth, field of view
  correct). `pytest -m unreal` passes: 41.2 dB held out after 2,500 steps.
- Fill placement (voxel flood fill), Godot and Unreal: tested on the test rooms only. 50 positions,
  all inside, 5 cells past the window rejected as outdoors. `pytest -m godot` (6) and
  `pytest -m unreal` (4) pass.
- GDGS square glitches: the tile sort buffer overflowed near large splats. Fixed and reproduced
  both ways: forced overflow, before and after the fix. Normal renders are unchanged (78 dB or
  better against the old renderer).
- Godot `--import` crash on Windows on the first import that also enables the addons: g2s retries once.
- The viewports stop drawing during an Unreal scan (temporary Realtime override). The room capture ran fine with it.
- SplatObject (from Real2Virtu): highlight, motion, collision follow. Done in its own session.
- 5090 setup: `scripts/setup_windows.ps1`, `scripts/pack.ps1`, README "Running on another machine".

## Needs testing

- Fill on a real large level. The corridor run on the 5090 used it, but its training collapsed (above).
  Check its capture log line "the open space reachable from the start point spans ...": how many
  positions, and how many cells were rejected as too close or outdoors.
- Fill in an interior with no sky behind its openings (a building inside an enclosed level). The
  outdoor test cannot tell inside from outside there; only Fill Reach limits it.
- The Scan to Splat buttons clicked by hand, in Unreal and in Godot. Only the test hooks and the command line have run.
- Texture sharpness of Unreal captures with the viewports not drawing (`r.Streaming.FullyLoadUsedTextures`
  is forced on, but not checked on a level with streamed textures).
- Lumen-heavy levels: whether 16 warmup frames are enough per view. Look for noisy or flickering pictures.
- Unreal versions other than 5.8, Linux with Unreal, the Mobile renderer in Godot.
- World Partition: only the corridor level so far. Regions are loaded around the start actor, out to `Fill Reach` plus 100 m.
- Collision is not carried over from Unreal (the Godot scene has no `Collision` node).

## Hang-ups met on the way

- UnrealBuildTool needed the .NET Framework 4.8 SDK, which VS 2022 Build Tools did not include.
  It was installed on this machine; `scripts/setup_windows.ps1` checks for it.
- Unreal commandlets cannot place actors, so `tests/unreal/build_room.py` runs in the full editor
  (`-ExecutePythonScript`) and quits.
- Git Bash turns `/Game/...` into `C:/Program Files/Git/Game/...`. Run `g2s build` for Unreal maps from PowerShell or cmd.
- The 5090 setup once hit `LNK1104` building gsplat: another Python process held `gsplat_cuda.pyd`
  open. Close other g2s runs before the first build.
- A push from a hidden shell waits for the GitHub login; push from a normal terminal the first time.
