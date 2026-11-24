# Project notes

Working notes: what was found, what went wrong, what is still open. README.md describes how
to use the tool; this file records the state of the work. Last updated 2026-11-18.

## Open problems

### Training collapses on large Unreal levels (second cause fixed, to confirm on the 5090)

**Second cause: the position learning rate.** 5090, same capture, `--cap 4000000 --steps
40000`: faint share 30% at 5.5k, 57% at 10k, 85% at the end; loss and depth error climbing
from 7.5k (single views 2-3 m off). Held-out 13.63 dB; clean left 3 Gaussians (3.4M
transparent, 560k hidden) and export replaced the splat in the Godot project with them.

Reproduced on this machine with the old corridor capture (720 views, 50% size, 4M cap):
a 40k-step schedule cut at 15k gives 49% faint at 14k and climbing, 830k hidden, 17.8 dB.
The same at 12k steps stays at 28%: the long schedule is what hurts. The position learning
rate (`1.6e-4 x scene scale`, decaying to 1% over the run) and with it MCMC's position
noise were scaled to the camera spread, 37 m here: right for cameras around an object,
ten times too much for cameras inside a level, whose walls are ~2 m away. A long run keeps
it high for longer and knocks Gaussians off the surfaces.

| 40k schedule, cut at 15k, 4M cap | faint at 14k | hidden (clean) | held-out train / clean |
|---|---|---|---|
| scale = camera spread (37 m) | 49%, climbing | 830k | 17.83 / 15.81 dB |
| scale = viewing distance (1.8 m) | 14%, flat | 65k | 20.08 / 19.51 dB |

Fix (`train.viewing_distance`): scene scale is the median depth the cameras see, if smaller
than the spread. Tried before and dismissed at 5k steps (no difference there); the
difference only shows in long runs. Also tried: handing Gaussians that no view has shown
for two passes to MCMC (hidden ones never fade, so relocation picks them more): it moved
only 1,800 of 510k "hidden"; those still draw a little, so not the cause. Reverted.

Also fixed on the way:
- The held-out score laid the render over the photo, so a missing surface scored as
  perfect: 3 Gaussians scored 85 dB. Now over the background colour Godot shows. Scores
  before 2026-11-18 are too high wherever the splat had holes, and clean "improving" PSNR
  (strays below, 19.96 -> 20.40) was partly that: with the honest score clean costs 0.6 to
  1.2 dB on the corridor. Which of its removals costs most is not measured yet; the
  transparent ones (under 0.02 opacity, a million or more) are the first suspect.
- Clean stops before export when it keeps under 5% (`CleanCfg.min_kept_share`): the Godot
  project keeps its previous splat.

First fix, 5090 rerun, 2026-11-18: new capture of the same level (390 positions, 2,106
training views, 800 px), default 20k steps, cap 1.2M. Faint share 11% at 500, peak 32% at
9-10k, down to 25% when relocation stopped (16.6k), 32% at the end. Held-out PSNR 21.20 dB
after training (was 18.50), 22.08 dB after clean (was 19.00). Clean removed 385,955 as
transparent (was 898,765) and 101,185 hidden; 669,464 Gaussians exported (was 240,733).
The rise after relocation stops comes from the picture itself (no regulariser then):
Gaussians it pushes transparent are no longer recycled. Clean drops them.

**First cause: the opacity regulariser.** It is a tiny, steady push down on opacity, and Adam
turns a push with nothing against it into a full-size step. Before the fix, every Gaussian
in the camera's view got it, also those hidden behind walls, which the picture gives no
gradient. In a level of many rooms each frame has most of the level in view, hidden, so
opacity fell all through training. MCMC relocation hid it while it ran (it moves the dead
ones), then the share jumped once relocation stops at 83% of the steps. 80k steps leave
14k steps of fading after that: the collapse.

5090 rerun, default 20k steps (2,106 views): faint share 11% at step 500, 57% by 13k,
jump to 66% at 17k (relocation stopped at 16.6k), 75% at the end. 18.50 dB held out
(19.00 after clean), cleanup removed 898,765 as transparent.

A/B on this machine, old corridor capture (720 views, 50% picture size), share of
Gaussians under 0.02 opacity:

| Run (steps) | step 4k | step 8k | step 9k | end | Held-out PSNR (train) |
|---|---|---|---|---|---|
| before the fix (10k) | 11% | 20% | 28% | 28%+ | 20.00 dB |
| MCMC position noise / 10 (10k) | 11% | 16% | 21% | | 19.77 dB |
| no opacity regulariser (10k) | 3% | 6% | 7% | 8% | 20.02 dB |
| regulariser only on Gaussians that drew something (10k) | 7% | 15% | 20% | 25% | 19.84 dB |
| that, and no regulariser after relocation stops (20k): **the fix** | 10% at 5k | 23% at 10k | 17% at 15k | 21% | 19.84 dB |

