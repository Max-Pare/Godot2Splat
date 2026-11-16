"""Camera and depth maths shared by the stages. Numpy only.

Conventions on the Python side: cameras are camera-to-world matrices with OpenCV
axes (x right, y down, z forward); the world is Godot's (+Y up, metres). Pixel
centres sit at half-integers, as in gsplat. Depth is the distance along the
camera's forward axis, not along the ray.
"""
from __future__ import annotations

import math

import numpy as np

# A Godot camera looks along -Z with +Y up; OpenCV looks along +Z with +Y down.
GODOT_TO_CV = np.diag([1.0, -1.0, -1.0, 1.0])

# Depth files written by the Godot capture (depth_pass.gdshader): three half floats
# per pixel, all within 0..1. With mm the depth in whole millimetres:
# R = (mm div DEPTH_SPLIT) / DEPTH_SPLIT, G = (mm mod DEPTH_SPLIT) / DEPTH_SPLIT, B = metres / far.
DEPTH_SPLIT = 2048


def godot_transform(values) -> np.ndarray:
    """[4,4] matrix of a Godot Transform3D written as 12 numbers: the basis
    vectors x, y, z (the matrix columns), then the origin."""
    v = np.asarray(values, dtype=np.float64).reshape(4, 3)
    m = np.eye(4)
    m[:3, :3] = v[:3].T
    m[:3, 3] = v[3]
    return m


def camera_from_godot(values) -> np.ndarray:
    """OpenCV camera-to-world matrix of a Godot camera's global transform."""
    return godot_transform(values) @ GODOT_TO_CV


def intrinsics(width: int, height: int, fov_y_deg: float) -> np.ndarray:
    """Pinhole matrix of a Godot camera with a vertical field of view and square pixels."""
    f = (height / 2.0) / math.tan(math.radians(fov_y_deg) / 2.0)
    return np.array([[f, 0.0, width / 2.0], [0.0, f, height / 2.0], [0.0, 0.0, 1.0]])


def scale_K(K: np.ndarray, sx: float, sy: float) -> np.ndarray:
    """Intrinsics of the same view resized by (sx, sy)."""
    out = K.copy()
    out[0] *= sx
    out[1] *= sy
    return out


def backproject(depth_m: np.ndarray, K: np.ndarray, c2w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """World points for every pixel with depth > 0. Returns (points [M,3], pixel index [M,2] as v,u)."""
    v, u = np.nonzero(depth_m > 0)
    z = depth_m[v, u].astype(np.float64)
    cam = np.stack([(u + 0.5 - K[0, 2]) / K[0, 0] * z, (v + 0.5 - K[1, 2]) / K[1, 1] * z, z], axis=1)
    return cam @ c2w[:3, :3].T + c2w[:3, 3], np.stack([v, u], axis=1)


def thin(points: np.ndarray, colors: np.ndarray, voxel: float) -> tuple[np.ndarray, np.ndarray]:
    """Keep one point per voxel."""
    if len(points) == 0:
        return points, colors
    _, idx = np.unique(np.floor(points / voxel).astype(np.int64), axis=0, return_index=True)
    return points[idx], colors[idx]


def decode_depth(raw: np.ndarray, far: float) -> np.ndarray:
    """Depth in whole millimetres (int64, 0 = nothing there) from a capture depth
    image `raw` [h,w,3] of half floats; `far` is the capture camera's far plane.

    R and G hold the exact value. B repeats it at half-float precision and guards
    against a renderer that changed the numbers on their way to the image (exposure,
    tonemapping): the two must agree, otherwise ValueError.
    """
    r, g, b = (raw[..., i].astype(np.float64) * DEPTH_SPLIT for i in range(3))
    whole = np.abs(r - np.rint(r)) + np.abs(g - np.rint(g)) < 1e-3
    mm = (np.rint(r) * DEPTH_SPLIT + np.rint(g)).astype(np.int64)
    plain = b / DEPTH_SPLIT * far * 1000.0
    # Half floats keep about 1 part in 2000; allow twice that plus rounding.
    agree = np.abs(mm - plain) <= 0.001 * plain + 2
    bad = float((~(whole & agree)).mean())
    if bad > 0.001:
        raise ValueError(f"{bad:.1%} of the depth values did not arrive as written")
    return mm


def depth_unit_for(max_mm: int) -> int:
    """Millimetres per count so that `max_mm` fits a 16-bit depth image."""
    return max(1, math.ceil(max_mm / 65535))


def panorama_directions(width: int, height: int) -> np.ndarray:
    """[height,width,3] world directions of an equirectangular sky image, as read by
    res://addons/godot2splat/import/panorama_sky.gdshader: the image centre looks
    along -Z, u grows towards +X, the top row is straight up."""
    u = (np.arange(width) + 0.5) / width
    v = (np.arange(height) + 0.5) / height
    a = (u - 0.5) * 2.0 * np.pi
    p = v * np.pi
    return np.stack([np.sin(a)[None, :] * np.sin(p)[:, None],
                     np.broadcast_to(np.cos(p)[:, None], (height, width)),
                     -np.cos(a)[None, :] * np.sin(p)[:, None]], axis=-1)


def cube_to_panorama(faces: list[np.ndarray], c2ws: list[np.ndarray], width: int, height: int) -> np.ndarray:
    """Equirectangular image [height,width,C] from square 90 degree views that
    together cover every direction. `c2ws` are the views' OpenCV camera matrices."""
    import cv2

    dirs = panorama_directions(width, height)
    out = np.zeros((height, width, faces[0].shape[2]), dtype=faces[0].dtype)
    best = np.full((height, width), -np.inf)
    cams = [dirs @ c2w[:3, :3] for c2w in c2ws]  # the directions in each camera's axes
    for cam in cams:
        best = np.maximum(best, cam[..., 2])
    for face, cam in zip(faces, cams):
        size = face.shape[0]
        z = np.maximum(cam[..., 2], 1e-9)
        # 90 degree field of view: focal length is half the image size.
        map_x = (cam[..., 0] / z * size / 2 + size / 2 - 0.5).astype(np.float32)
        map_y = (cam[..., 1] / z * size / 2 + size / 2 - 0.5).astype(np.float32)
        sampled = cv2.remap(face, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        mine = cam[..., 2] >= best - 1e-9
        out[mine] = sampled.reshape(height, width, -1)[mine]
    return out
