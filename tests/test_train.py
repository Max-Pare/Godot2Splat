"""Training helpers written for rendered scenes: sky handling and run sizing."""
import pytest

torch = pytest.importorskip("torch")

from g2s.stages import train  # noqa: E402


def test_masks_leave_out_the_border_between_surface_and_sky():
    depth = torch.full((8, 8), 2.0)
    depth[:, 5:] = 0.0                      # sky on the right
    solid, empty = train.solid_and_empty(depth, (16, 16))
    assert solid.shape == (16, 16) and solid.dtype == torch.bool
    assert solid[:, :8].all() and not solid[:, 8:].any()     # depth columns 0-3
    assert empty[:, 12:].all() and not empty[:, :12].any()   # depth columns 6-7
    assert not (solid | empty)[:, 8:12].any()                # columns 4 and 5: asked nothing


def test_masks_without_sky():
    solid, empty = train.solid_and_empty(torch.full((4, 4), 1.5), (8, 8))
    assert solid.all() and not empty.any()


def test_backdrop_rewards_opaque_surfaces_and_clear_sky():
    photo = torch.rand(4, 4, 3)
    solid = torch.zeros(4, 4, dtype=torch.bool)
    solid[:, :2] = True
    colour = torch.tensor([1.0, 0.0, 1.0])
    clear = train.over_backdrop(torch.zeros(4, 4, 3), torch.zeros(4, 4, 1), photo, solid, colour)
    assert torch.allclose(clear[:, 2:], photo[:, 2:])        # clear over sky: already right
    assert torch.allclose(clear[:, :2], colour.expand(4, 2, 3))   # clear over a surface: wrong
    opaque = train.over_backdrop(photo, torch.ones(4, 4, 1), photo, solid, colour)
    assert torch.allclose(opaque, photo)


def test_alpha_loss_pushes_each_region_its_own_way():
    solid = torch.tensor([[True, True, False, False]])
    empty = torch.tensor([[False, False, False, True]])
    good = torch.tensor([[1.0, 1.0, 0.3, 0.0]])
    bad = torch.tensor([[0.0, 0.0, 0.3, 1.0]])
    assert float(train.alpha_loss(good, solid, empty)) == 0.0
    assert float(train.alpha_loss(bad, solid, empty)) == 2.0
    none = torch.zeros(1, 4, dtype=torch.bool)
    assert float(train.alpha_loss(bad, none, none)) == 0.0


def test_run_size_follows_the_capture():
    assert train.auto_size(20_000, 1_500_000, 30, 50_000) == (6_000, 300_000)
    assert train.auto_size(20_000, 1_500_000, 100, 400_000) == (15_000, 1_200_000)
    assert train.auto_size(20_000, 1_500_000, 700, 900_000) == (20_000, 1_500_000)
    assert train.scaled_size(640, 640, 50) == (320, 320) and train.scaled_size(640, 640, 250) == (640, 640)
