"""Config loading — config.yaml with ${VAR} environment variable interpolation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(value: Any) -> Any:
    """Recursively replace ${VAR} with os.environ[VAR] (empty string if unset)."""
    if isinstance(value, str):
        return _VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def load_config(path: str | Path = "config.yaml") -> dict:
    """Load config.yaml, interpolating ${VAR} references against the environment.

    Falls back to config.yaml.example if config.yaml doesn't exist, so a fresh
    checkout still works with env vars alone.
    """
    path = Path(path)
    if not path.exists():
        example = path.with_name(path.stem + ".yaml.example")
        if example.exists():
            path = example
        else:
            return {}

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return _interpolate(raw)


def get_provider_config(config: dict, provider_name: str) -> dict:
    """Look up a provider's config block by name, raising a clear error if missing."""
    providers = config.get("providers", {})
    if provider_name not in providers:
        raise ValueError(
            f"Unknown provider '{provider_name}'. Configured providers: {list(providers)}"
        )
    return providers[provider_name]


def get_pass_provider(config: dict, pass_name: str) -> str:
    """Resolve which provider a given pass should use, falling back to default_provider."""
    pass_models = config.get("pass_models", {})
    return pass_models.get(pass_name) or config.get("default_provider", "anthropic")
