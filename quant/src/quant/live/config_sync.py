"""ConfigSync — passive SSM Parameter Store poller.

The trader's main loop calls :meth:`ConfigSync.poll` once per tick.  If SSM's
``Version`` is unchanged the JSON is never parsed and ``None`` is returned, so
the hot path is a single lightweight API call.  When the version changes and
the payload validates, the new config dict is returned for the caller to apply
synchronously — no threads, no callbacks, no locks.

SSM is the sole source of truth.
"""

import json
import logging
import urllib.request
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

_IMDS_TOKEN_URL = "http://169.254.169.254/latest/api/token"
_IMDS_REGION_URL = "http://169.254.169.254/latest/meta-data/placement/region"


def _imds_region() -> str:
    """Fetch the AWS region from IMDSv2. Returns 'us-east-1' if not on EC2."""
    try:
        token_req = urllib.request.Request(
            _IMDS_TOKEN_URL,
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        with urllib.request.urlopen(token_req, timeout=2) as r:
            token = r.read().decode()
        region_req = urllib.request.Request(
            _IMDS_REGION_URL,
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(region_req, timeout=2) as r:
            return r.read().decode()
    except Exception:
        return "us-east-1"


class ConfigSync:
    """Stateful SSM poller. Call :meth:`poll` from the main loop each tick."""

    def __init__(self, ssm_param_name: str, logger: logging.Logger) -> None:
        self._ssm_param = ssm_param_name
        self._logger = logger
        self._ssm = boto3.client("ssm", region_name=_imds_region())
        self._last_version: int | None = None

    @property
    def last_version(self) -> int | None:
        return self._last_version

    def poll(self) -> dict[str, Any] | None:
        """Return a validated config dict if SSM ``Version`` changed, else ``None``.

        Returns ``None`` on any error (SSM unavailable, JSON parse failure,
        schema validation failure, or unchanged version).  On parse/validation
        failure the version is still committed so subsequent polls short-circuit
        on the unchanged version rather than re-logging the same error every
        cycle — the operator must push a new version to retry.
        """
        try:
            resp = self._ssm.get_parameter(Name=self._ssm_param, WithDecryption=False)
            param = resp["Parameter"]
            version: int = param["Version"]

            # Hot path: version unchanged — nothing to do.
            if version == self._last_version:
                return None

            try:
                config = json.loads(param["Value"])
            except json.JSONDecodeError as e:
                self._logger.error(
                    f"[ConfigSync] v{version} parse error — keeping prior config: {e}"
                )
                self._last_version = version
                return None

            if not self._validate(config):
                self._logger.error(
                    f"[ConfigSync] v{version} failed schema validation "
                    "— keeping prior config."
                )
                self._last_version = version
                return None

            old, self._last_version = self._last_version, version
            self._logger.info(f"[ConfigSync] config updated v{old} → v{version}.")
            return config

        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ParameterNotFound":
                self._logger.warning(
                    f"[ConfigSync] SSM parameter '{self._ssm_param}' not found."
                )
            else:
                self._logger.error(f"[ConfigSync] SSM fetch error: {e}")
        except BotoCoreError as e:
            self._logger.error(f"[ConfigSync] SSM transport error: {e}")
        except KeyError as e:
            self._logger.error(f"[ConfigSync] SSM response shape error: {e}")
        except Exception as e:
            self._logger.error(f"[ConfigSync] Unexpected error during poll: {e}")
        return None

    @staticmethod
    def _validate(config: Any) -> bool:
        """Minimal guard: dict with a strategies list; each entry has a string 'name'."""
        if not isinstance(config, dict):
            return False
        strategies = config.get("strategies")
        if not isinstance(strategies, list):
            return False
        return all(
            isinstance(s, dict) and isinstance(s.get("name"), str) for s in strategies
        )
