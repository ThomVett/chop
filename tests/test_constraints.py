import pytest

import numpy as np

import torch
from torch import nn
import torch.nn.functional as F
import chop
from chop import utils
from chop.utils.image import group_patches
import chop.constraints


def test_nuclear_norm():

    batch_size = 8
    channels = 3
    m = 32
    n = 35
    alpha = 1.
    constraint = chop.constraints.NuclearNormBall(alpha)

    grad = torch.rand(batch_size, channels, m, n)
    iterate = torch.rand(batch_size, channels, m, n)
    constraint.lmo(-grad, iterate)
    constraint.prox(iterate - .1 * grad)


@pytest.mark.parametrize('constraint', [chop.constraints.L1Ball,
                                        chop.constraints.L2Ball,
                                        chop.constraints.LinfBall,
                                        chop.constraints.Simplex,
                                        chop.constraints.NuclearNormBall,
                                        chop.constraints.GroupL1Ball,
                                        chop.constraints.Cone])
@pytest.mark.parametrize('alpha', [1., 10., .5])
def test_projections(constraint, alpha):
    """Tests that projections are true projections:
    ..math::
        p\circ p = p
    """
    batch_size = 8
    if constraint == chop.constraints.GroupL1Ball:
        groups = group_patches()
        prox = constraint(alpha, groups).prox
    elif constraint == chop.constraints.Cone:
        directions = torch.rand(batch_size, 3, 32, 32)
        prox = constraint(directions, cos_angle=.2).prox
    else:
        prox = constraint(alpha).prox

    for _ in range(10):
        data = torch.rand(batch_size, 3, 32, 32)

        proj_data = prox(data)
        # SVD reconstruction doesn't do better than 1e-5
        double_proj = prox(proj_data)
        assert double_proj.allclose(proj_data, atol=1e-5), (double_proj, proj_data)


def test_GroupL1LMO():
    batch_size = 2
    alpha = 1.
    groups = group_patches(x_patch_size=2, y_patch_size=2, x_image_size=6, y_image_size=6)
    constraint = chop.constraints.GroupL1Ball(alpha, groups)
    data = torch.rand(batch_size, 3, 6, 6)
    grad = torch.rand(batch_size, 3, 6, 6)

    constraint.lmo(-grad, data)


def test_groupL1Prox():
    batch_size = 2
    alpha = 10
    groups = group_patches(x_patch_size=2, y_patch_size=2, x_image_size=6, y_image_size=6)
    constraint = chop.constraints.GroupL1Ball(alpha, groups)
    data = torch.rand(batch_size, 3, 6, 6)

    constraint.prox(-data, step_size=.3)


def test_cone_constraint():
    # Standard second order cone
    u = torch.tensor([[0., 0., 1.]])
    cos_alpha = .5

    cone = chop.constraints.Cone(u, cos_alpha)

    for inp, correct_prox in [(torch.tensor([[1., 0, 0]]), torch.tensor([[.5, 0, .5]])),
                              (torch.tensor([[0, 1., 0]]), torch.tensor([[0, .5, .5]])),
                              (u, u),
                              (-u, torch.zeros_like(u))
                              ]:
        assert cone.prox(inp).eq(correct_prox).all()

    # Moreau decomposition: x = proj_x + (x - proj_x) where
    # the two vectors are orthogonal
    for _ in range(10):
        x = torch.rand(*u.shape)
        proj_x = cone.prox(x)
        assert utils.bdot(x - proj_x, proj_x).allclose(torch.zeros_like(x), atol=4e-7)


@pytest.mark.parametrize('Constraint', [chop.constraints.L1Ball,
                                        chop.constraints.L2Ball,
                                        chop.constraints.LinfBall,
                                        chop.constraints.Simplex,
                                        chop.constraints.NuclearNormBall,
                                        chop.constraints.GroupL1Ball,
                                        chop.constraints.Box,
                                        chop.constraints.Cone])
@pytest.mark.parametrize('alpha', [.1, 1., 20.])
def test_feasible(Constraint, alpha):
    """Tests if prox and LMO yield feasible points"""
    # TODO: implement feasibility check method in each constraint.

    if Constraint == chop.constraints.GroupL1Ball:
        groups = group_patches(x_patch_size=2, y_patch_size=2, x_image_size=6, y_image_size=6)
        constraint = Constraint(alpha, groups)
    elif Constraint == chop.constraints.Cone:
        directions = torch.rand(2, 3, 6, 6)
        cos_alpha = .2
        constraint = Constraint(directions, cos_alpha)
    elif Constraint == chop.constraints.Box:
        constraint = Constraint(-1., 10.)
    else:
        constraint = Constraint(alpha)
    for _ in range(10):
        try:
            data = (alpha + 1) * torch.rand(2, 3, 6, 6)
            assert constraint.is_feasible(constraint.prox(data)).all()
        except AttributeError:  # Constraint doesn't have a prox operator
            pass
        try:
            grad = (alpha + 1) * torch.rand(2, 3, 6, 6)
            update_dir, _ = constraint.lmo(-grad, data)
            s = update_dir + data
            assert constraint.is_feasible(s).all()
        except AttributeError:  # Constraint doesn't have an LMO
            pass


@pytest.mark.parametrize('p', [1, 2, np.inf])
def test_model_constraint_maker(p):

    class Net(nn.Module):
        def __init__(self):
            super(Net, self).__init__()
            self.conv1 = nn.Conv2d(1, 32, 3, 1)
            self.conv2 = nn.Conv2d(32, 64, 3, 1)
            self.dropout1 = nn.Dropout(0.25)
            self.dropout2 = nn.Dropout(0.5)
            self.fc1 = nn.Linear(9216, 128)
            self.fc2 = nn.Linear(128, 10)

        def forward(self, x):
            x = self.conv1(x)
            x = F.relu(x)
            x = self.conv2(x)
            x = F.relu(x)
            x = F.max_pool2d(x, 2)
            x = self.dropout1(x)
            x = torch.flatten(x, 1)
            x = self.fc1(x)
            x = F.relu(x)
            x = self.dropout2(x)
            x = self.fc2(x)
            output = F.log_softmax(x, dim=1)
            return output

    model = Net()
    constraints = chop.constraints.make_Lp_model_constraints(model, p)

    assert len(constraints) == len(list(model.parameters()))

    chop.constraints.make_feasible(model, [constraint.prox for constraint in constraints])

    for param, constraint in zip(model.parameters(), constraints):
        assert torch.allclose(param, constraint.prox(param.unsqueeze(0)).squeeze(0), atol=1e-5)
