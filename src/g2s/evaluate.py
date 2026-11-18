"""Scores a trained splat against captured pictures it was not trained on.

Renders the splat from the held-out cameras (rendering only, no training) and
compares with the pictures Godot rendered there:
  full colour  - as training sees it (view-dependent colour)
  base colour  - without the view-dependent part
Pixels where the scene has no surface (sky) are left out; where the splat is clear
on a surface, the background colour shows, as in Godot. Side-by-side pictures
(Godot | full colour | base colour) go to <workspace>/eval/<stage>.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import device as device_mod
from .config import Config
from .workspace import Workspace

N_PICTURES = 8


def psnr(a, b, mask=None) -> float:
    """Peak signal-to-noise ratio in dB of two images with values 0-1, over `mask`."""
    import torch

    err = (a - b) ** 2
    if mask is not None:
        err = err[mask]
    return -10 * torch.log10(err.mean().clamp_min(1e-10)).item()


def backdrop_colour(params):
    """What Godot shows where the splat is clear and no sky was captured: the
    opacity-weighted mean base colour (as export.mean_colour), a [3] tensor."""
    import torch

    from . import splat_io

    if len(params["means"]) == 0:
        return torch.zeros(3, device=params["means"].device)
    rgb = (0.5 + splat_io.SH_C0 * params["sh0"][:, 0, :]).clamp(0, 1)
    weight = torch.sigmoid(params["opacities"].flatten())[:, None] + 1e-9
    return (rgb * weight).sum(dim=0) / weight.sum()


def score(ws: Workspace, cfg: Config, stage: str) -> dict:
    """PSNR and depth error of the `stage` checkpoint ("train" or "clean")."""
    import cv2
    import torch

    from .stages import ingest, train

    dev = device_mod.resolve(cfg.device)
    scene = ingest.load_scene(ws)
    which = np.flatnonzero(scene["split"] == "test")
    held_out = len(which) > 0
    if not held_out:
        # Nothing was held out: fall back to a sample of the training views.
        which = np.flatnonzero(scene["split"] == "train")[::8]
    images, depths, _ = train.load_views(ws, scene, which)
    params = {k: v.to(dev) for k, v in
              torch.load(ws.dir(stage) / "ckpt.pt", map_location=dev).items()}
    degree = int(round((params["shN"].shape[1] + 1) ** 0.5)) - 1
    backdrop = backdrop_colour(params)
    unit = float(scene["depth_unit"])
    out = ws.root / "eval" / stage
    out.mkdir(parents=True, exist_ok=True)
    picked = set(np.linspace(0, len(which) - 1, min(N_PICTURES, len(which))).astype(int).tolist())

    def to_bgr(img):
        return cv2.cvtColor((img.cpu().numpy() * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)

    full, base, depth_err, names, sheets = [], [], [], [], {}
    with torch.no_grad():
        for n, i in enumerate(which):
            gt = images[n].to(dev).float() / 255
            h, w = gt.shape[:2]
            depth = depths[n].to(dev).float() * unit
            solid, _ = train.solid_and_empty(depth, (h, w))
            if not bool(solid.any()):
                continue
            c2w = torch.tensor(scene["c2w"][i], dtype=torch.float32, device=dev)
            K = torch.tensor(scene["K"][i], dtype=torch.float32, device=dev)
            a, alpha, _ = train.render(params, c2w, K, w, h, degree, mode="RGB+ED")
            b = train.render(params, c2w, K, w, h, 0)[0]
            # Only pixels with a surface are scored. Where the splat is clear there, the
            # game shows the scene's background colour, not the surface: score it so.
            # (Scoring over the picture itself gave an empty splat a perfect score.)
            a_rgb = (a[..., :3] + (1 - alpha) * backdrop).clamp(0, 1)
            b_rgb = (b[..., :3] + (1 - alpha) * backdrop).clamp(0, 1)
            full.append(psnr(a_rgb, gt, solid))
            base.append(psnr(b_rgb, gt, solid))
            depth_err.append(float(train.depth_loss(a[..., 3], depth)))
            names.append(Path(str(scene["names"][i])).stem)
            # Keep an even sample of the views, and always the worst one so far.
            if n in picked or full[-1] <= min(full):
                sheets[names[-1]] = (full[-1], n in picked,
                                     np.hstack([to_bgr(gt), to_bgr(a_rgb), to_bgr(b_rgb)]))
    worst = names[int(np.argmin(full))]
    for name, (_, sampled, sheet) in sheets.items():
        if sampled or name == worst:
            cv2.imwrite(str(out / f"{name}.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])
    result = {
        "stage": stage, "gaussians": int(len(params["means"])), "views": len(full),
        "held_out": held_out,
        "psnr": float(np.mean(full)), "psnr_worst": float(np.min(full)), "worst_view": worst,
        "psnr_base": float(np.mean(base)),
        "depth_error_m": float(np.mean(depth_err)),
        "pictures": str(out),
        "per_view": {name: round(v, 2) for name, v in zip(names, full)},
    }
    (out / "score.json").write_text(json.dumps(result, indent=2))
    return result


def report(ws: Workspace, cfg: Config, stage: str) -> dict:
    r = score(ws, cfg, stage)
    kind = "held-out views" if r["held_out"] else "training views (none were held out)"
    print(f"{stage}: {r['gaussians']} Gaussians scored on {r['views']} {kind}: "
          f"PSNR {r['psnr']:.2f} dB (worst view {r['worst_view']}: {r['psnr_worst']:.2f}), "
          f"base colour only {r['psnr_base']:.2f} dB, depth error {r['depth_error_m'] * 100:.1f} cm; "
          f"pictures in {r['pictures']}", flush=True)
    return r
