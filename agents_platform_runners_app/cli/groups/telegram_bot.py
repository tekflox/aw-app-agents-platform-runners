"""``agents-platform telegram-bot`` — ``/api/telegram/bots`` on
agents-platform-multitenant (routes: ``backend/app/api/telegram.py:5233-
5302``, prefix ``/api/telegram``, gated by ``require_tenant``, satisfied by
this app's identity JWT).

**Redaction is the whole point of this file existing as its own module.**
``GET /bots`` returns every bot's ``token`` and ``webhook_secret`` in
plaintext — the exact field pair that was a live credential leak on
2026-08-13 (see the comment above ``_admin_gate`` in ``telegram.py``).
``list`` redacts both by default, in the table AND the ``--json`` path;
``--show-secrets`` reveals them. A redaction that only covers the pretty
path is not a redaction.
"""
from __future__ import annotations

import argparse
import secrets
import sys

from ... import platform_base
from ..client import PlatformClient
from ..output import emit_json, emit_table, fail, ok

GROUP = "telegram-bot"
DESCRIPTION = "Manage the Telegram bots wired to Agents Platform agents"

_SECRET_FIELDS = ("token", "webhook_secret")


def register(sub) -> None:
    p_list = sub.add_parser("list", help="list Telegram bots")
    p_list.add_argument("--show-secrets", action="store_true",
                         help="reveal token/webhook_secret in full (redacted by default)")
    p_list.set_defaults(func=_list)

    p_add = sub.add_parser("add", help="register a Telegram bot")
    p_add.add_argument("bot_id", help="e.g. aw-17")
    p_add.add_argument("--token", required=True, help="Telegram Bot API token")
    p_add.add_argument("--name", default=None, help="defaults to the bot id")
    p_add.add_argument("--agent", dest="agent_slug", default=None,
                        help="agent slug inbound messages dispatch to")
    p_add.add_argument("--webhook-secret", default=None,
                        help="defaults to a fresh secrets.token_hex(32)")
    p_add.add_argument("--disabled", action="store_true", help="create disabled")
    p_add.add_argument("--sysadmin", action="store_true",
                        help="make this the sysadmin bot — a RADIO, not a checkbox: "
                             "the server demotes every other bot flagged sysadmin")
    p_add.add_argument("--admin-user-id", dest="admin_user_ids", action="append", default=[],
                        help="repeatable")
    p_add.add_argument("--workspace", default=None,
                        help="workspace whose runners execute this bot's turns "
                             "(defaults to THIS workspace); '' registers a legacy "
                             "bot that uses the agent's baked-in runner")
    p_add.add_argument("--no-register-webhook", action="store_true",
                        help="skip the automatic register-webhook call after create")
    p_add.add_argument("--show-secrets", action="store_true",
                        help="print the (possibly auto-generated) token/webhook_secret in full")
    p_add.set_defaults(func=_add)

    p_del = sub.add_parser("delete", help="delete a Telegram bot")
    p_del.add_argument("bot_id")
    p_del.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_del.set_defaults(func=_delete)


def _redact(bot: dict) -> dict:
    out = dict(bot)
    for field in _SECRET_FIELDS:
        value = out.get(field) or ""
        out[field] = f"{value[:4]}…{value[-4:]}" if len(value) > 8 else "…"
    return out


def _row(bot: dict) -> tuple:
    return (
        str(bot.get("id", "")), str(bot.get("name", "")), str(bot.get("agent_slug") or ""),
        str(bot.get("workspace") or ""),
        str(bot.get("enabled", "")), str(bot.get("is_sysadmin", "")), str(bot.get("token", "")),
    )


def _list(client: PlatformClient, ns: argparse.Namespace) -> int:
    bots = client.get("/api/telegram/bots") or []
    if not ns.show_secrets:
        bots = [_redact(b) for b in bots]
    if ns.as_json:
        emit_json(bots)
    else:
        headers = ("ID", "NAME", "AGENT", "WORKSPACE", "ENABLED", "SYSADMIN", "TOKEN")
        emit_table([_row(b) for b in bots], headers)
    return 0


def _resolve_workspace(ns: argparse.Namespace) -> str | None:
    """Which workspace's runners this bot's turns execute on.

    Defaults to THIS workspace, read through ``platform_base.workspace_env``
    and NOT ``os.environ.get`` directly: an app container has the var only in
    the 0600 ``.env`` the server mirrors it into, so a raw env read comes back
    empty and silently registers the bot as the literal default (see
    ``tests/test_runner_registration.py``, the same trap for the runner slug).

    ``--workspace ''`` is an explicit opt-out that sends NULL — a legacy bot
    that keeps using the agent's baked-in runner.
    """
    if ns.workspace is not None:
        return ns.workspace or None
    return platform_base.workspace_env("AW_WORKSPACE") or None


def _add(client: PlatformClient, ns: argparse.Namespace) -> int:
    webhook_secret = ns.webhook_secret or secrets.token_hex(32)
    body = {
        "id": ns.bot_id,
        "name": ns.name or ns.bot_id,
        "token": ns.token,
        "webhook_secret": webhook_secret,
        "enabled": not ns.disabled,
        "is_sysadmin": ns.sysadmin,
        "agent_slug": ns.agent_slug,
        "admin_user_ids": ns.admin_user_ids,
        "workspace": _resolve_workspace(ns),
    }
    bot = client.post("/api/telegram/bots", json_body=body)

    webhook_result = None
    webhook_error = None
    if not ns.no_register_webhook:
        try:
            webhook_result = client.post(f"/api/telegram/bots/{ns.bot_id}/register-webhook")
        except Exception as exc:  # noqa: BLE001 — created bot must still be reported
            webhook_error = str(exc)

    if ns.as_json:
        payload = dict(bot) if ns.show_secrets else _redact(bot)
        payload["webhook"] = webhook_result if webhook_error is None else {"error": webhook_error}
        emit_json(payload)
    else:
        ok(f"Created bot '{ns.bot_id}'.")
        if ns.show_secrets:
            ok(f"webhook_secret: {webhook_secret}")
        if webhook_error is None and webhook_result is not None:
            ok(f"Webhook registered: {webhook_result.get('webhook_url')}")
        elif webhook_error is not None:
            fail(f"Bot created, but webhook registration failed: {webhook_error}")

    if webhook_error is not None:
        return 1
    return 0


def _delete(client: PlatformClient, ns: argparse.Namespace) -> int:
    if not ns.yes:
        if not sys.stdin.isatty():
            fail(f"Refusing to delete bot '{ns.bot_id}' without --yes on a non-interactive stdin.")
            return 1
        answer = input(f"Delete bot '{ns.bot_id}'? [y/N] ").strip().lower()
        if answer != "y":
            ok("Aborted.")
            return 0
    client.delete(f"/api/telegram/bots/{ns.bot_id}")
    ok(f"Deleted bot '{ns.bot_id}'.")
    return 0
