from typing import NamedTuple, Tuple, Protocol
from functools import partial

import chex
import jax.numpy as jnp
import jax
from typing import Callable

from fabjax.sampling.base import TransitionOperator, LogProbFn, create_point, Point, get_intermediate_log_prob
from fabjax.sampling.resampling import log_effective_sample_size, optionally_resample
from fabjax.utils.jax_util import broadcasted_where


IntermediateLogProb = Callable[[chex.Array, int], chex.Array]


class SMCState(NamedTuple):
    """State of the SMC sampler."""
    transition_operator_state: chex.ArrayTree  # For MCMC.
    key: chex.PRNGKey  # Control randomness in resampling.


class SmcStepFn(Protocol):
    def __call__(self, x0: chex.Array, smc_state: SMCState, log_q_fn: LogProbFn, log_p_fn: LogProbFn) -> \
            Tuple[Point, chex.Array, SMCState, dict]:
        """
        Run the SMC forward pass.

        Args:
            x0: Samples from `q` for initialising the SMC chain.
            smc_state: State of the SMC sampler. Contains the parameters for the transition operator.
            log_q_fn: Log density of the base distribution (typically the flow being trained).
            log_p_fn: Log density of the target distribution that we wish to approximate with the distribution `q`.

        Returns:
            point: Final point in the SMC forward pass.
            log_w: Unnormalized log importance weights.
            smc_state: Updated SMC state.
            info: Info for diagnostics/logging.
        """


class SequentialMonteCarloSampler(NamedTuple):
    """
    Attributes:
        init: Initialise the SMC sampler state.
        step: Run a forward pass of the SMC sampler.
        transition_operator: Transition operator for performing mcmc.
        use_resampling: Whether resampling is used. If not used then the algorithm becomes AIS.
        betas: The values \in [0, 1] for interpolating between the base and SMC target distibution.
        alpha: Alpha value in alpha-divergence. The SMC target will be set to \alpha log_p - (\alpha - 1) log_q
            which is the optimal target distribution for estimating the alpha-divergence loss.
            Typically we use \alpha=2. Alternatively setting \alpha=1 sets the AIS target to \log_p.
    """
    init: Callable[[chex.PRNGKey], SMCState]
    step: SmcStepFn
    transition_operator: TransitionOperator
    use_resampling: bool
    betas: chex.Array
    alpha: float = 2.



def log_weight_contribution_point(point: Point, ais_step_index: int, betas: chex.Array, alpha: float):
    """Calculate a points contribution to the SMC log weights.
    AIS step index is between 0 and n_intermediate_distributions."""
    log_numerator = get_intermediate_log_prob(point.log_q, point.log_p, betas[ais_step_index + 1], alpha)
    log_denominator = get_intermediate_log_prob(point.log_q, point.log_p, betas[ais_step_index], alpha)
    return log_numerator - log_denominator


def ais_inner_transition(point: Point, log_w: chex.Array, trans_op_state: chex.Array, betas: chex.Array,
                        ais_step_index: int, transition_operator: TransitionOperator,
                        log_q_fn: LogProbFn, log_p_fn: LogProbFn, alpha: float) -> \
        Tuple[Tuple[Point, chex.Array], Tuple[chex.ArrayTree, dict]]:
    """Perform inner iteration of AIS, incrementing the log_w appropriately."""
    beta = betas[ais_step_index]

    new_point, trans_op_state, info = transition_operator.step(point, trans_op_state, beta, alpha, log_q_fn, log_p_fn)

    # Remove invalid samples.
    valid_samples = jnp.isfinite(new_point.log_q) & jnp.isfinite(new_point.log_p) & \
                    jnp.alltrue(jnp.isfinite(new_point.x), axis=-1)
    point = jax.tree_map(lambda a, b: broadcasted_where(valid_samples, a, b), new_point, point)

    log_w = log_w + log_weight_contribution_point(point, ais_step_index, betas, alpha)

    return (point, log_w), (trans_op_state, info)


def replace_invalid_samples_with_valid_ones(point: Point, key: chex.PRNGKey) -> Point:
    """Replace invalid (non-finite) samples in the point with valid ones (where valid ones are sampled uniformly)."""
    valid_samples = jnp.isfinite(point.log_q) & jnp.isfinite(point.log_p) & \
                    jnp.alltrue(jnp.isfinite(point.x), axis=-1)
    p = jnp.where(valid_samples, jnp.ones_like(valid_samples), jnp.zeros_like(valid_samples))
    indices = jax.random.choice(key, jnp.arange(valid_samples.shape[0]), p=p, shape=valid_samples.shape)
    alt_points = jax.tree_map(lambda x: x[indices], point)

    # Replace invalid samples with valid samples
    point = jax.tree_map(lambda a, b: broadcasted_where(valid_samples, a, b), point, alt_points)
    return point


