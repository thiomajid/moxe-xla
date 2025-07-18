import functools

import jax
import jax.numpy as jnp


@functools.partial(jax.jit, static_argnames=("eps", "normalize"))
def compute_entropy(probs: jax.Array, eps: float = 1e-6, normalize: bool = True):
    """
    Computes the entropy of a probability distribution.

    If 'normalize' is True, the entropy is divided by log(n), where n is the number
    of outcomes, so that a uniform distribution yields a value of 1.

    Parameters:
        probs (jax.Array): Tensor of probabilities with shape (..., n)
        eps (float): Small constant to avoid log(0)
        normalize (bool): Whether to normalize the entropy by log(n)

    Returns:
        jax.Array: The (normalized) entropy
    """

    # Compute raw entropy (using the natural logarithm)
    cliped_probs = jnp.clip(probs, min=eps)
    entropy = -jnp.sum(probs * jnp.log(cliped_probs), axis=-1)
    entropy = jax.lax.cond(
        normalize,
        # Maximum entropy (for a uniform distribution) is log(n)
        # probs.shape[-1] represents the number of possible outcomes
        lambda x: x / jnp.log(jnp.array(probs.shape[-1], dtype=probs.dtype)),
        lambda x: x,
        operand=entropy,
    )

    return entropy


@jax.jit
def self_balance_auxiliary_group_loss(pm: jax.Array, ps: jax.Array):
    """
    Calculates the self-balance auxiliary group loss.  This loss penalizes the deviation
    of the mLSTM expert probability (pm) and sLSTM expert probability (ps) from each other,
    encouraging them to be balanced.

    Args:
        pm (jax.Array): Scalar tensor for mLSTM expert probability.
        ps (jax.Array): Scalar tensor for sLSTM expert probability.

    Returns:
        jax.Array: A scalar tensor representing the loss.
    """
    return (pm - ps) ** 2


@jax.jit
def bounded_auxiliary_group_loss(pm: jax.Array, ps: jax.Array):
    """
    Calculates a bounded auxiliary group loss. This loss penalizes the deviation of both
    mLSTM expert probability (pm) and sLSTM expert probability (ps) from an ideal value of 0.5,
    encouraging both experts to be moderately active.

    Args:
        pm (jax.Array): Scalar tensor for mLSTM expert probability.
        ps (jax.Array): Scalar tensor for sLSTM expert probability.

    Returns:
        jax.Array: A scalar tensor representing the loss.
    """
    ideal = 0.5
    loss = (pm - ideal) ** 2 + (ps - ideal) ** 2

    return loss


@functools.partial(jax.jit, static_argnames=("eps",))
def kl_auxiliary_group_loss(pm: jax.Array, ps: jax.Array, eps: float = 1e-8):
    """
    Computes KL divergence between observed distribution p = [pm, ps] and target q = [0.5, 0.5].
    Given that KL(p||q) = p*log(p/0.5), this loss is computed as:
      L = pm * log(2*pm) + ps * log(2*ps)

    This loss encourages the expert probabilities to be close to a uniform distribution.

    Args:
        pm (jax.Array): Scalar tensor for mLSTM expert probability.
        ps (jax.Array): Scalar tensor for sLSTM expert probability.
        eps (float): Small constant for numerical stability.

    Returns:
        jax.Array: A scalar tensor representing the loss.
    """
    loss = pm * jnp.log(2 * pm + eps) + ps * jnp.log(2 * ps + eps)
    return loss


@functools.partial(jax.jit, static_argnames=("eps",))
def js_auxiliary_group_loss(pm: jax.Array, ps: jax.Array, eps: float = 1e-8):
    """
    Computes Jensen-Shannon divergence between observed distribution p = [pm, ps]
    and target uniform distribution q = [0.5, 0.5].

    JS(P||Q) = 0.5 * KL(P||M) + 0.5 * KL(Q||M) where M = 0.5 * (P + Q)

    For expert balancing, this provides smoother gradients than KL divergence
    and is bounded between 0 and log(2) ≈ 0.693.

    Args:
        pm (jax.Array): Scalar tensor for mLSTM expert probability.
        ps (jax.Array): Scalar tensor for sLSTM expert probability.
        eps (float): Small constant for numerical stability.

    Returns:
        jax.Array: A scalar tensor representing the loss (non-negative).
    """
    # Calculate midpoint distribution M = 0.5 * (P + Q)
    m_pm = 0.25 + 0.5 * pm  # 0.5 * (pm + 0.5)
    m_ps = 0.25 + 0.5 * ps  # 0.5 * (ps + 0.5)

    # Calculate KL(P||M)
    kl_p_m = pm * jnp.log((pm + eps) / (m_pm + eps)) + ps * jnp.log(
        (ps + eps) / (m_ps + eps)
    )

    # Calculate KL(Q||M) where Q = [0.5, 0.5]
    kl_q_m = 0.5 * jnp.log(0.5 / (m_pm + eps)) + 0.5 * jnp.log(0.5 / (m_ps + eps))

    # JS = 0.5 * (KL(P||M) + KL(Q||M))
    js_div = 0.5 * (kl_p_m + kl_q_m)

    return js_div