The fix (`train.regularise`): the regulariser goes only to Gaussians whose opacity got a
gradient from the picture, and stops when relocation stops. Held-out PSNR on this capture
stays near 20 dB whatever the run; it is limited by the capture (720 Box views, held-out
views looking at blown-out sky), not by the fading.

Earlier investigation, kept for reference:

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

### Corridor splat brighter and flatter than the level (fixed, to confirm on the 5090)

Seen in Godot on the 5090 corridor splat: much brighter and flatter than the level in
Unreal, lights still visible. The capture pictures themselves were like that (eval pictures:
windows and sunlit floor clipped to white), so not training or Godot. Cause: the exposure
meter ignored the level's post process volumes. `PPV_Global` and the hallway volumes hold
auto exposure to EV100 8 (9 in the green hallway) to 12. The meter measured 4.78 at
`SplatStart` and used it: every picture 3.2 stops (about 9x) too bright, highlights pushed
into the tonemapper's shoulder, so the contrast went flat.

Fix (`LevelExposureAt` in CaptureJob.cpp): the meter blends the level's volumes at the start
point the way the renderer does (priority, blend weight and radius) and clamps the measured
EV100 to their Min/Max. Their Exposure Compensation is used too; the capture volume's
`Exposure Compensation` is now added on top, default 0 (was 1, Unreal's default, which the
level's value now supplies). The hallways also differ in colour grading; that stays per
view, as in the game.

Still to confirm on the 5090: the capture log should say "measured at the start point as
4.78, held to the level's range 8.00 to 12.00".

### Strays outside the level (fixed)

Seen in Godot on the 5090 corridor splat: the splat node's bounding box many times larger than
the level, and collision generated with the gdgs panel coming out as one big block (gdgs
sizes its voxels from that box: longest side / 128). Cause: Gaussians no capture view shows,
above ceilings, below floors and inside walls. Training never sees them, so they keep
whatever they drifted to. On the old corridor run 20% of the cleaned splat was such strays;
40 of them more than 10 m out stretched the box to 70 x 44 x 105 m around a level 4 m tall.

Clean now drops them (`hidden`, `CleanCfg.drop_hidden`): behind the surface by more than
10 cm / 5% in every view that has them in its picture. 20k-step corridor run: 98,975
removed, held-out PSNR 19.96 -> 20.40 dB (old score, too kind to holes; see above), box
34.5 x 14.2 x 71.1 -> 33.2 x 4.4 x 62.3 m.

Also from that look: the splat sat ~90 m from the Godot origin, where the level is in
Unreal's world. Unreal exports now put the capture volume's position (the start actor) at
the Godot origin; the manifest keeps the Unreal position as `source.origin`.

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
- First-person Player (from Real2Virtu) in the generated scene, camera at the start point,
  flying when there is no collision. Checked headless (build, rebuild keeps it, runs without
  script errors); not yet walked by hand. The pipeline test assertions for it have not run.
- Rebuilds keep what the gdgs panels add to `Splat` (CollisionBody, CollisionSeed, lighting,
  relight and panel settings), in the same place in the world when the splat's centre moves.
  Checked headless with a hand-made CollisionBody and seed; not yet with a real gdgs collision.
- Performance overlay on the Player (F3). Checked in an off-screen window on the 5060 Laptop with the
  4.1M-Gaussian corridor: GPU 7.8 ms (0.1 ms with 50 Gaussians, so the splat is counted), VRAM
  3.41 GiB of which buffers 2.24 GiB (gdgs's), 60 fps under vsync. F3 toggling checked headless.
  Godot's process time includes the vsync wait, so the CPU line shows render and physics only.
- 5090 setup: `scripts/setup_windows.ps1`, `scripts/pack.ps1`, README "Running on another machine".
- Smaller splat files, nothing lost. The PLY no longer has the normals (always zero, unread):
  the corridor's 603 MiB became 574 MiB. gdgs stored each splat twice in its imported `.res`
  (`point_data_byte` was `point_data_float.to_byte_array()`); now once. Corridor, in a scratch
  project: `.res` 1197 MiB to 613 MiB, load 535 ms to 200 ms, same bytes, same `xyz` and box,
  collision snapshot fine; an old `.res` loads and takes 612 MiB of RAM instead of twice that.
  Godot's own compression (`ResourceSaver.FLAG_COMPRESS`) was tried first: 5.5% smaller and
  loading 6.5 times slower (0.36 s to 2.3 s), so it is not used. Trained floats barely compress
  (zlib 91%, lzma 89.5%, 81% with the bytes regrouped), so this is about all a lossless change gets.

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
