"""Stage 4: tidy the trained splat.

1. Drop Gaussians that only add haze: near-transparent, oversized, needle-shaped, isolated.
2. Drop Gaussians floating in space that the scene's depth maps show as empty.

Later stages read the result through `output()`.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .. import device as device_mod
from .. import geometry, splat_io
from ..config import Config
from ..workspace import Workspace
from . import ingest, train


def output(ws: Workspace, name: str) -> Path:
    """Path of a splat file for later stages: the cleaned one when it exists."""
    cleaned = ws.root / "clean" / name
    return cleaned if cleaned.exists() else ws.dir("train") / name


def haze_mask(means: np.ndarray, sigma: np.ndarray, opacity: np.ndarray, cams: np.ndarray, c) -> dict:
    """Boolean masks (True = remove) per reason. `sigma` [N,3] are linear sizes.

    Size limits grow with the distance to the nearest camera: a 5 m Gaussian is a
    blob inside the room and a normal piece of the view through a window.
    """
    from scipy.spatial import cKDTree

    s = np.sort(sigma, axis=1)
    big, mid = s[:, 2], s[:, 1]
    dist = cKDTree(cams).query(means)[0]
    reasons = {
        "transparent": opacity < c.min_opacity,
        "oversized": big > np.maximum(c.max_size, c.max_size_per_m * dist),
        "needle": (big > c.needle_ratio * np.maximum(mid, 1e-9))
                  & (big > np.maximum(c.needle_length, c.needle_length_per_m * dist)),
    }
    gone = reasons["transparent"] | reasons["oversized"] | reasons["needle"]
    # Isolation is judged among the Gaussians that survive the tests above.
    lonely = np.zeros(len(means), dtype=bool)
    keep = np.flatnonzero(~gone)
    if len(keep) > 4:
        gap = cKDTree(means[keep]).query(means[keep], k=4)[0][:, 1:].mean(axis=1)
        limit = np.maximum.reduce([np.full(len(keep), c.isolated_gap), 3 * big[keep],
                                   c.isolated_gap_per_m * dist[keep]])
        lonely[keep] = gap > limit
    reasons["isolated"] = lonely
    return reasons


def _neighbourhood(depth, size: int = 3):
    """Per pixel: nearest and farthest valid depth in a size x size window (0 = no data)."""
    import torch
    import torch.nn.functional as F

    d = depth[None, None]
    pad = size // 2
    far = F.max_pool2d(d, size, stride=1, padding=pad)[0, 0]
    inf = torch.where(d > 0, d, torch.full_like(d, float("inf")))
    near = -F.max_pool2d(-inf, size, stride=1, padding=pad)[0, 0]
    return near, far


def free_space_mask(means, depths, Ks, c2ws, margin: float, margin_rel: float,
                    min_views: int, min_share: float):
    """True for points that lie well in front of the scene surface in most of the
    views that see them. `depths` [V,h,w] in scene units (0 = no data), `Ks` for the
    depth maps, cameras with OpenCV axes. Torch tensors on one device.

    The nearest depth in a 3x3 window is used, so points on the edge of a foreground
    object are not judged against the background behind it.
    """
    import torch

    n = len(means)
    free = torch.zeros(n, device=means.device)
    seen = torch.zeros(n, device=means.device)
    for depth, K, c2w in zip(depths, Ks, c2ws):
        h, w = depth.shape
        cam = (means - c2w[:3, 3]) @ c2w[:3, :3]
        z = cam[:, 2]
        u = (cam[:, 0] / z.clamp_min(1e-6) * K[0, 0] + K[0, 2]).floor().long()
        v = (cam[:, 1] / z.clamp_min(1e-6) * K[1, 1] + K[1, 2]).floor().long()
        ok = (z > 0.05) & (u >= 0) & (u < w) & (v >= 0) & (v < h)
        near, _ = _neighbourhood(depth)
        idx = (v.clamp(0, h - 1), u.clamp(0, w - 1))
        d_here, d_near = depth[idx], near[idx]
        ok &= (d_here > 0) & torch.isfinite(d_near)
        tol = torch.maximum(torch.full_like(z, margin), margin_rel * d_near)
        seen += (ok & (z < d_here + tol)).float()   # not hidden behind the surface
        free += (ok & (z < d_near - tol)).float()
    return (free >= min_views) & (free >= min_share * seen.clamp_min(1))


def _depth_views(ws: Workspace, scene: dict, device):
    """Every view's depth map (scene units), its intrinsics and its pose."""
    import cv2
    import torch

    unit = float(scene["depth_unit"])
    depths, Ks = [], []
    for i, name in enumerate(scene["depth_names"]):
        counts = cv2.imread(str(ingest.depth_maps(ws) / str(name)), cv2.IMREAD_UNCHANGED)
        if counts is None:
            raise SystemExit(f"clean: missing depth map {name}")
        w, h = scene["sizes"][i]
        depths.append(torch.from_numpy(counts.astype(np.float32) * unit).to(device))
        Ks.append(geometry.scale_K(scene["K"][i], counts.shape[1] / w, counts.shape[0] / h))
    return (depths, torch.tensor(np.array(Ks), dtype=torch.float32, device=device),
            torch.tensor(scene["c2w"], dtype=torch.float32, device=device))


def run(ws: Workspace, cfg: Config) -> None:
    import torch

    c = cfg.clean
    scene = ingest.load_scene(ws)
    ckpt = torch.load(ws.dir("train") / "ckpt.pt", map_location="cpu")
    arr = {k: v.numpy() for k, v in ckpt.items()}
    n0 = len(arr["means"])
    stats = {"before": n0}

    reasons = haze_mask(arr["means"], np.exp(arr["scales"]), splat_io._sigmoid(arr["opacities"]),
                        scene["c2w"][:, :3, 3], c)
    gone = np.zeros(n0, dtype=bool)
    for name, mask in reasons.items():
        stats[name] = int((mask & ~gone).sum())
        gone |= mask

    if c.carve:
        device = device_mod.resolve(cfg.device)
        depths, Ks, c2ws = _depth_views(ws, scene, device)
        with torch.no_grad():
            floating = free_space_mask(
                torch.tensor(arr["means"], dtype=torch.float32, device=device), depths, Ks, c2ws,
                c.margin, c.margin_rel, c.carve_min_views, c.carve_min_share).cpu().numpy()
        stats["in_free_space"] = int((floating & ~gone).sum())
        gone |= floating
    arr = {k: v[~gone] for k, v in arr.items()}

    stats["after"] = len(arr["means"])
    d = ws.reset("clean")
    torch.save({k: torch.from_numpy(v) for k, v in arr.items()}, d / "ckpt.pt")
    splat_io.write_ply(d / "scene.ply", **{k: arr[k] for k in train.PARAM_NAMES})
    (d / "clean.json").write_text(json.dumps(stats, indent=2))
    removed = ", ".join(f"{stats[k]} {k.replace('_', ' ')}" for k in
                        ("transparent", "oversized", "needle", "isolated", "in_free_space") if k in stats)
    print(f"clean: {n0} Gaussians -> {stats['after']} (removed {removed})")

    from .. import evaluate
    evaluate.report(ws, cfg, "clean")
