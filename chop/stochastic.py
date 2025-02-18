"""
Stochastic optimizers.
=========================

This module contains stochastic first order optimizers.
These are meant to be used in replacement of optimizers such as SGD, Adam etc,
for training a model over batches of a dataset.
The API in this module is inspired by torch.optim.

"""

import warnings

import torch
from torch.optim import Optimizer
import numpy as np


EPS = np.finfo(np.float32).eps


def backtracking_step_size(
    x,
    f_t,
    old_f_t,
    f_grad,
    certificate,
    lipschitz_t,
    max_step_size,
    update_direction,
    norm_update_direction,
):
    """Backtracking step-size finding routine for FW-like algorithms

    Args:
        x: array-like, shape (n_features,)
            Current iterate
        f_t: float
            Value of objective function at the current iterate.
        old_f_t: float
            Value of objective function at previous iterate.
        f_grad: callable
            Callable returning objective function and gradient at
            argument.
        certificate: float
            FW gap
        lipschitz_t: float
            Current value of the Lipschitz estimate.
        max_step_size: float
            Maximum admissible step-size.
        update_direction: array-like, shape (n_features,)
            Update direction given by the FW variant.
        norm_update_direction: float
            Squared L2 norm of update_direction
    Returns:
        step_size_t: float
            Step-size to be used to compute the next iterate.
        lipschitz_t: float
            Updated value for the Lipschitz estimate.
        f_next: float
            Objective function evaluated at x + step_size_t d_t.
        grad_next: array-like
            Gradient evaluated at x + step_size_t d_t.
    """
    ratio_decrease = 0.9
    ratio_increase = 2.0
    max_ls_iter = 100
    if old_f_t is not None:
        tmp = (certificate ** 2) / (2 * (old_f_t - f_t) * norm_update_direction)
        lipschitz_t = max(min(tmp, lipschitz_t), lipschitz_t * ratio_decrease)
    for _ in range(max_ls_iter):
        step_size_t = certificate / (norm_update_direction * lipschitz_t)
        if step_size_t < max_step_size:
            rhs = -0.5 * step_size_t * certificate
        else:
            step_size_t = max_step_size
            rhs = (
                -step_size_t * certificate
                + 0.5 * (step_size_t ** 2) * lipschitz_t * norm_update_direction
            )
        f_next, grad_next = f_grad(x + step_size_t * update_direction)
        if f_next - f_t <= rhs + EPS:
            # .. sufficient decrease condition verified ..
            break
        else:
            lipschitz_t *= ratio_increase
    else:
        warnings.warn(
            "Exhausted line search iterations in minimize_frank_wolfe", RuntimeWarning
        )
    return step_size_t, lipschitz_t, f_next, grad_next


def normalize_gradient(grad, normalization):
    if normalization == 'none':
        return grad
    elif normalization == 'Linf':
        grad = grad / abs(grad).max()

    elif normalization == 'sign':
        grad = torch.sign(grad)

    elif normalization == 'L2':
        grad = grad / torch.norm(grad)

    return grad
        

