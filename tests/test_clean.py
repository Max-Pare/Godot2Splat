"""Splat cleanup filters (copied from Real2Virtu)."""
import numpy as np
import pytest

from g2s.config import CleanCfg
from g2s.stages import clean


def test_haze_filters_keep_the_room_and_far_scenery():
    rng = np.random.default_rng(0)
    n = 4000
    means = rng.uniform([-2, 0, -2], [2, 0.02, 2], size=(n, 3))  # a densely covered floor
    sigma = np.full((n, 3), 0.02)
    opacity = np.full(n, 0.8)
    cams = np.array([[0.0, 1.5, 0.0], [0.5, 1.5, 0.5]])

    opacity[0] = 0.005                      # haze
    sigma[1] = [0.02, 0.02, 0.6]            # blob inside the room
    # Large, but far away: the view through a window, made of several Gaussians.
    means[2], sigma[2] = [0, 5, 80], 6.0
    means[6:26] = [0, 5, 80] + rng.uniform(-8, 8, size=(20, 3))
    sigma[6:26] = 6.0
    sigma[3] = [0.005, 0.005, 0.4]          # needle
    means[4] = [0, 9, 0]                    # alone, far from everything
    sigma[5] = [0.01, 0.01, 0.15]           # thin but short: a cable, keep

    r = clean.haze_mask(means, sigma, opacity, cams, CleanCfg())
    assert list(np.flatnonzero(r["transparent"])) == [0]
    assert r["oversized"][1] and not r["oversized"][2]
    assert r["needle"][3] and not r["needle"][5]
    assert r["isolated"][4] and not r["isolated"][2]
    removed = r["transparent"] | r["oversized"] | r["needle"] | r["isolated"]
    assert removed[[0, 1, 3, 4]].all() and not removed[6:26].any()
    assert removed.sum() <= 6  # the room itself stays


def test_free_space_needs_agreeing_views_and_spares_edges():
    torch = pytest.importorskip("torch")
    K = torch.tensor([[40.0, 0, 16], [0, 40.0, 12], [0, 0, 1]])
    depth = torch.full((24, 32), 2.0)       # a wall 2 m ahead
    depth[:, 20:] = 0.6                     # a near object on the right
    views = []
    for x in (-0.05, 0.0, 0.05, 0.1):
        c2w = torch.eye(4)
        c2w[0, 3] = x
        views.append(c2w)
    c2ws = torch.stack(views)
    pts = torch.tensor([
        [0.0, 0.0, 1.0],    # floating in front of the wall
        [0.0, 0.0, 2.0],    # on the wall
        [0.0, 0.0, 3.0],    # behind the wall: hidden, not judged
        [0.16, 0.0, 1.0],   # at the object's edge: its window holds the near depth, so it is spared
    ])
    args = dict(margin=0.12, margin_rel=0.06, min_share=0.8)
    mask = clean.free_space_mask(pts, [depth] * 4, K.expand(4, 3, 3), c2ws, min_views=3, **args)
    assert mask.tolist() == [True, False, False, False]
    # Too few views to be sure.
    mask = clean.free_space_mask(pts, [depth] * 2, K.expand(2, 3, 3), c2ws[:2], min_views=3, **args)
    assert not mask.any()


def test_hidden_points_are_the_ones_no_view_shows():
    torch = pytest.importorskip("torch")
    K = torch.tensor([[40.0, 0, 16], [0, 40.0, 12], [0, 0, 1]])
    depth = torch.full((24, 32), 2.0)       # a wall 2 m ahead
    depth[:, :4] = 0.0                      # no depth on the left edge: sky
    c2ws = torch.eye(4)[None]
    pts = torch.tensor([
        [0.0, 0.0, 1.0],    # in front of the wall
        [0.0, 0.0, 2.05],   # on the wall, within the hidden margin
        [0.0, 0.0, 3.0],    # behind the wall
        [0.0, 0.0, -1.0],   # behind the camera: outside every view
        [-1.2, 0.0, 3.0],   # in the sky pixels: shown
    ])
    _, hidden = clean.free_and_hidden(pts, [depth], K[None], c2ws, margin=0.03, margin_rel=0.01,
                                      min_views=1, min_share=0.8)
    assert hidden.tolist() == [False, False, True, True, False]


def test_contribution_weighs_what_the_pictures_show():
    """A Gaussian hidden behind a wider opaque one adds next to nothing (Gaussians have
    soft edges, so one the same size would show around it); a faint one adds little."""
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("no GPU")
    pytest.importorskip("gsplat")
    from g2s import geometry

    # Camera at the origin looking down +z (OpenCV); front, hidden behind it, faint beside.
    means = np.array([[0.0, 0.0, 3.0], [0.0, 0.0, 4.0], [1.0, 0.0, 3.0]])
    arr = {"means": means,
           "quats": np.tile([1.0, 0.0, 0.0, 0.0], (3, 1)),
           "scales": np.log(np.array([[0.6] * 3, [0.2] * 3, [0.2] * 3])),
           "opacities": np.log(np.array([0.99, 0.99, 0.05]) / (1 - np.array([0.99, 0.99, 0.05])))}
    K = geometry.intrinsics(200, 200, 90)
    w = clean.contribution(arr, np.eye(4)[None], K[None], np.array([[200, 200]]), "cuda", scale=1.0)
    front, hidden, faint = w
    assert front > 10 * faint > 0
    assert hidden < 0.02 * front
