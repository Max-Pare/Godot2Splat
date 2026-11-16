"""The test room: one definition for the Godot scene and for the checks against it.

`python tests/fixture_room.py` rewrites tests/fixture_project/room.tscn.

The room is 6 x 2.7 x 5 m inside (x -3..3, y 0..2.7, z -2.5..2.5) with 0.2 m walls,
a window in the +X wall, and a few props. Everything is an axis-aligned box or a
sphere, so the distance from any point to the nearest surface has a closed form.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

T = 0.2  # wall thickness
ROOM_LO, ROOM_HI = np.array([-3.0, 0.0, -2.5]), np.array([3.0, 2.7, 2.5])

# name, centre, size, material, has a collision body
SHELL = [
    ("Floor", (0, -T / 2, 0), (6 + 2 * T, T, 5 + 2 * T), "floor", True),
    ("Ceiling", (0, 2.7 + T / 2, 0), (6 + 2 * T, T, 5 + 2 * T), "ceiling", False),
    ("WallWest", (-3 - T / 2, 1.35, 0), (T, 2.7, 5 + 2 * T), "wall", True),
    ("WallNorth", (0, 1.35, -2.5 - T / 2), (6 + 2 * T, 2.7, T), "wall", True),
    ("WallSouth", (0, 1.35, 2.5 + T / 2), (6 + 2 * T, 2.7, T), "wall", True),
    ("WallEastLow", (3 + T / 2, 0.5, 0), (T, 1.0, 5 + 2 * T), "wall", True),
    ("WallEastHigh", (3 + T / 2, 2.35, 0), (T, 0.7, 5 + 2 * T), "wall", False),
    ("WallEastLeft", (3 + T / 2, 1.5, -1.725), (T, 1.0, 1.95), "wall", False),
    ("WallEastRight", (3 + T / 2, 1.5, 1.725), (T, 1.0, 1.95), "wall", False),
]
PROP_BOXES = [
    ("TableTop", (-1.2, 0.72, -1.0), (1.4, 0.06, 0.8), "wood", True),
    ("TableFoot", (-1.2, 0.345, -1.0), (0.3, 0.69, 0.3), "wood", False),
    ("Cabinet", (2.2, 0.9, 1.9), (0.8, 1.8, 0.6), "blue", True),
    ("Crate", (0.8, 0.25, -1.6), (0.5, 0.5, 0.5), "green", True),
]
SPHERE = ("Ball", (-1.2, 1.05, -1.0), 0.3, "red")

VOLUME = {"position": (0.3, 1.35, 0.4), "size": (5.6, 2.5, 4.6), "image_size": 640, "test_every": 8}

MATERIALS = {
    "floor": ((0.55, 0.40, 0.28), (0.42, 0.30, 0.20), 2.0),
    "ceiling": ((0.90, 0.90, 0.88), (0.80, 0.80, 0.78), 1.0),
    "wall": ((0.85, 0.80, 0.70), (0.72, 0.68, 0.60), 2.5),
    "wood": ((0.60, 0.45, 0.25), (0.45, 0.32, 0.18), 12.0),
    "blue": ((0.20, 0.35, 0.75), (0.12, 0.22, 0.50), 6.0),
    "green": ((0.25, 0.65, 0.30), (0.15, 0.45, 0.20), 10.0),
    "red": ((0.85, 0.15, 0.12), (0.95, 0.75, 0.20), 9.0),
}


def _box_sdf(p: np.ndarray, lo, hi) -> np.ndarray:
    """Signed distance to an axis-aligned box: negative inside."""
    c, h = (np.asarray(lo) + hi) / 2, (np.asarray(hi) - lo) / 2
    q = np.abs(p - c) - h
    return np.linalg.norm(np.maximum(q, 0), axis=1) + np.minimum(q.max(axis=1), 0)


def _solids():
    for _, centre, size, _, _ in SHELL + PROP_BOXES:
        c, s = np.array(centre, dtype=float), np.array(size, dtype=float)
        yield c - s / 2, c + s / 2


def surface_distance(p: np.ndarray) -> np.ndarray:
    """Distance of points [N,3] to the nearest surface of the room or its props.
    Meant for points in open air or on a surface, not inside a wall."""
    _, centre, radius, _ = SPHERE
    best = np.abs(np.linalg.norm(p - np.array(centre), axis=1) - radius)
    for lo, hi in _solids():
        best = np.minimum(best, np.abs(_box_sdf(p, lo, hi)))
    return best


def in_open_air(p: np.ndarray) -> np.ndarray:
    """True for points inside the room and outside every prop."""
    _, centre, radius, _ = SPHERE
    inside = (_box_sdf(p, ROOM_LO, ROOM_HI) < 0) & (np.linalg.norm(p - np.array(centre), axis=1) > radius)
    for lo, hi in _solids():
        inside &= _box_sdf(p, lo, hi) > 0
    return inside


def _vec(v) -> str:
    return "Vector3(%s)" % ", ".join(f"{float(x):g}" for x in v)


def _at(v) -> str:
    return "Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, %s)" % ", ".join(f"{float(x):g}" for x in v)


def _looking(direction, origin) -> str:
    """Transform3D text of a node whose -Z axis points along `direction`."""
    z = -np.asarray(direction, dtype=float)
    z /= np.linalg.norm(z)
    x = np.cross([0.0, 1.0, 0.0], z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    rows = np.stack([x, y, z], axis=1)  # columns are the basis vectors
    return "Transform3D(%s)" % ", ".join(f"{v:.6f}" for v in [*rows.reshape(-1), *origin])


def scene_text() -> str:
    sub, nodes = [], []
    for name, (a, b, scale) in MATERIALS.items():
        sub.append(f'[sub_resource type="ShaderMaterial" id="mat_{name}"]\nshader = ExtResource("1")\n'
                   f'shader_parameter/color_a = Color({a[0]}, {a[1]}, {a[2]}, 1)\n'
                   f'shader_parameter/color_b = Color({b[0]}, {b[1]}, {b[2]}, 1)\n'
                   f'shader_parameter/scale = {scale}\n')
    for name, centre, size, material, body in SHELL + PROP_BOXES:
        sub.append(f'[sub_resource type="BoxMesh" id="mesh_{name}"]\nsize = {_vec(size)}\n')
        parent = "."
        if body:
            sub.append(f'[sub_resource type="BoxShape3D" id="shape_{name}"]\nsize = {_vec(size)}\n')
            nodes.append(f'[node name="{name}" type="StaticBody3D" parent="."]\ntransform = {_at(centre)}\n')
            nodes.append(f'[node name="Shape" type="CollisionShape3D" parent="{name}"]\n'
                         f'shape = SubResource("shape_{name}")\n')
            nodes.append(f'[node name="Mesh" type="MeshInstance3D" parent="{name}"]\n'
                         f'mesh = SubResource("mesh_{name}")\n'
                         f'surface_material_override/0 = SubResource("mat_{material}")\n')
        else:
            nodes.append(f'[node name="{name}" type="MeshInstance3D" parent="{parent}"]\n'
                         f'transform = {_at(centre)}\nmesh = SubResource("mesh_{name}")\n'
                         f'surface_material_override/0 = SubResource("mat_{material}")\n')
    name, centre, radius, material = SPHERE
    sub.append(f'[sub_resource type="SphereMesh" id="mesh_{name}"]\nradius = {radius}\nheight = {2 * radius}\n'
               f'radial_segments = 96\nrings = 48\n')
    nodes.append(f'[node name="{name}" type="MeshInstance3D" parent="."]\ntransform = {_at(centre)}\n'
                 f'mesh = SubResource("mesh_{name}")\nsurface_material_override/0 = SubResource("mat_{material}")\n')

    sub.append('[sub_resource type="ProceduralSkyMaterial" id="sky_material"]\n'
               'sky_top_color = Color(0.2, 0.4, 0.8, 1)\nsky_horizon_color = Color(0.7, 0.8, 0.9, 1)\n'
               'ground_bottom_color = Color(0.2, 0.25, 0.2, 1)\nground_horizon_color = Color(0.6, 0.7, 0.6, 1)\n')
    sub.append('[sub_resource type="Sky" id="sky"]\nsky_material = SubResource("sky_material")\n')
    # Filmic tonemapping, ambient occlusion and glow: the scan has to cope with all three.
    sub.append('[sub_resource type="Environment" id="environment"]\nbackground_mode = 2\n'
               'sky = SubResource("sky")\ntonemap_mode = 2\nssao_enabled = true\nglow_enabled = true\n')
    nodes.append('[node name="WorldEnvironment" type="WorldEnvironment" parent="."]\n'
                 'environment = SubResource("environment")\n')
    nodes.append(f'[node name="Sun" type="DirectionalLight3D" parent="."]\n'
                 f'transform = {_looking((-1.0, -0.6, -0.25), (0, 5, 0))}\nlight_energy = 2.0\nshadow_enabled = true\n')
    nodes.append('[node name="Lamp" type="OmniLight3D" parent="."]\n'
                 f'transform = {_at((0, 2.3, 0.3))}\nlight_color = Color(1, 0.93, 0.8, 1)\n'
                 'light_energy = 1.5\nshadow_enabled = true\nomni_range = 9.0\n')
    v = VOLUME
    nodes.append(f'[node name="SplatCaptureVolume" type="Node3D" parent="."]\ntransform = {_at(v["position"])}\n'
                 f'script = ExtResource("2")\nsize = {_vec(v["size"])}\nimage_size = {v["image_size"]}\n'
                 f'test_every = {v["test_every"]}\n')
    head = (f'[gd_scene load_steps={len(sub) + 3} format=3]\n\n'
            '[ext_resource type="Shader" path="res://pattern.gdshader" id="1"]\n'
            '[ext_resource type="Script" path="res://addons/godot2splat/capture_volume.gd" id="2"]\n\n')
    return head + "\n".join(sub) + '\n[node name="Room" type="Node3D"]\n\n' + "\n".join(nodes)


if __name__ == "__main__":
    out = Path(__file__).parent / "fixture_project" / "room.tscn"
    out.write_text(scene_text())
    print(f"wrote {out}")