@jax.jit
def router_z_loss(router_logits: jax.Array):
    """
    Calculates the router z-loss, as introduced in (https://arxiv.org/abs/2202.08906).
    This loss encourages the router logits to be more confident.

    Args:
        router_raw_logits (jax.Array): The raw logits from the router.

    Returns:
        jax.Array: A scalar tensor representing the z-loss.
    """
    z_loss = jnp.mean(jax.nn.logsumexp(router_logits, axis=-1) ** 2)
    return z_loss


@functools.partial(
    jax.jit,
    static_argnames=("num_experts", "top_k"),
)
def auxiliary_load_balancing_loss(
    num_experts: int,
    router_probs: jax.Array,
    top_k: int,
):
    """
    Calculates the auxiliary load balancing loss for Mixture of Experts (MoE) models,
    as described in (https://arxiv.org/abs/2101.03961). This loss encourages a more uniform distribution
    of tokens across experts, preventing some experts from being overloaded while others are underutilized.

    Args:
        num_experts (int): The total number of experts in the MoE layer.
        selected_experts (jax.Array): A tensor indicating which experts were selected for each token.
                                        Shape: (batch_size, sequence_length, top_k) or (total_tokens, top_k)
        selected_experts_weights (jax.Array): The weights assigned to each selected expert for each token.
                                                Shape: (batch_size, sequence_length, top_k) or (total_tokens, top_k)
        top_k (int): The number of experts selected per token.

    Returns:
        Tuple[jax.Array, jax.Array, jax.Array]: A tuple containing:
            - aux_loss (jax.Array): A scalar tensor representing the auxiliary load balancing loss.
            - expert_load (jax.Array): A tensor containing the sum of weights routed to each expert.
            - expert_token_counts (jax.Array): A tensor containing the number of tokens routed to each expert.
    """
    # Infer dtype from weights for load calculation
    load_dtype = router_probs.dtype
    count_dtype = jnp.int32

    selected_experts_weights, selected_experts = jax.lax.top_k(router_probs, top_k)

    # Initialize count tensors for each expert
    expert_load = jnp.zeros(num_experts, dtype=load_dtype)
    expert_token_counts = jnp.zeros(num_experts, dtype=count_dtype)

    # Reshape selected_experts and their weights for scatter operation
    flat_experts = selected_experts.reshape(-1)  # [B*S*top_k] or [total_tokens*top_k]
    flat_weights = selected_experts_weights.reshape(
        -1
    )  # [B*S*top_k] or [total_tokens*top_k]

    # Accumulate weights (load) by expert index
    expert_load = expert_load.at[flat_experts].add(flat_weights)

    # Accumulate token counts by expert index (add 1 for each assignment)
    # Create an array of ones with the same shape as flat_weights but with count_dtype
    ones_for_counts = jnp.ones_like(flat_weights, dtype=count_dtype)
    expert_token_counts = expert_token_counts.at[flat_experts].add(ones_for_counts)

    # Calculate total tokens from the shape of selected_experts (assuming B, S, K or N, K)
    total_tokens = jax.lax.cond(
        selected_experts.ndim == 3,
        lambda: selected_experts.shape[0] * selected_experts.shape[1],
        lambda: selected_experts.shape[0],
    )

    # Normalize expert_load by total tokens * top_k to get average usage probability per expert
    # This represents the fraction of total routing probability mass directed to each expert
    expert_usage = expert_load / jnp.array(total_tokens * top_k, dtype=load_dtype)

    # Calculate auxiliary loss for load balancing (mean squared error vs uniform)
    ideal_usage = jnp.ones_like(expert_usage) / num_experts
    # The scaling factor num_experts is common practice, matching the original paper's scaling
    aux_loss = jnp.mean((expert_usage - ideal_usage) ** 2) * num_experts

    return (
        aux_loss,
        expert_load,
        expert_token_counts,
    )
