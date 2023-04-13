from typing import NamedTuple, Sequence, Union

import chex
import distrax
import jax.numpy as jnp

from fabjax.flow.flow import FlowRecipe, Flow, create_flow
from fabjax.flow.distrax_with_extra import ChainWithExtra
from fabjax.flow.build_coupling_bijector import build_split_coupling_bijector
from fabjax.flow.act_norm import build_act_norm_layer

class FlowDistConfig(NamedTuple):
    dim: int
    n_layers: int
    conditioner_mlp_units: Sequence[int]
    type: Union[str, Sequence[str]] = 'split_coupling'
    act_norm: bool = True
    identity_init: bool = True
    compile_n_unroll: int = 2


def build_flow(config: FlowDistConfig) -> Flow:
    recipe = create_flow_recipe(config)
    flow = create_flow(recipe)
    return flow


def create_flow_recipe(config: FlowDistConfig) -> FlowRecipe:
    flow_type = [config.type] if isinstance(config.type, str) else config.type
    for flow in flow_type:
        assert flow in ['split_coupling']

    def make_base() -> distrax.Distribution:
        base = distrax.MultivariateNormalDiag(loc=jnp.zeros(config.dim), scale_diag=jnp.ones(config.dim))
        return base

    def make_bijector():
        # Note that bijector.inverse moves through this forwards, and bijector.fowards reverses the bijector order
        bijectors = []
        if config.act_norm:
            bijectors.append(build_act_norm_layer(dim=config.dim, identity_init=config.identity_init))
        if 'split_coupling' in flow_type:
            bijector = build_split_coupling_bijector(
                dim=config.dim,
                identity_init=config.identity_init,
                mlp_units=config.conditioner_mlp_units
            )
            bijectors.append(bijector)

        return ChainWithExtra(bijectors)


    definition = FlowRecipe(
        make_base=make_base,
        make_bijector=make_bijector,
        n_layers=config.n_layers,
        config=config,
        dim=config.dim,
        compile_n_unroll=config.compile_n_unroll,
        )
    return definition
