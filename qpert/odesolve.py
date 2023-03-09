from typing import Callable
import torch
import beeblo as bb
import functorch


def func_and_jac(func: Callable[[torch.Tensor, torch.Tensor], torch.Tensor], yt: torch.Tensor, tpts: torch.Tensor):
    # func(y, t) -> dy/dt: [(..., ny), (..., 1)] -> (..., ny)
    # yt: (..., nt, ny)
    # tpts: (..., nt, 1)
    # returns: (..., nt, ny) and (..., nt, ny, ny)
    nbatch_dims = yt.ndim - 1
    jacfunc = functorch.jacrev(func, argnums=0)  # [(ny), (1)] -> (ny, ny)
    for _ in range(nbatch_dims):
        jacfunc = functorch.vmap(jacfunc)

    # compute the function and its jacobian
    fyt = func(yt, tpts)
    jac_fyt = jacfunc(yt, tpts)
    return fyt, jac_fyt

def conv_gt(rhs: torch.Tensor, gt: torch.Tensor, y0: torch.Tensor, tpts: torch.Tensor) -> torch.Tensor:
    # solve dy/dt + g(t) y = rhs(t) with y(0) = y0
    # rhs: (..., nt)
    # gt: (..., nt)
    # y0: (..., 1)
    # tpts: (..., nt)
    # return: (..., nt)

    # applying conv_gt(rhs) + conv_gt(y0 * delta(0))
    dt = tpts[..., 1:] - tpts[..., :-1]  # (..., nt - 1)
    half_dt = dt * 0.5

    # integrate gt with trapezoidal method
    trapz_area = (gt[..., :-1] + gt[..., 1:]) * half_dt  # (..., nt - 1)
    zero_pad = torch.zeros((*gt.shape[:-1], 1), dtype=gt.dtype, device=gt.device)  # (..., 1)
    gt_int = torch.cumsum(trapz_area, dim=-1)  # (..., nt - 1)
    gt_int = torch.cat((zero_pad, gt_int), dim=-1)  # (..., nt)

    # compute log[integral_0^t rhs(tau) * exp(gt_int(tau)) dtau] with trapezoidal method
    exp_content = torch.log(torch.complex(rhs, torch.zeros_like(rhs))) + gt_int  # (..., nt)
    # TODO: change this to logaddexp
    exp_content2 = torch.stack((exp_content[..., :-1], exp_content[..., 1:]), dim=-1) + torch.log(half_dt)[..., None]  # (..., nt - 1, 2)
    trapz_area_gj = torch.logcumsumexp(exp_content2, dim=-1)[..., -1]  # (..., nt - 1)
    log_area_int = torch.logcumsumexp(trapz_area_gj, dim=-1)  # (..., nt - 1)
    log_conv = log_area_int - gt_int[..., 1:]  # (..., nt - 1)
    conv_res = torch.exp(log_conv).real
    # print(exp_content.shape, gt_int.shape, exp_content2.shape, trapz_area_gj.shape)
    conv_res = torch.cat((zero_pad, conv_res), dim=-1)  # (..., nt)

    # add the initial condition
    conv_res = conv_res + y0 * torch.exp(-gt_int)

    # results: exp(-gt_int(t)) * integral(t)
    # res = torch.exp(log_integral - gt_int)
    return conv_res

