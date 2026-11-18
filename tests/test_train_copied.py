"""Training helpers: the regulariser, and the image loss copied from Real2Virtu."""
import pytest
import torch

from g2s.stages.train import regularise


def test_regulariser_reaches_only_gaussians_that_drew_something():
    # Gaussian 1 is in view but hidden: the picture gave it no gradient, so the
    # regulariser must not either (alone, Adam would turn it into a full step).
    params = {"opacities": torch.zeros(3, requires_grad=True),
              "scales": torch.zeros(3, 3, requires_grad=True)}
    params["opacities"].grad = torch.tensor([0.5, 0.0, -0.2])
    params["scales"].grad = torch.zeros(3, 3)
    regularise(params, opacity_reg=0.01, scale_reg=0.01)
    g_op, g_sc = params["opacities"].grad, params["scales"].grad
    # sigmoid(0) = 0.5: d/dx of 0.01 * mean over the 2 drawing Gaussians = 0.01 * 0.25 / 2
    assert torch.allclose(g_op, torch.tensor([0.5 + 0.00125, 0.0, -0.2 + 0.00125]))
    assert g_sc[1].abs().sum() == 0 and torch.allclose(g_sc[0], torch.full((3,), 0.01 / 6))


def test_loss_without_convolutions_matches_the_convolution_one():
    from g2s.stages.train import _photo_loss_conv, _photo_loss_shifted

    g = torch.Generator().manual_seed(0)
    gt = torch.rand(37, 52, 3, generator=g)
    img = (gt + 0.2 * torch.randn(37, 52, 3, generator=g)).requires_grad_()
    want = _photo_loss_conv(img, gt, 0.2)
    got = _photo_loss_shifted(img, gt, 0.2)
    assert abs(float(want) - float(got)) < 1e-6
    g_want, = torch.autograd.grad(want, img)
    g_got, = torch.autograd.grad(got, img)
    assert (g_want - g_got).abs().max() < 1e-7 + 1e-4 * g_want.abs().max()


def test_rocm_loss_on_the_gpu_matches_the_cpu_reference():
    """The compiled loss the AMD path trains with, against the plain one on the CPU."""
    from g2s import device
    from g2s.stages.train import _photo_loss_conv, make_photo_loss

    if device.gpu_backend() != "rocm" or not torch.cuda.is_available():
        pytest.skip("AMD ROCm only")
    g = torch.Generator().manual_seed(1)
    gt = torch.rand(120, 160, 3, generator=g)
    img = (gt + 0.2 * torch.randn(120, 160, 3, generator=g)).requires_grad_()
    want = _photo_loss_conv(img, gt, 0.2)
    g_want, = torch.autograd.grad(want, img)
    loss = make_photo_loss("rocm")
    img_gpu = img.detach().cuda().requires_grad_()
    for _ in range(2):   # second call: the compiled kernels, not the compile itself
        got = loss(img_gpu, gt.cuda(), 0.2)
        g_got, = torch.autograd.grad(got, img_gpu)
    assert abs(float(want) - float(got)) < 1e-5
    assert (g_want - g_got.cpu()).abs().max() < 1e-7 + 1e-3 * g_want.abs().max()
