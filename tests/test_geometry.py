"""Camera conventions, depth decoding and the sky panorama. Numpy only."""
import numpy as np
import pytest

from g2s import geometry, splat_io

IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


def _project(point, K, c2w):
    """Pixel position and depth of a world point (OpenCV camera)."""
    cam = (np.asarray(point, dtype=float) - c2w[:3, 3]) @ c2w[:3, :3]
    return K[0, 0] * cam[0] / cam[2] + K[0, 2], K[1, 1] * cam[1] / cam[2] + K[1, 2], cam[2]


def test_godot_transform_lists_basis_vectors_then_origin():
    # A node turned a quarter turn about Y: its X axis points along world -Z.
    m = geometry.godot_transform([0, 0, -1, 0, 1, 0, 1, 0, 0, 5, 6, 7])
    assert np.allclose(m[:3, 0], [0, 0, -1]) and np.allclose(m[:3, 2], [1, 0, 0])
    assert np.allclose(m[:3, 3], [5, 6, 7]) and np.allclose(m[3], [0, 0, 0, 1])


def test_godot_camera_looks_along_minus_z_with_y_up():
    K = geometry.intrinsics(640, 480, 90.0)
    assert np.isclose(K[1, 1], 240.0) and np.isclose(K[0, 0], 240.0)  # vertical field of view
    assert (K[0, 2], K[1, 2]) == (320.0, 240.0)
    c2w = geometry.camera_from_godot(IDENTITY)
    u, v, z = _project([0, 0, -2], K, c2w)
    assert np.allclose([u, v, z], [320, 240, 2])
    u, v, _ = _project([0.5, 0, -2], K, c2w)    # to the right of the camera
    assert u > 320 and np.isclose(v, 240)
    u, v, _ = _project([0, 0.5, -2], K, c2w)    # above the camera: higher in the picture
    assert np.isclose(u, 320) and v < 240


def test_backproject_inverts_projection_for_a_moved_camera():
    K = geometry.intrinsics(64, 64, 90.0)
    # Camera at (1, 2, 3), turned a quarter turn about Y (looks along -X).
    c2w = geometry.camera_from_godot([0, 0, -1, 0, 1, 0, 1, 0, 0, 1, 2, 3])
    depth = np.zeros((64, 64))
    depth[10, 40] = 2.5
    pts, vu = geometry.backproject(depth, K, c2w)
    assert vu.tolist() == [[10, 40]]
    u, v, z = _project(pts[0], K, c2w)
    assert np.allclose([u, v, z], [40.5, 10.5, 2.5])   # pixel centres sit at half-integers
    assert pts[0][0] < 1.0                              # in front of the camera means towards -X


def _encode(metres, far):
    """What depth_pass.gdshader writes, as the half floats read back from Godot."""
    mm = np.floor(np.asarray(metres, dtype=np.float64) * 1000.0 + 0.5)
    high = np.floor(mm / geometry.DEPTH_SPLIT)
    raw = np.stack([high / geometry.DEPTH_SPLIT, (mm - high * geometry.DEPTH_SPLIT) / geometry.DEPTH_SPLIT,
                    mm / (far * 1000.0)], axis=-1)
    return raw.astype("<f2")


def test_depth_round_trip_is_exact_to_the_millimetre():
    rng = np.random.default_rng(0)
    metres = rng.uniform(0.05, 480.0, size=(40, 50))
    metres[0, :5] = 0.0   # sky
    metres[1, :3] = [0.001, 2.047, 2.048]
    mm = geometry.decode_depth(_encode(metres, 500.0), 500.0)
    assert np.array_equal(mm, np.floor(metres * 1000.0 + 0.5).astype(np.int64))
    assert (mm[0, :5] == 0).all()


@pytest.mark.parametrize("factor", [0.8, 1.3])
def test_depth_altered_by_the_renderer_is_refused(factor):
    raw = (_encode(np.full((20, 20), 3.217), 500.0).astype(np.float64) * factor).astype("<f2")
    with pytest.raises(ValueError, match="did not arrive as written"):
        geometry.decode_depth(raw, 500.0)


def test_depth_unit_keeps_sixteen_bits():
    assert geometry.depth_unit_for(4000) == 1
    assert geometry.depth_unit_for(65535) == 1
    assert geometry.depth_unit_for(65536) == 2
    assert geometry.depth_unit_for(500_000) == 8


def test_thin_keeps_one_point_per_voxel():
    pts = np.array([[0.001, 0, 0], [0.004, 0.003, 0], [0.5, 0, 0]])
    cols = np.array([[1, 1, 1], [2, 2, 2], [3, 3, 3]])
    p, c = geometry.thin(pts, cols, 0.02)
    assert len(p) == 2 and sorted(c[:, 0].tolist())[-1] == 3


def test_panorama_layout_matches_the_sky_shader():
    d = geometry.panorama_directions(8, 4)
    assert np.allclose(np.linalg.norm(d, axis=-1), 1.0)
    wide = geometry.panorama_directions(360, 180)
    assert np.allclose(wide[90, 180], [0, 0, -1], atol=0.02)   # centre: -Z
    assert np.allclose(wide[90, 270], [1, 0, 0], atol=0.02)    # a quarter to the right: +X
    assert np.allclose(wide[0, 0], [0, 1, 0], atol=0.02)       # top row: up
    # The shader's lookup, written out: u = atan(x, -z) / TAU + 0.5, v = acos(y) / PI.
    u = np.arctan2(wide[..., 0], -wide[..., 2]) / (2 * np.pi) + 0.5
    v = np.arccos(np.clip(wide[..., 1], -1, 1)) / np.pi
    assert np.allclose(u, ((np.arange(360) + 0.5) / 360)[None, :], atol=1e-6)
    assert np.allclose(v, ((np.arange(180) + 0.5) / 180)[:, None], atol=1e-6)


