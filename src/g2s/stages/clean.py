"""Stage 4: tidy the trained splat.

1. Drop Gaussians that only add haze: near-transparent, oversized, needle-shaped, isolated.
2. Drop Gaussians floating in space that the scene's depth maps show as empty, and
   Gaussians no view shows (hidden behind the surface in every view).
3. With a budget (`keep`), keep only the Gaussians that add most to the pictures.

Later stages read the result through `output()`.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .. import device as device_mod
from .. import geometry, gsplat_compat, splat_io
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
    return free_and_hidden(means, depths, Ks, c2ws, margin, margin_rel, min_views, min_share)[0]


def free_and_hidden(means, depths, Ks, c2ws, margin: float, margin_rel: float,
                    min_views: int, min_share: float, hidden_margin: float = 0.1,
                    hidden_margin_rel: float = 0.05):
    """`free_space_mask`, and True for points that no view shows: outside every view,
    or behind the surface (by more than the hidden margins) in every view that has
    them in its picture. Training never sees these, so they keep whatever they drifted
    to: they are the strays above ceilings, below floors and inside walls. A point in a
    pixel without depth (sky, beyond the far clip) counts as shown."""
    import torch

    n = len(means)
    free = torch.zeros(n, device=means.device)
    seen = torch.zeros(n, device=means.device)
    shown = torch.zeros(n, dtype=torch.bool, device=means.device)
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
        shown |= ok & ((d_here <= 0) | (z < d_here + torch.maximum(
            torch.full_like(z, hidden_margin), hidden_margin_rel * d_here)))
        ok &= (d_here > 0) & torch.isfinite(d_near)
        tol = torch.maximum(torch.full_like(z, margin), margin_rel * d_near)
        seen += (ok & (z < d_here + tol)).float()   # not hidden behind the surface
        free += (ok & (z < d_near - tol)).float()
    return (free >= min_views) & (free >= min_share * seen.clamp_min(1)), ~shown


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


def contribution(arr: dict, c2ws: np.ndarray, Ks: np.ndarray, sizes: np.ndarray, device,
                 scale: float = 0.5) -> np.ndarray:
    """How much each Gaussian adds to the pictures: its blending weight (its opacity
    times the transparency in front of it) summed over every pixel of every view, the
    views rendered at `scale` of their size. LightGaussian's global significance
    (arXiv 2311.17245). With every colour 1 the picture is the sum of those weights,
    so the gradient of its sum by a Gaussian's colour is exactly that Gaussian's weight."""
    import torch
    gsplat_compat.prepare()
    from gsplat import rasterization

    def t(a):
        return torch.tensor(np.asarray(a), dtype=torch.float32, device=device)

    means, quats = t(arr["means"]), t(arr["quats"])
    scales, opacities = torch.exp(t(arr["scales"])), torch.sigmoid(t(arr["opacities"]))
    ones = torch.ones((len(means), 3), device=device, requires_grad=True)
    total = torch.zeros(len(means), device=device)
    for c2w, K, (w0, h0) in zip(c2ws, Ks, sizes):
        w, h = max(1, round(w0 * scale)), max(1, round(h0 * scale))
        image, _, _ = rasterization(
            means=means, quats=quats, scales=scales, opacities=opacities, colors=ones,
            viewmats=torch.linalg.inv(t(c2w))[None], Ks=t(geometry.scale_K(K, w / w0, h / h0))[None],
            width=w, height=h, packed=False)
        image.sum().backward()
        total += ones.grad.sum(dim=1) / 3
        ones.grad = None
    return total.cpu().numpy()


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
            floating, hidden = free_and_hidden(
                torch.tensor(arr["means"], dtype=torch.float32, device=device), depths, Ks, c2ws,
                c.margin, c.margin_rel, c.carve_min_views, c.carve_min_share,
                c.hidden_margin, c.hidden_margin_rel)
        floating, hidden = floating.cpu().numpy(), hidden.cpu().numpy()
        stats["in_free_space"] = int((floating & ~gone).sum())
        gone |= floating
        if c.drop_hidden:
            stats["hidden"] = int((hidden & ~gone).sum())
            gone |= hidden
    arr = {k: v[~gone] for k, v in arr.items()}

    if c.keep and len(arr["means"]) > c.keep:
        # Held-out views stay out of the choice, so the score below is a fair one.
        which = np.flatnonzero(scene["split"] == "train")
        print(f"clean: weighing {len(arr['means'])} Gaussians over {len(which)} views "
              f"to keep {c.keep}", flush=True)
        weight = contribution(arr, scene["c2w"][which], scene["K"][which], scene["sizes"][which],
                              device_mod.resolve(cfg.device))
        kept = np.sort(np.argsort(-weight)[:c.keep])
        stats["over_budget"] = len(arr["means"]) - c.keep
        stats["kept_share_of_the_pictures"] = float(weight[kept].sum() / max(float(weight.sum()), 1e-12))
        arr = {k: v[kept] for k, v in arr.items()}

    stats["after"] = len(arr["means"])
    d = ws.reset("clean")
    torch.save({k: torch.from_numpy(v) for k, v in arr.items()}, d / "ckpt.pt")
    splat_io.write_ply(d / "scene.ply", **{k: arr[k] for k in train.PARAM_NAMES})
    (d / "clean.json").write_text(json.dumps(stats, indent=2))
    removed = ", ".join(f"{stats[k]} {k.replace('_', ' ')}" for k in
                        ("transparent", "oversized", "needle", "isolated", "in_free_space", "hidden",
                         "over_budget")
                        if k in stats)
    print(f"clean: {n0} Gaussians -> {stats['after']} (removed {removed})")
    if "over_budget" in stats:
        print(f"clean: the {stats['after']} kept add {stats['kept_share_of_the_pictures']:.1%} "
              f"of what all of them added to the training pictures")

    from .. import evaluate
    evaluate.report(ws, cfg, "clean")
    if stats["after"] + stats.get("over_budget", 0) < c.min_kept_share * n0:
        # A healthy run keeps about half. Exporting this would replace the splat in the
        # Godot project with almost nothing.
        raise SystemExit(f"clean: only {stats['after']} of {n0} Gaussians are left; training failed. "
                         f"Nothing was exported: the Godot project keeps its previous splat. "
                         f"Check the train log's faint share and the pictures in {ws.root / 'eval'}.")
