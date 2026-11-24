"""Compatibility module: the SSM-bound ``ConfigSync`` (design D6).

The generic version-tracking/validation/apply shell now lives in
``quant.config``; SSM-specific behavior (IMDS region, version short-circuit on
the SSM ``Version`` number, invalid-payload-once) lives in the SSM backend
``quant.config.ssm.SSMConfigSource`` — not in the core shell. This module
keeps the historical SSM-bound constructor surface working for downstream
importers::

    from quant.live.config_sync import ConfigSync  # SSM-backed (quant[ssm])

The live trader itself selects backends by configuration via
``quant.config.ConfigSync`` (local-first file/env default; ``ssm`` opt-in).
"""

from __future__ import annotations

import logging
from typing import Any

from quant.config import ConfigSync as _ConfigSync
from quant.config.base import validate_config
from quant.config.ssm import _imds_region

__all__ = ["ConfigSync", "_imds_region"]


class ConfigSync(_ConfigSync):
    """SSM-backed ``ConfigSync`` with the historical constructor surface.

    Equivalent to building ``quant.config.ConfigSync`` over an
    ``SSMConfigSource``; requires the ``quant[ssm]`` extra at poll time (boto3
    is imported lazily, exactly as with the generic shell).
    """

    def __init__(
        self,
        ssm_param_name: str,
        logger: logging.Logger | None = None,
        region_name: str | None = None,
    ) -> None:
        from quant.config.ssm import SSMConfigSource  # lazy boto3

        source = SSMConfigSource(
            param_name=ssm_param_name,
            region_name=region_name,
            logger=logger,
        )
        super().__init__(source=source, logger=logger)

    @staticmethod
    def _validate(config: Any) -> bool:
        """Historical private alias for :func:`quant.config.validate_config`."""
        return validate_config(config)
