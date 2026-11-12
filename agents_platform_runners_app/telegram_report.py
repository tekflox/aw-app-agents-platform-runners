"""Relay a plain-text report to agents-platform-multitenant's sysadmin
Telegram bot (Kanban ``feature:kanban-need-human-deterministic-reping``).

Same shape as ``notion_token_sync.py``: the caller (aw-app-notion's reping
branch, so far) has no agents-platform credential of its own, and this app
is the one holding ``agents_platform_token`` in its config — so the caller
POSTs to this app over loopback and this app relays onward with its own
Bearer token. Nothing here reads or interprets the report; ``title``/``text``
go straight through to AP-MT's ``POST /api/telegram/report``
(``report_notify``, ``backend/app/api/telegram.py``), which resolves the
tenant from the bearer and delivers via that tenant's sysadmin bot.
"""
from __future__ import annotations

import httpx

from . import platform_base as platform_base_mod

TIMEOUT_S = 20.0


class TelegramReportSyncError(RuntimeError):
    """agents-platform-multitenant could not be reached or refused the call."""


class TelegramReportNotConfigured(TelegramReportSyncError):
    """This workspace has no agents-platform to relay to, so there is no bot
    to send through. Distinct from a plain failure for the same reason
    ``NotionTokenNotConfigured`` is: the caller treats "nothing to send to"
    differently from "sending failed"."""


def _platform(config: dict) -> tuple[str, str]:
    token = (config or {}).get("agents_platform_token")
    if not token:
        raise TelegramReportNotConfigured("agents_platform_token is not configured")
    return platform_base_mod.resolve(config).rstrip("/"), token


def send(config: dict, title: str, text: str) -> dict:
    base, token = _platform(config)
    url = f"{base}/api/telegram/report"
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.post(url, json={"title": title, "text": text},
                               headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise TelegramReportSyncError(
            f"agents-platform-multitenant unreachable at {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise TelegramReportSyncError(
            f"agents-platform-multitenant refused the call ({resp.status_code}): "
            f"{resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise TelegramReportSyncError(
            "agents-platform-multitenant returned a non-JSON body") from exc
