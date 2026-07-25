"""Token accounting and cost tracking — writes rows to llm_call_log."""

from __future__ import annotations

import sqlite3

from shotbreak.core.config import get_provider_config
from shotbreak.core.llm.provider import LLMResponse


def calc_cost(config: dict, provider_name: str, input_tokens: int, output_tokens: int) -> float:
    """Compute USD cost for a call from the provider's configured per-1k rates."""
    provider_cfg = get_provider_config(config, provider_name)
    cost_in = provider_cfg.get("cost_per_1k_input", 0.0) or 0.0
    cost_out = provider_cfg.get("cost_per_1k_output", 0.0) or 0.0
    return (input_tokens / 1000) * cost_in + (output_tokens / 1000) * cost_out


def log_call(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    pass_name: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    breakdown_run_id: int | None = None,
    scene_id: int | None = None,
    element_id: int | None = None,
    had_reference_image: bool = False,
    error: str | None = None,
) -> int:
    """Insert one llm_call_log row. Returns the new row id."""
    cur = conn.execute(
        """INSERT INTO llm_call_log
           (breakdown_run_id, project_id, pass_name, provider, model, scene_id, element_id,
            input_tokens, output_tokens, cost_usd, latency_ms, had_reference_image, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            breakdown_run_id, project_id, pass_name, provider, model, scene_id, element_id,
            input_tokens, output_tokens, cost_usd, latency_ms, int(had_reference_image), error,
        ),
    )
    return cur.lastrowid


def log_response(
    conn: sqlite3.Connection,
    config: dict,
    response: LLMResponse,
    *,
    project_id: int,
    pass_name: str,
    provider_name: str,
    breakdown_run_id: int | None = None,
    scene_id: int | None = None,
    element_id: int | None = None,
    had_reference_image: bool = False,
) -> int:
    """Convenience wrapper: compute cost from an LLMResponse and log it in one call."""
    cost_usd = calc_cost(config, provider_name, response.input_tokens, response.output_tokens)
    return log_call(
        conn,
        project_id=project_id,
        pass_name=pass_name,
        provider=response.provider,
        model=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cost_usd=cost_usd,
        latency_ms=response.latency_ms,
        breakdown_run_id=breakdown_run_id,
        scene_id=scene_id,
        element_id=element_id,
        had_reference_image=had_reference_image,
    )


def log_error(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    pass_name: str,
    provider: str,
    model: str,
    error: str,
    breakdown_run_id: int | None = None,
    scene_id: int | None = None,
    element_id: int | None = None,
) -> int:
    """Log a failed call (no usage/cost available)."""
    return log_call(
        conn,
        project_id=project_id,
        pass_name=pass_name,
        provider=provider,
        model=model,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        latency_ms=0,
        breakdown_run_id=breakdown_run_id,
        scene_id=scene_id,
        element_id=element_id,
        error=error,
    )
