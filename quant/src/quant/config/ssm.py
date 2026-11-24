"""SSM Parameter Store backend (spec `runtime-config`; ``quant[ssm]`` extra).

boto3 is imported **lazily** on first fetch and this module has no boto3 import
at the top level, so a base install without the ``ssm`` extra can import it —
and run the local-first file/env backends — with zero AWS dependencies.

The SSM-specific behavior from the original poller is preserved *in this
backend, not the core*: region resolution via IMDSv2, version short-circuit on
the SSM ``Version`` number, and invalid-payload-once adoption (via the shared
``BaseConfigSource`` machinery). One ``get_parameter`` call per poll returns
both the version and the value, keeping the hot path a single lightweight
backend call.
"""

from __future__ import annotations

import logging
import urllib.request
from typing import Any

from quant.config.base import BaseConfigSource

_IMDS_TOKEN_URL = "http://169.254.169.254/latest/api/token"
_IMDS_REGION_URL = "http://169.254.169.254/latest/meta-data/placement/region"


def _imds_region() -> str:
    """Fetch the AWS region from IMDSv2; falls back to 'us-east-1' off EC2."""
    try:
        token_request = urllib.request.Request(
            _IMDS_TOKEN_URL,
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        with urllib.request.urlopen(token_request, timeout=2) as r:
            token = r.read().decode()
        region_request = urllib.request.Request(
            _IMDS_REGION_URL,
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(region_request, timeout=2) as r:
            return r.read().decode()
    except Exception:
        return "us-east-1"


class SSMConfigSource(BaseConfigSource):
    """SSM Parameter Store backend; version = the parameter's SSM ``Version``.

    ``region_name`` is resolved once from IMDS when not given, at first use.
    Poll errors (parameter missing, AWS transport errors, malformed responses,
    boto3 not installed) return ``None`` silently and prior config stays
    active.
    """

    name = "ssm"

    def __init__(
        self,
        param_name: str,
        *,
        region_name: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._param_name = param_name
        self._region_name = region_name
        self._ssm: Any = None  # lazy boto3 SSM client
        self._cached_value: str | None = None

    def _client(self) -> Any:
        """Lazily create the boto3 SSM client (never at import time)."""
        if self._ssm is None:
            import boto3  # quant[ssm] extra; base install never needs it

            region = self._region_name or _imds_region()
            self._ssm = boto3.client("ssm", region_name=region)
        return self._ssm

    def scan(self) -> tuple[Any, Any] | None:
        try:
            response = self._client().get_parameter(
                Name=self._param_name, WithDecryption=False
            )
            param = response["Parameter"]
            version: int = int(param["Version"])
            value: str = param["Value"]
            self._cached_value = value
            return version, value
        except Exception:
            self._cached_value = None
            return None  # poll error — silent; prior config stays active
