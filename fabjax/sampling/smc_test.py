"""smc seems to be working, but is very sensitive to the tuning of the step size of HMC."""

import chex
import jax.numpy as jnp
import jax
import distrax
import matplotlib.pyplot as plt
import numpy as np
from tqdm import tqdm

from fabjax.sampling.mcmc.hmc import build_blackjax_hmc
from fabjax.sampling.mcmc.metropolis import build_metropolis
from fabjax.sampling.smc import build_smc
from fabjax.sampling.resampling import simple_resampling
from fabjax.sampling.base import create_point
from fabjax.utils.plot import plot_contours_2D, plot_marginal_pair, plot_history
from fabjax.utils.logging import ListLogger


def analytic_alpha_2_div(mean_q: chex.Array, mean_p: chex.Array):
    """Calculate \int p(x)^2/q dx where p and q are unit-variance Gaussians."""
    return jnp.exp(jnp.sum(mean_p**2 + mean_q**2 - 2*mean_p*mean_q))


def plot_samples(x_smc, x_p, x_q, dist_q, dist_p):
    # Visualise samples.
    bound = 10
    fig, axs = plt.subplots(2, 2, figsize=(15, 15), sharex=True, sharey=True)
    # smc samples
    plot_contours_2D(dist_q.log_prob, axs[0, 0], bound=bound)
    plot_marginal_pair(x_smc, axs[0, 0], bounds=(-bound, bound), alpha=0.2)
    plot_contours_2D(lambda x: 2*dist_p.log_prob(x) - dist_q.log_prob(x), axs[0, 1], bound=bound)
    plot_marginal_pair(x_smc, axs[0, 1], bounds=(-bound, bound), alpha=0.2)

    # Samples from the distributions themselves
    plot_contours_2D(dist_p.log_prob, axs[1, 0], bound=bound)
    plot_marginal_pair(x_q, axs[1, 0], bounds=(-bound, bound), alpha=0.2)
    plot_contours_2D(dist_p.log_prob, axs[1, 1], bound=bound)
    plot_marginal_pair(x_p, axs[1, 1], bounds=(-bound, bound), alpha=0.2)

    axs[0, 0].set_title("samples smc vs log prob q contours")
    axs[0, 1].set_title("samples smc vs log prob p^2/q contours")
    axs[1, 0].set_title("samples q vs log prob q contours")
    axs[1, 1].set_title("samples p vs log prob p contours")
    plt.tight_layout()
    plt.show()


def problem_setup(dim: int):

    loc_q = jnp.zeros((dim,))
    scale_q = jnp.ones((dim,))
    dist_q = distrax.MultivariateNormalDiag(loc_q, scale_q)

    loc_p = jnp.zeros((dim,)) + 1
    scale_p = scale_q
    dist_p = distrax.MultivariateNormalDiag(loc_p, scale_p)
    true_Z = analytic_alpha_2_div(loc_q, loc_p)

    return dist_q, dist_p, true_Z


def setup_smc(
        n_intermediate_distributions: int,
        tune_step_size: bool = True,
        transition_n_outer_steps: int = 1,
        target_p_accept = 0.65,
        spacing_type = 'linear',
        resampling: bool = False,
        alpha = 2.,
        use_hmc: bool = False
):
    if use_hmc:
        init_step_size = 1e-4 if tune_step_size else 0.1  # If step size fixed then pick a good one.
        transition_operator = build_blackjax_hmc(dim=2, n_outer_steps=transition_n_outer_steps,
                                                 init_step_size=init_step_size, target_p_accept=target_p_accept,
                                                 adapt_step_size=tune_step_size)
    else:
        init_step_size = 1e-4 if tune_step_size else 1.  # If step size fixed then pick a good one.
        transition_operator = build_metropolis(dim=2, n_steps=transition_n_outer_steps, init_step_size=init_step_size,
                                               target_p_accept=target_p_accept, tune_step_size=tune_step_size)

    smc = build_smc(transition_operator=transition_operator,
                    n_intermediate_distributions=n_intermediate_distributions, spacing_type=spacing_type,
                    alpha=alpha, use_resampling=resampling)
    return smc