def build_smc(
        transition_operator: TransitionOperator,
        n_intermediate_distributions: int,
        spacing_type: str = 'linear',
        alpha: float = 2.,
        use_resampling: bool = True,
        resampling_threshold: float = 0.3
              ) -> SequentialMonteCarloSampler:
    """
    Create a Sequential Monte Carlo Sampler.

    Args:
        transition_operator: Transition operator for MCMC (e.g. HMC).
        n_intermediate_distributions: Number of intermediate distributions (number of MCMC steps).
        spacing_type: Spacing between intermediate distributions `linear` or `geometric`.
        alpha: Alpha value in alpha-divergence. The SMC target will be set to \alpha log_p - (\alpha - 1) log_q
            which is the optimal target distribution for estimating the alpha-divergence loss.
            Typically we use \alpha=2. Alternatively setting \alpha=1 sets the AIS target to \log_p.
        use_resampling: Whether or not to re-sample whenever the effective sample size drops below
            `resampling_threshold`. Is equivalent to AIS if resampling is not used.
        resampling_threshold: Threshold for resampling.

    Returns:
        smc: A Sequential Monte Carlo Sampler.

    """

    if spacing_type == "geometric":
        # Rough heuristic, copying ratio used in example in AIS paper.
        # One quarter of Beta linearly spaced between 0 and 0.01
        n_intermediate_linspace_points = int(n_intermediate_distributions / 4)
        # The rest geometrically spaced between 0.01 and 1.0
        n_intermediate_geomspace_points = n_intermediate_distributions - \
                                          n_intermediate_linspace_points - 1
        betas = jnp.concatenate([jnp.linspace(0, 0.01, n_intermediate_linspace_points + 2)[:-1],
                                  jnp.geomspace(0.01, 1, n_intermediate_geomspace_points + 2)])
    elif spacing_type == "linear":
        betas = jnp.linspace(0.0, 1.0, n_intermediate_distributions + 2)
    else:
        raise NotImplementedError


    def init(key: chex.PRNGKey) -> SMCState:
        """Initialise the state of the SMC sampler."""
        key1, key2 = jax.random.split(key)
        trans_op_state = transition_operator.init(key1)
        trans_op_state = jax.tree_map(lambda x: jnp.repeat(x[None, ...], n_intermediate_distributions, axis=0),
                                      trans_op_state)
        return SMCState(trans_op_state, key2)


    def step(x0: chex.Array, smc_state: SMCState, log_q_fn: LogProbFn, log_p_fn: LogProbFn) -> \
            Tuple[Point, chex.Array, SMCState, dict]:
        """
        Run the SMC forward pass.

        Args:
            x0: Samples from `q` for initialising the SMC chain.
            smc_state: State of the SMC sampler. Contains the parameters for the transition operator.
            log_q_fn: Log density of the base distribution (typically the flow being trained).
            log_p_fn: Log density of the target distribution that we wish to approximate with the distribution `q`.

        Returns:
            point: Final point in the SMC forward pass.
            log_w: Unnormalized log importance weights.
            smc_state: Updated SMC state.
            info: Info for diagnostics/logging.
        """
        chex.assert_rank(x0, 2)  # [batch_size, dim]
        info = {}

        # Setup start of AIS chain.
        point0 = jax.vmap(
            partial(create_point, log_q_fn=log_q_fn, log_p_fn=log_p_fn, with_grad=transition_operator.uses_grad)
        )(x0)

        # Sometimes the flow produces nan samples - remove these.
        key, subkey = jax.random.split(smc_state.key)
        point0 = replace_invalid_samples_with_valid_ones(point0, key)

        log_w_init = log_weight_contribution_point(point0, 0, betas=betas, alpha=alpha)
        log_w = log_w_init

        # Run MCMC from point0 sampling from pi_0 to point_n generated by MCMC targetting pi_{n-1}.
        # Setup scan body function.
        def body_fn(carry: Point, xs: Tuple[chex.ArrayTree, int]) -> \
                Tuple[Tuple[Point, chex.Array], Tuple[chex.ArrayTree, dict]]:
            info = {}
            point, log_w = carry
            key, trans_op_state, ais_step_index = xs
            if use_resampling:
                point, log_w, log_ess = optionally_resample(key=key, log_weights=log_w, samples=point,
                                                   resample_threshold=resampling_threshold)
                info.update(ess=jnp.exp(log_ess))
            (point, log_w), (trans_op_state, info_transition) = ais_inner_transition(point, log_w, trans_op_state, betas,
                                                                          ais_step_index,
                                                                          transition_operator, log_q_fn, log_p_fn,
                                                                          alpha)
            info.update(info_transition)
            return (point, log_w), (trans_op_state, info)

        # Run scan.
        key, subkey = jax.random.split(key)
        per_step_inputs = (jax.random.split(subkey, n_intermediate_distributions),
                           smc_state.transition_operator_state,
                           jnp.arange(n_intermediate_distributions) + 1)
        scan_init = (point0, log_w)
        (point, log_w), (trans_op_states, infos) = jax.lax.scan(body_fn, init=scan_init, xs=per_step_inputs,
                                                                length=n_intermediate_distributions)

        chex.assert_trees_all_equal_structs(smc_state.transition_operator_state, trans_op_states)
        smc_state = SMCState(transition_operator_state=trans_op_states, key=key)

        # Info for logging.
        log_ess_q_p = log_effective_sample_size(point0.log_p - point0.log_q)
        log_ess_ais = log_effective_sample_size(log_w)
        info.update(log_ess_q_p=log_ess_q_p, log_ess_smc_final=log_ess_ais,
                    ess_q_p=jnp.exp(log_ess_q_p), ess_smc_final=jnp.exp(log_ess_ais))
        for i in range(n_intermediate_distributions):
            info.update({f"dist{i+1}_" + key: value for key, value in jax.tree_map(lambda x: x[i], infos).items()})


        is_finite = jnp.all(jnp.isfinite(point.x), axis=-1) & jnp.isfinite(log_w)
        info.update(n_finite_ais_samples=jnp.sum(is_finite))
        info.update(ais_max_abs_x=jnp.max(jnp.abs(point.x)))
        return point, log_w, smc_state, info


    return SequentialMonteCarloSampler(init=init, step=step, betas=betas,
                                       transition_operator=transition_operator,
                                       use_resampling=use_resampling,
                                       alpha=alpha)

