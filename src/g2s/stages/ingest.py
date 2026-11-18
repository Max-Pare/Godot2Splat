"""Stage 2: turn the Godot capture into what training reads.

Capture folder (written by res://addons/godot2splat/capture/capture_runner.gd):
  capture.json        image size, field of view, one entry per view with its camera
  images/NAME.png     colour, as the game shows it (tonemapped, sRGB)
  depth/NAME.bin      half-float RGB, see geometry.decode_depth
  sky/NAME.png        six views of the environment alone

Result in the ingest folder:
  scene.npz           names, split, K, sizes, c2w, points, colors, depth_names, depth_unit
  depth/NAME.png      uint16 counts of `depth_unit` metres, 0 = nothing there (sky)
  sky.png             equirectangular sky; only when some view sees the sky
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .. import geometry
from ..config import Config
from ..workspace import Workspace


def load_capture(ws: Workspace) -> dict:
    return json.loads((ws.dir("capture") / "capture.json").read_text())


def images(ws: Workspace) -> Path:
    return ws.dir("capture") / "images"


def depth_maps(ws: Workspace) -> Path:
    return ws.dir("ingest") / "depth"


def load_scene(ws: Workspace) -> dict:
    return dict(np.load(ws.dir("ingest") / "scene.npz", allow_pickle=False))


def read_depth_file(path: Path, width: int, height: int) -> np.ndarray:
    """Raw capture depth image as [height,width,3] half floats."""
    raw = np.fromfile(path, dtype="<f2")
    if raw.size != width * height * 3:
        raise SystemExit(f"ingest: {path} has {raw.size} values, expected {width}x{height}x3")
    return raw.reshape(height, width, 3)


def run(ws: Workspace, cfg: Config) -> None:
    import cv2

    c = cfg.ingest
    src = ws.dir("capture")
    cap = load_capture(ws)
    w, h = (int(v) for v in cap["image_size"])
    dw, dh = (int(v) for v in cap["depth_size"])
    K = geometry.intrinsics(w, h, float(cap["fov_y"]))
    K_depth = geometry.scale_K(K, dw / w, dh / h)
    views = cap["views"]

    c2ws = [geometry.camera_from_godot(v["transform"]) for v in views]
    depths_mm = []
    for v in views:
        try:
            depths_mm.append(geometry.decode_depth(
                read_depth_file(src / "depth" / f"{v['name']}.bin", dw, dh), float(cap["far"])))
        except ValueError as err:
            raise SystemExit(
                f"ingest: depth of view {v['name']}: {err}. The renderer altered the depth pass "
                f"output; physical light units or a custom exposure on the project are likely causes.")
    unit_mm = geometry.depth_unit_for(max(int(d.max()) for d in depths_mm))

    d = ws.reset("ingest")
    (d / "depth").mkdir()
    # Start points gather per view and are thinned together whenever they have doubled
    # since the last thinning: thinning (or joining) them after every view made ingest
    # grow with the square of the views (2,000 views took most of half an hour).
    chunks_p, chunks_c = [np.zeros((0, 3))], [np.zeros((0, 3), dtype=np.uint8)]
    gathered, limit = 0, 4 * c.max_points
    empty_views = 0
    for v, c2w, mm in zip(views, c2ws, depths_mm):
        counts = np.rint(mm / unit_mm).astype(np.uint16)
        cv2.imwrite(str(d / "depth" / f"{v['name']}.png"), counts)
        empty_views += bool((counts == 0).any())
        if v["split"] != "train":
            continue
        # Start points for training: scene surfaces seen in this view, coloured from it.
        bgr = cv2.imread(str(src / "images" / f"{v['name']}.png"))
        if bgr is None:
            raise SystemExit(f"ingest: missing image {v['name']}.png")
        small = cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_AREA)
        sparse = np.zeros_like(mm)
        sparse[::c.point_stride, ::c.point_stride] = mm[::c.point_stride, ::c.point_stride]
        p, vu = geometry.backproject(sparse / 1000.0, K_depth, c2w)
        p, rgb = geometry.thin(p, small[vu[:, 0], vu[:, 1], ::-1], c.voxel)
        chunks_p.append(p)
        chunks_c.append(rgb)
        gathered += len(p)
        if gathered > limit:
            pts, cols = geometry.thin(np.concatenate(chunks_p), np.concatenate(chunks_c), c.voxel)
            chunks_p, chunks_c, gathered = [pts], [cols], len(pts)
            limit = max(4 * c.max_points, 2 * len(pts))
    pts, cols = geometry.thin(np.concatenate(chunks_p), np.concatenate(chunks_c), c.voxel)
    if len(pts) > c.max_points:
        idx = np.random.default_rng(0).choice(len(pts), c.max_points, replace=False)
        pts, cols = pts[idx], cols[idx]

    split = np.array([v["split"] for v in views])
    n_train = int((split == "train").sum())
    if n_train < c.min_views:
        raise SystemExit(f"ingest: only {n_train} training views; enlarge the capture volume, lower "
                         f"its camera spacing, or check that its start point is in open air")

    has_sky = empty_views > 0 and bool(cap.get("sky"))
    if has_sky:
        faces = [cv2.imread(str(src / "sky" / f"{s['name']}.png")) for s in cap["sky"]]
        if any(f is None for f in faces):
            raise SystemExit("ingest: sky pictures missing from the capture")
        pano = geometry.cube_to_panorama(
            faces, [geometry.camera_from_godot(s["transform"]) for s in cap["sky"]],
            c.sky_width, c.sky_width // 2)
        cv2.imwrite(str(d / "sky.png"), pano)

    np.savez(
        d / "scene.npz",
        names=np.array([f"{v['name']}.png" for v in views]),
        depth_names=np.array([f"{v['name']}.png" for v in views]),
        split=split,
        K=np.tile(K, (len(views), 1, 1)),
        sizes=np.tile([w, h], (len(views), 1)),
        c2w=np.array(c2ws),
        points=pts, colors=cols.astype(np.uint8),
        depth_unit=np.float64(unit_mm / 1000.0),
    )
    print(f"ingest: {n_train} training views, {len(views) - n_train} held out, {w}x{h}; "
          f"{len(pts)} start points; depth in steps of {unit_mm} mm; "
          f"{'sky seen in ' + str(empty_views) + ' views' if empty_views else 'no sky visible'}")
