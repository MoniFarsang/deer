from abc import abstractmethod
from typing import Any, Callable, List, Optional
import jax.numpy as jnp
from deer.deer_iter import deer_mode2_iteration
from deer.maths import matmul_recursive
from deer.utils import get_method_meta, check_method


__all__ = ["solve_idae"]

def solve_idae(func: Callable[[jnp.ndarray, jnp.ndarray, Any, Any], jnp.ndarray],
               y0: jnp.ndarray, xinp: Any, params: Any,
               tpts: jnp.ndarray,
               method: Optional["SolveIDAEMethod"] = None,
               ) -> jnp.ndarray:
    r"""
    Solve the implicit differential algebraic equations (IDAE) systems.

    .. math::

        f(\dot{y}, y, x; \theta) = 0

    where :math:`\dot{y}` is the time-derivative of the output signal :math:`y`, :math:`x` is the input signal
    at given sampling time :math:`t`, and :math:`\theta` are the parameters of the function.
    The tentative initial condition is given by :math:`y(0) = y_0`.

    Arguments
    ---------
    func: Callable[[jnp.ndarray, jnp.ndarray, Any, Any], jnp.ndarray]
        Function to evaluate the residual of the IDAE system.
        The arguments are:
        (1) time-derivative of the output signal :math:`\dot{y}` ``(ny,)``,
        (2) output signal :math:`y` ``(ny,)``,
        (3) input signal :math:`x` ``(*nx,)`` in a pytree, and
        (4) parameters :math:`\theta` in a pytree.
        The return value is the residual of the IDAE system ``(ny,)``.
    y0: jnp.ndarray
        Tentative initial condition on :math:`y` ``(ny,)``. If the IDAE system has algebraic variables, then
        the initial values of the algebraic variables might be different to what is supplied.
    xinp: Any
        The external input signal of shape ``(nsamples, *nx)`` in a pytree.
    params: Any
        The parameters of the function ``func``.
    tpts: jnp.ndarray
        The time points to evaluate the solution ``(nsamples,)``.
    yinit_guess: jnp.ndarray or None
        The initial guess of the output signal ``(nsamples, ny)``.
        If None, it will be initialized as 0s.
    max_iter: int
        The maximum number of iterations to perform.
    memory_efficient: bool
        If True, then use the memory efficient algorithm for the DEER iteration.
    """
    if method is None:
        method = DEER()
    check_method(method, solve_idae)
    return method.compute(func, y0, xinp, params, tpts)

class SolveIDAEMethod(metaclass=get_method_meta(solve_idae)):
    @abstractmethod
    def compute(self, func: Callable[[jnp.ndarray, Any, Any], jnp.ndarray],
                y0: jnp.ndarray, xinp: Any, params: Any, tpts: jnp.ndarray):
        pass

class DEER(SolveIDAEMethod):
    """
    Solve the implicit DAE method using DEER method for backward Euler's method.

    Arguments
    ---------
    yinit_guess: Optional[jnp.ndarray]
        The initial guess of the output signal ``(nsamples, ny)``.
        If None, it will be initialized as all ``y0``.
    max_iter: int
        The maximum number of DEER iterations to perform.
    memory_efficient: bool
        If True, then use the memory efficient algorithm for the DEER iteration.
    """
    def __init__(self, yinit_guess: Optional[jnp.ndarray] = None, max_iter: int = 10000,
                 memory_efficient: bool = True):
        self.yinit_guess = yinit_guess
        self.max_iter = max_iter
        self.memory_efficient = memory_efficient

    def compute(self, func: Callable[[jnp.ndarray, Any, Any], jnp.ndarray],
                y0: jnp.ndarray, xinp: Any, params: Any, tpts: jnp.ndarray):
        # y0: (ny,) the initial states (it's not checked for correctness)
        # xinp: pytree, each has `(nsamples, *nx)`
        # tpts: (nsamples,) the time points
        # returns: (nsamples, ny), including the initial states

        # set the default initial guess
        yinit_guess = self.yinit_guess
        if yinit_guess is None:
            yinit_guess = jnp.zeros((tpts.shape[0], y0.shape[-1]), dtype=tpts.dtype) + y0

        def func2(yshifts: List[jnp.ndarray], x: Any, params: Any) -> jnp.ndarray:
            # yshifts: [2] + (ny,)
            # x is dt
            y, ym1 = yshifts
            dt, xinp = x
            return func((y - ym1) / dt, y, xinp, params)

        def linfunc(y: jnp.ndarray, lin_params: Any) -> List[jnp.ndarray]:
            # y: (nsamples, ny)
            # we're using backward euler's method, so we need to shift the values by one
            ym1 = jnp.concatenate((y[:1], y[:-1]), axis=0)  # (nsamples, ny)
            return [y, ym1]

        # dt[i] = t[i] - t[i - 1]
        dt_partial = tpts[1:] - tpts[:-1]  # (nsamples - 1,)
        dt = jnp.concatenate((dt_partial[:1], dt_partial), axis=0)  # (nsamples,)

        xinput = (dt, xinp)
        inv_lin_params = (y0,)
        yt = deer_mode2_iteration(
            lin_func=linfunc,
            inv_lin=self.solve_idae_inv_lin,
            func=func2,
            p_num=2,
            params=params,
            xinput=xinput,
            inv_lin_params=inv_lin_params,
            yinit_guess=yinit_guess,
            max_iter=self.max_iter,
            memory_efficient=self.memory_efficient,
            clip_ytnext=True,
        )
        return yt

    def solve_idae_inv_lin(self, jacs: List[jnp.ndarray], z: jnp.ndarray,
                        inv_lin_params: Any) -> jnp.ndarray:
        # solving the equation: M0_i @ y_i + M1_i @ y_{i-1} = z_i
        # M: (nsamples, ny, ny)
        # G: (nsamples, ny, ny)
        # rhs: (nsamples, ny)
        # inv_lin_params: (y0,) where tpts: (nsamples,), y0: (ny,)
        M0, M1 = jacs
        y0, = inv_lin_params  # tpts: (nsamples,), y0: (ny,)

        # using index [1:] because we don't need to compute y_0 again (it's already available from y0)
        M0inv = jnp.linalg.inv(M0[1:])
        M0invM1 = -jnp.einsum("...ij,...jk->...ik", M0inv, M1[1:])
        M0invz = jnp.einsum("...ij,...j->...i", M0inv, z[1:])
        y = matmul_recursive(M0invM1, M0invz, y0)  # (nsamples, ny)
        return y