def test_smc_visualise_as_step_size_tuned():
    """Visualise smc as we tune the hmc params"""

    dim = 2
    key = jax.random.PRNGKey(0)
    batch_size = 1000
    n_smc_distributions = 20

    dist_q, dist_p, true_Z = problem_setup(dim)
    smc = setup_smc(n_smc_distributions)
    x_p = dist_p.sample(seed=key, sample_shape=(batch_size,))
    x_q = dist_q.sample(seed=key, sample_shape=(batch_size,))


    smc_state = smc.init(key)

    logger = ListLogger()



    n_iters = 20
    n_plots = 5
    plot_iter = np.linspace(0, n_iters-1, n_plots, dtype=int)

    # Run MCMC chain.
    for i in range(n_iters):
        # Loop so transition operator step size can be tuned.
        # This makes a massive difference.
        key, subkey = jax.random.split(key)
        positions = dist_q.sample(seed=subkey, sample_shape=(batch_size,))
        point, log_w, smc_state, info = smc.step(positions, smc_state, dist_q.log_prob, dist_p.log_prob)

        # Plot and log info.
        if i in plot_iter:
            plot_samples(point.x, x_p, x_q, dist_q, dist_p)
        Z_estimate = jnp.mean(jnp.exp(log_w))
        log_abs_error = jnp.log(jnp.abs(Z_estimate - true_Z))  # log makes scale more reasonable in plot.
        info.update(log_abs_error_z_estimate=log_abs_error)
        logger.write(info)


    plot_history(logger.history)
    plt.show()


def test_reasonable_resampling():
    """One way to check the log weights are reasonable is to resample in proportion to them, and see if it makes the
    samples more similar to the target."""

    dim = 2
    key = jax.random.PRNGKey(0)
    batch_size = 1000
    n_smc_distributions = 1  # Make smc low quality.

    # Setup
    dist_q, dist_p, true_Z = problem_setup(dim)
    smc = setup_smc(n_smc_distributions, tune_step_size=False)
    x_p = dist_p.sample(seed=key, sample_shape=(batch_size,))
    x_q = dist_q.sample(seed=key, sample_shape=(batch_size,))
    smc_state = smc.init(key)

    # Generate samples.
    key, subkey = jax.random.split(key)
    positions = dist_q.sample(seed=subkey, sample_shape=(batch_size,))
    point, log_w, smc_state, info = smc.step(positions, smc_state, dist_q.log_prob, dist_p.log_prob)

    _, resamples = simple_resampling(key, log_w, point)

    # Visualise samples before and after resampling.
    plot_samples(point.x, x_p, x_q, dist_q, dist_p)
    plot_samples(resamples.x, x_p, x_q, dist_q, dist_p)


def test_smc_visualise_with_num_dists():
    """Visualise quality of smc estimates with increasing number of intermediate distributions."""
    dim = 2
    key = jax.random.PRNGKey(0)
    batch_size = 128
    alpha = 2.  # sets target to just be p.

    logger = ListLogger()

    dist_q, dist_p, true_Z = problem_setup(dim)
    true_Z = 1. if alpha == 1. else true_Z
    x_p = dist_p.sample(seed=key, sample_shape=(batch_size,))
    x_q = dist_q.sample(seed=key, sample_shape=(batch_size,))



    for n_smc_distributions in tqdm([1, 4, 16, 64, 128]):
        # Setup
        smc = setup_smc(n_smc_distributions, tune_step_size=True, alpha=alpha, spacing_type='geometric')
        smc_state = smc.init(key)

        for i in range(20):  # Allow for some step size tuning - ends up being SUPER IMPORTANT.
            # Generate samples.
            key, subkey = jax.random.split(key)
            positions = dist_q.sample(seed=subkey, sample_shape=(batch_size,))
            point, log_w, smc_state, info = smc.step(positions, smc_state, dist_q.log_prob, dist_p.log_prob)

        Z_estimate = jnp.mean(jnp.exp(log_w))
        log_abs_error = jnp.log(jnp.abs(Z_estimate - true_Z))  # log makes scale more reasonable in plot.
        info.update(log_abs_error_z_estimate=log_abs_error)
        info.update(z_estimate=Z_estimate)
        logger.write(info)

        plot_samples(point.x, x_p, x_q, dist_q, dist_p)
        _, resamples = simple_resampling(key, log_w, point)
        plot_samples(resamples.x, x_p, x_q, dist_q, dist_p)

    keys = ['log_abs_error_z_estimate', 'ess_smc_final', 'dist1_mean_acceptance_rate', 'z_estimate', 'dist1_step_size']
    history = {key: logger.history[key] for key in keys}
    plot_history(history)
    plt.show()