def _looking(direction, up):
    """Godot transform numbers of a camera at the origin looking along `direction`."""
    z = -np.asarray(direction, dtype=float)
    x = np.cross(up, z)
    y = np.cross(z, x)
    return [*x, *y, *z, 0, 0, 0]


def test_cube_to_panorama_puts_each_face_in_its_direction():
    looks = [((1, 0, 0), (0, 1, 0)), ((-1, 0, 0), (0, 1, 0)), ((0, 1, 0), (0, 0, 1)),
             ((0, -1, 0), (0, 0, -1)), ((0, 0, 1), (0, 1, 0)), ((0, 0, -1), (0, 1, 0))]
    faces = [np.full((16, 16, 3), 10 * (i + 1), dtype=np.uint8) for i in range(6)]
    c2ws = [geometry.camera_from_godot(_looking(d, up)) for d, up in looks]
    pano = geometry.cube_to_panorama(faces, c2ws, 64, 32)
    assert pano[16, 32, 0] == 60    # centre looks along -Z
    assert pano[16, 48, 0] == 10    # +X
    assert pano[16, 16, 0] == 20    # -X
    assert pano[16, 0, 0] == 50     # the left and right edges look along +Z
    assert pano[0, 5, 0] == 30 and pano[31, 5, 0] == 40   # up and down
    assert set(np.unique(pano)) == {10, 20, 30, 40, 50, 60}


def test_cube_to_panorama_keeps_the_picture_upright():
    # The face looking along -Z is bright in its top half only.
    looks = [((1, 0, 0), (0, 1, 0)), ((-1, 0, 0), (0, 1, 0)), ((0, 1, 0), (0, 0, 1)),
             ((0, -1, 0), (0, 0, -1)), ((0, 0, 1), (0, 1, 0)), ((0, 0, -1), (0, 1, 0))]
    faces = [np.zeros((32, 32, 3), dtype=np.uint8) for _ in range(6)]
    faces[5][:16] = 200
    c2ws = [geometry.camera_from_godot(_looking(d, up)) for d, up in looks]
    pano = geometry.cube_to_panorama(faces, c2ws, 128, 64)
    assert pano[24, 64, 0] == 200 and pano[40, 64, 0] == 0


def test_ply_layout_and_recentring(tmp_path):
    rng = np.random.default_rng(0)
    n, k = 10, 15
    g = dict(means=rng.normal(size=(n, 3)), scales=rng.normal(size=(n, 3)) - 3,
             quats=rng.normal(size=(n, 4)), opacities=rng.normal(size=n),
             sh0=rng.normal(size=(n, 1, 3)), shN=rng.normal(size=(n, k, 3)))
    splat_io.write_ply(tmp_path / "s.ply", **g)
    names, data = splat_io.read_ply(tmp_path / "s.ply")
    assert len(names) == 3 + 3 + 45 + 1 + 3 + 4 and data.shape == (10, 59)
    assert "nx" not in names                        # zeros that nothing reads
    assert np.allclose(data[:, :3], g["means"], atol=1e-6)
    # f_rest is channel-major: first value is coefficient 0 of red.
    assert np.allclose(data[:, 6], g["shN"][:, 0, 0], atol=1e-6)
    centre = g["means"].mean(axis=0)
    xyz = splat_io.transform_ply(tmp_path / "s.ply", tmp_path / "c.ply", 1.0, centre)
    assert np.allclose(xyz.mean(axis=0), 0, atol=1e-6)
    moved_names, moved = splat_io.read_ply(tmp_path / "c.ply")
    assert moved_names == names
    assert np.allclose(moved[:, 3:], data[:, 3:])   # only the positions change


def test_export_drops_the_normals_of_older_splats(tmp_path):
    """Splats trained before the normals were left out still have them; the copy
    exported to Godot does not, and is otherwise the same."""
    rng = np.random.default_rng(1)
    n = 6
    g = dict(means=rng.normal(size=(n, 3)), scales=rng.normal(size=(n, 3)) - 3,
             quats=rng.normal(size=(n, 4)), opacities=rng.normal(size=n),
             sh0=rng.normal(size=(n, 1, 3)), shN=rng.normal(size=(n, 15, 3)))
    splat_io.write_ply(tmp_path / "new.ply", **g)
    names, data = splat_io.read_ply(tmp_path / "new.ply")
    old = np.concatenate([data[:, :3], np.zeros((n, 3)), data[:, 3:]], axis=1)
    splat_io._write_table(tmp_path / "old.ply", names[:3] + ["nx", "ny", "nz"] + names[3:], old)
    splat_io.transform_ply(tmp_path / "old.ply", tmp_path / "out.ply", 1.0, np.zeros(3))
    out_names, out = splat_io.read_ply(tmp_path / "out.ply")
    assert out_names == names and np.allclose(out, data)
