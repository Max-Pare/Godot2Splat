"""The test room's own distance function, which the pipeline test relies on."""
import numpy as np

import fixture_room as fr


def test_distances_to_known_surfaces():
    p = np.array([
        [1.0, 1.0, 0.5],      # open floor, 1 m up
        [2.9, 1.5, 0.0],      # in front of the window: the wall plane is open there
        [-1.2, 1.35, -1.0],   # on top of the ball
        [0.0, 0.0, 0.0],      # on the floor
        [2.2, 0.9, 1.6],      # on the cabinet's front
    ])
    d = fr.surface_distance(p)
    assert np.isclose(d[0], 1.0)
    assert np.isclose(d[1], np.hypot(0.1, 0.5))   # nearest: the edge of the window opening
    assert np.allclose(d[2:], 0.0, atol=1e-9)


def test_open_air():
    p = np.array([[0.0, 1.0, 0.0], [3.5, 1.5, 0.0], [2.2, 0.9, 1.9], [-1.2, 1.05, -1.0], [0.0, -0.1, 0.0]])
    assert fr.in_open_air(p).tolist() == [True, False, False, False, False]


def test_scene_file_is_up_to_date():
    from pathlib import Path
    scene = Path(__file__).parent / "fixture_project" / "room.tscn"
    assert scene.read_text() == fr.scene_text(), "run: python tests/fixture_room.py"
