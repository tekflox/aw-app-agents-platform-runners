"""HTTP client for agents-platform-multitenant's REST API, shared by every
``aw-workspace-cli agents-platform <group>`` command (see ``cli/main.py`` and
``cli/groups/``).

Deliberately NOT a wrapper around ``mcp_server.py`` (2939 lines of async
hand-rolled JSON-RPC keyed off env vars a different process bakes into
``mcp.json``) — every group verb here is one ``httpx`` call, so sharing that
machinery would buy nothing but an async bridge and a permanent coupling to
the gateway's env contract. See ``docs/architecture/cli.md`` for the full
"what I rejected, and why".

Config resolution reuses, rather than re-derives, two things this app
already owns:

- ``<workspace_home>/app-config/agents-platform-runners.json`` — the same
  0600 file the app's own config-save path writes.
- ``platform_base.resolve(config)`` for the base URL. **Never**
  ``config["agents_platform_base"]`` directly — that field holds a legacy
  bridge-address literal on hosts that have saved their config at least
  once, and ``resolve()`` is the one place that already knows to treat it
  as unset. See ``platform_base.py``'s own docstring for why.

Leaf module — this package must never import ``agents_platform_runners_app
.plugin`` (docker/redis/fastapi, ~10k lines) or ``identity_token`` (which
imports ``plugin`` at module scope). Only ``platform_base`` is safe to
import from here.
"""
from __future__ import annotations

import json
import os

import httpx

from .. import platform_base

DEFAULT_TIMEOUT = 30.0


def _workspace_home() -> str:
    home = os.environ.get("AW_WORKSPACE_HOME")
    if home:
        return home
    container_dir = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
    return os.path.join(container_dir, ".aw-workspace")


def _config_path() -> str:
    return os.path.join(_workspace_home(), "app-config", "agents-platform-runners.json")


def _load_config() -> dict:
    path = _config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


class NotConfigured(RuntimeError):
    """Raised when this app's config file is missing/unreadable, or carries
    no token — this CLI only works once agents-platform-runners is
    installed and activated in this workspace."""


class PlatformError(RuntimeError):
    """Raised for any non-2xx response from agents-platform-multitenant.

    Carries the parsed status/body so a group can build an actionable
    message (a 401 in particular has exactly one likely cause — see
    ``main.py``'s error mapping)."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


class PlatformClient:
    def __init__(self, base: str | None = None, token: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT):
        config = _load_config()
        # platform_base.resolve() already honours AW_AGENTS_PLATFORM_BASE
        # itself, so an explicit `base` argument is the only thing that
        # needs to short-circuit it here.
        self.base = (base or platform_base.resolve(config)).rstrip("/")
        env_token = os.environ.get("AW_AGENTS_PLATFORM_TOKEN")
        self.token = token or env_token or str(config.get("agents_platform_token") or "")
        self.timeout = timeout

    def _require_configured(self) -> None:
        if not self.token:
            raise NotConfigured(
                f"agents-platform-runners has no token configured "
                f"({_config_path()} is missing or empty). Install and activate "
                "the app, or set AW_AGENTS_PLATFORM_TOKEN for a one-off override."
            )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _request(self, method: str, path: str, *, json_body=None, params=None):
        self._require_configured()
        url = f"{self.base}{path}"
        try:
            resp = httpx.request(
                method, url, json=json_body, params=params, headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as e:
            raise PlatformError(0, str(e)) from e
        if resp.status_code >= 400:
            try:
                data = resp.json()
                detail = data.get("detail") or data.get("error") or resp.text
            except ValueError:
                detail = resp.text
            raise PlatformError(resp.status_code, str(detail)[:300])
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def get(self, path: str, params: dict | None = None):
        return self._request("GET", path, params=params)

    def post(self, path: str, json_body: dict | None = None, params: dict | None = None):
        return self._request("POST", path, json_body=json_body, params=params)

    def put(self, path: str, json_body: dict | None = None):
        return self._request("PUT", path, json_body=json_body)

    def delete(self, path: str, params: dict | None = None):
        return self._request("DELETE", path, params=params)