def solve_ivp(func: Callable[[torch.Tensor, torch.Tensor], torch.Tensor], y0: torch.Tensor, tpts: torch.Tensor):
    # func(y, t) -> dy/dt: [(..., ny), (..., 1)] -> (..., ny)
    # y0: (..., ny)
    # tpts: (..., nt)
    # returns: (..., nt, ny)
    ny = y0.shape[-1]
    y0 = y0.unsqueeze(-2)  # (..., 1, ny)
    tpts = tpts.unsqueeze(-1)  # (..., nt, 1)

    # define functions that will be frequently used
    T = lambda x: x.transpose(-2, -1)
    bmm = lambda x, y: (x @ y[..., None])[..., 0]
    solve = lambda x, y: torch.linalg.solve(x, y[..., None])[..., 0]

    # first guess: all zeros
    # yt0: (..., nt, ny)
    yt = torch.zeros((*y0.shape[:-2], tpts.shape[-2], y0.shape[-1]), dtype=y0.dtype, device=y0.device)
    converge = False
    for i in range(100):
        # fyt0: (..., nt, ny), jac_fyt0: (..., nt, ny, ny)
        fyt, jac_fyt = func_and_jac(func, yt, tpts)

        if ny > 1:
            # add random noise to increase the chance of diagonalizability
            jac_fyt2 = jac_fyt + torch.randn_like(jac_fyt) * 1e-8
            # eival_g: (..., nt, ny), eivec_g: (..., nt, ny, ny)
            eival_g, eivec_g = torch.linalg.eig(jac_fyt2)

            # compute the right hand side (the argument of the inverse linear operator)
            # rhs: (..., nt, ny)
            rhs = fyt - bmm(jac_fyt, yt)
            wrhs = solve(eivec_g, rhs)  # (..., nt, ny)

            # compute the initial values
            u0 = solve(eivec_g, y0)  # (..., nt, ny)
            
            # compute the convolution
            ut = T(conv_gt(T(wrhs), T(eival_g), T(u0), T(tpts)))  # (..., nt, ny)
            yt_new = bmm(eivec_g, ut)  # (..., nt, ny)

        else:
            gt = -jac_fyt[..., 0]
            gty = gt * yt  # (..., nt, ny)

            rhs = fyt + gty  # (..., nt, ny)
            # rhs.T: (..., ny, nt), gt.T: (..., ny, nt), y0.T: (..., ny, 1), tpts.T: (..., 1, nt)
            yt_new = conv_gt(rhs.transpose(-2, -1), gt.transpose(-2, -1), y0.transpose(-2, -1), tpts.transpose(-2, -1))
            yt_new = yt_new.transpose(-2, -1)  # (..., nt, ny)

        diff = torch.mean(torch.abs(yt_new - yt))
        print(f"Iter {i + 1}:", diff)
        yt = yt_new
        if diff < 1e-6:
            converge = True
            break

    if not converge:
        print("Does not converge")
    return yt

if __name__ == "__main__":
    torch.manual_seed(123)
    dtype = torch.float64
    device = torch.device('cuda')
    module = bb.nn.MLP(1, 1).to(dtype).to(device)
    def func(y, t):
        # (..., ny), (..., 1) -> (..., ny)
        dfdy = -module(y) * 60 * y - 10 * y ** 3 + torch.sin(600 * t)
        return dfdy

    def fun(t, y):
        y = torch.as_tensor(y)
        t = torch.as_tensor(t)
        fy = func(y, t)
        return fy.detach().numpy()

    npts = 100000
    tpts = torch.linspace(0, 10, npts, dtype=dtype, device=device)  # (ntpts,)
    y0 = torch.zeros(1, dtype=dtype, device=device)  # (ny=1,)
    import time
    t0 = time.time()
    with torch.no_grad():
        yt = solve_ivp(func, y0, tpts).detach()  # (ntpts, ny)
    t1 = time.time()
    print(t1 - t0)

    from scipy.integrate import solve_ivp as solve_ivp2
    module = module.to(torch.device('cpu'))
    t0 = time.time()
    res = solve_ivp2(fun, t_span=(tpts[0].cpu(), tpts[-1].cpu()), y0=y0.cpu(), t_eval=tpts.cpu(), atol=1e-6, rtol=1e-7)
    t1 = time.time()
    print(t1 - t0)

    import matplotlib.pyplot as plt
    plt.plot(tpts.cpu(), yt.cpu()[..., 0])
    plt.plot(res.t, res.y[0])
    plt.savefig("fig.png")


"""
dy/dt = f(y, t)
dydt + g(t) * y = f(y, t) + g(t) * y
y = conv_gt(f(y, t) + g(t) * y) + conv_gt(y0 * delta(0))
g(t) = -df / dy
"""