class PGD(Optimizer):
    """Proximal Gradient Descent

    Args:
      params: [torch.Parameter]
        List of parameters to optimize over
      prox: [callable or None]
        List of prox operators, one per parameter.
      lr: float
        Learning rate
      momentum: float in [0, 1]

      normalization: str
        Type of gradient normalization to be used.
        Possible values are 'none', 'L2', 'Linf', 'sign'.

    """
    name = 'PGD'
    POSSIBLE_NORMALIZATIONS = {'none', 'L2', 'Linf', 'sign'}

    def __init__(self, params, prox=None, lr=.1, momentum=.9, normalization='none'):
        if prox is None:
            prox = [None] * len(params)

        self.prox = []
        for prox_el in prox:
            if prox_el is not None:
                self.prox.append(lambda x, s=None: prox_el(x.unsqueeze(0)).squeeze())
            else:
                self.prox.append(lambda x, s=None: x)

        if not (type(lr) == float or lr == 'sublinear'):
            raise ValueError("lr must be float or 'sublinear'.")
        self.lr = lr

        if type(momentum) == float:
            if not(0. <= momentum <= 1.):
                raise ValueError("Momentum must be in [0., 1.].")
        self.momentum = momentum

        if normalization in self.POSSIBLE_NORMALIZATIONS:
            self.normalization = normalization
        else:
            raise ValueError(f"Normalization must be in {self.POSSIBLE_NORMALIZATIONS}")
        defaults = dict(prox=self.prox, name=self.name, normalization=self.normalization)
        super(PGD, self).__init__(params, defaults)

    @property
    @torch.no_grad()
    def certificate(self):
        """A generator over the current convergence certificate estimate
        for each optimized parameter."""
        for groups in self.param_groups:
            for p in groups['params']:
                state = self.state[p]
                yield state['certificate']

    @torch.no_grad()
    def step(self, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        idx = 0
        for groups in self.param_groups:
            for p in groups['params']:
                if p.grad is None:
                    continue

                grad = p.grad

                if grad.is_sparse:
                    raise RuntimeError(
                        'We do not yet support sparse gradients.')

                state = self.state[p]
                # Initialization
                if len(state) == 0:
                    state['step'] = 0.
                    state['grad_estimate'] = torch.zeros_like(
                        p, memory_format=torch.preserve_format)

                state['step'] += 1.
                state['grad_estimate'].add_(grad - state['grad_estimate'], alpha=1. - self.momentum)

                grad_est = normalize_gradient(state['grad_estimate'], self.normalization)

                if self.lr == 'sublinear':
                    step_size = 1. / (state['step'] + 1.)
                else:
                    step_size = self.lr

                new_p = self.prox[idx](p - step_size * grad_est, 1.)
                state['certificate'] = torch.norm((p - new_p) / step_size)
                p.copy_(new_p)
                idx += 1
        return loss


class PGDMadry(Optimizer):
    """PGD from [1]. 

    Args:
      params: [torch.Tensor]
        list of parameters to optimize

      lmo: [callable]
        list of lmo operators for each parameter

      prox: [callable or None] or None
        list of prox operators for each parameter

      lr: float > 0
        learning rate

    References:
      Madry, Aleksander, and Makelov, Aleksandar, and Schmidt, Ludwig,
      and Tsipras, Dimitris, and Vladu, Adrian. Towards Deep Learning Models
      Resistant to Adversarial Attacks. ICLR 2018.
    """
    name = 'PGD-Madry'

    def __init__(self, params, lmo, prox=None, lr=1e-2):
        self.prox = []
        for prox_el in prox:
            if prox_el is None:
                def prox_el(x, s=None):
                    return x

            def _prox(x, s=None):
                return prox_el(x.unsqueeze(0), s).squeeze()
            self.prox.append(_prox)

        self.lmo = []
        for lmo_el in lmo:
            def _lmo(u, x):
                update_direction, max_step_size = lmo_el(u.unsqueeze(0), x.unsqueeze(0))
                return update_direction.squeeze(dim=0), max_step_size
            self.lmo.append(_lmo)

        if not (type(lr) == float or lr == 'sublinear'):
            raise ValueError("lr must be float or 'sublinear'.")

        self.lr = lr
        defaults = dict(prox=self.prox, lmo=self.lmo, name=self.name)
        super(PGDMadry, self).__init__(params, defaults)

    @property
    @torch.no_grad()
    def certificate(self):
        """A generator over the current convergence certificate estimate
        for each optimized parameter."""
        for groups in self.param_groups:
            for p in groups['params']:
                state = self.state[p]
                yield state['certificate']

    @torch.no_grad()
    def step(self, step_size=None, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        idx = 0
        for groups in self.param_groups:
            for p in groups['params']:
                if p.grad is None:
                    continue
                grad = p.grad
                if grad.is_sparse:
                    raise RuntimeError(
                        'We do not yet support sparse gradients.')
                # Keep track of the step
                state = self.state[p]
                if len(state) == 0:
                    state['step'] = 0.
                state['step'] += 1.

                if self.lr == 'sublinear':
                    step_size = 1. / (state['step'] + 1.)
                else:
                    step_size = self.lr
                lmo_res, _ = self.lmo[idx](-p.grad, p)
                normalized_grad = lmo_res + p
                new_p = self.prox[idx](p + step_size * normalized_grad)
                state['certificate'] = torch.norm((p - new_p) / step_size)
                p.copy_(new_p)
                idx += 1
        return loss


class S3CM(Optimizer):
    """
    Stochastic Three Composite Minimization (S3CM)

    Args:
      params: [torch.Tensor]
        list of parameters to optimize

      prox1: [callable or None] or None
        Proximal operator for first constraint set.

      prox2: [callable or None] or None
        Proximal operator for second constraint set.
    
      lr: float > 0
        Learning rate
    
      normalization: str in {'none', 'L2', 'Linf', 'sign'}
        Normalizes the gradient. 'L2', 'Linf' divide the gradient by the corresponding norm.
        'sign' uses the sign of the gradient.

    References:
      Yurtsever, Alp, and Vu, Bang Cong, and Cevher, Volkan.
      "Stochastic Three-Composite Convex Minimization" NeurIPS 2016
    """
    name = "S3CM"
    POSSIBLE_NORMALIZATIONS = {'none', 'L2', 'Linf', 'sign'}

    def __init__(self, params, prox1=None, prox2=None, lr=.1, normalization='none'):
        if not type(lr) == float:
            raise ValueError("lr must be a float.")

        self.lr = lr
        if normalization in self.POSSIBLE_NORMALIZATIONS:
            self.normalization = normalization
        else:
            raise ValueError(f"Normalization must be in {self.POSSIBLE_NORMALIZATIONS}")

        if prox1 is None:
            prox1 = [None] * len(params)
        if prox2 is None:
            prox2 = [None] * len(params)

        self.prox1 = []
        self.prox2 = []

        for prox1_, prox2_ in zip(prox1, prox2):
            if prox1_ is None:
                def prox1_(x, s=None): return x

            if prox2_ is None:
                def prox2_(x, s=None): return x

            self.prox1.append(lambda x, s=None: prox1_(x.unsqueeze(0), s).squeeze(dim=0))
            self.prox2.append(lambda x, s=None: prox2_(x.unsqueeze(0), s).squeeze(dim=0))

        defaults = dict(lr=self.lr, prox1=self.prox1, prox2=self.prox2,
                        normalization=self.normalization)
        super(S3CM, self).__init__(params, defaults)


    @torch.no_grad()
    def step(self, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        idx = 0
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad

                grad = normalize_gradient(grad, self.normalization)

                if grad.is_sparse:
                    raise RuntimeError(
                        'S3CM does not yet support sparse gradients.')
                state = self.state[p]
                # initialization
                if len(state) == 0:
                    state['step'] = 0
                    state['iterate_1'] = p.clone().detach()
                    state['iterate_2'] = self.prox2[idx](p, self.lr)
                    state['dual'] = (state['iterate_1'] - state['iterate_2']) / self.lr

                state['iterate_2'] = self.prox2[idx](state['iterate_1'] + self.lr * state['dual'], self.lr)
                state['dual'].add_((state['iterate_1'] - state['iterate_2']) / self.lr)
                state['iterate_1'] = self.prox1[idx](state['iterate_2'] 
                                                     - self.lr * (grad + state['dual']), self.lr)

                p.copy_(state['iterate_2'])
                idx += 1


class PairwiseFrankWolfe(Optimizer):
    """Pairwise Frank-Wolfe algorithm"""
    name = "Pairwise-FW"

    def __init__(self, params, lmo_pairwise, lr=.1, momentum=.9):
        if not (type(lr) == float or lr == 'sublinear'):
            raise ValueError("lr must be float or 'sublinear'.")

        def _lmo(u, x):
            update_direction, max_step_size = lmo_pairwise(u.unsqueeze(0), x.unsqueeze(0))
            return update_direction.squeeze(dim=0), max_step_size
        self.lmo = _lmo
        self.lr = lr
        self.momentum = momentum
        defaults = dict(lmo=self.lmo, name=self.name, lr=self.lr, momentum=self.momentum)
        super(PairwiseFrankWolfe, self).__init__(params, defaults)

        raise NotImplementedError


class FrankWolfe(Optimizer):
    """Class for the Stochastic Frank-Wolfe algorithm given in Mokhtari et al.
    This is essentially Frank-Wolfe with Momentum.
    We use the tricks from [1] for gradient normalization.

    Args:
      params: [torch.Tensor]
        Parameters to optimize over.

      lmo: [callable]
        List of LMO operators.

      lr: float
        Learning rate

      momentum: float in [0, 1]
        Amount of momentum to be used in gradient estimator

      weight_decay: float > 0
        Amount of L2 regularization to be added

      normalization: str in {'gradient', 'none'}
        Gradient normalization to be used. 'gradient' option is described in [1].

    References:
      Pokutta, Sebastian, and Spiegel, Christoph and Zimmer, Max,
      Deep Neural Network Training with Frank Wolfe. 2020.
    """
    name = 'Frank-Wolfe'
    POSSIBLE_NORMALIZATIONS = {'gradient', 'none'}

    def __init__(self, params, lmo, lr=.1, momentum=.9, 
                 weight_decay=0.,
                 normalization='none'):

        self.lmo = []
        for oracle in lmo:
            def _lmo(u, x):
                update_direction, max_step_size = oracle(u.unsqueeze(0), x.unsqueeze(0))
                return update_direction.squeeze(dim=0), max_step_size
            self.lmo.append(_lmo)

        if type(lr) == float:
            if not (0. < lr <= 1.):
                raise ValueError("lr must be in (0., 1.].")
        self.lr = lr
        if type(momentum) == float:
            if not(0. <= momentum <= 1.):
                raise ValueError("Momentum must be in [0., 1.].")
        self.momentum = momentum
        if not (weight_decay >= 0):
            raise ValueError("weight_decay should be nonnegative.")
        self.weight_decay = weight_decay
        if normalization not in self.POSSIBLE_NORMALIZATIONS:
            raise ValueError(f"Normalization must be in {self.POSSIBLE_NORMALIZATIONS}.")
        self.normalization = normalization
        defaults = dict(lmo=self.lmo, name=self.name, lr=self.lr, 
                        momentum=self.momentum,
                        weight_decay=weight_decay,
                        normalization=self.normalization)
        super(FrankWolfe, self).__init__(params, defaults)

    @property
    @torch.no_grad()
    def certificate(self):
        """A generator over the current convergence certificate estimate
        for each optimized parameter."""
        for group in self.param_groups:
            for p in group['params']:
                state = self.state[p]
                yield state['certificate']

    @torch.no_grad()
    def step(self, closure=None):
        """Performs a single optimization step.
        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss
        """
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        idx = 0
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad + self.weight_decay * p
                if grad.is_sparse:
                    raise RuntimeError(
                        'SFW does not yet support sparse gradients.')
                state = self.state[p]
                if len(state) == 0:
                    state['step'] = 0
                    state['grad_estimate'] = torch.zeros_like(
                        p, memory_format=torch.preserve_format)

                if self.lr == 'sublinear':
                    step_size = 1. / (state['step'] + 1.)
                elif type(self.lr) == float:
                    step_size = self.lr
                else:
                    raise ValueError("lr must be float or 'sublinear'.")

                if self.momentum is None:
                    rho = (1. / (state['step'] + 1)) ** (1/3)
                    momentum = 1. - rho
                else:
                    momentum = self.momentum

                state['step'] += 1.

                state['grad_estimate'].add_(grad - state['grad_estimate'], alpha=1. - momentum)
                update_direction, _ = self.lmo[idx](-state['grad_estimate'], p)
                state['certificate'] = (-state['grad_estimate'] * update_direction).sum()
                if self.normalization == 'gradient':
                    grad_norm = torch.norm(state['grad_estimate'])
                    step_size = min(1., step_size * grad_norm / torch.linalg.norm(update_direction))
                elif self.normalization == 'none':
                    pass
                p.add_(step_size * update_direction)
                idx += 1
        return loss
