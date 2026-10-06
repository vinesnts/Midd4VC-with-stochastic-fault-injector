"""Constant fault-acceleration and time-recovery utilities.

For the constant-scaling model from Section 26.1:

    CF = 1 / AF
    t_accelerated = t_original * CF = t_original / AF
    t_original = t_accelerated / CF = t_accelerated * AF

Factors are configured as ``AF_<parameter>`` environment variables.  A value
of 1.0 preserves the original behavior.
"""

from __future__ import annotations

import os


def acceleration_factor(parameter: str, default: float = 1.0) -> float:
    """Return the positive acceleration factor for a time parameter."""
    raw_value = os.getenv(f"AF_{parameter}", str(default))
    factor = float(raw_value)
    if factor <= 0:
        raise ValueError(f"AF_{parameter} must be greater than zero")
    return factor


def compression_factor(parameter: str, default: float = 1.0) -> float:
    """Return CF, the inverse of the acceleration factor."""
    return 1.0 / acceleration_factor(parameter, default)


def accelerated_time(original_seconds: float, parameter: str) -> float:
    """Compress an original time value for accelerated execution."""
    return float(original_seconds) / acceleration_factor(parameter)


def recovered_time(accelerated_seconds: float, parameter: str) -> float:
    """Recover the original time from an accelerated time value."""
    return float(accelerated_seconds) * acceleration_factor(parameter)


def scaling_metadata(parameter: str) -> dict[str, float | str]:
    """Return auditable original/AF/CF metadata for a time parameter."""
    factor = acceleration_factor(parameter)
    return {
        "parameter": parameter,
        "acceleration_factor": factor,
        "compression_factor": 1.0 / factor,
    }

